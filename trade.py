import math
import asyncio
from typing import Tuple, Optional

import ccxt.async_support as ccxt

# --------------- 유틸 ---------------

def normalize_symbol(tv_symbol: str) -> Tuple[str, str, str]:
    """
    "ETHUSDT.P" -> ("ETH/USDT:USDT", "ETH", "USDT")
    """
    s = tv_symbol.upper().strip()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        # 비정형은 그대로 두되, ccxt가 이해 못할 수 있음
        base = s
        quote = "USDT"
        return f"{base}/USDT:USDT", base, quote
    base = s[:-4]
    quote = "USDT"
    return f"{base}/USDT:USDT", base, quote


async def _ticker_last(ex, ccxt_symbol: str) -> float:
    t = await ex.fetch_ticker(ccxt_symbol)
    px = float(t.get("last") or t.get("close") or 0)
    if px <= 0:
        raise RuntimeError("no last price")
    return px


async def _market_meta(ex, ccxt_symbol: str):
    m = await ex.market(ccxt_symbol)
    prec = int(m.get("precision", {}).get("amount") or 0)
    min_amt = float(m.get("limits", {}).get("amount", {}).get("min") or 0)
    return prec, min_amt


def _round_amount(amt: float, prec: int, min_amt: float) -> float:
    if prec > 0:
        step = 10 ** (-prec)
        amt = math.floor(amt / step) * step
    if min_amt and amt < min_amt:
        amt = 0.0 if prec == 0 else round(min_amt, prec)
    return max(0.0, amt)


# --------------- 포지션 조회 ---------------

async def _fallback_positions_v2(ex, product_type: str):
    res = await ex.private_mix_get_v2_mix_position_all_position({
        "marginCoin": "USDT",
        "productType": product_type,
    })
    return res.get("data", []) if isinstance(res, dict) else []


async def _fallback_positions_v1(ex, product_type: str):
    res = await ex.private_mix_get_position_all_position({
        "marginCoin": "USDT",
        "productType": product_type,
    })
    return res.get("data", []) if isinstance(res, dict) else []


def _extract_net_from_raw(raw_list, ccxt_symbol: str) -> Tuple[float, Optional[str], float]:
    """
    raw symbol 예: "ETHUSDT_UMCBL"
    return: (netQty, side, absQty)
    """
    key = ccxt_symbol.split("/")[0] + "USDT"
    long_amt = 0.0
    short_amt = 0.0
    mark_px = 0.0
    for it in raw_list:
        sym = str(it.get("symbol") or "")
        if key not in sym:
            continue
        hold_side = (it.get("holdSide") or it.get("side") or "").lower()
        amt = float(it.get("total") or it.get("available") or it.get("size") or 0.0)
        if hold_side == "long":
            long_amt += amt
        elif hold_side == "short":
            short_amt += amt
        mp = float(it.get("markPrice") or it.get("averageOpenPrice") or 0.0)
        if mp > 0:
            mark_px = mp
    net = long_amt - short_amt
    if abs(net) < 1e-12:
        return 0.0, None, 0.0
    side = "long" if net > 0 else "short"
    return net, side, abs(net) if abs(net) > 1e-12 else (0.0, None, 0.0)


async def fetch_net_position(ex, ccxt_symbol: str, product_type: str) -> Tuple[float, Optional[str], float, float]:
    """
    return: (netQty, side, absQty, markPrice)
    """
    # 1) ccxt 우선
    try:
        pos = await ex.fetch_positions([ccxt_symbol])
        long_amt = 0.0
        short_amt = 0.0
        mark_px = 0.0
        for p in pos or []:
            if p.get("symbol") != ccxt_symbol:
                continue
            side = str(p.get("side") or "").lower()
            amt = float(p.get("contracts") or p.get("size") or 0)
            if side == "long":
                long_amt += amt
            elif side == "short":
                short_amt += amt
            mp = float(p.get("markPrice") or p.get("info", {}).get("markPrice") or 0)
            if mp > 0:
                mark_px = mp
        net = long_amt - short_amt
        if abs(net) < 1e-12:
            # 무포지션 → 그래도 mark price는 확보
            if mark_px <= 0:
                mark_px = await _ticker_last(ex, ccxt_symbol)
            return 0.0, None, 0.0, mark_px
        side = "long" if net > 0 else "short"
        if mark_px <= 0:
            mark_px = await _ticker_last(ex, ccxt_symbol)
        return net, side, abs(net), mark_px
    except Exception:
        pass

    # 2) fallback v2 → v1
    try:
        raw = await _fallback_positions_v2(ex, product_type)
    except Exception:
        raw = []
    if not raw:
        try:
            raw = await _fallback_positions_v1(ex, product_type)
        except Exception:
            raw = []

    net, side, abs_amt = _extract_net_from_raw(raw, ccxt_symbol)
    mark_px = await _ticker_last(ex, ccxt_symbol)
    return net, side, abs_amt, mark_px


