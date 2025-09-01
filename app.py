# app.py  — tv-bitget-router (FULL VERSION)

import os
import json
import math
import time
import logging
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from cachetools import TTLCache

import ccxt.async_support as ccxt  # asyncio 버전
import asyncio

# ----------------------------
# Logging
# ----------------------------
logger = logging.getLogger("tv-bitget-router")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

# ----------------------------
# ENV
# ----------------------------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

# 포지션당 지갑의 고정 마진 비율 (예: 1/20 = 0.05)
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))

# 최대 동시 보유 코인 수 (롱/숏 합계가 아니라 '코인 심볼' 개수 기준)
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# 숏 허용 여부
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

# 드라이런
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# (안전) 기본 레버리지 가정값 (Bitget UI에 설정된 실제 레버리지 조회가 실패할 때만 사용)
DEFAULT_ASSUMED_LEVERAGE = float(os.getenv("DEFAULT_ASSUMED_LEVERAGE", "10"))

# Bitget product / margin coin
MARGIN_COIN = "USDT"
PRODUCT_TYPE = "USDT-FUTURES"  # Bitget v2 mix endpoints

# ----------------------------
# FastAPI
# ----------------------------
app = FastAPI()

# ----------------------------
# Exchange (ccxt)
# ----------------------------
ex: Optional[ccxt.bitget] = None
markets_loaded = False

# 최근 처리된 알림 키(idempotency) — 중복 체결 방지
# key 예: f"{symbol}|{side}|{rounded_size}"
recent_alerts = TTLCache(maxsize=5000, ttl=90)

# 오픈 심볼 캐시 (현재 보유중인 코인 심볼 세트)
open_symbols_cache: Dict[str, str] = {}  # { "BTC/USDT:USDT": "long"/"short" }

# ----------------------------
# Models
# ----------------------------
class TVAlert(BaseModel):
    secret: Optional[str] = None
    symbol: str
    side: str
    orderType: Optional[str] = "market"
    size: Optional[float] = None


# ----------------------------
# Helpers
# ----------------------------
def tv_to_ccxt_symbol(tv_symbol: str) -> Optional[str]:
    """
    TradingView 심볼 (예: 'HBARUSDT.P') -> ccxt 심볼 (예: 'HBAR/USDT:USDT')
    실패하면 None
    """
    s = tv_symbol.upper().replace(".P", "").strip()
    if not s.endswith("USDT"):
        return None
    base = s[:-4]
    if not base:
        return None
    # ccxt(비트겟 선물) 표준 표기
    return f"{base}/USDT:USDT"


def round_amount(market: dict, amount: float) -> float:
    """시장 정밀도/스텝에 맞게 수량 반올림"""
    if amount <= 0:
        return 0.0
    precision = market.get("precision", {}).get("amount")
    step = market.get("limits", {}).get("amount", {}).get("min")
    # precision 우선
    if precision is not None:
        amount = float(ex.amount_to_precision(market["symbol"], amount))
    # 최소 수량 보정
    min_amt = market.get("limits", {}).get("amount", {}).get("min")
    if min_amt:
        if amount < float(min_amt):
            amount = 0.0
    return amount


async def ensure_exchange():
    """ccxt bitget 인스턴스 준비 & 마켓 로드 & 보유 포지션 동기화"""
    global ex, markets_loaded, open_symbols_cache

    if ex is None:
        ex = ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_API_PASSWORD,
            "options": {
                "defaultType": "swap",  # 선물
            },
            "enableRateLimit": True,
        })

    if not markets_loaded:
        await ex.load_markets()
        markets_loaded = True
        logger.info("Bitget markets loaded")

    # 부팅/재시작 후 한번 현재 보유 포지션 동기화
    if not open_symbols_cache:
        try:
            # ccxt의 통합 포지션 조회 (모든 심볼)
            positions = await ex.fetch_positions()
            for p in positions:
                sym = p.get("symbol")
                contracts = float(p.get("contracts", 0) or 0)
                side = p.get("side")
                if contracts > 0 and side in ("long", "short"):
                    open_symbols_cache[sym] = side
            if open_symbols_cache:
                logger.info(f"Open symbols synced: {open_symbols_cache}")
        except Exception as e:
            logger.warning(f"fetch_positions failed (will continue): {e}")


async def get_price(symbol: str) -> Optional[float]:
    """마크 프라이스 근사값"""
    try:
        ticker = await ex.fetch_ticker(symbol)
        # Bitget 선물에서는 mark/stats가 없는 경우도 있으니
        price = ticker.get("last") or ticker.get("close") or ticker.get("ask") or ticker.get("bid")
        return float(price) if price else None
    except Exception as e:
        logger.warning(f"fetch_ticker failed {symbol}: {e}")
        return None


