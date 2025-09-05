import ccxt.async_support as ccxt
from decimal import Decimal
from typing import Dict, Any, Optional, List

# Single place to adapt CCXT <-> Bitget specifics

def make_exchange(api_key: str, api_secret: str, api_password: str, enable_rate_limit: bool = True):
    ex = ccxt.bitget({
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_password,
        "enableRateLimit": enable_rate_limit,
        "options": {
            "defaultType": "swap",     # futures
        },
    })
    return ex

async def get_market(ex, symbol: str) -> Dict[str, Any]:
    await ex.load_markets()
    return ex.market(symbol)

async def get_mark_price(ex, symbol: str) -> float:
    t = await ex.fetch_ticker(symbol)
    # prefer mark if present, else last
    return float(t.get("info", {}).get("markPrice") or t.get("last"))

async def fetch_positions_all(ex, product_type: str, margin_coin: str, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    # CCXT bitget unifies; request all then filter
    pos = await ex.fetch_positions([symbol] if symbol else None, {"productType": product_type, "marginCoin": margin_coin})
    # Ensure consistent shape
    return pos

async def fetch_balance_usdt_equity(ex) -> Decimal:
    bal = await ex.fetch_balance(params={"type": "swap"})
    # Use total USDT (equity)
    usdt = bal.get("total", {}).get("USDT")
    if usdt is None:
        usdt = bal.get("USDT", {}).get("total", 0)
    return Decimal(str(usdt or 0))

async def market_order(ex, symbol: str, side: str, qty, product_type: str, margin_coin: str, reduce_only: bool = False):
    params = {
        "productType": product_type,
        "marginCoin": margin_coin,
        "reduceOnly": reduce_only,
    }
    return await ex.create_order(symbol=symbol, type="market", side=side, amount=float(qty), params=params)

async def reduce_only_order(ex, symbol: str, side: str, qty, product_type: str, margin_coin: str):
    return await market_order(ex, symbol, side, qty, product_type, margin_coin, reduce_only=True)