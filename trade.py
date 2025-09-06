import os
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

import ccxt.async_support as ccxt

log = logging.getLogger("router.trade")

# ---- sizing / risk env ----
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1/20
FORCE_FIXED_SIZING = os.getenv("FORCE_FIXED_SIZING", "true").lower() == "true"
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"  # keep effective margin per pos fixed
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT")
MARGIN_MODE = os.getenv("MARGIN_MODE", "cross").lower()  # informative only here
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"

# optional explicit leverage (UI shows 10x; we only need it for sizing math)
LEVERAGE = float(os.getenv("LEVERAGE", "10"))

# ---------- helpers ----------

def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step

def normalize_symbol(raw: str) -> str:
    """
    Accepts:
      'LINKUSDT', 'LINKUSDT:USDT', 'LINK/USDT:USDT', 'LINKUSDT.P', 'LINKUSDT.PERP'
    Returns ccxt unified: 'LINK/USDT:USDT'
    """
    s = raw.strip().upper()
    s = s.replace("_", "").replace("-", "")
    if s.endswith(".P") or s.endswith(".PERP"):
        s = s.split(".")[0]
    s = s.replace(":USDT", "")
    s = s.replace("/USDT", "").replace("USDT", "/USDT")
    # if already unified-like, keep
    if "/USDT:USDT" in s:
        return s
    base = s.split("/USDT")[0]
    return f"{base}/USDT:USDT"

async def _active_symbols(ex: ccxt.Exchange) -> List[str]:
    try:
        pos = await ex.fetch_positions()
    except Exception as e:
        log.error("[CCXT_ERROR] fetch_positions failed: %s", e)
        return []
    active = []
    for p in pos or []:
        amt = abs(float(p.get("contracts") or p.get("contractSize") or 0))
        if amt > 0:
            active.append(p["symbol"])
    return list(set(active))

async def _position_size_side(ex: ccxt.Exchange, unified_symbol: str) -> Tuple[float, Optional[str]]:
    """
    Returns (abs_contracts, side) side in {"long","short",None}
    """
    try:
        pos = await ex.fetch_positions([unified_symbol])
    except Exception as e:
        log.error("[CCXT_ERROR] fetch_positions failed: %s", e)
        return 0.0, None

    for p in pos or []:
        if p.get("symbol") != unified_symbol:
            continue
        contracts = float(p.get("contracts") or 0)
        if contracts > 0:
            return abs(contracts), "long"
        if contracts < 0:
            return abs(contracts), "short"
    return 0.0, None

async def _mark_price(ex: ccxt.Exchange, unified_symbol: str) -> float:
    ticker = await ex.fetch_ticker(unified_symbol)
    return float(ticker["last"] or ticker["close"] or ticker["ask"] or ticker["bid"])

async def _equity_usdt(ex: ccxt.Exchange) -> float:
    bal = await ex.fetch_balance()
    usdt = bal.get(MARGIN_COIN, {}) or bal.get("USDT", {})
    total = usdt.get("total")
    if total is None:
        # fallback
        total = (usdt.get("free") or 0) + (usdt.get("used") or 0)
    return float(total or 0)

async def _compute_order_qty(ex: ccxt.Exchange, unified_symbol: str) -> float:
    """
    Fixed margin per position:
      margin_per_pos = equity * FRACTION_PER_POSITION
      qty = (margin_per_pos * LEVERAGE) / price
    """
    markets = await ex.load_markets()
    m = markets[unified_symbol]
    price = await _mark_price(ex, unified_symbol)
    equity = await _equity_usdt(ex)

    margin_per_pos = equity * FRACTION_PER_POSITION
    notional = margin_per_pos * LEVERAGE  # what we buy/sell
    qty = notional / price

    amount_step = m.get("limits", {}).get("amount", {}).get("step") or m.get("precision", {}).get("amount")
    if amount_step is None:
        amount_step = 0
    qty = _round_down(qty, float(amount_step or 0))
    return max(qty, float(m.get("limits", {}).get("amount", {}).get("min") or 0))

async def _place_market(
    ex: ccxt.Exchange,
    unified_symbol: str,
    side: str,
    amount: float,
    reduce_only: bool,
) -> Dict[str, Any]:
    params = {"reduceOnly": reduce_only}
    return await ex.create_order(unified_symbol, "market", side, amount, None, params)

# ---------- router ----------

async def smart_route(
    ex: ccxt.Exchange,
    unified_symbol: str,
    side: str,
    order_type: str,
    incoming_size: float,
    intent: str,
    product_type: str,
) -> Dict[str, Any]:
    side = side.lower()  # "buy" or "sell"
    intent = intent.lower()  # "open" | "add" | "close"

    # load markets early to fail-fast for bad symbols
    await ex.load_markets()
    if unified_symbol not in ex.markets:
        # try one more normalization pass using market ids
        raise ValueError(f"Unknown symbol after normalize: {unified_symbol}")

    # close intent: always close entire position, do not re-enter
    pos_amt, pos_side = await _position_size_side(ex, unified_symbol)
    if intent == "close":
        if pos_amt <= 0:
            return {"action": "close", "status": "skipped", "reason": "no position"}
        # opposite side to reduce
        close_side = "sell" if pos_side == "long" else "buy"
        # tolerance to avoid 'close more than held'
        close_amt = pos_amt * (1.0 - min(max(CLOSE_TOLERANCE_PCT, 0.0), 0.2))
        if close_amt <= 0:
            close_amt = pos_amt
        order = await _place_market(ex, unified_symbol, close_side, close_amt, reduce_only=True)
        return {"action": "close", "side": close_side, "amount": close_amt, "order": order}

    # deny short opens if configured
    if not ALLOW_SHORTS and side == "sell" and pos_amt == 0:
        return {"action": "open", "status": "blocked", "reason": "shorts disabled"}

    # limit concurrent coins
    active = await _active_symbols(ex)
    if unified_symbol not in active and len(active) >= MAX_COINS:
        return {"action": "open", "status": "blocked", "reason": "max coins reached", "active": active}

    # compute amount
    amount = await _compute_order_qty(ex, unified_symbol) if FORCE_FIXED_SIZING else max(float(incoming_size or 0), 0.0)
    if amount <= 0:
        return {"action": "open", "status": "blocked", "reason": "zero size"}

    # if we already have a position on opposite side and intent is "open"/"add", do not flip;
    # TradingView should send explicit "close" first.
    if pos_amt > 0 and pos_side is not None:
        if (pos_side == "long" and side == "sell") or (pos_side == "short" and side == "buy"):
            if not REENTER_ON_OPPOSITE:
                return {"action": "open", "status": "blocked", "reason": "opposite while position open"}
        # same-side -> DCA add
    order = await _place_market(ex, unified_symbol, side, amount, reduce_only=False)
    return {"action": "open_or_add", "side": side, "amount": amount, "order": order}