async def get_user_leverage(symbol: str, market: dict) -> float:
    """
    유저가 Bitget UI에서 설정한 레버리지를 얻는다.
    실패 시 DEFAULT_ASSUMED_LEVERAGE로 fallback.
    """
    try:
        # Bitget v2 mix position single-position (ccxt raw 호출)
        # ccxt 메서드명 추정: privateMixGetV2MixPositionSinglePosition
        # 필요한 파라미터: symbol(id), productType, marginCoin
        bitget_symbol_id = market.get("id")
        if hasattr(ex, "privateMixGetV2MixPositionSinglePosition"):
            resp = await ex.privateMixGetV2MixPositionSinglePosition({
                "symbol": bitget_symbol_id,
                "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN,
            })
            data = (resp or {}).get("data")
            # 포지션이 없으면 빈 객체/None 일 수 있음
            if data and isinstance(data, dict):
                lev = data.get("leverage")
                if lev is not None:
                    return float(lev)
    except Exception as e:
        logger.info(f"get_user_leverage fallback -> {e}")

    return DEFAULT_ASSUMED_LEVERAGE


async def get_equity_usdt() -> float:
    """지갑(USDT) 총 잔고 (cross equity에 가장 근접)"""
    try:
        bal = await ex.fetch_balance()
        u = bal.get("USDT") or {}
        # total 또는 free 중 total 우선
        total = u.get("total")
        if total is None:
            total = u.get("free", 0)
        return float(total or 0)
    except Exception as e:
        logger.warning(f"fetch_balance failed: {e}")
        return 0.0


def is_duplicate(alert: TVAlert) -> bool:
    """같은 알림 중복 처리 방지 (짧은 시간 내 동일 키는 skip)"""
    key = f"{alert.symbol}|{alert.side}|{round(float(alert.size or 0), 6)}"
    if key in recent_alerts:
        return True
    recent_alerts[key] = time.time()
    return False


async def figure_position_side(symbol: str) -> Optional[str]:
    """
    현재 보유중인 포지션 방향 반환: "long"/"short"/None
    캐시 우선, 캐시 없으면 조회
    """
    if symbol in open_symbols_cache:
        return open_symbols_cache[symbol]

    try:
        # ccxt 통합 포지션 조회 (심볼 지정)
        pos = await ex.fetch_position(symbol)
        contracts = float(pos.get("contracts", 0) or 0)
        side = pos.get("side")
        if contracts > 0 and side in ("long", "short"):
            open_symbols_cache[symbol] = side
            return side
    except Exception:
        # 일부 거래소는 fetch_position 미지원 -> fetch_positions 후 필터 대체
        try:
            positions = await ex.fetch_positions([symbol])
            for p in positions:
                if p.get("symbol") == symbol:
                    contracts = float(p.get("contracts", 0) or 0)
                    side = p.get("side")
                    if contracts > 0 and side in ("long", "short"):
                        open_symbols_cache[symbol] = side
                        return side
        except Exception:
            pass
    return None


def update_open_symbols(symbol: str, side_after: Optional[str]):
    """체결 후 오픈심볼 캐시 갱신"""
    if side_after is None:
        if symbol in open_symbols_cache:
            del open_symbols_cache[symbol]
    else:
        open_symbols_cache[symbol] = side_after


async def calc_open_amount(symbol: str, market: dict) -> Tuple[float, float]:
    """
    신규(또는 물타기) 진입 시 주문 수량 계산.
    - 목표 마진 = equity * FRACTION_PER_POSITION
    - 실제 레버리지는 bitget에 설정된 값 조회 (실패시 기본값)
    - 목표 명목가(notional) = 목표 마진 * 레버리지
    - 수량 = notional / 현재가
    반환: (amount, price)  ; amount=0이면 스킵
    """
    price = await get_price(symbol)
    if not price or price <= 0:
        return 0.0, 0.0

    equity = await get_equity_usdt()
    if equity <= 0:
        return 0.0, price

    # 목표 "마진" (지갑의 1/20)
    target_margin = equity * FRACTION_PER_POSITION

    # 심볼별 설정 레버리지 (Bitget UI)
    lev = await get_user_leverage(symbol, market)

    # 목표 명목가(=주문 금액)
    desired_notional = target_margin * lev

    # 수량(계약수)
    raw_amount = desired_notional / price
    amount = round_amount(market, raw_amount)

    return amount, price


async def place_order_bitget(symbol: str, side: str, amount: float, reduce_only: bool) -> dict:
    """비트겟 마켓 주문"""
    params = {
        "reduceOnly": reduce_only,
        "marginCoin": MARGIN_COIN,
    }
    if DRY_RUN:
        logger.info(f"[DRY_RUN] create_order {symbol} {side} {amount} reduceOnly={reduce_only}")
        return {"dryrun": True}

    return await ex.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=amount,
        params=params
    )


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
async def root():
    await ensure_exchange()
    return {"ok": True, "service": "tv-bitget-router", "open_symbols": open_symbols_cache}


