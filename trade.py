import os
from typing import Any, Dict, List, Optional, Tuple, Literal

import ccxt.async_support as ccxt


BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_SECRET = os.getenv("BITGET_SECRET", "")
# Bitget needs passphrase/password (sometimes called "password")
BITGET_PASSWORD = os.getenv("BITGET_PASSWORD", "")

# default product type for Bitget futures
DEFAULT_PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "USDT-FUTURES")


async def get_exchange() -> ccxt.bitget:
    """
    Returns an authenticated ccxt.async_support.bitget instance.
    """
    if not (BITGET_API_KEY and BITGET_SECRET and BITGET_PASSWORD):
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    ex = ccxt.bitget(
        {
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_SECRET,
            "password": BITGET_PASSWORD,
            "options": {
                # ensure we use unified futures endpoints
                "defaultType": "swap",  # perp/futures
            },
            "enableRateLimit": True,
        }
    )
    await ex.load_markets()
    return ex


async def fetch_symbol_positions(
    ex: ccxt.bitget,
    symbol: str,
    product_type: str,
) -> List[Dict[str, Any]]:
    """
    Fetch all positions (filtered to symbol when supported).
    Bitget via ccxt requires 'productType' param.
    """
    params = {"productType": product_type}
    # Some ccxt versions ignore the symbols filter for bitget; still pass it if available
    try:
        positions = await ex.fetch_positions([symbol], params)
    except Exception:
        positions = await ex.fetch_positions(None, params)
    return positions or []


def _extract_net_position_for_symbol(
    positions: List[Dict[str, Any]],
    symbol: str,
) -> Tuple[float, Optional[str]]:
    """
    Compute net position size for the given symbol.
    Returns (net_size, side) where side in {"long","short",None} and net_size >= 0.
    """
    net = 0.0
    long_amt = 0.0
    short_amt = 0.0
    for p in positions:
        if p.get("symbol") != symbol:
            continue
        amt = float(p.get("contracts", p.get("amount", 0)) or 0)
        side = p.get("side")
        if side == "long":
            long_amt += amt
        elif side == "short":
            short_amt += amt
        else:
            # some exchanges report positive/negative separately
            amt_signed = float(p.get("contracts", p.get("amount", 0)) or 0)
            if amt_signed >= 0:
                long_amt += amt_signed
            else:
                short_amt += abs(amt_signed)
    net = long_amt - short_amt
    if net > 0:
        return net, "long"
    if net < 0:
        return abs(net), "short"
    return 0.0, None


async def get_net_position(
    ex: ccxt.bitget, symbol: str, product_type: str
) -> Tuple[float, Optional[str]]:
    positions = await fetch_symbol_positions(ex, symbol, product_type)
    return _extract_net_position_for_symbol(positions, symbol)


async def place_order(
    ex: ccxt.bitget,
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["market", "limit"],
    size: float,
    product_type: str,
    reduce_only: bool,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "productType": product_type,
        "reduceOnly": reduce_only,
    }

    amount = size
    price_arg = None if order_type == "market" else float(price) if price else None

    return await ex.create_order(
        symbol=symbol,
        type=order_type,
        side=side,
        amount=amount,
        price=price_arg,
        params=params,
    )


async def smart_route(
    ex: ccxt.bitget,
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["market", "limit"],
    size: float,
    intent: Literal["entry", "scale", "close", "auto"],
    reenter_on_opposite: bool,
    product_type: Optional[str] = None,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Intent rules:
      - entry : open new in the direction of `side` (reduceOnly = False)
      - scale : add to existing in the direction of `side` (reduceOnly = False)
      - close : close existing; `side` should be the *opposite* of the open direction (reduceOnly = True)
      - auto  : infer from current net position and incoming `side`
                * no position -> entry
                * long position & side=='buy'  -> scale
                * long position & side=='sell' -> close (no auto-reverse unless reenter_on_opposite=1)
                * short position & side=='sell'-> scale
                * short position & side=='buy' -> close
                * if reenter_on_opposite==1 and incoming signal is opposite while flat after close,
                  we immediately place an entry in that direction.
    """
    pt = product_type or DEFAULT_PRODUCT_TYPE

    # Always ensure markets are loaded (safe if already loaded)
    await ex.load_markets()

    net_size, net_side = await get_net_position(ex, symbol, pt)

    if intent == "auto":
        if net_size == 0:
            intent_to_use = "entry"
        elif net_side == "long":
            intent_to_use = "scale" if side == "buy" else "close"
        else:  # net_side == "short"
            intent_to_use = "scale" if side == "sell" else "close"
    else:
        intent_to_use = intent

    # Determine reduceOnly flag
    reduce_only = intent_to_use == "close"

    # If intent is close but there is no position, ignore (no-op)
    if intent_to_use == "close" and net_size == 0:
        return {"skipped": True, "reason": "no position to close"}

    # Place the primary order
    primary = await place_order(
        ex=ex,
        symbol=symbol,
        side=side,
        order_type=order_type,
        size=size,
        product_type=pt,
        reduce_only=reduce_only,
        price=price,
    )

    # Optional immediate re-entry on opposite after closing (strategy preference)
    maybe_reenter: Optional[Dict[str, Any]] = None
    if intent_to_use == "close" and reenter_on_opposite:
        opp_side = "buy" if side == "sell" else "sell"
        maybe_reenter = await place_order(
            ex=ex,
            symbol=symbol,
            side=opp_side,
            order_type=order_type,
            size=size,
            product_type=pt,
            reduce_only=False,
            price=price,
        )

    out: Dict[str, Any] = {
        "intent": intent_to_use,
        "reduceOnly": reduce_only,
        "submitted": primary,
    }
    if maybe_reenter:
        out["reentered"] = maybe_reenter
    return out