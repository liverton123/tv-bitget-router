import os
import re
from typing import Optional, Literal, Dict, Any, List
import ccxt.async_support as ccxt

# ---------- normalization helpers ----------
def normalize_product_type(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    t = v.strip().lower()
    # bitget productType aliases
    alias = {
        "umcbl": "umcbl",          # USDT-M linear perpetual
        "usdt": "umcbl",
        "usdt-perp": "umcbl",
        "usdt-futures": "umcbl",
        "linear": "umcbl",
        "perp": "umcbl",
        "swap": "umcbl",
        "dmcbl": "dmcbl",          # coin-M
        "coin": "dmcbl",
        "coin-perp": "dmcbl",
    }
    return alias.get(t, t)

_SYMBOL_P_PATTERN = re.compile(r"^([A-Z0-9]+)USDT\.P$")
def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    # Convert BINANCE/TV style perpetual ticker HBARUSDT.P -> CCXT unified HBAR/USDT:USDT
    m = _SYMBOL_P_PATTERN.match(s)
    if m:
        base = m.group(1)
        return f"{base}/USDT:USDT"
    # Already unified? return as-is
    return s

# ---------- exchange factory ----------
async def get_exchange():
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
                "defaultType": "swap",     # perpetual
                "defaultSubType": "linear" # USDT-margined by default
            },
        }
    )
    await ex.load_markets()
    return ex

# ---------- positions ----------
async def fetch_symbol_positions(ex, symbol: str, product_type: str) -> List[Dict[str, Any]]:
    params = {"productType": product_type}
    try:
        pos = await ex.fetch_positions([symbol], params)
    except Exception:
        pos = await ex.fetch_positions(None, params)
    # CCXT returns unified 'symbol' like HBAR/USDT:USDT
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

# ---------- orders ----------
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
    if not product_type:
        raise ValueError("product_type is required")
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
        "symbol": symbol,
    }

# ---------- router ----------
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
    product_type = normalize_product_type(product_type)
    if not product_type:
        raise ValueError("invalid product_type")

    net = await get_net_position(ex, symbol, product_type)
    ops: List[Dict[str, Any]] = []

    if intent == "close":
        ops.append(await place(ex, symbol, side, order_type, size, product_type, True, price))
        return {"ops": ops, "net_before": net}

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