@app.post("/webhook")
async def webhook(req: Request):
    await ensure_exchange()

    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        alert = TVAlert(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Bad payload: {e}")

    # Secret 검증
    if WEBHOOK_SECRET and (alert.secret or "") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 중복 알림 방지
    if is_duplicate(alert):
        logger.info(f"skip duplicate: {alert.symbol} {alert.side} {alert.size}")
        return {"ok": True, "skip": "duplicate"}

    # 심볼 매핑
    ccxt_symbol = tv_to_ccxt_symbol(alert.symbol)
    if not ccxt_symbol:
        logger.info(f"skip: cannot map symbol {alert.symbol}")
        return {"ok": True, "skip": "bad_symbol"}

    if ccxt_symbol not in ex.markets:
        logger.info(f"skip: market not found {ccxt_symbol}")
        return {"ok": True, "skip": "unknown_market"}

    market = ex.markets[ccxt_symbol]

    # 현재 보유 방향 파악
    held_side = await figure_position_side(ccxt_symbol)  # "long"/"short"/None

    tv_side = (alert.side or "").lower().strip()
    if tv_side not in ("buy", "sell"):
        return {"ok": True, "skip": "bad_side"}

    # 액션 판정
    # buy  : long 열기 or short 청산
    # sell : short 열기 or long 청산
    reduce_only = False
    order_side = tv_side  # ccxt order side (buy/sell)

    if tv_side == "buy":
        if held_side == "short":
            reduce_only = True  # 숏 청산
        else:
            reduce_only = False  # 롱 신규/추가
    else:  # tv_side == "sell"
        if held_side == "long":
            reduce_only = True  # 롱 청산
        else:
            reduce_only = False  # 숏 신규/추가

    # 숏 신규 진입 허용 검사
    if (held_side is None) and (tv_side == "sell") and (not ALLOW_SHORTS):
        logger.info(f"skip: shorts not allowed ({ccxt_symbol})")
        return {"ok": True, "skip": "shorts_blocked"}

    # 최대 코인 수 제한: 신규 '오픈'일 때만 체크 (청산/추가 제외)
    is_new_open = (held_side is None) and (reduce_only is False)
    if is_new_open and len(open_symbols_cache) >= MAX_COINS:
        logger.info(f"skip: MAX_COINS reached ({ccxt_symbol})")
        return {"ok": True, "skip": "max_coins"}

    # 수량 산출
    amount = 0.0
    price_for_log = 0.0

    if reduce_only:
        # 청산일 때: TV가 주는 size가 있을 수도 있지만
        # 안전하게 현재 포지션 수량(contracts) 이하로만 처리
        try:
            pos = await ex.fetch_position(ccxt_symbol)
        except Exception:
            pos = None

        held_contracts = float((pos or {}).get("contracts", 0) or 0)
        if held_contracts <= 0:
            logger.info(f"skip: no position to reduce ({ccxt_symbol})")
            return {"ok": True, "skip": "no_position_to_reduce"}

        # TV size가 오면 그만큼만, 없으면 전량
        req_size = float(alert.size or 0)
        if req_size > 0:
            amount = min(held_contracts, req_size)
        else:
            amount = held_contracts

        amount = round_amount(market, amount)
        if amount <= 0:
            logger.info(f"skip: reduce size too small ({ccxt_symbol})")
            return {"ok": True, "skip": "reduce_too_small"}

    else:
        # 신규/물타기 진입: 지갑의 1/20 마진 + 현재 레버리지에 맞춰 명목가 계산
        amount, price_for_log = await calc_open_amount(ccxt_symbol, market)
        if amount <= 0:
            logger.info(f"skip: calc amount is zero | {{'symbol':'{ccxt_symbol}','price':{price_for_log}}}")
            return {"ok": True, "skip": "amount_zero"}

    # 주문 실행
    try:
        res = await place_order_bitget(ccxt_symbol, order_side, amount, reduce_only)
        logger.info(f"ORDER OK | {ccxt_symbol} {order_side} {amount} reduceOnly={reduce_only} -> {res}")

        # 오픈심볼 캐시 갱신(대략적)
        if reduce_only:
            # 포지션이 모두 닫혔는지 확인 (남아있으면 유지)
            try:
                p = await ex.fetch_position(ccxt_symbol)
                contracts_left = float(p.get("contracts", 0) or 0)
                side_left = p.get("side")
                if contracts_left > 0 and side_left in ("long", "short"):
                    update_open_symbols(ccxt_symbol, side_left)
                else:
                    update_open_symbols(ccxt_symbol, None)
            except Exception:
                # 조회 실패시 보수적으로 삭제
                update_open_symbols(ccxt_symbol, None)
        else:
            # 신규/추가 오픈 후 방향 기록
            after_side = "long" if tv_side == "buy" else "short"
            update_open_symbols(ccxt_symbol, after_side)

        return {"ok": True, "executed": True}

    except ccxt.BaseError as e:
        # Bitget 공통 오류 로깅
        msg = f"{type(e).__name__}: {str(e)}"
        logger.error(f"order_failed: {msg}")
        # 메시지 안에 Bitget status code 들어있는 경우가 많음
        return {"ok": False, "error": msg}
    except Exception as e:
        logger.error(f"order_failed: {e}")
        return {"ok": False, "error": str(e)}


# ----------------------------
# Shutdown hook
# ----------------------------
@app.on_event("shutdown")
async def _shutdown():
    global ex
    if ex is not None:
        try:
            await ex.close()
        except Exception:
            pass
