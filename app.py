import os, json, math, logging
from typing import Any, Dict, List, Optional, Tuple

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

# ====== ENV ======
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY      = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET   = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

# 포지션 사이징
USE_FIXED_MARGIN        = os.getenv("USE_FIXED_MARGIN", "true").lower() == "true"
MARGIN_PER_TRADE_USDT   = float(os.getenv("MARGIN_PER_TRADE_USDT", "6"))   # 마진 6달러
UI_LEVERAGE             = float(os.getenv("UI_LEVERAGE", "10"))            # UI에서 설정한 레버리지값
FRACTION_PER_POSITION   = float(os.getenv("FRACTION_PER_POSITION", "0.05"))# 백업: 시드 1/20

# 거래 설정
ALLOW_SHORTS  = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
PRODUCT_TYPE  = os.getenv("BITGET_PRODUCT_TYPE", "UMCBL").upper()  # USDT-M Perp
MARGIN_COIN   = os.getenv("BITGET_MARGIN_COIN", "USDT").upper()
LOG_LEVEL     = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tv-bitget-router")

app = FastAPI()

# ====== helpers ======
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
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
        "options": {"defaultType": "swap"},
    })

async def ensure_markets(ex: ccxt.Exchange):
    try:
        await ex.load_markets(reload=False)
    except Exception:
        await ex.load_markets(reload=True)

async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
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

def is_prodtype_error(e: Exception) -> bool:
    msg = getattr(e, "message", "") or str(e)
    return "40019" in msg or "40020" in msg or "productType" in msg

# ---- 포지션 조회(다계단 + RAW 백업) ----
async def fetch_positions_all(ex: ccxt.Exchange) -> List[Dict[str, Any]]:
    try:
        return await ex.fetch_positions(None, params={"productType": PRODUCT_TYPE})
    except Exception as e1:
        if not is_prodtype_error(e1):
            raise
        log.warning("positions step1 failed (%s) -> step2", e1)

    try:
        return await ex.fetch_positions(None, params={"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN})
    except Exception as e2:
        if not is_prodtype_error(e2):
            raise
        log.warning("positions step2 failed (%s) -> step3", e2)

    try:
        return await ex.fetch_positions(None, params={"marginCoin": MARGIN_COIN})
    except Exception as e3:
        if not is_prodtype_error(e3):
            raise
        log.warning("positions step3 failed (%s) -> RAW", e3)

    try:
        res = await ex.privateMixGetV2MixPositionAllPosition({
            "productType": PRODUCT_TYPE,
            "marginCoin": MARGIN_COIN,
        })
        data = (res or {}).get("data") or []
        out = []
        for it in data:
            try:
                sym = ex.safe_symbol(f"{it.get('symbol')}/USDT:USDT", market=None)
            except Exception:
                sym = f"{(it.get('symbol') or '').upper()}/USDT:USDT"
            side = (it.get("holdSide") or "").lower()  # long/short
            contracts = f2(it.get("total"))
            out.append({"symbol": sym, "side": side, "contracts": contracts, "info": it})
        return out
    except Exception as e4:
        log.error("positions RAW failed (%s) -> assume empty", e4)
        return []

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

# ====== models ======
class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str               # "buy" | "sell"
    orderType: str = "market"
    size: float | None = None
    dca: bool | None = None  # 물타기 신호 여부(선택)

# ====== routes ======
@app.get("/")
async def root():
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
            "useFixedMargin": USE_FIXED_MARGIN, "marginUSDT": MARGIN_PER_TRADE_USDT, "uiLeverage": UI_LEVERAGE}

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

        # 현재 순포지션
        net = await fetch_net_position(ex, ccxt_symbol)

        # DCA 신호면: 순포지션 없으면 스킵
        if (msg.dca is True) and abs(net) < 1e-12:
            log.info("DCA skip: no existing position | %s", ccxt_symbol)
            return {"ok": True, "skip": "dca_no_position", "symbol": msg.symbol}

        # reduceOnly 판정(반대 방향이면 감산)
        is_reduce_only = (side == "buy" and net < -1e-12) or (side == "sell" and net > 1e-12)

        # 사이징
        market, price = await fetch_market_and_price(ex, ccxt_symbol)

        if USE_FIXED_MARGIN:
            # 목표 마진(USDT) → 명목치 = 마진 * UI레버리지 → 수량
            target_notional = MARGIN_PER_TRADE_USDT * UI_LEVERAGE
            raw_amount = max(target_notional / price, 0.0)
        else:
            equity = await fetch_equity_usdt(ex)
            target_notional = equity * FRACTION_PER_POSITION
            raw_amount = max(target_notional / price, 0.0)

        amount = apply_limits(raw_amount, price, market)
        if amount <= 0:
            return {"ok": True, "skip": "amount_zero", "symbol": msg.symbol}

        plan = {
            "symbol_tv": msg.symbol,
            "symbol": ccxt_symbol,
            "side": side,
            "reduceOnly": is_reduce_only,
            "price": price,
            "target_notional": target_notional,
            "amount": amount,
            "dca": bool(msg.dca),
            "useFixedMargin": USE_FIXED_MARGIN,
            "uiLeverage": UI_LEVERAGE,
            "marginUSDT": MARGIN_PER_TRADE_USDT,
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