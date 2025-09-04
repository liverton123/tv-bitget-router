import os
import math
import logging
import traceback
from typing import Optional, Tuple

import ccxt

log = logging.getLogger("router.trade")

PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # Bitget USDT-M Perp
API_KEY      = os.getenv("BITGET_API_KEY")
API_SECRET   = os.getenv("BITGET_API_SECRET")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD")
DRY_RUN      = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

def build_exchange() -> ccxt.bitget:
    missing = [k for k,v in {
        "BITGET_API_KEY": API_KEY,
        "BITGET_API_SECRET": API_SECRET,
        "BITGET_API_PASSWORD": API_PASSWORD
    }.items() if not v]
    if missing:
        raise RuntimeError(f"BITGET API envs missing: {', '.join(missing)}")

    ex = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            # USDT-M perpetual
            "defaultType": "swap",
            "defaultSubType": "linear",
            "defaultSettle": "USDT",
        },
    })
    return ex


async def fetch_net_position(ex: ccxt.bitget, symbol: str) -> Tuple[float, float]:
    """
    현재 순포지션 수량(>0 long, <0 short)과 미실현 손익에 영향 없는 진짜 size 합.
    Bitget ccxt fetchPositions는 params에 productType 필요.
    """
    params = {"productType": PRODUCT_TYPE}  # ← 중요!
    try:
        positions = await ex.fetch_positions([symbol], params=params)
    except Exception:
        # 일부 심볼은 거래내역 없으면 []가 반환되기도 함
        log.exception("[CCXT_ERROR] fetch_positions failed")
        return 0.0, 0.0

    qty_long = 0.0
    qty_short = 0.0

    for p in positions or []:
        amt = float(p.get("contracts") or p.get("contractsAbs") or p.get("info", {}).get("total", 0) or 0)
        side = p.get("side") or p.get("direction") or ""
        if side.lower() == "long":
            qty_long += amt
        elif side.lower() == "short":
            qty_short += amt

    net = qty_long - qty_short
    gross = qty_long + qty_short
    return net, gross


async def place_order(
    ex: ccxt.bitget,
    symbol: str,
    side: str,             # "buy" or "sell"
    size: float,           # coin 수량(ETH 0.034 등)
    reduce_only: bool
):
    """
    Bitget USDT-M Perp 지정:
    - createOrder(symbol, type, side, amount, price=None, params)
    - params에 reduceOnly, productType 필수
    """
    params = {
        "productType": PRODUCT_TYPE,  # ← MUST
        "reduceOnly": reduce_only
    }

    if DRY_RUN:
        log.info(f"[DRY_RUN] createOrder {symbol} {side} {size} reduceOnly={reduce_only} params={params}")
        return {"dryRun": True}

    order = await ex.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=size,
        price=None,
        params=params
    )
    return order


async def smart_route(
    ex: ccxt.bitget,
    symbol: str,
    side: str,     # "buy"/"sell" (진입 또는 청산 모두 포함)
    size: float
):
    """
    현재 순포지션(net)을 기준으로:
    - 같은 방향(side) => 물타기(추가 진입)
    - 반대 방향:
        - size == |net| => 전량 청산
        - size  > |net| => 청산 + 방향전환(남는 수량으로 반대 진입)
        - size  < |net| => 부분 청산
    """
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy/sell")

    net, _ = await fetch_net_position(ex, symbol)
    log.info(f"[ROUTER] {symbol} net={net} incoming side={side} size={size}")

    # long: net>0, short: net<0
    def same_dir(net_: float, s: str) -> bool:
        return (net_ > 0 and s == "buy") or (net_ < 0 and s == "sell")

    if net == 0:
        # 신규 진입
        log.info(f"[ROUTER] new entry => {side} {size}")
        return [await place_order(ex, symbol, side, size, reduce_only=False)]

    # 기존 방향과 동일 -> 추가 진입
    if same_dir(net, side):
        log.info(f"[ROUTER] add entry => {side} {size}")
        return [await place_order(ex, symbol, side, size, reduce_only=False)]

    # 반대 방향 => 청산/전환
    close_qty = min(abs(net), size)
    remain    = max(0.0, size - abs(net))

    results = []
    # 청산(반대 방향, reduceOnly=True)
    log.info(f"[ROUTER] close part/all => {side} {close_qty} (reduceOnly)")
    if close_qty > 0:
        results.append(await place_order(ex, symbol, side, close_qty, reduce_only=True))

    # 만약 주문 수량이 기존 포지션보다 크면 나머지로 방향전환 신규 진입
    if remain > 0:
        log.info(f"[ROUTER] reverse remainder => {side} {remain}")
        results.append(await place_order(ex, symbol, side, remain, reduce_only=False))

    return results