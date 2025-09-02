# app.py — Bitget USDT-M Futures router (full, fixed)
import os
import json
import math
import logging
from typing import Any, Dict, Tuple, Optional

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# ---------- Env ----------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1/20
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------- Logging ----------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("router")

app = FastAPI()


# ---------- Helpers ----------
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView: 'HBARUSDT.P' → CCXT(Bitget): 'HBAR/USDT:USDT'
    예) 1000BONKUSDT.P → 1000BONK/USDT:USDT
    """
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        raise ValueError(f"Unsupported quote in symbol: {tv_symbol}")
    base = s[:-4]
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


# ---------- CCXT ----------
def build_exchange() -> ccxt.bitget:
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},  # USDT-M
    })
    return ex


async def ensure_markets(ex: ccxt.Exchange):
    try:
        await ex.load_markets(reload=False)
    except Exception:
        await ex.load_markets(reload=True)


async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    bal = await ex.fetch_balance({'type': 'swap'})
    usdt = bal.get('USDT') or {}
    total = safe_float(usdt.get('total'))
    if total <= 0:
        total = safe_float(usdt.get('free')) + safe_float(usdt.get('used'))
    return max(total, 0.0)


async def fetch_market_and_price(ex: ccxt.Exchange, ccxt_symbol: str) -> Tuple[Dict[str, Any], float]:
    market = ex.market(ccxt_symbol)
    t = await ex.fetch_ticker(ccxt_symbol)
    price = safe_float(t.get('last')) or safe_float(t.get('mark'))
    if price <= 0 and isinstance(t.get('info'), dict):
        price = safe_float(t['info'].get('last')) or safe_float(t['info'].get('markPrice'))
    if price <= 0:
        raise RuntimeError(f"Could not fetch price for {ccxt_symbol}")
    return market, price


async def fetch_net_position(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    pos_list = await ex.fetch_positions([ccxt_symbol], params={"productType": "umcbl"})
    net = 0.0
    for p in pos_list:
        if p.get('symbol') != ccxt_symbol:
            continue
        contracts = safe_float(p.get('contracts', 0.0))
        side = (p.get('side') or "").lower()
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net


def build_reduce_only_params() -> Dict[str, Any]:
    return {"reduceOnly": True, "productType": "umcbl"}


def notional_to_amount(notional_usdt: float, price: float) -> float:
    return max(notional_usdt / price, 0.0)


def apply_limits(amount: float, price: float, market: Dict[str, Any]) -> float:
    precision = None
    if isinstance(market.get('precision'), dict):
        precision = market['precision'].get('amount')
    limits = market.get('limits') or {}
    min_amt = safe_float((limits.get('amount') or {}).get('min'))
    min_cost = safe_float((limits.get('cost') or {}).get('min'))
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
    reduce_only: bool
):
    params = {"productType": "umcbl"}
    if reduce_only:
        params.update(build_reduce_only_params())
    return await ex.create_order(ccxt_symbol, 'market', side, amount, None, params)


# ---------- Schemas ----------
class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str
    orderType: str = Field(default="market")
    size: float | None = None


# ---------- Routes ----------
@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        data = TVAlert(**payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad body: {e}")

    if WEBHOOK_SECRET and data.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    side = (data.side or "").lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Unsupported side: {data.side}")

    try:
        ccxt_symbol = tv_to_ccxt_symbol(data.symbol)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex = build_exchange()
    try:
        await ensure_markets(ex)
        equity = await fetch_equity_usdt(ex)
        if equity <= 0:
            raise HTTPException(status_code=400, detail="No equity")

        notional_usdt = equity * FRACTION_PER_POSITION
        market, price = await fetch_market_and_price(ex, ccxt_symbol)
        net = await fetch_net_position(ex, ccxt_symbol)

        reduce_only = False
        if side == "buy" and net < -1e-12:
            reduce_only = True
        elif side == "sell" and net > 1e-12:
            reduce_only = True

        raw_amount = notional_to_amount(notional_usdt, price)
        amount = apply_limits(raw_amount, price, market)
        if amount <= 0:
            log.info("skip: amount zero | sym=%s price=%.8f notional=%.4f", ccxt_symbol, price, notional_usdt)
            return {"ok": True, "skip": "amount zero", "symbol": data.symbol, "price": price}

        log.info(
            "order plan | tv=%s ccxt=%s side=%s reduceOnly=%s equity=%.4f notional=%.4f price=%.8f amount=%.10f",
            data.symbol, ccxt_symbol, side, reduce_only, equity, notional_usdt, price, amount
        )

        order = await place_market_order(ex, ccxt_symbol, side, amount, reduce_only)

        log.info("order done | id=%s sym=%s side=%s reduceOnly=%s amt=%s",
                 order.get('id'), ccxt_symbol, side, reduce_only, order.get('amount'))

        return {
            "ok": True,
            "order": order,
            "symbol": data.symbol,
            "reduceOnly": reduce_only,
            "equity": equity,
            "notional": notional_usdt,
            "price": price,
            "amount": amount
        }

    except Exception as e:
        log.exception("runtime_error")
        raise HTTPException(status_code=500, detail=f"runtime_error: {e}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass