import os, math, logging
from typing import Any, Dict, List, Optional
import ccxt.async_support as ccxt
from config import (
    FRACTION_PER_TRADE, MAX_OPEN_POSITIONS, DEFAULT_LEVERAGE,
    REENTER_ON_OPPOSITE, PRODUCT_TYPE, MARGIN_COIN,
    REFERENCE_BALANCE_USDT, DRY_RUN
)

log = logging.getLogger("router.trade")

def normalize_symbol(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if not s: raise ValueError("symbol empty")
    if s.endswith(".P"): s = s[:-2]
    if "/" in s and ":USDT" in s: return s
    if s.endswith("USDT") and "/" not in s: return f"{s[:-4]}/USDT:USDT"
    if "/" in s and s.endswith("USDT") and ":USDT" not in s: return f"{s}:USDT"
    if "USDT" in s and "/" not in s and ":USDT" not in s:
        base = s.replace("USDT", "")
        return f"{base}/USDT:USDT"
    return s

def _round_down(amount: float, precision: int) -> float:
    if precision < 0: precision = 0
    q = 10 ** precision
    return math.floor(float(amount) * q) / q

async def make_exchange():
    ex = ccxt.bitget({
        "apiKey": os.getenv("BITGET_API_KEY", ""),
        "secret": os.getenv("BITGET_API_SECRET", ""),
        "password": os.getenv("BITGET_API_PASSWORD", ""),
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    if DRY_RUN:
        log.warning("[DRY_RUN] orders will NOT be sent")
    return ex

async def _market_info(ex, symbol: str) -> Dict[str, Any]:
    markets = await ex.load_markets()
    m = markets.get(symbol) or {}
    if not m:
        await ex.load_markets(True)
        m = ex.markets.get(symbol) or {}
    if not m:
        raise ValueError(f"unknown market {symbol}")
    return m

async def _ensure_leverage(ex, symbol: str):
    try:
        await ex.set_leverage(DEFAULT_LEVERAGE, symbol, {"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN})
    except Exception as e:
        log.info("set_leverage skipped: %s", e)

async def _fetch_balance_tot_free_usdt(ex):
    bal = await ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    return float(usdt.get("total") or 0), float(usdt.get("free") or 0)

async def _fetch_positions_all(ex) -> List[Dict[str, Any]]:
    try:
        return await ex.fetch_positions(None, {"productType": PRODUCT_TYPE})
    except Exception as e:
        log.error("fetch_positions error: %s", e)
        return []

async def _count_open_symbols(ex) -> int:
    pos = await _fetch_positions_all(ex)
    syms = set()
    for p in pos or []:
        qty = float(p.get("contracts") or p.get("info", {}).get("total", 0) or 0)
        if abs(qty) > 0:
            syms.add((p.get("symbol") or p.get("info", {}).get("symbol", "")).upper())
    return len(syms)

async def _try_fetch_position(ex, symbol: str) -> Optional[Dict[str, Any]]:
    for pt in (PRODUCT_TYPE, PRODUCT_TYPE.upper()):
        try:
            p = await ex.fetch_position(symbol, {"productType": pt})
            if isinstance(p, dict) and p:
                return p
        except Exception:
            pass
    positions = await _fetch_positions_all(ex)
    su = symbol.upper()
    for p in positions or []:
        ps = (p.get("symbol") or p.get("info", {}).get("symbol", "")).upper()
        if ps == su:
            return p
    return None

async def get_net_position(ex, symbol: str) -> float:
    p = await _try_fetch_position(ex, symbol)
    if not p: return 0.0
    qty = float(p.get("contracts") or p.get("info", {}).get("total", 0) or 0)
    side = (p.get("side") or p.get("info", {}).get("holdSide", "")).lower()
    if side == "long":  return qty
    if side == "short": return -qty
    return 0.0

async def calc_entry_size_fixed_margin(ex, symbol: str) -> float:
    m = await _market_info(ex, symbol)
    total, free = await _fetch_balance_tot_free_usdt(ex)
    seed = REFERENCE_BALANCE_USDT if REFERENCE_BALANCE_USDT > 0 else total
    desired_margin = seed * FRACTION_PER_TRADE
    min_cost = float(m.get("limits", {}).get("cost", {}).get("min", 5) or 5)
    min_margin = min_cost / max(1, DEFAULT_LEVERAGE)
    required_margin = max(desired_margin, min_margin)
    if free < required_margin: return 0.0
    ticker = await ex.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or 0)
    if price <= 0: return 0.0
    notional = required_margin * DEFAULT_LEVERAGE
    amount = notional / price
    min_amt = float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0)
    prec = int(m.get("precision", {}).get("amount", 3))
    amount = max(amount, min_amt)
    amount = _round_down(amount, prec)
    return amount

async def place_order(ex, symbol: str, side: str, amount: float, reduce_only: bool):
    if float(amount) <= 0:
        return {"skipped": True, "reason": "non-positive size"}
    if DRY_RUN:
        log.info("[DRY_RUN] %s %s size=%s reduceOnly=%s", side, symbol, amount, reduce_only)
        return {"dry_run": True, "side": side, "symbol": symbol, "size": amount, "reduceOnly": reduce_only}
    return await ex.create_order(
        symbol=symbol, type="market", side=side, amount=amount, price=None,
        params={"reduceOnly": reduce_only, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}
    )

async def close_all(ex, symbol: str):
    net = await get_net_position(ex, symbol)
    if net == 0:
        return [{"skipped": True, "reason": "already flat"}]
    m = await _market_info(ex, symbol)
    prec = int(m.get("precision", {}).get("amount", 3))
    qty = _round_down(abs(net), prec)
    side_close = "sell" if net > 0 else "buy"
    out = []
    res = await place_order(ex, symbol, side_close, qty, True)
    out.append({"close_all": res})
    net_after = await get_net_position(ex, symbol)
    if abs(net_after) > 0:
        qty2 = _round_down(abs(net_after), prec)
        if qty2 > 0:
            res2 = await place_order(ex, symbol, "sell" if net_after > 0 else "buy", qty2, True)
            out.append({"close_residual": res2})
    return out

async def route_signal(ex, symbol: str, side: Optional[str], action: Optional[str]):
    if (action or "").lower() == "close":
        return await close_all(ex, symbol)
    if side is None:
        return [{"skipped": True, "reason": "no side/action"}]

    await _ensure_leverage(ex, symbol)
    size = await calc_entry_size_fixed_margin(ex, symbol)
    if size <= 0:
        return [{"skipped": True, "reason": "insufficient free margin"}]

    net = await get_net_position(ex, symbol)
    out: List[Dict[str, Any]] = []

    if net == 0:
        opened = await _count_open_symbols(ex)
        if opened >= MAX_OPEN_POSITIONS:
            return [{"skipped": True, "reason": f"max open positions reached ({opened}/{MAX_OPEN_POSITIONS})"}]
        res = await place_order(ex, symbol, "buy" if side.lower() == "buy" else "sell", size, False)
        out.append({"entry": res})
        return out

    m = await _market_info(ex, symbol)
    prec = int(m.get("precision", {}).get("amount", 3))
    net_abs = abs(net)
    s = side.lower()

    if net > 0:
        if s == "buy":
            out.append({"add_long": await place_order(ex, symbol, "buy", size, False)})
        else:
            close_qty = _round_down(net_abs, prec)
            if close_qty > 0:
                out.append({"close_long": await place_order(ex, symbol, "sell", close_qty, True)})
            if REENTER_ON_OPPOSITE:
                out.append({"open_short": await place_order(ex, symbol, "sell", size, False)})
    else:
        if s == "sell":
            out.append({"add_short": await place_order(ex, symbol, "sell", size, False)})
        else:
            close_qty = _round_down(net_abs, prec)
            if close_qty > 0:
                out.append({"close_short": await place_order(ex, symbol, "buy", close_qty, True)})
            if REENTER_ON_OPPOSITE:
                out.append({"open_long": await place_order(ex, symbol, "buy", size, False)})

    return out