# app.py
# -----------------------------------------------------------------------------
# TradingView → FastAPI Webhook → Bitget(USDT-M SWAP) 주문 라우터 (ccxt async)
# - 신규/추가/청산을 포지션 상태로 자동 판별
# - reduceOnly 정확 적용(청산/감소 시)
# - 청산 수량 포지션 수량으로 캡핑(초과 청산 방지)
# - MAX_COINS, FRACTION_PER_POSITION, FORCE_EQUAL_NOTIONAL, ALLOW_SHORTS, DRY_RUN 지원
# - TV 심볼(예: "1000BONKUSDT.P") → Bitget ccxt 심볼("1000BONK/USDT:USDT") 변환
# - 풍부한 로깅
# -----------------------------------------------------------------------------

import os
import json
import math
import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import ccxt.async_support as ccxt  # async ccxt
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# -----------------------------------------------------------------------------
# 환경변수
# -----------------------------------------------------------------------------
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "").strip()
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "").strip()
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# 동작 옵션들
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

# 시드 비중/최대 코인
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# -----------------------------------------------------------------------------
# FastAPI
# -----------------------------------------------------------------------------
app = FastAPI(title="tv-bitget-router", version="1.0.0")

# 전역 거래소 인스턴스
exchange: Optional[ccxt.bitget] = None

# -----------------------------------------------------------------------------
# 유틸
# -----------------------------------------------------------------------------
def log(msg: str, **kw):
    # 단순 로그 출력(uvicorn 로그로 확인)
    if kw:
        print(msg, "|", json.dumps(kw, ensure_ascii=False))
    else:
        print(msg)


