# app.py
import os
import re
import json
import time
import math
import asyncio
import logging
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request
from pydantic import BaseModel
from cachetools import TTLCache

import ccxt.async_support as ccxt  # 비동기 ccxt
from dotenv import load_dotenv

# ----------------------------
# 기본 세팅
# ----------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("router")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1/20
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

# ccxt 객체/마켓 캐시
exchange: Optional[ccxt.bitget] = None
markets: Dict[str, Any] = {}
markets_cache = TTLCache(maxsize=1, ttl=60 * 60)  # 1시간

# 최근 포지션 목록 캐시 (Bitget fetchPositions 가끔 느릴 수 있어 10초 캐시)
positions_cache: TTLCache = TTLCache(maxsize=1, ttl=10)

app = FastAPI()


# ----------------------------
# 모델
# ----------------------------
class TVAlert(BaseModel):
    secret: Optional[str] = None
    symbol: str
    side: str                    # "buy" | "sell"
    orderType: Optional[str] = "market"
    size: Optional[float] = None # (참고용, 실제 수량은 마진/가격으로 산출)


# ----------------------------
# 유틸 & 초기화
# ----------------------------
async def ensure_exchange():
    """ccxt bitget 초기화 + 마켓 로딩(캐시)"""
    global exchange, markets
    if exchange is None:
        exchange = ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_API_PASSWORD,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # 선물(USDT-Perp)
            },
        })
    if "markets" not in markets_cache:
        await exchange.load_markets(True)
        markets = exchange.markets
        markets_cache["markets"] = True


def tv_symbol_to_ccxt(tv_symbol: str) -> str:
    """
    TradingView 심볼 -> Bitget(CCXT) 심볼
    입력 예: SPKUSDT.P / 1000BONKUSDT.P / ETHUSDT.P / VIRTUALUSDT.P
    출력 예: SPK/USDT:USDT / 1000BONK/USDT:USDT / ETH/USDT:USDT / VIRTUAL/USDT:USDT
    """
    raw = tv_symbol.strip()
    s = raw.replace(".P", "")
    s = re.sub(r"(PERP)$", "", s, flags=re.IGNORECASE)

    m = re.match(r"^(.+?)USDT$", s, flags=re.IGNORECASE)
    base = m.group(1) if m else s  # 끝의 USDT 제거

    # 후보 순회
    candidates = [
        f"{base}/USDT:USDT",
        f"{base}/USDT",
        base,
    ]
    for cand in candidates:
        if cand in markets:
            return markets[cand]["symbol"]

    # 느슨 매칭: base/quote로 비교
    upper_base = base.upper()
    for mkt in markets.values():
        if mkt.get("type") == "swap" and mkt.get("linear"):
            if mkt.get("base", "").upper() == upper_base and mkt.get("quote", "").upper() == "USDT":
                return mkt["symbol"]

    # 마지막 fallback
    return f"{base}/USDT:USDT"


async def fetch_price(symbol: str) -> float:
    """마켓가(가격) 조회"""
    try:
        ticker = await exchange.fetch_ticker(symbol)
        # ticker['last'] 없으면 mid로
        price = ticker.get("last") or (ticker["bid"] + ticker["ask"]) / 2
        return float(price)
    except Exception as e:
        logger.warning(f"fetch_price fail {symbol}: {e}")
        raise


async def fetch_equity_usdt() -> float:
    """지갑 총액(USDT 기준) – 현재 시드로 간주"""
    bal = await exchange.fetch_balance()
    usdt = bal.get("USDT", {})
    # total 이 없으면 free 사용
    equity = float(usdt.get("total") or usdt.get("free") or 0.0)
    return equity


async def fetch_positions_map() -> Dict[str, Dict[str, Any]]:
    """
    현재 보유 포지션 맵:
      { symbol: {"side": "long|short", "contracts": float} ... }
    contracts는 포지션 수량(계약수) – 0 이면 미보유
    """
    if "positions" in positions_cache:
        return positions_cache["positions"]

    pos_map: Dict[str, Dict[str, Any]] = {}
    try:
        positions = await exchange.fetch_positions()
        for p in positions:
            # 스왑/USDT 만 사용
            if p.get("marginMode") == "cross" or True:
                sym = p.get("symbol")
                contracts = float(p.get("contracts") or p.get("contractSize") or 0.0)
                if contracts <= 0:
                    continue
                side = p.get("side")
                # 일부 거래소는 "long"/"short" 대신 "sell"/"buy" 형태일 수도 있으니 정규화
                if side not in ("long", "short"):
                    if float(p.get("contracts", 0)) > 0 and float(p.get("unrealizedPnl", 0)) is not None:
                        # ccxt bitget 은 기본적으로 side 제공함
                        pass
                pos_map[sym] = {"side": side, "contracts": contracts}
    except Exception as e:
        logger.warning(f"fetch_positions failed: {e}")

    positions_cache["positions"] = pos_map
    return pos_map


def count_open_symbols(pos_map: Dict[str, Dict[str, Any]]) -> int:
    return len(pos_map)


def calc_order_amount(price: float, equity: float) -> float:
    """
    주문 수량(코인 수) = (equity × FRACTION_PER_POSITION) / price
    - 레버리지는 Bitget UI에서 설정된 값을 따름. (여기선 마진만 관리)
    """
    margin_usdt = max(0.0, equity * FRACTION_PER_POSITION)
    if margin_usdt <= 0 or price <= 0:
        return 0.0
    qty = margin_usdt / price
    return qty


