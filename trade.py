import os
import re
from typing import Dict, Any, Optional, Tuple
import ccxt.async_support as ccxt

API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS = os.getenv("BITGET_API_PASSWORD", "")
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))

def normalize_symbol(sym: str) -> str:
    s = sym.upper().strip()
    if re.match(r"^[A-Z0-9]+USDT(\.P)?$", s):
        base = s.replace(".P", "").replace("USDT", "")
        return f"{base}/USDT:USDT"
    return s

async def get_exchange(product_type: str):
    ex = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASS,
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear",
            "productType": product_type,
        },
        "enableRateLimit": True,
    })
    await ex.load_markets()
    return ex

async def fetch_symbol_positions(ex, symbol: Optional[str], product_type: str):
    params = {"productType": product_type}
    positions = await ex.fetch_positions([symbol] if symbol else None, params)
    return positions

def position_side_and_size(positions) -> Tuple[str, float]:
    if not positions:
        return "flat", 0.0
    p = positions[0]
    contracts = float(p.get("contracts", 0) or 0)
    if contracts <= 0:
        return "flat", 0.0
    return p.get("side", "flat"), contracts

async def market_order(ex, symbol: str, side: str, size: float, reduce_only: bool):
    params: Dict[str, Any] = {"reduceOnly": reduce_only}
    return await ex.create_order(symbol, "market", side, size, None, params)

async def smart_route(
    symbol: str,
    side: str,                 # "buy" | "sell"
    order_type: str,           # must be "market"
    size: float,
    intent: str,               # "open" | "add" | "close" | "flip" | "auto"
    product_type: str,
    flags: Dict[str, bool],
):
    assert order_type == "market", "only market supported"

    sym = normalize_symbol(symbol)
    ex = await get_exchange(product_type)
    try:
        positions = await fetch_symbol_positions(ex, sym, product_type)
        pos_side, pos_size = position_side_and_size(positions)
        side_dir = "long" if side == "buy" else "short"

        if side_dir == "short" and not flags.get("ALLOW_SHORTS", True):
            return {"skipped": "shorts_disabled"}

        if intent == "auto":
            if pos_side == "flat":
                action = "open"
            elif pos_side == side_dir:
                action = "add"
            else:
                action = "flip" if flags.get("REENTER_ON_OPPOSITE", False) else "close"
        else:
            action = intent

        if action == "open" and flags.get("REQUIRE_INTENT_FOR_OPEN", True) and intent != "open":
            return {"skipped": "open_requires_intent"}
        if action == "add" and flags.get("REQUIRE_INTENT_FOR_ADD", True) and intent != "add":
            return {"skipped": "add_requires_intent"}
        if action == "close" and pos_side == "flat" and flags.get("IGNORE_CLOSE_WHEN_FLAT", True):
            return {"skipped": "flat_close_ignored"}

        if action == "open":
            if pos_side != "flat":
                return {"skipped": "already_in_position"}
            order = await market_order(ex, sym, side, size, reduce_only=False)
            return {"executed": "open", "order": order}

        if action == "add":
            if pos_side != side_dir:
                return {"skipped": "add_wrong_side_or_flat"}
            order = await market_order(ex, sym, side, size, reduce_only=False)
            return {"executed": "add", "order": order}

        if action == "close":
            if pos_side == "flat":
                return {"skipped": "flat_close_ignored"}
            close_side = "sell" if pos_side == "long" else "buy"
            qty = max(size, pos_size * (1 - CLOSE_TOLERANCE_PCT))
            order = await market_order(ex, sym, close_side, qty, reduce_only=True)
            return {"executed": "close", "order": order}

        if action == "flip":
            if pos_side != "flat":
                close_side = "sell" if pos_side == "long" else "buy"
                await market_order(ex, sym, close_side, pos_size, reduce_only=True)
            order = await market_order(ex, sym, side, size, reduce_only=False)
            return {"executed": "flip", "order": order}

        return {"skipped": "unknown_action"}

    finally:
        try:
            await ex.close()
        except Exception:
            pass