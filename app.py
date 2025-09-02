# app.py — TV → Bitget USDT-M(UMCBL) router
import os
import json
import math
import logging
from typing import Any, Dict, Optional, Tuple

import ccxt.async_support as ccxt
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# ====== ENV ======
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1/20
ALLOW_SHORTS = (os.getenv("ALLOW_SHORTS", "true").lower() == "true")       # 기본 true
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Bitget USDT-M perpetual 제품 타입 (대문자 필수)
PRODUCT_TYPE = "UMCBL"

# ====== LOGGING ======
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tv-bitget-router")

app = FastAPI()


# ====== UTILS ======
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼 예)
      'HBARUSDT.P'     -> 'HBAR/USDT:USDT'
      '1000BONKUSDT.P' -> '1000BONK/USDT:USDT'
    """
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        raise ValueError(f"Unsupported quote (USDT only): {tv_symbol}")
    base = s[:-4]  # drop 'USDT'
    return f"{base}/USDT:USDT"


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def round_to_precision(amount: float, precision: Optional[int]) -> float:
    if precision is None:
        return amount
    factor = 10 ** precision
    return math.floor(amount * factor + 1e-12) / factor


# ====== CCXT ======
def build_exchange() -> ccxt.bitget:
    return ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # USDT-M perpetual
        },
    })


async def ensure_markets(ex: ccxt.Exchange):
    try:
        await ex.load_markets(reload=False)
    except Exception:
        await ex.load_markets(reload=True)


async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    # 스왑 계정 잔고
    bal = await ex.fetch_balance({'type': 'swap', "productType": PRODUCT_TYPE})
    usdt = bal.get("USDT") or {}
    total = safe_float(usdt.get("total"))
    if total <= 0:
        total = safe_float(usdt.get("free")) + safe_float(usdt.get("used"))
    return max(total, 0.0)


async def fetch_market_and_price(ex: ccxt.Exchange, ccxt_symbol: str) -> Tuple[Dict[str, Any], float]:
    market = ex.market(ccxt_symbol)
    # 가격 조회 (productType은 여기에 안 써도 됨)
    t = await ex.fetch_ticker(ccxt_symbol)
    price = safe_float(t.get("last")) or safe_float(t.get("mark"))
    if price <= 0 and isinstance(t.get("info"), dict):
        price = safe_float(t["info"].get("markPrice")) or safe_float(t["info"].get("close"))
    if price <= 0:
        raise RuntimeError(f"Could not fetch price for {ccxt_symbol}")
    return market, price


async def fetch_net_position(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    """
    현 심볼의 순 계약수(롱+ / 숏-) 계산.
    Bitget v2 allPosition 엔드포인트가 productType 필수 -> 대문자 UMCBL!
    """
    pos_list = await ex.fetch_positions([ccxt_symbol], params={"productType": PRODUCT_TYPE})
    net = 0.0
    for p in pos_list:
        if p.get("symbol") != ccxt_symbol:
            continue
        contracts = safe_float(p.get("contracts", 0.0))
        side = (p.get("side") or "").lower()
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net


def apply_limits(amount: float, price: float, market: Dict[str, Any]) -> float:
    precision = None
    if isinstance(market.get("precision"), dict):
        precision = market["precision"].get("amount")
    limits = market.get("limits") or {}
    min_amt = safe_float((limits.get("amount") or {}).get("min"))
    min_cost = safe_float((limits.get("cost") or {}).get("min"))

    amt = amount
    if precision is not None:
        amt = round_to_precision(amt, precision)
    if min_amt and amt < min_amt:
        amt = min_amt
        if precision is not None:
            amt = round_to_precision(amt, precision)
    if min_cost and price * amt < min_cost:
        amt = min_cost / price
        if precision is not None:
            amt = round_to_precision(amt, precision)
    return amt


async def place_market_order(
    ex: ccxt.Exchange,
    ccxt_symbol: str,
    side: str,
    amount: float,
    reduce_only: bool,
):
    params = {"productType": PRODUCT_TYPE}
    if reduce_only:
        params["reduceOnly"] = True
    # Bitget은 market 주문에서 price=None
    return await ex.create_order(ccxt_symbol, "market", side, amount, None, params)


# ====== SCHEMAS ======
class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str                       # 'buy' or 'sell'
    orderType: str = Field(default="market")
    size: float | None = None       # TV에 표시되는 수량(참고용), 실제 체결은 아래 로직이 계산


# ====== ROUTES ======
@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        msg = TVAlert(**body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad body: {e}")

    if WEBHOOK_SECRET and msg.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    side = (msg.side or "").lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Unsupported side: {msg.side}")
    if side == "sell" and not ALLOW_SHORTS:
        raise HTTPException(status_code=400, detail="Shorts are disabled")

    try:
        ccxt_symbol = tv_to_ccxt_symbol(msg.symbol)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex = build_exchange()
    try:
        await ensure_markets(ex)

        equity = await fetch_equity_usdt(ex)
        if equity <= 0:
            raise HTTPException(status_code=400, detail="No USDT equity in swap account")

        # 목표 명목가: (시드 × FRACTION).  (※ 마진고정/레버리지반영 버전이 필요하면 알려줘!)
        market, price = await fetch_market_and_price(ex, ccxt_symbol)
        target_notional = equity * FRACTION_PER_POSITION
        raw_amount = max(target_notional / price, 0.0)
        amount = apply_limits(raw_amount, price, market)
        if amount <= 0:
            log.info("skip: calc amount is zero | sym=%s price=%.10f notional=%.4f", ccxt_symbol, price, target_notional)
            return {"ok": True, "skip": "amount is zero", "symbol": msg.symbol, "price": price}

        # 현재 포지션 방향에 따라 reduceOnly 판별
        net = await fetch_net_position(ex, ccxt_symbol)
        reduce_only = (side == "buy" and net < -1e-12) or (side == "sell" and net > 1e-12)

        log.info(
            "plan: %s | side=%s reduceOnly=%s equity=%.4f notional=%.4f price=%.10f amount=%.10f",
            ccxt_symbol, side, reduce_only, equity, target_notional, price, amount
        )

        order = await place_market_order(ex, ccxt_symbol, side, amount, reduce_only)

        log.info("done: id=%s sym=%s side=%s reduceOnly=%s amount=%s",
                 order.get("id"), ccxt_symbol, side, reduce_only, order.get("amount"))

        return {
            "ok": True,
            "symbol": msg.symbol,
            "reduceOnly": reduce_only,
            "equity": equity,
            "notional": target_notional,
            "price": price,
            "amount": amount,
            "order": order,
        }

    except ccxt.BaseError as e:
        # 거래소 메시지 그대로 노출해 디버깅 쉽게
        log.exception("exchange_error")
        raise HTTPException(status_code=500, detail=f"exchange_error: {getattr(e, 'message', str(e))}")
    except Exception as e:
        log.exception("runtime_error")
        raise HTTPException(status_code=500, detail=f"runtime_error: {e}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass