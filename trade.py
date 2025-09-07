import os
import re
import math
import logging
from typing import Any, Dict, List, Optional

import ccxt.async_support as ccxt

logger = logging.getLogger("router.trade")

BITGET_PARAMS = {"productType": "umcbl"}  # USDT-M perpetual

def normalize_symbol(tv_symbol: str) -> str:
    """
    Accepts TradingView symbols like 'HBARUSDT' or 'HBARUSDT.P'
    Returns ccxt market symbol 'HBAR/USDT:USDT' after market load.
    We first strip suffix like '.P'
    """
    base = re.sub(r"\.[A-Za-z]+$", "", tv_symbol)
    return base

async def get_exchange() -> ccxt.bitget:
    key = os.getenv("bitget_api_key")
    secret = os.getenv("bitget_api_secret")
    password = os.getenv("bitget_api_password")
    if not key or not secret or not password:
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    ex = ccxt.bitget({
        "apiKey": key,
        "secret": secret,
        "password": password,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "defaultSettle": "USDT",
            "productType": "umcbl",
        },
    })
    await ex.load_markets()
    return ex

def pick_market_symbol(ex: ccxt.bitget, tv_symbol: str) -> str:
    """
    Map 'HBARUSDT' -> ccxt market id for USDT-M swap.
    """
    sym = normalize_symbol(tv_symbol)
    # Prefer markets with type=swap and settle=USDT
    for m in ex.markets.values():
        if m.get("type") == "swap" and (m.get("settle") or m.get("swap")) == "USDT":
            if m.get("id", "").replace("-", "").replace("_", "").replace(":", "").startswith(sym.upper()):
                return m["symbol"]
        if m.get("type") == "swap" and m.get("symbol", "").upper().startswith(sym.upper().replace("USDT", "/USDT")):
            return m["symbol"]
    # Fallback: try common pattern
    guess = sym.replace("USDT", "/USDT:USDT")
    return guess

async def fetch_net_position(ex: ccxt.bitget, market_symbol: str) -> float:
    """
    Returns signed contracts (positive long, negative short).
    """
    positions = await ex.fetch_positions(None, BITGET_PARAMS)
    net = 0.0
    for p in positions:
        if p.get("symbol") != market_symbol:
            continue
        contracts = float(p.get("contracts") or 0)  # contracts count
        side = (p.get("side") or "").lower()
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net

def to_trade_amount(ex: ccxt.bitget, market: Dict[str, Any], raw_size: Any) -> float:
    """
    Precision and min amount guard. Returns 0 if too small after rounding.
    """
    try:
        size = float(raw_size)
    except Exception:
        raise ValueError(f"Invalid size: {raw_size}")

    prec = float(ex.amount_to_precision(market["symbol"], size))
    limits = (market.get("limits") or {}).get("amount") or {}
    min_amt = limits.get("min")
    if min_amt is not None:
        try:
            min_amt = float(min_amt)
        except Exception:
            min_amt = None
    if min_amt and prec < min_amt:
        logger.info(f"SKIP_TOO_SMALL symbol={market['symbol']} raw={size} prec={prec} min={min_amt}")
        return 0.0
    return prec

async def place(ex: ccxt.bitget, symbol: str, side: str, amount: float, reduce_only: bool) -> Dict[str, Any]:
    if amount <= 0:
        return {"status": "skipped", "reason": "too_small"}
    params = {"reduceOnly": reduce_only, **BITGET_PARAMS}
    logger.info(f"ORDER symbol={symbol} side={side} amt={amount} reduceOnly={reduce_only}")
    order = await ex.create_order(symbol, "market", side, amount, None, params)
    return {"status": "filled", "id": order.get("id"), "amount": amount, "reduceOnly": reduce_only}

async def smart_route(ex: ccxt.bitget, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Route: entry/scale/exit/flip based on current net position and incoming side/size.
    - side 'buy' means long direction, 'sell' means short direction.
    - If opposite direction and amount > existing position, we close then open the remainder.
    """
    tv_symbol = str(data["symbol"]).upper()
    side = str(data["side"]).lower().strip()
    order_type = str(data["orderType"]).lower().strip()
    raw_size = data["size"]

    if order_type != "market":
        raise ValueError("Only market orders are supported.")

    mkt_symbol = pick_market_symbol(ex, tv_symbol)
    market = ex.market(mkt_symbol) if mkt_symbol in ex.markets else {"symbol": mkt_symbol}
    amount = to_trade_amount(ex, market, raw_size)
    if amount == 0:
        return {"status": "skipped", "reason": "too_small"}

    net = await fetch_net_position(ex, market["symbol"])
    logger.info(f"STATE symbol={market['symbol']} net={net} incoming_side={side} amt={amount}")

    # Direction helpers
    def same_dir(net_pos: float, s: str) -> bool:
        return (net_pos > 0 and s == "buy") or (net_pos < 0 and s == "sell")

    def opposite_dir(net_pos: float, s: str) -> bool:
        return (net_pos > 0 and s == "sell") or (net_pos < 0 and s == "buy")

    results: List[Dict[str, Any]] = []

    if net == 0:
        # Fresh entry
        res = await place(ex, market["symbol"], side, amount, reduce_only=False)
        results.append({"decision": "OPEN_LONG" if side == "buy" else "OPEN_SHORT", **res})
        return {"ok": True, "results": results}

    if same_dir(net, side):
        # Scale in
        res = await place(ex, market["symbol"], side, amount, reduce_only=False)
        results.append({"decision": "SCALE_IN_LONG" if side == "buy" else "SCALE_IN_SHORT", **res})
        return {"ok": True, "results": results}

    # Opposite direction -> reduce or flip
    close_amt = min(abs(net), amount)
    if close_amt > 0:
        close_side = "sell" if net > 0 else "buy"  # to offset existing
        res_close = await place(ex, market["symbol"], close_side, close_amt, reduce_only=True)
        results.append({"decision": "REDUCE_EXISTING", **res_close})

    remainder = max(0.0, amount - close_amt)
    if remainder > 0:
        # Flip remainder to new side
        res_open = await place(ex, market["symbol"], side, remainder, reduce_only=False)
        results.append({"decision": "FLIP_OPEN", **res_open})

    return {"ok": True, "results": results}