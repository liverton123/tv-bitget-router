# app.py
import os
import json
import math
import time
import asyncio
import logging
from typing import Optional, Dict, Any

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field

# --------------------------
# Logger (app.logger 쓰지 말 것!)
# --------------------------
logger = logging.getLogger("tv-bitget-router")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(_h)

app = FastAPI(title="tv-bitget-router")

# --------------------------
# ENV
# --------------------------
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# 포지션 등분 비율(예: 0.05 => 시드의 1/20)
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))
# 동시 보유 가능한 코인 수 제한
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
# 숏 허용 여부
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
# 동일 명목가 강제 (소수/정수 단위 코인 간 편차 완화)
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"

# --------------------------
# CCXT (Bitget Perp/Swap 전용)
# --------------------------
ex: Optional[ccxt.bitget] = None
markets: Dict[str, Any] = {}


async def ensure_exchange():
    global ex, markets
    if ex is None:
        ex = ccxt.bitget({
            "apiKey": API_KEY,
            "secret": API_SECRET,
            "password": API_PASSWORD,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",          # 선물(영구) 전용
                "defaultSubType": "linear",     # USDT 선호
            },
        })
        await ex.load_markets()
        markets = ex.markets
        logger.info("Bitget markets loaded | defaultType=swap")

# --------------------------
# 유틸
# --------------------------
def tv_symbol_to_ccxt(tv_symbol: str) -> str:
    # TradingView 알림: 'WIFUSDT.P' 같은 형태 → CCXT 심볼 유추
    base = tv_symbol.replace(".P", "").strip()
    # Bitget Perp: 대부분 'BASEUSDT/USDT:USDT' 로 들어간다. (ccxt가 변환)
    # market가 있으면 그대로 symbol 사용, 없으면 fallback
    # 우선 marketId로 조회
    for m in (base, f"{base}/USDT:USDT", f"{base}/USDT"):
        if m in markets:
            return markets[m]["symbol"]
    # 마지막 fallback (ccxt가 내부에서 다시 변환 시도)
    return f"{base}/USDT:USDT"

async def fetch_price(symbol: str) -> float:
    ticker = await ex.fetch_ticker(symbol)
    return float(ticker["last"])

