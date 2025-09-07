import os
from typing import Optional, Literal, Dict, Any, List
import ccxt.async_support as ccxt

# --- exchange bootstrap -------------------------------------------------------
async def get_exchange():
    # Accept multiple env var names to avoid deployment mismatches
    key = (
        os.getenv("BITGET_API_KEY")
        or os.getenv("BITGET_KEY")
        or os.getenv("API_KEY")
    )
    secret = (
        os.getenv("BITGET_API_SECRET")
        or os.getenv("BITGET_SECRET")
        or os.getenv("API_SECRET")
    )
    # Bitget uses "password" for passphrase in CCXT
    password = (
        os.getenv("BITGET_PASSPHRASE")
        or os.getenv("BITGET_API_PASSWORD")
        or os.getenv("BITGET_PASSWORD")
        or os.getenv("API_PASSWORD")
        or os.getenv("PASSPHRASE")
        or os.getenv("PASSWORD")
    )
    if not key or not secret or not password:
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    ex = ccxt.bitget(
        {
            "apiKey": key,
            "secret": secret,
            "password": password,
            "enableRateLimit": True,
            "options": {
                # USDT-M perpetual
                "defaultType": "swap",
                "defaultSubType": "linear",
            },
        }
    )
    await ex.load_markets()
    return ex

# --- position helpers ---------------------------------------------------------
async def fetch_symbol_positions(ex, symbol: str, product_type: str) -> List[Dict[str, Any]]:
    params = {"productType": product_type}
    try:
        pos = await ex.fetch_positions([symbol], params)
        return [p for p in pos if p.get("symbol") == symbol]
    except Exception:
        pos = await ex.fetch_positions(None, params)
        return [p for p in pos if p.get("symbol") == symbol]

async def get_net_position(ex, symbol: str, product_type: str) -> float:
    positions = await fetch_symbol_positions(ex, symbol, product_type)
    net = 0.0
    for p in positions:
        side = (p.get("side") or p.get("positionSide") or "").lower()
        contracts = p.get("contracts") or p.get("positionAmt") or p.get("size") or 0
        try:
            qty = float(contracts)
        except Exception:
            qty = 0.0
        if side.startswith("long"):
            net += qty
        elif side.startswith("short"):
            net -= qty
    return net

# --- order helper -------------------------------------------------------------
async def place(
    ex,
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["market", "limit"],
    size: float,
    product_type: str,
    reduce_only: bool = False,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    params = {"productType": product_type, "reduceOnly": reduce_only}
    amount = float(size)
    if order_type == "limit":
        if price is None:
            raise ValueError("price required for limit orders")
        order = await ex.create_order(symbol, "limit", side, amount, price, params)
    else:
        order = await ex.create_order(symbol, "market", side, amount, None, params)
    return {
        "id": order.get("id"),
        "reduceOnly": reduce_only,
        "amount": amount,
        "type": order_type,
        "side": side,
    }

# --- router -------------------------------------------------------------------
async def smart_route(
    ex,
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["market", "limit"],
    size: float,
    intent: Optional[Literal["entry", "scale", "close", "auto"]],
    reenter_on_opposite: bool,
    product_type: str,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    if intent is None:
        intent = "auto"

    net = await get_net_position(ex, symbol, product_type)
    ops: List[Dict[str, Any]] = []

    # explicit close
    if intent == "close":
        ops.append(await place(ex, symbol, side, order_type, size, product_type, True, price))
        return {"ops": ops, "net_before": net}

    # explicit entry/scale
    if intent in ("entry", "scale"):
        ops.append(await place(ex, symbol, side, order_type, size, product_type, False, price))
        return {"ops": ops, "net_before": net}

    # auto
    if net == 0:
        ops.append(await place(ex, symbol, side, order_type, size, product_type, False, price))
        return {"ops": ops, "net_before": net}

    side_is_buy = side == "buy"

    # same-direction add
    if (side_is_buy and net > 0) or ((not side_is_buy) and net < 0):
        ops.append(await place(ex, symbol, side, order_type, size, product_type, False, price))
        return {"ops": ops, "net_before": net}

    # opposite-direction: close first, then optionally re-enter
    qty_to_close = min(abs(net), float(size))
    if qty_to_close > 0:
        ops.append(await place(ex, symbol, side, order_type, qty_to_close, product_type, True, price))

    remaining = float(size) - qty_to_close
    if remaining > 0 and reenter_on_opposite:
        ops.append(await place(ex, symbol, side, order_type, remaining, product_type, False, price))

    return {"ops": ops, "net_before": net}