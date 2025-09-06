import os
import math
import asyncio
from typing import Any, Dict, List, Tuple, Optional

import ccxt.async_support as ccxt


# ---------- exchange lifecycle ----------

async def get_exchange():
    api_key = os.getenv("BITGET_API_KEY")
    secret = os.getenv("BITGET_API_SECRET")
    password = (
        os.getenv("BITGET_PASSWORD")
        or os.getenv("BITGET_API_PASSWORD")
        or os.getenv("BITGET_PASSPHRASE")
    )
    if not api_key or not secret or not password:
        missing = []
        if not api_key: missing.append("BITGET_API_KEY")
        if not secret: missing.append("BITGET_API_SECRET")
        if not password: missing.append("BITGET_PASSWORD|BITGET_API_PASSWORD|BITGET_PASSPHRASE")
        raise RuntimeError(f"Missing Bitget credentials: {', '.join(missing)}")

    ex = ccxt.bitget({
        "apiKey": api_key,
        "secret": secret,
        "password": password,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # USDT-margined perpetuals
        },
    })
    await ex.load_markets()
    return ex


async def close_exchange(ex):
    try:
        if ex is not None:
            await ex.close()
    except Exception:
        pass


# ---------- symbol helpers ----------

def normalize_symbol_for_bitget(sym: str) -> str:
    # Examples:
    #  "BTCUSDT.P" -> "BTC/USDT:USDT"
    #  "BTC/USDT:USDT" -> unchanged
    s = sym.replace("-", "").replace("_", "").upper()
    if "/" in sym and ":" in sym:
        return sym  # already normalized
    if s.endswith("USDT.P") or s.endswith("USDT:USDT"):
        base = s.split("USDT")[0]
        return f"{base}/USDT:USDT"
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    # fallback: let ccxt try to map it
    return sym


def norm_side(side: str) -> str:
    return side.lower()


def norm_intent(intent: Optional[str]) -> Optional[str]:
    return intent.lower() if intent else None


def norm_order_type(order_type: str) -> str:
    return order_type.lower()


def norm_product_type(product_type: str | None) -> str:
    if not product_type:
        return "umcbl"
    return product_type.lower()


# ---------- positions & sizing ----------

async def fetch_symbol_positions(ex, symbol: str, product_type: str) -> List[Dict[str, Any]]:
    params = {"productType": product_type}
    try:
        # ccxt will call GET /api/mix/v2/position/allPosition under the hood for bitget
        pos = await ex.fetch_positions(None, params)
        if not pos:
            return []
        sym_norm = symbol
        out = []
        for p in pos:
            if p.get("symbol") == sym_norm or p.get("info", {}).get("symbol") == sym_norm:
                out.append(p)
            # Some drivers return unified symbol in "symbol" != info.symbol; accept either exact unified match
            if p.get("symbol") and p.get("symbol").upper() == sym_norm.upper():
                if p not in out:
                    out.append(p)
        return out
    except Exception:
        # if exchange doesn't filter, still return all and let getter compute net
        return []


def net_from_positions(positions: List[Dict[str, Any]]) -> Tuple[float, str]:
    """
    Returns (net_contracts, direction) where direction in {'long','short','flat'}.
    Contracts can be float; positive means long, negative short.
    """
    net = 0.0
    for p in positions:
        side = p.get("side") or p.get("positionSide")
        contracts = p.get("contracts") or p.get("contractSize") or p.get("info", {}).get("total")
        if contracts is None:
            # fallbacks: size in base
            contracts = p.get("contracts") or p.get("size") or 0.0
        try:
            qty = float(contracts)
        except Exception:
            qty = 0.0
        if (side or "").lower() == "long":
            net += qty
        elif (side or "").lower() == "short":
            net -= qty
        else:
            # one-way mode may report only one row; treat as net if size>0 and 'side' in info
            raw_side = (p.get("info", {}).get("holdSide") or p.get("info", {}).get("side") or "").lower()
            if raw_side == "long":
                net += qty
            elif raw_side == "short":
                net -= qty
    if abs(net) < 1e-9:
        return 0.0, "flat"
    return (abs(net), "long" if net > 0 else "short")