async def fetch_open_positions_map() -> Dict[str, Dict[str, Any]]:
    """
    현재 오픈 포지션을 심볼별로 맵으로 반환
    {
      "WIFUSDT/USDT:USDT": {"side":"long","size": 123.0},  # size는 코인 수
      ...
    }
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        poss = await ex.fetch_positions()  # 모든 포지션
        for p in poss:
            amt = float(p.get("contracts") or p.get("contractsSize") or 0.0)
            if amt == 0:
                continue
            side = "long" if (float(p.get("side") in (None, "long")) or float(p.get("contracts", 0)) > 0) else "short"
            out[p["symbol"]] = {"side": side, "size": abs(amt)}
    except Exception as e:
        logger.warning(f"fetch_positions failed: {e}")
    return out

async def free_balance_usdt() -> float:
    try:
        bal = await ex.fetch_balance()
        # futures USDT
        for k in ("USDT", "usdt"):
            if k in bal and isinstance(bal[k], dict):
                return float(bal[k].get("free", 0.0))
        return float(bal.get("free", 0.0))
    except Exception as e:
        logger.warning(f"fetch_balance failed: {e}")
        return 0.0

def calc_amount(symbol: str, price: float, free_usdt: float) -> float:
    """
    마진(현금)은 항상 시드의 FRACTION_PER_POSITION 만큼.
    레버리지는 Bitget 설정값 그대로 사용 (우린 개입 X).
    amount(코인수) = (free_usdt * fraction) / price
    """
    if free_usdt <= 0:
        return 0.0
    notional = free_usdt * FRACTION_PER_POSITION
    if FORCE_EQUAL_NOTIONAL:
        # 코인 단위별 차이를 줄이기 위해 동일 명목 기준 유지
        pass
    amt = max(notional / max(price, 1e-12), 0.0)
    try:
        amt = ex.amount_to_precision(symbol, amt)
    except Exception:
        # precision 불가시 소수 6자리 제한
        amt = float(f"{amt:.6f}")
    return float(amt)

async def open_coins_count(positions_map: Dict[str, Dict[str, Any]]) -> int:
    return len(positions_map)

def is_entry_signal(side: str, cur_pos_side: Optional[str]) -> bool:
    # side == "buy" 이면 롱 진입/증액, "sell" 이면 숏 진입/증액
    if cur_pos_side is None:
        return True  # 신규 진입
    # 같은 방향이면 증액(물타기)
    if side == "buy" and cur_pos_side == "long":
        return True
    if side == "sell" and cur_pos_side == "short":
        return True
    return False

def is_exit_signal(side: str, cur_pos_side: Optional[str]) -> bool:
    # 반대 방향이면 청산 신호로 본다
    if cur_pos_side is None:
        return False
    if side == "buy" and cur_pos_side == "short":
        return True
    if side == "sell" and cur_pos_side == "long":
        return True
    return False

async def place_order_bitget(symbol: str, side: str, amount: float) -> Dict[str, Any]:
    # side: "buy"/"sell"
    # 시장가
    params = {"reduceOnly": False}
    try:
        return await ex.create_order(symbol, "market", side, amount, None, params)
    except ccxt.BaseError as e:
        # Bitget API 에러 메시지 노출
        try:
            data = getattr(e, "response", None)
            logger.error(f"order_failed: {getattr(e, 'message', str(e))} | resp={data}")
        except Exception:
            logger.error(f"order_failed: {e}")
        raise

# --------------------------
# Pydantic
# --------------------------
class TVAlert(BaseModel):
    secret: str = Field(..., description="Webhook secret")
    symbol: str = Field(..., description="e.g. WIFUSDT.P")
    side: str = Field(..., description="buy or sell")
    orderType: Optional[str] = None
    size: Optional[float] = None

# --------------------------
# Routes
# --------------------------
@app.get("/status")
async def status():
    await ensure_exchange()
    return {"ok": True, "markets": len(markets), "dry_run": DRY_RUN}

@app.post("/webhook")
async def webhook(msg: TVAlert, request: Request):
    await ensure_exchange()

    # 1) secret 체크
    if WEBHOOK_SECRET and msg.secret != WEBHOOK_SECRET:
        logger.warning("auth_failed: wrong secret")
        raise HTTPException(status_code=401, detail="invalid secret")

    side = msg.side.lower().strip()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="invalid side")
    if side == "sell" and not ALLOW_SHORTS:
        logger.info("short_disabled | skip")
        return {"ok": True, "skipped": "short_disabled"}

    # 2) 심볼 변환
    ccxt_symbol = tv_symbol_to_ccxt(msg.symbol)
    price = await fetch_price(ccxt_symbol)

    # 3) 현재 보유 포지션 조회
    pos_map = await fetch_open_positions_map()
    cur = pos_map.get(ccxt_symbol)  # {'side':'long'|'short','size':float} or None

    # 4) 신규 진입/물타기 vs 청산 구분
    entry = is_entry_signal(side, cur.get("side") if cur else None)
    exit_ = is_exit_signal(side, cur.get("side") if cur else None)

    plan = {
        "symbol": ccxt_symbol,
        "tv_symbol": msg.symbol,
        "incoming_side": side,
        "mode": "entry" if entry else ("exit" if exit_ else "skip"),
        "position": cur or {},
        "price": price,
    }
    logger.info(f"plan: {json.dumps(plan, ensure_ascii=False)}")

    # 4-1) 진입/물타기인데 최대 코인 수 초과면 skip
    if entry and (await open_coins_count(pos_map)) >= MAX_COINS and (cur is None):
        logger.info(f"skip: max_coins reached ({MAX_COINS})")
        return {"ok": True, "skipped": "max_coins"}

    if DRY_RUN:
        logger.info("dry_run: skip real order")
        return {"ok": True, "dry_run": True, **plan}

    # 5) 수량 계산
    free_usdt = await free_balance_usdt()
    amount = calc_amount(ccxt_symbol, price, free_usdt)
    if amount <= 0:
        logger.info(f"skip: calc amount is zero | symbol={ccxt_symbol}, price={price}, free={free_usdt}")
        return {"ok": True, "skipped": "amount_is_zero", **plan}

    # 6) 청산 신호면 reduceOnly 로 청산
    if exit_:
        # 현재 보유 수량만큼 반대 주문
        reduce_amt = cur["size"]
        try:
            params = {"reduceOnly": True}
            if side == "buy":
                res = await ex.create_order(ccxt_symbol, "market", "buy", reduce_amt, None, params)
            else:
                res = await ex.create_order(ccxt_symbol, "market", "sell", reduce_amt, None, params)
            logger.info(f"close_order_ok: {res}")
            return {"ok": True, "close": True, "result": res}
        except Exception as e:
            logger.error(f"close_order_failed: {e}")
            raise HTTPException(status_code=500, detail=f"close_failed: {e}")

    # 7) 진입/물타기
    try:
        res = await place_order_bitget(ccxt_symbol, side, amount)
        logger.info(f"open_order_ok: {res}")
        return {"ok": True, "result": res, **plan, "amount": amount}
    except Exception as e:
        # Bitget 쪽에서 'side mismatch' 등이 올 수 있음 → 그대로 200은 주되, 내용 노출
        logger.error(f"open_order_failed: {e}")
        return {"ok": False, "error": str(e), **plan}

@app.on_event("shutdown")
async def _shutdown():
    if ex is not None:
        try:
            await ex.close()
        except Exception:
            pass
