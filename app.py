import os
import json
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# ----------------------- Env -----------------------
load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1/20
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PRODUCT_TYPE = "UMCBL"  # Bitget USDT-M Perp (대문자 필수)

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tv-bitget-router")

app = FastAPI()

# --------------------- Utils -----------------------
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        raise ValueError(f"Unsupported quote (USDT only): {tv_symbol}")
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
    q = 10 ** precision
    return math.floor(amount * q + 1e-12) / q

def build_exchange() -> ccxt.bitget:
    return ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })

async def ensure_markets(ex: ccxt.Exchange):
    try:
        await ex.load_markets(reload=False)
    except Exception:
        await ex.load_markets(reload=True)

async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    # 중요: balance에는 productType 넣지 말 것 (40020 유발)
    bal = await ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT") or {}
    total = safe_float(usdt.get("total"))
    if total <= 0:
        total = safe_float(usdt.get("free")) + safe_float(usdt.get("used"))
    return max(total, 0.0)

async def fetch_market_and_price(ex: ccxt.Exchange, ccxt_symbol: str) -> Tuple[Dict[str, Any], float]:
    market = ex.market(ccxt_symbol)
    t = await ex.fetch_ticker(ccxt_symbol)
    price = safe_float(t.get("last")) or safe_float(t.get("mark"))
    if price <= 0 and isinstance(t.get("info"), dict):
        price = safe_float(t["info"].get("markPrice")) or safe_float(t["info"].get("close"))
    if price <= 0:
        raise RuntimeError(f"Could not fetch price for {ccxt_symbol}")
    return market, price

async def fetch_positions_all(ex: ccxt.Exchange) -> List[Dict[str, Any]]:
    return await ex.fetch_positions(params={"productType": PRODUCT_TYPE})

async def fetch_net_position(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    pos = await ex.fetch_positions([ccxt_symbol], params={"productType": PRODUCT_TYPE})
    net = 0.0
    for p in pos:
        if p.get("symbol") != ccxt_symbol:
            continue
        contracts = safe_float(p.get("contracts"))
        side = (p.get("side") or "").lower()
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net

async def count_open_symbols(ex: ccxt.Exchange) -> int:
    pos = await fetch_positions_all(ex)
    syms = set()
    for p in pos:
        if abs(safe_float(p.get("contracts"))) > 1e-12:
            syms.add(p.get("symbol"))
    return len(syms)

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
    return await ex.create_order(ccxt_symbol, "market", side, amount, None, params)

# -------------------- Models ----------------------
class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str              # "buy" | "sell"
    orderType: str = Field(default="market")
    size: float | None = None  # TV가 보내도 무시 (우리가 계산)

# -------------------- Routes ----------------------
@app.get("/")
async def root():
    return {"ok": True}

@app.post("/webhook")
async def webhook(req: Request):
    # 1) 파싱/인증
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

    # 2) 심볼 변환
    try:
        ccxt_symbol = tv_to_ccxt_symbol(msg.symbol)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex = build_exchange()
    try:
        await ensure_markets(ex)

        # 3) 포지션 상태
        net = await fetch_net_position(ex, ccxt_symbol)
        opened_cnt = await count_open_symbols(ex)

        is_reduce_only = (side == "buy" and net < -1e-12) or (side == "sell" and net > 1e-12)
        is_new_open = abs(net) <= 1e-12

        # MAX_COINS 초과 시 신규 오픈만 스킵(물타기/정리는 허용)
        if is_new_open and opened_cnt >= MAX_COINS:
            log.info("skip: max_coins reached | open=%d max=%d sym=%s", opened_cnt, MAX_COINS, msg.symbol)
            return {"ok": True, "skip": "max_coins", "open": opened_cnt, "max": MAX_COINS, "symbol": msg.symbol}

        # 4) 수량 계산: 시드×fraction → 명목가 기준
        equity = await fetch_equity_usdt(ex)
        market, price = await fetch_market_and_price(ex, ccxt_symbol)
        target_notional = equity * FRACTION_PER_POSITION
        raw_amount = max(target_notional / price, 0.0)
        amount = apply_limits(raw_amount, price, market)
        if amount <= 0:
            log.info("skip: calc amount is zero | sym=%s price=%.10f notional=%.4f",
                     ccxt_symbol, price, target_notional)
            return {"ok": True, "skip": "amount_zero", "symbol": msg.symbol}

        plan = {
            "sym": ccxt_symbol,
            "tv": msg.symbol,
            "side": side,
            "reduceOnly": is_reduce_only,
            "is_new_open": is_new_open,
            "open_count": opened_cnt,
            "equity": equity,
            "price": price,
            "notional": target_notional,
            "amount": amount,
        }
        log.info("plan: %s", json.dumps(plan, ensure_ascii=False))

        # 5) 주문
        order = await place_market_order(ex, ccxt_symbol, side, amount, is_reduce_only)
        log.info("done: id=%s sym=%s side=%s ro=%s amt=%s",
                 order.get("id"), ccxt_symbol, side, is_reduce_only, order.get("amount"))
        return {"ok": True, "order": order, "plan": plan}

    except ccxt.BaseError as e:
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