# ---------- orders ----------

async def place_market(ex, symbol: str, side: str, amount: float, reduce_only: bool):
    params = {"reduceOnly": reduce_only}
    return await ex.create_order(symbol, "market", side, amount, None, params)


# ---------- router ----------

async def smart_route(
    ex,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    intent: Optional[str],
    reenter_on_opposite: bool,
    product_type: str,
    require_intent_for_open: bool,
) -> Dict[str, Any]:
    """
    - Distinguishes open/close/scale_in/scale_out.
    - If intent is 'close', closes any existing position only (no new entry).
    - If require_intent_for_open=True and intent is None/auto/close/scale_out, will never open.
    - If no position exists and we receive a 'close' -> no-op.
    - Never flips side implicitly; if opposite signal comes and intent!=open/scale_in, it will only try reduce-only.
    """
    sym = normalize_symbol_for_bitget(symbol)
    s = norm_side(side)
    i = norm_intent(intent)
    ot = norm_order_type(order_type)
    pt = norm_product_type(product_type)

    if ot != "market":
        # keep implementation simple; only market supported here
        ot = "market"

    # load markets already done in get_exchange
    # get positions
    positions = await fetch_symbol_positions(ex, sym, pt)
    net_qty, net_dir = net_from_positions(positions)

    # helper flags
    wants_open = i in {"open", "scale_in"} or (i == "auto" and not require_intent_for_open)
    wants_close_only = i in {"close", "scale_out"} or (i == "auto" and not wants_open)

    # If explicit close
    if i == "close":
        if net_dir == "flat":
            return {"action": "close", "status": "no_position"}
        # close entire net with reduceOnly
        close_side = "sell" if net_dir == "long" else "buy"
        amt = net_qty
        if amt <= 0:
            return {"action": "close", "status": "no_position"}
        order = await place_market(ex, sym, close_side, amt, True)
        return {"action": "close", "status": "filled", "order": order}

    # Scale out: reduce up to `size`, capped by net
    if i == "scale_out":
        if net_dir == "flat":
            return {"action": "scale_out", "status": "no_position"}
        reduce_side = "sell" if net_dir == "long" else "buy"
        amt = min(size, net_qty)
        if amt <= 0:
            return {"action": "scale_out", "status": "no_position"}
        order = await place_market(ex, sym, reduce_side, amt, True)
        return {"action": "scale_out", "status": "filled", "order": order}

    # At this point: i in {open, scale_in} or i is None/auto
    if require_intent_for_open and not wants_open:
        # Do not open. If there is an opposite signal and we have a position, reduce-only up to `size`.
        if wants_close_only and net_dir != "flat":
            reduce_side = "sell" if net_dir == "long" else "buy"
            amt = min(size, net_qty)
            if amt > 0:
                order = await place_market(ex, sym, reduce_side, amt, True)
                return {"action": "reduce_only", "status": "filled", "order": order}
        return {"action": "ignored_open", "reason": "intent_required"}

    # We are allowed to open/scale-in
    # Decide requested direction from signal side
    req_dir = "long" if s == "buy" else "short" if s == "sell" else "flat"

    # If there is an opposite net and reenter_on_opposite is False, close first, do not flip
    if net_dir != "flat" and net_dir != req_dir:
        if not reenter_on_opposite:
            # Just close, no new entry
            close_side = "sell" if net_dir == "long" else "buy"
            order = await place_market(ex, sym, close_side, net_qty, True)
            return {"action": "close_on_opposite", "status": "filled", "order": order}
        else:
            # Close then open requested side
            close_side = "sell" if net_dir == "long" else "buy"
            _ = await place_market(ex, sym, close_side, net_qty, True)
            open_side = "buy" if req_dir == "long" else "sell"
            order = await place_market(ex, sym, open_side, size, False)
            return {"action": "flip", "status": "filled", "order": order}

    # Same direction or flat -> open/scale-in
    open_side = "buy" if req_dir == "long" else "sell"
    order = await place_market(ex, sym, open_side, size, False)
    return {"action": "open" if net_dir == "flat" else "scale_in", "status": "filled", "order": order}