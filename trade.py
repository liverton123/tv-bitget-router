import os, math, logging
from typing import Any, Dict, List
import ccxt.async_support as ccxt

log = logging.getLogger("router.trade")

DEFAULT_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.1"))
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "false").lower() == "true"
EQUAL_NOTIONAL_USDT  = float(os.getenv("EQUAL_NOTIONAL_USDT", "100"))
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

def normalize_symbol(raw: Any) -> str:
    if raw is None: raise ValueError("symbol missing")
    s = str(raw).strip()
    if not s: raise ValueError("symbol empty")
    if "/" in s and ":USDT" in s: return s.upper()
    u = s.upper()
    if u.endswith(".P"): u = u[:-2]
    if "/" in u and u.endswith("USDT") and ":USDT" not in u: return f"{u}:USDT"
    if u.endswith("USDT") and "/" not in u:
        return f"{u[:-4]}/USDT:USDT"
    if "USDT" in u and ":USDT" not in u and "/" not in u:
        base = u.replace("USDT", "")
        return f"{base}/USDT:USDT"
    return u

async def make_exchange(api_key: str, api_secret: str, password: str, dry_run: bool):
    ex = ccxt.bitget({
        "apiKey": api_key or "",
        "secret": api_secret or "",
        "password": password or "",
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
        },
    })
    if dry_run:
        log.warning("[DRY_RUN] orders will NOT be sent")
    return ex

async def _merge_params(base: Dict[str, Any], product_type: str, margin_coin: str) -> Dict[str, Any]:
    p = dict(base or {})
    p["productType"] = product_type
    p["marginCoin"] = margin_coin
    return p

async def fetch_positions_all(ex, product_type: str, margin_coin: str) -> List[Dict[str, Any]]:
    try:
        # NOTE: ccxt signature = fetch_positions(symbols=None, params={})
        params = await _merge_params({}, product_type, margin_coin)
        positions = await ex.fetch_positions(None, params)
        return positions or []
    except Exception as e:
        log.error("[CCXT_ERROR] fetch_positions failed: %s", e)
        raise

async def get_net_position(ex, symbol: str, product_type: str, margin_coin: str) -> float:
    positions = await fetch_positions_all(ex, product_type, margin_coin)
    net = 0.0
    su = symbol.upper()
    for p in positions:
        psym = (p.get("symbol") or p.get("info", {}).get("symbol", "")).upper()
        if psym != su: continue
        qty = float(p.get("contracts") or p.get("info", {}).get("total", 0) or 0)
        side = (p.get("side") or p.get("info", {}).get("holdSide", "")).lower()
        if side == "long":  net += qty
        if side == "short": net -= qty
    return net

async def _clamp_by_balance(ex, symbol: str, desired_amount: float) -> float:
    if desired_amount <= 0: return 0.0
    bal = await ex.fetch_balance()
    usdt_free = float(bal.get("USDT", {}).get("free", 0) or bal.get("USDT", {}).get("total", 0) or 0)
    ticker = await ex.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or 0)
    if price <= 0: return 0.0
    max_notional = usdt_free * DEFAULT_LEVERAGE
    max_amount = max_notional / price
    return max(0.0, min(desired_amount, max_amount))

async def calc_order_size(ex, symbol: str) -> float:
    markets = await ex.load_markets()
    m = markets.get(symbol) or {}
    if not m:
        await ex.load_markets(True)
        m = ex.markets.get(symbol) or {}
        if not m: raise ValueError(f"unknown market {symbol}")

    bal = await ex.fetch_balance()
    usdt = float(bal.get("USDT", {}).get("free", 0) or bal.get("USDT", {}).get("total", 0) or 0)
    ticker = await ex.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or 0)
    if price <= 0: raise ValueError("bad price")

    notional = usdt * FRACTION_PER_TRADE * DEFAULT_LEVERAGE
    if FORCE_EQUAL_NOTIONAL:
        notional = min(notional, EQUAL_NOTIONAL_USDT)
    amount = notional / price

    min_amt = float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0)
    if min_amt and amount < min_amt: amount = min_amt
    prec = int(m.get("precision", {}).get("amount", 4))
    amount = float(f"{amount:.{prec}f}")
    amount = await _clamp_by_balance(ex, symbol, amount)
    return amount

async def place_order(ex, symbol: str, side: str, size: float,
                      reduce_only: bool, order_type: str,
                      product_type: str, margin_coin: str):
    qty = float(size)
    if qty <= 0:
        return {"skipped": True, "reason": "non-positive size"}
    if DRY_RUN:
        log.info("[DRY_RUN] %s %s size=%s reduceOnly=%s", side, symbol, qty, reduce_only)
        return {"dry_run": True, "side": side, "symbol": symbol, "size": qty, "reduceOnly": reduce_only}

    params = await _merge_params({"reduceOnly": reduce_only}, product_type, margin_coin)
    try:
        return await ex.create_order(symbol=symbol, type=order_type, side=side,
                                     amount=qty, price=None, params=params)
    except ccxt.InsufficientFunds as e:
        log.error("[CCXT_ERROR] insufficient funds: %s", e)
        raise
    except ccxt.ExchangeError as e:
        log.error("[CCXT_ERROR] %s", e)
        raise

async def smart_route(ex, symbol: str, side: str, order_type: str, size: float,
                      product_type: str, margin_coin: str):
    if size in (None, 0, "0"):
        size = await calc_order_size(ex, symbol)

    net = await get_net_position(ex, symbol, product_type, margin_coin)
    log.info("[ROUTER] net=%s incoming=%s size=%s", net, side, size)
    out = []

    if net == 0:
        out.append({"entry": await place_order(ex, symbol, side, size, False, order_type, product_type, margin_coin)})
        return out

    if net > 0:
        if side == "buy":
            out.append({"add_long": await place_order(ex, symbol, "buy", size, False, order_type, product_type, margin_coin)})
        else:
            close_sz = min(abs(net) * (1 + CLOSE_TOLERANCE_PCT), size)
            out.append({"close_long": await place_order(ex, symbol, "sell", close_sz, True, order_type, product_type, margin_coin)})
            rem = max(0.0, size - close_sz)
            if rem > 0:
                out.append({"open_short": await place_order(ex, symbol, "sell", rem, False, order_type, product_type, margin_coin)})
    else:
        if side == "sell":
            out.append({"add_short": await place_order(ex, symbol, "sell", size, False, order_type, product_type, margin_coin)})
        else:
            close_sz = min(abs(net) * (1 + CLOSE_TOLERANCE_PCT), size)
            out.append({"close_short": await place_order(ex, symbol, "buy", close_sz, True, order_type, product_type, margin_coin)})
            rem = max(0.0, size - close_sz)
            if rem > 0:
                out.append({"open_long": await place_order(ex, symbol, "buy", rem, False, order_type, product_type, margin_coin)})
    return out