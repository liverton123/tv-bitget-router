import os
import math
from typing import Dict, Optional, Set

import ccxt.async_support as ccxt

BITGET_PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl").lower()  # USDT Perp
MARGIN_MODE = os.getenv("MARGIN_MODE", "cross").lower()

API_KEY = os.getenv("bitget_api_key", "")
API_SECRET = os.getenv("bitget_api_secret", "")
API_PASSWORD = os.getenv("bitget_api_password", "")

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"


async def get_exchange() -> ccxt.bitget:
    if not (API_KEY and API_SECRET and API_PASSWORD):
        raise ValueError("Missing Bitget credentials (key/secret/password).")
    ex = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "productType": BITGET_PRODUCT_TYPE,
        },
    })
    await ex.load_markets()
    return ex


def to_exchange_symbol(raw: str) -> str:
    s = raw.replace(" ", "").upper()
    if s.endswith(".P"):
        s = s[:-2]
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    return s


async def ensure_leverage(ex: ccxt.bitget, symbol: str, lev: int, margin_mode: str = "cross"):
    try:
        await ex.set_margin_mode(margin_mode, symbol)
    except Exception:
        pass
    try:
        await ex.set_leverage(lev, symbol)
    except Exception:
        pass


async def _market_info(ex: ccxt.bitget, symbol: str) -> Dict:
    m = ex.market(symbol)
    return {
        "precision_amount": m.get("precision", {}).get("amount", m.get("precision", {}).get("contract", 0)),
        "limits_amount_min": (m.get("limits", {}).get("amount", {}) or {}).get("min", 0),
    }


async def _round_amount(ex: ccxt.bitget, symbol: str, amount: float) -> float:
    info = await _market_info(ex, symbol)
    prec = info["precision_amount"]
    if prec and prec > 0:
        step = 10 ** (-prec)
        amount = math.floor(amount / step) * step
    min_amt = info["limits_amount_min"]
    if min_amt:
        amount = max(amount, min_amt)
    return max(amount, 0)


async def _price(ex: ccxt.bitget, symbol: str) -> float:
    t = await ex.fetch_ticker(symbol)
    return float(t["last"])


async def get_position_info(ex: ccxt.bitget, symbol: str) -> Dict:
    side = "none"
    contracts = 0.0
    notional = 0.0
    entry_price = 0.0
    try:
        positions = await ex.fetch_positions([symbol])
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("contracts", 0)) != 0:
                contracts = abs(float(p.get("contracts")))
                notional = float(p.get("notional", 0) or 0)
                entry_price = float(p.get("entryPrice", 0) or 0)
                side = p.get("side") or ("long" if float(p.get("contracts")) > 0 else "short")
                break
    except Exception:
        pass
    return {"side": side, "contracts": contracts, "notional": notional, "entry_price": entry_price}


async def get_open_positions_count(ex: ccxt.bitget) -> int:
    """현재 열려있는 '심볼' 개수(중복 제거)"""
    try:
        positions = await ex.fetch_positions()
    except Exception:
        return 0
    active: Set[str] = set()
    for p in positions:
        try:
            if float(p.get("contracts", 0)) != 0:
                active.add(p.get("symbol"))
        except Exception:
            continue
    return len(active)


async def place_order_market(
    ex: ccxt.bitget,
    symbol: str,
    side: str,
    *,
    contracts: Optional[float] = None,
    fixed_margin_usdt: Optional[float] = None,
    reduce_only: bool = False,
) -> float:
    if fixed_margin_usdt is not None:
        px = await _price(ex, symbol)
        if px <= 0:
            raise ValueError("Failed to fetch price")
        raw_qty = fixed_margin_usdt / px
    else:
        if contracts is None or contracts <= 0:
            raise ValueError("contracts or fixed_margin_usdt required")
        raw_qty = float(contracts)

    qty = await _round_amount(ex, symbol, raw_qty)
    if qty <= 0:
        raise ValueError("Quantity after rounding is zero")

    params = {"reduceOnly": reduce_only}
    order = await ex.create_order(symbol, "market", side, qty, None, params)
    return float(order.get("