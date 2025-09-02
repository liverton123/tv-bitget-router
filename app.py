# app.py  — Bitget USDT-M Perp router (Full, fixed)
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
    # floor rounding to avoid exceeding precision
    return math.floor(amount * factor + 1e-12) / factor


# ---------- CCXT ----------
def build_exchange() -> ccxt.bitget:
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # USDT-M linear swaps
        },
    })
    return ex


async def ensure_markets(ex: ccxt.Exchange):
    # 명시적으로 로드 (lazy 신뢰하지 않음)
    try:
        await ex.load_markets(reload=False)
    except Exception:
        await ex.load_markets(reload=True)


async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    """
    총 자산(Eq)을 USDT 기준으로. 없으면 free+used 근사.
    """
    bal = await ex.fetch_balance({'type': 'swap'})
    usdt = bal.get('USDT') or {}
    total = safe_float(usdt.get('total'))
    if total <= 0:
        total = safe_float(usdt.get('free')) + safe_float(usdt.get('used'))
    return max(total, 0.0)


async def fetch_market_and_price(ex: ccxt.Exchange, ccxt_symbol: str) -> Tuple[Dict[str, Any], float]:
    market = ex.market(ccxt_symbol)
    t = await ex.fetch_ticker(ccxt_symbol)
    price = safe_float(t.get('last'))
    if price <= 0:
        price = safe_float(t.get('mark'))
    if price <= 0 and isinstance(t.get('info'), dict):
        price = safe_float(t['info'].get('last')) or safe_float(t['info'].get('markPrice'))
    if price <= 0:
        raise RuntimeError(f"Could not fetch valid price for {ccxt_symbol}")
    return market, price


async def fetch_net_position(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    """
    >0: net long, <0: net short, 0: no position
    """
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
    return {"reduceOnly": True}


def notional_to_amount(notional_usdt: float, price: float) -> float:
    return max(notional_usdt / price, 0.0)


def apply_limits(amount: float, price: float, market: Dict[str, Any]) -> float:
    """
    precision / min amount / min cost 맞추기
    """
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

    # 최소 금액(명목) 체크
    if min_cost and price * amt < min_cost:
        amt = min_cost / price
        if precision is not None:
            amt = round_to_precision(amt, precision)

    return amt


async def place_market_order(
    ex: ccxt.Exchange,
    ccxt_symbol: str,
    side: str,        # 'buy' or 'sell'
    amount: float,
    reduce_only: bool
):
    params = {}
    if reduce_only:
        params.update(build_reduce_only_params())
    # price=None → market
    return await ex.create_order(ccxt_symbol, 'market', side, amount, None, params)


# ---------- Schemas ----------
class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str                 # "buy" | "sell"
    orderType: str = Field(default="market")
    size: float | None = None # 참고값(미사용)


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

    # side 정규화
    side = (data.side or "").lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Unsupported side: {data.side}")

    # 심볼 변환
    try:
        ccxt_symbol = tv_to_ccxt_symbol(data.symbol)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    ex = build_exchange()
    try:
        # 마켓 로드
        await ensure_markets(ex)

        # 시드 기반 명목가(=마진) 계산
        equity = await fetch_equity_usdt(ex)
        if equity <= 0:
            raise HTTPException(status_code=400, detail="No equity")

        notional_usdt = equity * FRACTION_PER_POSITION

        market, price = await fetch_market_and_price(ex, ccxt_symbol)
        net = await fetch_net_position(ex, ccxt_symbol)

        # reduceOnly 여부 판별
        reduce_only = False
        if side == "buy" and net < -1e-12:
            reduce_only = True
        elif side == "sell" and net > 1e-12:
            reduce_only = True

        # 신규/정리 상관없이 안전한 수량 계산
        raw_amount = notional_to_amount(notional_usdt, price)
        amount = apply_limits(raw_amount, price, market)

        if amount <= 0:
            log.info("skip: amount zero | sym=%s price=%.8f notional=%.4f", ccxt_symbol, price, notional_usdt)
            return {"ok": True, "skip": "calc amount is zero", "symbol": data.symbol, "price": price}

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

    except ccxt.BadSymbol as e:
        # 심볼 변환 또는 마켓 로딩 문제
        log.error("BadSymbol: %s", e)
        raise HTTPException(status_code=400, detail=f"BadSymbol: {e}")
    except ccxt.InsufficientFunds as e:
        log.error("InsufficientFunds: %s", e)
        raise HTTPException(status_code=400, detail=f"InsufficientFunds: {e}")
    except ccxt.BadRequest as e:
        log.error("BadRequest: %s", e)
        raise HTTPException(status_code=400, detail=f"BadRequest: {e}")
    except ccxt.BaseError as e:
        log.error("ccxt_error: %s", e)
        raise HTTPException(status_code=502, detail=f"ccxt_error: {e}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("runtime_error")
        raise HTTPException(status_code=500, detail=f"runtime_error: {e}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass