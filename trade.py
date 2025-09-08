import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

import ccxt.async_support as ccxt

logger = logging.getLogger("tv-bitget-router.trade")

# --------- helpers ---------
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    if v is None:
        return None
    v = v.strip()
    if v == "" or v.lower() in ("none", "null"):
        return None
    return v

def _to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView: 'HBARUSDT' or 'HBARUSDT.P' -> ccxt: 'HBAR/USDT:USDT'
    """
    s = tv_symbol.upper().strip()
    s = re.sub(r"\.[A-Za-z]+$", "", s)  # strip '.P' suffix if any
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    return tv_symbol

BITGET_PRODUCT_TYPE = _env("bitget_product_type", "USDT-FUTURES") or "USDT-FUTURES"
DEFAULT_LEVERAGE = float(_env("DEFAULT_LEVERAGE", "10") or "10")  # fallback leverage for first entry

# --------- exchange factory ---------
async def get_exchange() -> ccxt.bitget:
    api_key = _env("bitget_api_key")
    api_secret = _env("bitget_api_secret")
    api_password = _env("bitget_api_password")

    if not (api_key and api_secret and api_password):
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    ex = ccxt.bitget({
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_password,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear",
            # hint for bitget mix endpoints
            "defaultProductType": BITGET_PRODUCT_TYPE,
        },
    })
    await ex.load_markets()
    return ex

# --------- portfolio/position ---------
async def fetch_positions(ex: ccxt.bitget, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    params = {"productType": BITGET_PRODUCT_TYPE}
    try:
        if symbol:
            return await ex.fetch_positions([symbol], params)
        return await ex.fetch_positions(None, params)
    except Exception:
        # some ccxt versions behave differently; fallback to all
        return await ex.fetch_positions(None, params)

def net_position_for_symbol(positions: List[Dict[str, Any]], ccxt_symbol: str) -> Tuple[float, Optional[str], Optional[float]]:
    """
    Returns (net_contracts, side('long'|'short'|None), leverage_if_available)
    """
    net = 0.0
    side: Optional[str] = None
    lev: Optional[float] = None
    for p in positions:
        if p.get("symbol") != ccxt_symbol:
            continue
        contracts = float(p.get("contracts") or p.get("size") or 0.0)
        pside = (p.get("side") or "").lower() or None
        if pside in ("long", "short"):
            side = pside
            signed = contracts if pside == "long" else -contracts
            net += signed
        if lev is None:
            try:
                lev = float(p.get("leverage") or p.get("info", {}).get("leverage"))
            except Exception:
                lev = None
    if net == 0:
        side = None
    return net, side, lev

# --------- sizing with fixed margin ---------
async def _fetch_mid_price(ex: ccxt.bitget, ccxt_symbol: str) -> float:
    t = await ex.fetch_ticker(ccxt_symbol)
    price = t.get("last") or ( (t.get("bid") or 0) + (t.get("ask") or 0) ) / 2
    price = float(price)
    if price <= 0:
        raise ValueError(f"invalid price for {ccxt_symbol}")
    return price

async def compute_entry_amount_fixed_margin(
    ex: ccxt.bitget,
    ccxt_symbol: str,
    target_margin_usdt: float,
    leverage_hint: Optional[float],
) -> float:
    """
    amount = (target_margin * effective_leverage) / price
    If leverage is unknown on first entry, use DEFAULT_LEVERAGE.
    """
    price = await _fetch_mid_price(ex, ccxt_symbol)
    lev = float(leverage_hint) if leverage_hint and leverage_hint > 0 else DEFAULT_LEVERAGE
    notional = target_margin_usdt * lev
    raw_amount = notional / price

    # precision & min-amount guard
    amount = float(ex.amount_to_precision(ccxt_symbol, raw_amount))
    min_amt = (ex.markets[ccxt_symbol].get("limits", {}).get("amount", {}) or {}).get("min")
    if min_amt is not None:
        try:
            min_amt = float(min_amt)
        except Exception:
            min_amt = None
    if min_amt and amount < min_amt:
        logger.info(f"SKIP_TOO_SMALL symbol={ccxt_symbol} raw={raw_amount} prec={amount} min={min_amt}")
        return 0.0
    return amount

# --------- order placement ---------
async def place_market(
    ex: ccxt.bitget,
    ccxt_symbol: str,
    side: str,
    amount: float,
    reduce_only: bool,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if amount <= 0:
        return {"status": "skipped", "reason": "too_small"}
    params = {"productType": BITGET_PRODUCT_TYPE, "reduceOnly": reduce_only}
    if extra:
        params.update(extra)
    logger.info(f"ORDER symbol={ccxt_symbol} side={side} amt={amount} reduceOnly={reduce_only}")
    o = await ex.create_order(ccxt_symbol, "market", side, amount, None, params)
    return {"status": "filled", "id": o.get("id"), "amount": amount, "reduceOnly": reduce_only}

# --------- router ---------
async def smart_route(
    exchange: ccxt.bitget,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    - Entry/Scale: ignore incoming 'size' and use fixed margin sizing (6 USDT) with effective leverage.
    - Opposite signal: close first with reduceOnly, then (if leftover) open remainder using fixed sizing.
    """
    if order_type.lower() != "market":
        raise ValueError("Only market orders are supported")

    ccxt_symbol = _to_ccxt_symbol(symbol)
    # make sure market exists
    market = exchange.market(ccxt_symbol) if ccxt_symbol in exchange.markets else None
    if market is None:
        # attempt load/refresh then retry
        await exchange.load_markets()
        if ccxt_symbol not in exchange.markets:
            raise ValueError(f"Unknown market symbol {ccxt_symbol}")

    positions = await fetch_positions(exchange)
    net, pos_side, lev_from_pos = net_position_for_symbol(positions, ccxt_symbol)

    # classify intent using current position and incoming side
    incoming = side.lower().strip()
    same_dir = (pos_side == "long" and incoming == "buy") or (pos_side == "short" and incoming == "sell")
    opposite_dir = (pos_side == "long" and incoming == "sell") or (pos_side == "short" and incoming == "buy")

    FIXED_MARGIN_USDT = float(_env("FIXED_MARGIN_USDT", "6") or "6")

    results: List[Dict[str, Any]] = []

    if net == 0 or pos_side is None:
        # fresh entry
        amt = await compute_entry_amount_fixed_margin(exchange, ccxt_symbol, FIXED_MARGIN_USDT, lev_from_pos)
        if amt == 0:
            return {"ok": True, "results": [{"decision": "SKIP_ENTRY_TOO_SMALL"}]}
        r = await place_market(exchange, ccxt_symbol, incoming, amt, reduce_only=False)
        results.append({"decision": "OPEN_LONG" if incoming == "buy" else "OPEN_SHORT", **r})
        return {"ok": True, "results": results}

    if same_dir:
        # scale-in with fixed margin sizing
        amt = await compute_entry_amount_fixed_margin(exchange, ccxt_symbol, FIXED_MARGIN_USDT, lev_from_pos)
        if amt == 0:
            return {"ok": True, "results": [{"decision": "SKIP_SCALE_TOO_SMALL"}]}
        r = await place_market(exchange, ccxt_symbol, incoming, amt, reduce_only=False)
        results.append({"decision": "SCALE_IN_LONG" if incoming == "buy" else "SCALE_IN_SHORT", **r})
        return {"ok": True, "results": results}

    if opposite_dir:
        # close as much as possible first
        close_side = "sell" if pos_side == "long" else "buy"
        close_amt = float(abs(net))
        close_amt = float(exchange.amount_to_precision(ccxt_symbol, close_amt))
        if close_amt > 0:
            r_close = await place_market(exchange, ccxt_symbol, close_side, close_amt, reduce_only=True)
            results.append({"decision": "CLOSE_ALL", **r_close})

        # after full close, if strategy intends to flip immediately, open with fixed margin sizing
        amt = await compute_entry_amount_fixed_margin(exchange, ccxt_symbol, FIXED_MARGIN_USDT, lev_from_pos)
        if amt > 0:
            r_open = await place_market(exchange, ccxt_symbol, incoming, amt, reduce_only=False)
            results.append({"decision": "FLIP_OPEN", **r_open})

        return {"ok": True, "results": results}

    # Fallback (should not hit)
    return {"ok": True, "results": [{"decision": "NOOP"}]}