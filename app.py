import os
import time
import asyncio
from typing import Optional, Dict, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
import ccxt.async_support as ccxt

from trade import (
    normalize_symbol,
    fetch_net_position,
    to_coin_amount_from_contracts,
    clamp_amount_with_balance_and_caps,
    place_market_order,
    set_leverage_and_margin_mode_if_needed,
)

# -------------------------
# 환경 변수 (보수적 기본값)
# -------------------------
SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # USDT-M Perps
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

ALLOW_SHORT_OPEN = os.getenv("ALLOW_SHORT_OPEN", "false").lower() == "true"
MAX_USDT_PER_ORDER = float(os.getenv("MAX_USDT_PER_ORDER", "500"))     # 1회 최대 노치
MAX_POS_USDT = float(os.getenv("MAX_POS_USDT", "2000"))                # 포지션 총액 상한
SYMBOL_ALLOWLIST = {s.strip().upper() for s in os.getenv("SYMBOL_ALLOWLIST", "").split(",") if s.strip()}

# 레버리지/모드 (원하면 사용)
SET_LEVERAGE_ONCE = os.getenv("SET_LEVERAGE_ONCE", "true").lower() == "true"
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "cross")  # cross|isolated

# 중복 방지 캐시 (최근 N초)
DEDUPE_TTL_SEC = int(os.getenv("DEDUPE_TTL_SEC", "90"))
_seen: Dict[str, float] = {}

app = FastAPI()


# -------------------------
# 모델
# -------------------------
class TvPayload(BaseModel):
    secret: str
    symbol: str                   # e.g. "ETHUSDT.P"
    side: str                     # "buy" | "sell"
    orderType: str = Field(default="market")
    size: float                   # {{strategy.order.contracts}} (USDT 계약수/노치)

    @field_validator("side")
    @classmethod
    def _v_side(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("buy", "sell"):
            raise ValueError("side must be buy|sell")
        return v


def _new_bitget():
    key = os.getenv("BITGET_API_KEY")
    sec = os.getenv("BITGET_API_SECRET")
    pwd = os.getenv("BITGET_API_PASSWORD")
    if not (key and sec and pwd):
        raise HTTPException(status_code=500, detail="Bitget API env missing")
    return ccxt.bitget({
        "apiKey": key,
        "secret": sec,
        "password": pwd,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })


def _dedupe_key(p: TvPayload) -> str:
    # 같은 심볼/사이드/사이즈면 같은 키로 묶어 짧은 시간 중복 방지
    return f"{p.symbol.upper()}|{p.side}|{round(p.size, 8)}"


def _check_and_mark_dedupe(key: str) -> bool:
    now = time.time()
    stale = [k for k, t in _seen.items() if now - t > DEDUPE_TTL_SEC]
    for k in stale:
        _seen.pop(k, None)
    if key in _seen:
        return False
    _seen[key] = now
    return True


# -------------------------
# 웹훅
# -------------------------
@app.post("/webhook")
async def webhook(payload: TvPayload):
    if payload.secret != SECRET:
        raise HTTPException(status_code=403, detail="bad secret")

    # 허용 심볼 필터
    if SYMBOL_ALLOWLIST:
        pick = payload.symbol.upper().replace(".P", "")
        if pick not in SYMBOL_ALLOWLIST:
            return {"ok": True, "skipped": "symbol_not_whitelisted", "symbol": payload.symbol}

    # 중복 필터
    dedupe = _dedupe_key(payload)
    if not _check_and_mark_dedupe(dedupe):
        return {"ok": True, "skipped": "duplicate", "key": dedupe}

    # 심볼 정규화
    ccxt_symbol, base, quote = normalize_symbol(payload.symbol)

    ex = _new_bitget()
    try:
        # (선택) 레버리지/마진모드 기본 세팅
        if SET_LEVERAGE_ONCE:
            await set_leverage_and_margin_mode_if_needed(ex, ccxt_symbol, PRODUCT_TYPE, DEFAULT_LEVERAGE, MARGIN_MODE)

        # 현재 포지션 파악 (netQty>0=롱, <0=숏)
        net_qty, net_side, abs_amt, mark_price = await fetch_net_position(ex, ccxt_symbol, PRODUCT_TYPE)

        # 안전한 정책
        if payload.side == "buy":
            if net_side == "short":
                # 숏 청산 전량
                amount = abs_amt
                action = {"mode": "close_short", "reduceOnly": True}
                if DRY_RUN:
                    return {"ok": True, "dry_run": True, **action, "symbol": ccxt_symbol, "amount": amount}
                order = await place_market_order(ex, ccxt_symbol, "buy", amount, PRODUCT_TYPE, reduce_only=True)
                return {"ok": True, **action, "order": order}

            # 무포지션/롱 증액
            # TV contracts(USDT) → 코인수량 변환
            usdt_wanted = min(payload.size, MAX_USDT_PER_ORDER)
            amount = await to_coin_amount_from_contracts(ex, ccxt_symbol, usdt_wanted)
            # 잔고/상한 클램프
            amount = await clamp_amount_with_balance_and_caps(
                ex, ccxt_symbol, amount, MAX_USDT_PER_ORDER, MAX_POS_USDT, mark_price
            )
            if amount <= 0:
                return {"ok": False, "reason": "insufficient_balance_or_caps", "symbol": ccxt_symbol}
            action = {"mode": "open_or_add_long", "reduceOnly": False}
            if DRY_RUN:
                return {"ok": True, "dry_run": True, **action, "symbol": ccxt_symbol, "amount": amount}
            order = await place_market_order(ex, ccxt_symbol, "buy", amount, PRODUCT_TYPE, reduce_only=False)
            return {"ok": True, **action, "order": order}

        else:  # side == "sell"
            if net_side == "long":
                # 롱 청산 전량
                amount = abs_amt
                action = {"mode": "close_long", "reduceOnly": True}
                if DRY_RUN:
                    return {"ok": True, "dry_run": True, **action, "symbol": ccxt_symbol, "amount": amount}
                order = await place_market_order(ex, ccxt_symbol, "sell", amount, PRODUCT_TYPE, reduce_only=True)
                return {"ok": True, **action, "order": order}

            if net_side is None:
                # 무포지션에서 sell 신호 ⇒ 실수 청산신호로 보고 무시
                return {"ok": True, "skipped": "no_position_to_close", "symbol": ccxt_symbol}

            # 남은 경우: 순숏 상태에서 sell = 숏 증액
            if not ALLOW_SHORT_OPEN:
                return {"ok": True, "skipped": "short_add_blocked"}
            usdt_wanted = min(payload.size, MAX_USDT_PER_ORDER)
            amount = await to_coin_amount_from_contracts(ex, ccxt_symbol, usdt_wanted)
            amount = await clamp_amount_with_balance_and_caps(
                ex, ccxt_symbol, amount, MAX_USDT_PER_ORDER, MAX_POS_USDT, mark_price
            )
            if amount <= 0:
                return {"ok": False, "reason": "insufficient_balance_or_caps", "symbol": ccxt_symbol}
            action = {"mode": "add_short", "reduceOnly": False}
            if DRY_RUN:
                return {"ok": True, "dry_run": True, **action, "symbol": ccxt_symbol, "amount": amount}
            order = await place_market_order(ex, ccxt_symbol, "sell", amount, PRODUCT_TYPE, reduce_only=False)
            return {"ok": True, **action, "order": order}

    finally:
        try:
            await ex.close()
        except Exception:
            pass