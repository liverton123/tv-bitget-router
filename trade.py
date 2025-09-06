# trade.py
import os
import math
import ccxt.async_support as ccxt
from typing import Optional, Dict, Any, List


BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSWORD = os.getenv("BITGET_PASSWORD", "")
DEFAULT_PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # USDT-M perp
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"


# ---------- Exchange bootstrap ----------

async def get_exchange():
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # perps
        },
    })
    await ex.load_markets()
    return ex


# ---------- Utilities ----------

def normalize_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if s.endswith(":USDT"):
        s = s.replace(":USDT", "")
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    if "/" in s and ":USDT" in s:
        return s
    # fallback, let ccxt validate later
    return s


def _position_to_net_contracts(positions: List[Dict[str, Any]]) -> float:
    net = 0.0
    for p in positions:
        side = (p.get("side") or "").lower()
        contracts = float(p.get("contracts") or p.get("contractSize") or 0)  # ccxt normalizes to "contracts"
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net


async def fetch_symbol_positions(ex, symbol: str, product_type: str) -> List[Dict[str, Any]]:
    params = {"productType": product_type}
    try:
        pos = await ex.fetch_positions([symbol], params)
    except Exception:
        # some ccxt versions expect without list
        pos = await ex.fetch_positions(None, params)
        pos = [p for p in pos if p.get("symbol") == symbol]
    return pos


async def get_net_position(ex, symbol: str, product_type: str) -> float:
    pos = await fetch_symbol_positions(ex, symbol, product_type)
    return _position_to_net_contracts(pos)


async def place_order(
    ex,
    symbol: str,
    side: str,
    size: float,
    order_type: str,
    product_type: str,
    reduce_only: bool,
) -> Dict[str, Any]:
    # Bitget linear perp uses amount in contracts (base quantity)
    typ = order_type.lower()
    s = side.lower()
    params = {
        "reduceOnly": reduce_only,
        "productType": product_type,
    }
    if typ not in ("market", "limit"):
        typ = "market"
    if size <= 0:
        return {"status": "skipped", "reason": "non_positive_size"}
    order = await ex.create_order(symbol, typ, s, size, None, params)
    return {"status": "ok", "order": order}


# ---------- Router ----------

async def smart_route(
    ex,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    intent: str,
    reenter_on_opposite: bool,
    product_type: Optional[str] = None,
) -> Dict[str, Any]:
    # defensive ex creation
    if ex is None or isinstance(ex, str) or not hasattr(ex, "load_markets"):
        ex = await get_exchange()
    # ensure markets loaded
    if not getattr(ex, "markets", None):
        await ex.load_markets()

    product_type = product_type or DEFAULT_PRODUCT_TYPE
    sym = normalize_symbol(symbol)
    s = side.lower()
    it = (intent or "").lower()

    if s not in ("buy", "sell"):
        return {"status": "error", "reason": "invalid_side", "side": side}
    if not ALLOW_SHORTS and s == "sell" and it in ("open", "scale"):
        return {"status": "skipped", "reason": "shorts_disallowed"}

    # current net contracts
    net = await get_net_position(ex, sym, product_type)
    result: Dict[str, Any] = {"symbol": sym, "net_contracts": net, "intent": it}

    # ---- intent: close -> reduceOnly market in existing side only; if flat -> noop
    if it == "close":
        if abs(net) < 1e-9:
            result.update({"status": "noop", "reason": "no_position"})
            return result
        close_side = "sell" if net > 0 else "buy"
        close_size = abs(net) if size <= 0 else min(abs(net), size)
        od = await place_order(
            ex, sym, close_side, close_size, order_type, product_type, reduce_only=True
        )
        result.update({"action": "close", **od})
        return result

    # ---- intent: open / scale
    # if there is opposite position and reenter_on_opposite is False -> close-only first
    if net != 0 and ((net > 0 and s == "sell") or (net < 0 and s == "buy")):
        if not reenter_on_opposite:
            # close-only to flat
            close_side = "sell" if net > 0 else "buy"
            od = await place_order(
                ex, sym, close_side, abs(net), "market", product_type, reduce_only=True
            )
            result.update({"action": "flatten_first", **od})
            return result

    # scale or open in same direction; reduceOnly=False
    od = await place_order(
        ex, sym, s, size, order_type, product_type, reduce_only=False
    )
    result.update({"action": "open_or_scale", **od})
    return result