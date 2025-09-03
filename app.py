import os, json, math, logging
from typing import Any, Dict, List, Optional, Tuple

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY      = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET   = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

# 시드의 1/20 진입 (레버리지는 거래소 UI에서 지정)
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))

ALLOW_SHORTS  = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
PRODUCT_TYPE  = os.getenv("BITGET_PRODUCT_TYPE", "UMCBL").upper()  # USDT-M Perp
LOG_LEVEL     = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tv-bitget-router")

app = FastAPI()

# --------------------- helpers ---------------------
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):  # TradingView 퍼페츄얼 접미사 제거
        s = s[:-2]
    if not s.endswith("USDT"):
        raise ValueError(f"USDT 기준 심볼만 지원: {tv_symbol}")
    base = s[:-4]
    return f"{base}/USDT:USDT"

def f2(x, default=0.0) -> float:
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
        "options": {"defaultType": "swap"},   # USDT-M
    })

async def ensure_markets(ex: ccxt.Exchange):
    try:
        await ex.load_markets(reload=False)
    except Exception:
        await ex.load_markets(reload=True)

async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    # balance()에는 productType 절대 넣지 말 것 (40019/40020 방지)
    bal = await ex.fetch_balance({"type": "swap"})
    usdt = bal.get("USDT") or {}
    total = f2(usdt.get("total"))
    if total <= 0:
        total = f2(usdt.get("free")) + f2(usdt.get("used"))
    return max(total, 0.0)

async def fetch_market_and_price(ex: ccxt.Exchange, ccxt_symbol: str) -> Tuple[Dict[str, Any], float]:
    market = ex.market(ccxt_symbol)
    t = await ex.fetch_ticker(ccxt_symbol)
    price = f2(t.get("last")) or f2(t.get("mark")) or f2((t.get("info") or {}).get("markPrice"))
    if price <= 0:
        raise RuntimeError(f"가격 조회 실패: {ccxt_symbol}")
    return market, price

# ---- ★ 핵심 패치: 전체 포지션으로 조회(None) 후 필터 ★
async def fetch_positions_all(ex: ccxt.Exchange) -> List[Dict[str, Any]]:
    return await ex.fetch_positions(None, params={"productType": PRODUCT_TYPE})

async def fetch_net_position(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    pos_list = await fetch_positions_all(ex)
    net = 0.0
    for p in pos_list:
        if p.get("symbol") != ccxt_symbol:
            continue
        contracts = f2(p.get("contracts"))
        side = (p.get("side") or "").lower()
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net

def apply_limits(amount: float, price: float, market: Dict[str, Any]) -> float:
    precision = (market.get("precision") or {}).get("amount")
    limits = market.get("limits") or {}
    min_amt = f2((limits.get("amount") or {}).get("min"))
    min_cost = f2((limits.get("cost") or {}).get("min"))

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

# -------------------- models -----------------------
class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str               # "buy" | "sell"
    orderType: str = "market"
    size: float | None = None

# -------------------- routes -----------------------
@app.get("/")
async def root():
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "productType": PRODUCT_TYPE}

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

        # 현재 보유 수량(순수량)
        net = await fetch_net_position(ex, ccxt_symbol)

        # 반대방향이면 reduceOnly, 동일방향이면 진입/물타기
        is_reduce_only = (side == "buy" and net < -1e-12) or (side == "sell" and net > 1e-12)

        # 시드 * 1/20 / 가격  => 수량
        equity = await fetch_equity_usdt(ex)
        market, price = await fetch_market_and_price(ex, ccxt_symbol)
        notional = equity * FRACTION_PER_POSITION
        raw_amount = max(notional / price, 0.0)
        amount = apply_limits(raw_amount, price, market)

        if amount <= 0:
            log.info("skip: amount=0 | sym=%s price=%.10f notional=%.4f", ccxt_symbol, price, notional)
            return {"ok": True, "skip": "amount_zero", "symbol": msg.symbol}

        plan = {
            "symbol_tv": msg.symbol,
            "symbol": ccxt_symbol,
            "side": side,
            "reduceOnly": is_reduce_only,
            "equity": equity,
            "price": price,
            "notional": notional,
            "amount": amount,
        }
        log.info("plan: %s", json.dumps(plan, ensure_ascii=False))

        order = await place_market_order(ex, ccxt_symbol, side, amount, is_reduce_only)
        log.info("filled: id=%s sym=%s side=%s ro=%s amt=%s",
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