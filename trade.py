import os
import asyncio
import ccxt.async_support as ccxt

# Always use Bitget mix (USDT-M perpetual) productType
BITGET_PRODUCT_TYPE = "umcbl"  # required by Bitget mix endpoints

async def get_exchange() -> ccxt.bitget:
    api_key = os.getenv("BITGET_API_KEY", "").strip()
    api_secret = os.getenv("BITGET_API_SECRET", "").strip()
    api_password = os.getenv("BITGET_API_PASSWORD", "").strip()

    if not api_key or not api_secret or not api_password:
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    ex = ccxt.bitget({
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_password,
        # Make sure we talk to swap/mix endpoints
        "options": {
            "defaultType": "swap",
        },
    })
    # Load markets once before first call
    await ex.load_markets()
    return ex

async def fetch_symbol_positions(ex: ccxt.bitget, symbol: str):
    """
    Returns the list of positions for the given symbol.
    Bitget mix requires 'productType' in params.
    """
    try:
        positions = await ex.fetch_positions([symbol], {"productType": BITGET_PRODUCT_TYPE})
        return positions or []
    except Exception:
        # Fallback: some ccxt versions accept None for list arg; still require productType
        return await ex.fetch_positions(None, {"productType": BITGET_PRODUCT_TYPE})

def get_reduce_only_flag(current_qty: float, side: str) -> bool:
    """
    If there is an opposite signed quantity, setting reduceOnly=False opens/averages.
    If the order would reduce existing exposure, set reduceOnly=True.
    """
    if current_qty == 0:
        return False
    if current_qty > 0 and side == "sell":
        return True
    if current_qty < 0 and side == "buy":
        return True
    return False

def get_current_net_qty(positions) -> float:
    """
    Compute net position size (>0 long, <0 short) from ccxt positions payload.
    """
    if not positions:
        return 0.0
    net = 0.0
    for p in positions:
        # ccxt unifies sizes as floats; amount > 0 for long, < 0 for short in many exchanges.
        # On bitget, use contracts and side to compute signed quantity.
        contracts = float(p.get("contracts") or p.get("amount") or 0)  # contracts count
        side = (p.get("side") or "").lower()
        if contracts and side:
            signed = contracts if side == "long" else -contracts
            net += signed
    return net

async def place_order(ex: ccxt.bitget, symbol: str, side: str, order_type: str, size: float, price=None, reduce_only=False, extra_params=None):
    """
    Create order with required Bitget params.
    """
    params = {"productType": BITGET_PRODUCT_TYPE}
    if reduce_only:
        params["reduceOnly"] = True
    if extra_params:
        params.update(extra_params)

    if order_type == "market":
        return await ex.create_order(symbol, "market", side, size, None, params)
    elif order_type == "limit":
        if price is None:
            raise ValueError("Limit order requires price.")
        return await ex.create_order(symbol, "limit", side, size, price, params)
    else:
        raise ValueError(f"Unsupported orderType: {order_type}")

async def smart_route(ex: ccxt.bitget, alert: dict):
    """
    Unified handler for entry/DCA/exit signals from TradingView.
    It uses the side provided by the alert and sets reduceOnly automatically
    when the order would close existing exposure.
    """
    symbol = alert["symbol"]
    side = alert["side"].lower()
    order_type = alert.get("orderType", "market").lower()
    size = float(alert["size"])
    price = alert.get("price")
    extra_params = alert.get("params") or {}

    # Ensure markets are loaded (safe if called multiple times)
    await ex.load_markets()

    # Get current net position to decide reduceOnly
    positions = await fetch_symbol_positions(ex, symbol)
    net_qty = get_current_net_qty(positions)
    reduce_only = get_reduce_only_flag(net_qty, side)

    # Place order
    try:
        resp = await place_order(
            ex=ex,
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            price=price,
            reduce_only=reduce_only,
            extra_params=extra_params,
        )
        return {"symbol": symbol, "side": side, "size": size, "reduceOnly": reduce_only, "id": resp.get("id")}
    finally:
        # Always close the client
        try:
            await ex.close()
        except Exception:
            pass