def tv_symbol_to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼(예: '1000BONKUSDT.P', 'BTCUSDT.P')을
    Bitget USDT-M SWAP ccxt 심볼('1000BONK/USDT:USDT', 'BTC/USDT:USDT')로 변환
    """
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        # 혹시 모를 예외. 대다수는 USDT 페어
        raise ValueError(f"unexpected tv symbol (no USDT tail): {tv_symbol}")
    base = s[:-4]  # remove USDT
    return f"{base}/USDT:USDT"


def bool_env(v: str, default=False) -> bool:
    if v is None:
        return default
    return v.lower() == "true"


async def safe_fetch_ticker(symbol_ccxt: str) -> Optional[Dict[str, Any]]:
    try:
        return await exchange.fetch_ticker(symbol_ccxt)
    except Exception as e:
        log("fetch_ticker_error", symbol=symbol_ccxt, err=str(e))
        return None


def to_amount_precision(symbol_ccxt: str, amount: float) -> float:
    try:
        return float(exchange.amount_to_precision(symbol_ccxt, amount))
    except Exception:
        return float(f"{amount:.8f}")


# -----------------------------------------------------------------------------
# 포지션/사이드 판별 & 주문
# -----------------------------------------------------------------------------
async def get_position_size(symbol_ccxt: str) -> float:
    """
    현재 포지션 계약 수량을 방향부호로 반환
    롱=+, 숏=-, 없음=0
    """
    try:
        positions = await exchange.fetch_positions([symbol_ccxt])
    except Exception as e:
        log("fetch_positions_error", symbol=symbol_ccxt, err=str(e))
        return 0.0

    cur = 0.0
    for p in positions:
        if p.get("symbol") == symbol_ccxt:
            contracts = float(p.get("contracts") or 0.0)
            side = (p.get("side") or "").lower()
            if side == "long":
                cur = contracts
            elif side == "short":
                cur = -contracts
            else:
                cur = 0.0
            break
    return cur


async def get_open_coin_count() -> int:
    """
    현재 보유 중(계약>0)의 서로 다른 심볼 개수
    """
    try:
        allpos = await exchange.fetch_positions()
    except Exception as e:
        log("fetch_positions_all_error", err=str(e))
        return 0

    seen = set()
    for p in allpos:
        contracts = float(p.get("contracts") or 0.0)
        if contracts > 0:
            seen.add(p.get("symbol"))
    return len(seen)


async def equal_notional_size(symbol_ccxt: str) -> Optional[float]:
    """
    FORCE_EQUAL_NOTIONAL=True인 경우 사용할
    '현재 총자본 × FRACTION_PER_POSITION / 현재가격' 으로 계약수 산출
    """
    try:
        bal = await exchange.fetch_balance()
        # Bitget USDT-M 기준 자본: USDT 지갑 total (cross 기준)
        usdt = bal.get("USDT") or {}
        equity = float(usdt.get("total") or 0.0)
        if equity <= 0:
            # 혹시 general 'total' 사용
            equity = float(bal.get("total", {}).get("USDT", 0.0))
    except Exception as e:
        log("fetch_balance_error", err=str(e))
        return None

    if equity <= 0:
        log("equity_not_found_or_zero")
        return None

    tkr = await safe_fetch_ticker(symbol_ccxt)
    if not tkr:
        return None
    last = float(tkr.get("last") or 0)
    if last <= 0:
        log("ticker_last_invalid", symbol=symbol_ccxt, ticker=tkr)
        return None

    notional = max(0.0, equity * FRACTION_PER_POSITION)
    contracts = notional / last
    contracts = to_amount_precision(symbol_ccxt, contracts)
    return max(0.0, contracts)


async def place_order_bitget(symbol_ccxt: str, tv_side: str, tv_size: float) -> Dict[str, Any]:
    """
    TradingView가 보낸 사이드/사이즈를 받아 Bitget에 정확한 조합으로 주문
    - 포지션 상태를 읽어 신규/추가/감소/청산 판단
    - reduceOnly 정확 세팅
    - 청산 시 수량 캡핑
    """
    tv_side = tv_side.lower().strip()
    if tv_side not in ("buy", "sell"):
        raise RuntimeError(f"invalid tv_side: {tv_side}")

    # 사이즈 결정: FORCE_EQUAL_NOTIONAL=true면 재계산
    if FORCE_EQUAL_NOTIONAL:
        calc = await equal_notional_size(symbol_ccxt)
        if calc is not None and calc > 0:
            req_size = calc
        else:
            req_size = max(0.0, float(tv_size))
    else:
        req_size = max(0.0, float(tv_size))

    # 현재 포지션
    cur = await get_position_size(symbol_ccxt)

    # 신규 오픈 제한
    if cur == 0:
        # 롱/숏 신규 오픈 허용 여부
        if tv_side == "sell" and not ALLOW_SHORTS:
            log("skip_open_short_disallowed", symbol=symbol_ccxt)
            return {"skipped": True, "reason": "short_disallowed"}

        # 최대 코인 제한 검사
        open_cnt = await get_open_coin_count()
        if open_cnt >= MAX_COINS:
            log("skip_open_due_to_MAX_COINS", symbol=symbol_ccxt, open_cnt=open_cnt, MAX_COINS=MAX_COINS)
            return {"skipped": True, "reason": "max_coins_reached"}

        reduce_only = False
        side_to_send = tv_side
        size_to_send = req_size
        intent = "OPEN"
    elif cur > 0:
        # 롱 보유
        if tv_side == "buy":
            reduce_only = False
            side_to_send = "buy"
            size_to_send = req_size
            intent = "ADD_LONG"
        else:
            # sell → 롱 감소/청산
            reduce_only = True
            side_to_send = "sell"
            size_to_send = min(abs(cur), req_size)
            intent = "CLOSE_LONG"
    else:
        # 숏 보유(cur<0)
        if tv_side == "sell":
            reduce_only = False
            side_to_send = "sell"
            size_to_send = req_size
            intent = "ADD_SHORT"
        else:
            # buy → 숏 감소/청산
            reduce_only = True
            side_to_send = "buy"
            size_to_send = min(abs(cur), req_size)
            intent = "CLOSE_SHORT"

    size_to_send = to_amount_precision(symbol_ccxt, size_to_send)

    if size_to_send <= 0:
        log("skip_zero_size", symbol=symbol_ccxt, intent=intent, cur=cur, tv_side=tv_side)
        return {"skipped": True, "reason": "zero_size", "intent": intent}

    # 디버깅용 상세 로그
    log("order_intent",
        intent=intent, tv_side=tv_side, bitget_side=side_to_send,
        reduceOnly=reduce_only, size=size_to_send, cur_pos=cur, symbol=symbol_ccxt)

    if DRY_RUN:
        log("DRY_RUN_order_skipped")
        return {"dry_run": True, "intent": intent, "side": side_to_send, "size": size_to_send}

    params = {
        "reduceOnly": reduce_only,
    }

    try:
        order = await exchange.create_order(
            symbol=symbol_ccxt,
            type="market",
            side=side_to_send,
            amount=size_to_send,
            params=params,
        )
        log("order_ok", order=order)
        return order
    except ccxt.BaseError as e:
        log("order_failed", err=str(e))
        raise


# -----------------------------------------------------------------------------
# 요청 스키마
# -----------------------------------------------------------------------------
class TVPayload(BaseModel):
    secret: Optional[str] = None
    symbol: str
    side: str                # "buy" | "sell"
    orderType: Optional[str] = "market"  # 무시(시장가 고정)
    size: Optional[float] = 0


# -----------------------------------------------------------------------------
# FastAPI hooks
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global exchange
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSWORD):
        log("WARNING_api_keys_missing_or_empty")
    exchange = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",      # USDT-M Perp
        },
    })
    # 시장 메타 프리로드(precision 사용)
    try:
        await exchange.load_markets()
    except Exception as e:
        log("load_markets_error", err=str(e))
    log("startup_done", dry_run=DRY_RUN, force_equal_notional=FORCE_EQUAL_NOTIONAL,
        allow_shorts=ALLOW_SHORTS, fraction_per_position=FRACTION_PER_POSITION, max_coins=MAX_COINS)


@app.on_event("shutdown")
async def shutdown():
    try:
        await exchange.close()
    except Exception:
        pass
    log("shutdown_done")


# -----------------------------------------------------------------------------
# 라우트
# -----------------------------------------------------------------------------
@app.get("/")
async def status():
    return {"status": "ok", "service": "tv-bitget-router"}

@app.post("/webhook")
async def webhook(payload: TVPayload, request: Request):
    # 1) 보안 토큰 검사
    if WEBHOOK_SECRET:
        if not payload.secret or payload.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="invalid secret")

    # 2) TV → Bitget 심볼 변환
    try:
        symbol_ccxt = tv_symbol_to_ccxt_symbol(payload.symbol)
    except Exception as e:
        log("symbol_convert_error", tv_symbol=payload.symbol, err=str(e))
        raise HTTPException(status_code=422, detail=f"bad symbol: {payload.symbol}")

    # 3) 주문 실행
    try:
        res = await place_order_bitget(symbol_ccxt, payload.side, float(payload.size or 0.0))
        return JSONResponse({"ok": True, "result": res})
    except ccxt.BaseError as e:
        # Bitget/ccxt 에러를 그대로 노출(로그에는 남겨둠)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    except Exception as e:
        log("unhandled_error", err=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