async def place_order_bitget(symbol: str, side: str, amount: float, reduce_only: bool) -> Dict[str, Any]:
    """
    Bitget 주문 래퍼 – market + reduceOnly 지원
    side: 'buy' | 'sell'
    """
    if amount <= 0:
        raise ValueError("amount must be > 0")

    params = {"reduceOnly": reduce_only}
    try:
        if DRY_RUN:
            logger.info(f"[DRY] create_order {symbol} {side} {amount} reduceOnly={reduce_only}")
            return {"dry": True}

        order = await exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
            params=params,
        )
        return order
    except ccxt.BaseError as ex:
        # Bitget 오류 메시지를 그대로 노출
        text = getattr(ex, "args", [""])[0]
        logger.error(f"order_failed: {text}")
        raise


# ----------------------------
# 라우트
# ----------------------------
@app.get("/status")
async def status():
    await ensure_exchange()
    return {
        "ok": True,
        "markets_loaded": len(markets) > 0,
        "fraction_per_position": FRACTION_PER_POSITION,
        "max_coins": MAX_COINS,
        "allow_shorts": ALLOW_SHORTS,
        "dry_run": DRY_RUN,
    }


@app.post("/webhook")
async def webhook(msg: TVAlert, request: Request):
    started = time.time()
    await ensure_exchange()

    # 시크릿 검증
    if WEBHOOK_SECRET and msg.secret and msg.secret != WEBHOOK_SECRET:
        logger.info(f"skip: secret mismatch for {msg.symbol}")
        return {"ok": True, "skipped": "secret_mismatch"}

    tv_side = msg.side.lower().strip()  # 'buy' | 'sell'
    tv_symbol = msg.symbol.strip()

    # 심볼 매핑
    ccxt_symbol = tv_symbol_to_ccxt(tv_symbol)

    if ccxt_symbol not in markets:
        logger.info(f"skip: unknown symbol mapped -> {ccxt_symbol} (from {tv_symbol})")
        return {"ok": True, "skipped": "unknown_symbol", "tv_symbol": tv_symbol, "mapped": ccxt_symbol}

    # 현재 포지션 맵
    pos_map = await fetch_positions_map()
    cur = pos_map.get(ccxt_symbol)  # None | {"side": "long|short", "contracts": f}

    # 현재 가격 & 내 시드
    try:
        price = await fetch_price(ccxt_symbol)
    except Exception:
        return {"ok": False, "error": "price_fetch_failed"}
    equity = await fetch_equity_usdt()

    # 주문 수량 계산(마진=시드×fraction / price)
    qty = calc_order_amount(price, equity)

    # 포지션/시그널 조합별 의사결정
    # 1) 현재 포지션 없음
    if cur is None:
        # 신규 오픈 제한
        open_cnt = count_open_symbols(pos_map)
        can_open_new = open_cnt < MAX_COINS

        if tv_side == "buy":
            # 신규 롱 오픈
            if not can_open_new:
                logger.info(f"skip: MAX_COINS reached. cannot open LONG {ccxt_symbol}")
                return {"ok": True, "skipped": "max_coins", "intent": "open_long"}
            side = "buy"
            reduce_only = False
        else:  # "sell"
            # 신규 숏 오픈
            if not ALLOW_SHORTS:
                logger.info(f"skip: shorts disabled")
                return {"ok": True, "skipped": "shorts_disabled"}
            if not can_open_new:
                logger.info(f"skip: MAX_COINS reached. cannot open SHORT {ccxt_symbol}")
                return {"ok": True, "skipped": "max_coins", "intent": "open_short"}
            side = "sell"
            reduce_only = False

        if qty <= 0:
            logger.info(f"skip: calc amount is zero | {{'symbol': '{ccxt_symbol}', 'price': {price}}}")
            return {"ok": True, "skipped": "zero_amount"}

        order = await place_order_bitget(ccxt_symbol, side, qty, reduce_only)
        positions_cache.pop("positions", None)  # 포지션 캐시 무효화
        return {"ok": True, "action": "open", "order": order}

    # 2) 현재 포지션 있음 (long | short)
    cur_side = (cur.get("side") or "").lower()
    if cur_side not in ("long", "short"):
        logger.info(f"skip: unknown current position side {cur_side}")
        return {"ok": True, "skipped": "unknown_position"}

    # (a) 롱 보유 중
    if cur_side == "long":
        if tv_side == "buy":
            # 물타기(증가)
            side = "buy"
            reduce_only = False
        else:  # 'sell' -> 청산/감소
            side = "sell"
            reduce_only = True
    else:
        # (b) 숏 보유 중
        if tv_side == "sell":
            # 물타기(증가)
            side = "sell"
            reduce_only = False
        else:  # 'buy' -> 청산/감소
            side = "buy"
            reduce_only = True

    if qty <= 0:
        logger.info(f"skip: calc amount is zero | {{'symbol': '{ccxt_symbol}', 'price': {price}}}")
        return {"ok": True, "skipped": "zero_amount"}

    # 주문 실행
    order = await place_order_bitget(ccxt_symbol, side, qty, reduce_only)
    positions_cache.pop("positions", None)  # 포지션 캐시 무효화

    elapsed = round(time.time() - started, 3)
    return {
        "ok": True,
        "elapsed": elapsed,
        "action": "add_or_close",
        "tv": {"symbol": tv_symbol, "side": tv_side},
        "mapped": ccxt_symbol,
        "qty": qty,
        "reduceOnly": reduce_only,
        "order": order,
    }


@app.get("/")
async def root():
    return {"ok": True, "service": "tv-bitget-router", "docs": "/docs"}
