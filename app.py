# app.py — Bitget webhook router (full w/ robust logging)
import os
import json
import logging
import traceback
from typing import Optional, Dict, Any, Set, List

from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel, Field
from cachetools import TTLCache
import ccxt

# -------------------- Logging --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("router")

# -------------------- Env --------------------
BITGET_API_KEY      = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET   = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")  # Passphrase
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET") or os.getenv("WEBHOOK_KEY", "")
MAX_POSITIONS       = int(os.getenv("MAX_POSITIONS", "10"))
DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"

if not WEBHOOK_SECRET:
    log.warning("WEBHOOK_SECRET/WEBHOOK_KEY not set! All requests will be rejected.")

# -------------------- FastAPI --------------------
app = FastAPI(title="tv-bitget-router", version="1.0.0")

# -------------------- Exchange (USDT-M Perp by default) --------------------
exchange = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_API_PASSWORD,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",      # USDT-M perpetual
    },
})

try:
    exchange.load_markets()
    log.info("Bitget markets loaded. Default type: swap")
except Exception as e:
    log.error(f"load_markets failed: {e}")

# -------------------- Dedupe (avoid double fills) --------------------
dedupe = TTLCache(maxsize=4000, ttl=60 * 5)

# -------------------- Models --------------------
class TVOrder(BaseModel):
    # symbol
    symbol_tv: Optional[str] = Field(default=None, description="Alt symbol field from some templates")
    symbol:    Optional[str] = None

    # action/side
    action: Optional[str] = None         # strategy-driven (buy/sellshort/exit_*)
    side:   Optional[str] = None         # manual test (buy/sell)

    # qty
    qty:  Optional[float] = None
    size: Optional[float] = None

    # misc
    price:     Optional[float] = None
    order_id:  Optional[str]  = None
    id:        Optional[str]  = None
    ts:        Optional[str]  = None
    tag:       Optional[str]  = None
    reduceOnly: Optional[bool] = None
    orderType:  Optional[str]  = None

    # secrets (accept several names)
    webhook_key: Optional[str] = None
    key:         Optional[str] = None
    secret:      Optional[str] = None


# -------------------- Helpers --------------------
ENTRY_ACTIONS = {
    "buy": "buy",
    "sellshort": "sell",
    "sell_short": "sell",
}
EXIT_ACTIONS = {
    "sell": "sell",
    "buy_to_cover": "buy",
    "exit_long_stop": "sell",
    "exit_short_stop": "buy",
    "exit_long_channel": "sell",
    "exit_short_channel": "buy",
}

def mask_secret(s: Optional[str]) -> str:
    if not s:
        return ""
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]

def map_symbol(s: str) -> str:
    """
    Map various TV/Bitget symbols to ccxt unified swap symbol.
    Accepts:
      - "BINANCE:BTCUSDT.P", "BTCUSDT.P"
      - "DOGEUSDT_UMCBL", "BTCUSDT_UMCBL"
      - "DOGE/USDT:USDT" (already unified)
      - "DOGEUSDT", "BTCUSD"
    Returns unified: "DOGE/USDT:USDT", "BTC/USDT:USDT"
    """
    if not s:
        return s

    s0 = s.strip()

    # Already unified
    if "/" in s0 and ":" in s0:
        return s0

    # Remove exchange prefix
    if ":" in s0 and not s0.endswith(":USDT") and not s0.endswith(":USD"):
        s0 = s0.split(":")[-1]  # "BINANCE:BTCUSDT.P" -> "BTCUSDT.P"

    # TV future suffix
    if s0.endswith(".P"):
        s0 = s0[:-2]  # BTCUSDT.P -> BTCUSDT

    # Bitget API symbol (UMCBL/CMCBL)
    if s0.endswith("_UMCBL") or s0.endswith("_CMCBL"):
        pair = s0.split("_")[0]  # DOGEUSDT_UMCBL -> DOGEUSDT
        s0 = pair

    # Plain pairs
    if s0.endswith("USDT"):
        base = s0[:-4]
        return f"{base}/USDT:USDT"
    if s0.endswith("USD"):
        base = s0[:-3]
        return f"{base}/USD:USD"

    # Fallback
    return s0

async def fetch_open_symbols() -> Set[str]:
    try:
        positions = exchange.fetch_positions()
    except Exception as e:
        log.error(f"fetch_positions failed: {e}")
        return set()

    opened: Set[str] = set()
    for p in positions or []:
        sym  = p.get("symbol")
        size = p.get("contracts") or p.get("size") or 0
        try:
            sz = abs(float(size or 0))
        except Exception:
            sz = 0.0
        if sym and sz != 0:
            opened.add(sym)
    return opened