# --------------- 사이즈 변환/보정 ---------------

async def to_coin_amount_from_contracts(ex, ccxt_symbol: str, contracts_usdt: float) -> float:
    price = await _ticker_last(ex, ccxt_symbol)
    prec, min_amt = await _market_meta(ex, ccxt_symbol)
    raw = contracts_usdt / max(price, 1e-12)
    return _round_amount(raw, prec, min_amt)


async def clamp_amount_with_balance_and_caps(
    ex,
    ccxt_symbol: str,
    amount: float,
    max_usdt_per_order: float,
    max_pos_usdt: float,
    mark_price: float,
) -> float:
    """
    - 1회 주문 상한(USDT) 적용
    - 잔고 기반 가용량 확인
    - 포지션 총액 상한(USDT) 고려(가능한 경우)
    """
    if amount <= 0:
        return 0.0

    prec, min_amt = await _market_meta(ex, ccxt_symbol)

    # 1) 1회 상한
    order_amt_cap = max_usdt_per_order / max(mark_price, 1e-12)
    amount = min(amount, order_amt_cap)

    # 2) 잔고 제한 (USDT Free 98%)
    try:
        bal = await ex.fetch_balance()
        free = float(bal.get("USDT", {}).get("free") or 0.0)
        if free > 0:
            max_by_free = (free * 0.98) / max(mark_price, 1e-12)
            amount = min(amount, max_by_free)
    except Exception:
        pass

    # 3) 포지션 상한은 외부에서 순포지션 금액을 받아 계산하는 편이 맞지만
    #    여기서는 주문 상한 중심으로 보수적으로만 제한
    amount = _round_amount(amount, prec, min_amt)
    return max(0.0, amount)


# --------------- 주문 ---------------

async def _retry(coro_fn, retries=3, base_sleep=0.4):
    last = None
    for i in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            last = e
            await asyncio.sleep(base_sleep * (2 ** i))
    raise last

async def place_market_order(ex, ccxt_symbol: str, side: str, amount: float, product_type: str, reduce_only: bool):
    params = {
        "productType": product_type,
        "reduceOnly": reduce_only,
        "marginCoin": "USDT",
    }
    async def _go():
        return await ex.create_order(ccxt_symbol, "market", side, amount, None, params)
    return await _retry(_go)


# --------------- 레버리지/마진 모드 ---------------

async def set_leverage_and_margin_mode_if_needed(ex, ccxt_symbol: str, product_type: str, leverage: int, margin_mode: str):
    """
    Bitget은 심볼 단위 설정.
    실패하더라도 거래엔 영향 없으니 조용히 시도하고 실패는 무시.
    """
    sym = ccxt_symbol.split("/")[0] + "USDT"  # e.g. ETHUSDT
    try:
        # 마진 모드
        mode = 1 if margin_mode.lower() == "isolated" else 2  # 1 isolated, 2 cross
        await ex.private_mix_post_v2_mix_account_set_margin_mode({
            "productType": product_type,
            "symbol": f"{sym}_UMCBL",
            "marginMode": "isolated" if mode == 1 else "crossed",
        })
    except Exception:
        pass
    try:
        # 레버리지
        await ex.private_mix_post_v2_mix_account_set_leverage({
            "productType": product_type,
            "symbol": f"{sym}_UMCBL",
            "leverage": str(max(1, min(leverage, 125))),
            "holdSide": "both",
        })
    except Exception:
        pass