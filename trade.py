from decimal import Decimal
from typing import List, Dict, Any, Optional
import asyncio
from risk import (
    round_size_to_step,
    target_qty_for_margin,
    can_open_new_coin,
    FRACTION_PER_POSITION,
    LEVERAGE,
    PRODUCT_TYPE,
    MARGIN_COIN,
)
from bitget_ccxt import (
    get_mark_price,
    reduce_only_order,
    market_order,
    fetch_positions_all,
    fetch_balance_usdt_equity,
)

Dec = Decimal
D0 = Dec("0")

# ---------- Position helpers ----------

async def fetch_symbol_positions(ex, product_type: str, margin_coin: str) -> Dict[str, Dec]:
    """
    Returns {symbol: net_base_amount} for all symbols with non-zero position.
    """
    positions = await fetch_positions_all(ex, product_type, margin_coin)
    out: Dict[str, Dec] = {}
    for p in positions:
        sym = p.get("symbol")
        sz  = Dec(str(p.get("holdSideTotal", "0"))) if p.get("holdSide") else D0  # fallback
        # CCXT unifies: amount (signed). Prefer unified if present.
        amt = p.get("contracts") or p.get("amount") or 0
        side = p.get("side")
        signed = D0
        try:
            signed = Dec(str(amt))
        except Exception:
            signed = D0
        # If CCXT gives unsigned, reconstruct from side
        if signed == 0 and float(amt) != 0:
            signed = Dec(str(amt)) * (Dec("1") if (side or "long").lower() == "long" else Dec("-1"))
        if sym and signed != 0:
            out[sym] = signed
    return out

async def get_net_position(ex, symbol: str, product_type: str, margin_coin: str) -> Dec:
    positions = await fetch_positions_all(ex, product_type, margin_coin, symbol=symbol)
    net = D0
    for p in positions:
        amt = p.get("contracts") or p.get("amount") or 0
        side = (p.get("side") or "").lower()
        try:
            q = Dec(str(amt))
        except Exception:
            q = D0
        if q != 0:
            if side == "long":
                net += q
            elif side == "short":
                net -= q
    return net

# ---------- Order wrappers ----------

async def place_open(ex, symbol: str, side: str, qty: Dec, product_type: str, margin_coin: str) -> Dict[str, Any]:
    # Bitget linear USDT futures: "buy" opens long, "sell" opens short
    return await market_order(
        ex=ex,
        symbol=symbol,
        side=side,
        qty=qty,
        product_type=product_type,
        margin_coin=margin_coin,
        reduce_only=False,
    )

async def place_reduce_only(ex, symbol: str, side: str, qty: Dec, product_type: str, margin_coin: str) -> Dict[str, Any]:
    return await reduce_only_order(
        ex=ex,
        symbol=symbol,
        side=side,
        qty=qty,
        product_type=product_type,
        margin_coin=margin_coin,
    )

# ---------- Routing rules ----------

STRICT_EXIT_ONLY = True  # opposite signals close-only (no flip)

async def smart_route(
    ex,
    symbol: str,
    side: str,
    order_type: str,
    size: Dec,                    # incoming numeric "size" from webhook (base units or a plain number)
    product_type: str,
    margin_coin: str,
    intent: str = "",
) -> List[Dict[str, Any]]:
    """
    - intent="close": reduce-only up to existing amount; if flat, do nothing
    - intent="dca":   add only in same direction; otherwise ignore
    - intent="open" or "": apply position/margin rules and (if flat) allow open
    - never flip: opposite signal closes up to size, no reverse entry in the same call
    """
    # Current net (signed base)
    net = await get_net_position(ex, symbol, product_type, margin_coin)
    cur_dir = 0 if net == 0 else (1 if net > 0 else -1)
    new_dir = 1 if side == "buy" else -1
    out: List[Dict[str, Any]] = []

    # Closing intent
    if intent == "close":
        if net == 0:
            return out
        close_side = "buy" if net < 0 else "sell"
        close_amount = abs(net) if size >= abs(net) else size
        if close_amount > 0:
            out.append(await place_reduce_only(ex, symbol, close_side, close_amount, product_type, margin_coin))
        return out

    # Price and lot-step for quantity calc
    mark = await get_mark_price(ex, symbol)
    # Equity and per-entry target margin (fixed fraction of seed/equity)
    equity = await fetch_balance_usdt_equity(ex)
    target_margin = Dec(FRACTION_PER_POSITION) * equity  # USDT margin to consume
    # Convert target margin to base quantity using leverage and current price
    raw_qty = target_qty_for_margin(target_margin, Dec(LEVERAGE), Dec(mark))
    qty = await round_size_to_step(ex, symbol, raw_qty)

    # DCA intent: only in same direction; do not exceed coin limit
    if intent == "dca":
        if cur_dir == 0 or cur_dir == new_dir:
            # allow even if coin limit reached (it's the same symbol)
            if qty > 0:
                out.append(await place_open(ex, symbol, side, qty, product_type, margin_coin))
        return out

    # Open or unspecified intent
    if cur_dir == 0:
        # Respect coin limit (count distinct non-zero symbols)
        if not await can_open_new_coin(ex, symbol, PRODUCT_TYPE, MARGIN_COIN):
            return out  # refuse silently when over the limit
        if qty > 0:
            out.append(await place_open(ex, symbol, side, qty, product_type, margin_coin))
        return out

    # Already have a position
    if cur_dir == new_dir:
        # Same direction → this is an add (allowed)
        if qty > 0:
            out.append(await place_open(ex, symbol, side, qty, product_type, margin_coin))
        return out

    # Opposite direction → close-only, no flip in the same call
    if STRICT_EXIT_ONLY:
        close_side = "buy" if net < 0 else "sell"
        close_amount = abs(net) if size >= abs(net) else size
        if close_amount > 0:
            out.append(await place_reduce_only(ex, symbol, close_side, close_amount, product_type, margin_coin))
        return out

    # (If flip were allowed, you'd first close then open; we keep it disabled.)
    return out