async def fetch_position_size(symbol: str) -> float:
    # Try direct first
    try:
        positions = exchange.fetch_positions([symbol])
    except Exception:
        try:
            positions = exchange.fetch_positions()
        except Exception as e:
            log.error(f"fetch_positions fallback failed: {e}")
            return 0.0

    for p in positions or []:
        if p.get("symbol") == symbol:
            size = p.get("contracts") or p.get("size") or 0
            try:
                return abs(float(size or 0))
            except Exception:
                return 0.0
    return 0.0


# -------------------- Endpoints --------------------
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/status")
async def status():
    # 키 값은 마스킹해서 노출
    return {
        "live": True,
        "dry_run": DRY_RUN,
        "max_positions": MAX_POSITIONS,
        "has_api_key": bool(BITGET_API_KEY),
        "has_api_secret": bool(BITGET_API_SECRET),
        "has_api_password": bool(BITGET_API_PASSWORD),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "default_type": "swap",
    }

@app.post("/webhook")
async def webhook(order: TVOrder, x_webhook_key: Optional[str] = Header(default=None)):
    try:
        # ---- Auth ----
        provided = x_webhook_key or order.secret or order.key or order.webhook_key
        log.info(f"Auth check | provided={mask_secret(provided)} | expected_set={bool(WEBHOOK_SECRET)}")
        if WEBHOOK_SECRET and provided != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="invalid webhook key")

        # ---- Dedupe Key ----
        oid = order.order_id or order.id or f"{(order.symbol_tv or order.symbol)}:{order.ts}"
        if not oid:
            raise HTTPException(status_code=400, detail="missing order id")
        if oid in dedupe:
            log.info(f"Duplicate ignored: {oid}")
            return {"status": "duplicate_ignored"}
        dedupe[oid] = True

        # ---- Symbol ----
        raw_symbol = order.symbol_tv or order.symbol
        if not raw_symbol:
            raise HTTPException(status_code=400, detail="missing symbol")
        symbol = map_symbol(raw_symbol)
        log.info(f"Symbol map | in='{raw_symbol}' -> ccxt='{symbol}'")

        # ---- Side/Action ----
        reduce_only = bool(order.reduceOnly)
        side: Optional[str] = None

        if order.action:
            act = (order.action or "").lower()
            if act in ENTRY_ACTIONS:
                side = ENTRY_ACTIONS[act]
                reduce_only = False
            elif act in EXIT_ACTIONS:
                side = EXIT_ACTIONS[act]
                reduce_only = True
            else:
                log.info(f"Unknown action '{order.action}', ignoring.")
                return {"status": "ignored", "reason": f"unknown action {order.action}"}
        else:
            side = (order.side or "").lower()
            if side not in ("buy", "sell"):
                raise HTTPException(status_code=400, detail="missing/invalid side")

        # ---- Position limit (entries only) ----
        if not reduce_only and side in ("buy", "sell"):
            open_syms = await fetch_open_symbols()
            already_open = symbol in open_syms
            log.info(f"Open symbols={list(open_syms)} (count={len(open_syms)}) | already_open={already_open}")
            if not already_open and len(open_syms) >= MAX_POSITIONS:
                return {"status": "blocked", "reason": "max positions reached", "open_symbols": list(open_syms)}

        # ---- Amount ----
        amount = None
        if order.qty is not None:
            amount = float(order.qty)
        elif order.size is not None:
            amount = float(order.size)

        if reduce_only:
            if not amount or amount <= 0:
                amount = await fetch_position_size(symbol)
                log.info(f"ReduceOnly true -> fetched pos size={amount}")
        else:
            if not amount or amount <= 0:
                raise HTTPException(status_code=400, detail="qty/size required for entries")

        # ---- DRY RUN ----
        if DRY_RUN:
            log.info(f"[DRY] {symbol} {side} amount={amount} reduceOnly={reduce_only}")
            return {"status": "ok(dry)", "symbol": symbol, "side": side, "amount": amount, "reduceOnly": reduce_only}

        # ---- Place order ----
        params: Dict[str, Any] = {"reduceOnly": reduce_only}
        log.info(f"Placing order | symbol={symbol} side={side} amount={amount} params={params}")
        result = exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
        log.info(f"Order result: {json.dumps(result, default=str)[:1000]}")
        return {"status": "ok", "exchange_result": result}

    except HTTPException as he:
        log.error(f"HTTPError {he.status_code}: {he.detail}")
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Unhandled error: {e}\n{tb}")
        # 표준 500 응답 + 원인 텍스트(로그엔 풀, 응답엔 요약)
        raise HTTPException(status_code=500, detail=f"order failed: {str(e)}")
