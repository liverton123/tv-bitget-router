import os
from typing import Any, Dict, List, Tuple

import ccxt.async_support as ccxt

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
FORCE_FIXED_SIZING = os.getenv("FORCE_FIXED_SIZING", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT")


async def _active_symbols(ex: ccxt.Exchange, product_type: str) -> List[str]:
    positions = await ex.fetch_positions(params={"productType": product_type})
    act: List[str] = []
    for p in positions or []:
        amt = abs(float(p.get("contracts") or p.get("contractSize") or p.get("size") or 0))
        if amt > 0:
            act.append(p["symbol"])
    # unique by base symbol
    uniq = []
    seen = set()
    for s in act:
        base = s.split("/")[0]
        if base not in seen:
            seen.add(base)
            uniq.append(s)
    return uniq


async def _position_size_side(ex: ccxt.Exchange, symbol: str, product_type: str) -> Tuple[float, str | None]:
    ps = await ex.fetch_positions([symbol], params={"productType": product_type})
    amt = 0.0
    side = None
    for p in ps or []:
        contracts = float(p.get("contracts") or p.get("contractSize") or p.get("size") or 0.0)
        if contracts != 0:
            amt = abs(contracts)
            side = "long" if contracts > 0 else "short"
            break
    return amt, side


async def _ticker_price(ex: ccxt.Exchange, symbol: str) -> float:
    t = await ex.fetch_ticker(symbol)
    price = float(t.get("last") or t.get("close") or 0.0)
    if price <= 0:
        raise ValueError(f"no price for {symbol}")
    return price


async def _account_equity(ex: ccxt.Exchange) -> float:
    bal = await ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    total = float(usdt.get("total") or usdt.get("free") or 0.0)
    if total <= 0:
        # as a fallback, try info path
        total = float(bal.get("total", {}).get("USDT", 0.0))
    return total


async def _leverage_for_symbol(ex: ccxt.Exchange, symbol: str) -> float:
    m = ex.markets.get(symbol) or {}
    lev = float(m.get("limits", {}).get("leverage", {}).get("max", 10))  # fallback to 10x
    # we do not change leverage here; assume it is already set on the venue
    return max(1.0, lev)


async def _compute_order_qty(ex: ccxt.Exchange, symbol: str) -> float:
    equity = await _account_equity(ex)
    if equity <= 0:
        raise ValueError("equity is zero")
    price = await _ticker_price(ex, symbol)
    lev = await _leverage_for_symbol(ex, symbol)
    # Target initial margin = equity * FRACTION_PER_POSITION
    # quantity = margin * leverage / price
    margin_target = equity * FRACTION_PER_POSITION
    qty = (margin_target * lev) / price
    # round to exchange precision
    market = ex.markets.get(symbol) or {}
    step = float(market.get("precision", {}).get("amount") or market.get("limits", {}).get("amount", {}).get("min", 0)) or 1e-8
    qty = max(step, (qty // step) * step)
    return qty


async def _place_market(
    ex: ccxt.Exchange,
    symbol: str,
    side: str,
    amount: float,
    reduce_only: bool,
    product_type: str,
) -> Dict[str, Any]:
    params = {
        "reduceOnly": reduce_only,
        "productType": product_type,
        "marginCoin": MARGIN_COIN,
    }
    order = await ex.create_order(symbol, "market", side, amount, None, params)
    return order


async def smart_route(
    ex: ccxt.Exchange,
    unified_symbol: str,
    side: str,
    order_type: str,
    incoming_size: float,
    intent: str | None,
    product_type: str,
) -> Dict[str, Any]:
    side = side.lower()
    intent = (intent or "").lower()

    await ex.load_markets()
    if unified_symbol not in ex.markets:
        raise ValueError(f"unknown symbol: {unified_symbol}")

    pos_amt, pos_side = await _position_size_side(ex, unified_symbol, product_type)

    # Intent: close
    if intent == "close":
        if pos_amt <= 0:
            return {"action": "close", "status": "skipped", "reason": "no position"}
        close_side = "sell" if pos_side == "long" else "buy"
        order = await _place_market(ex, unified_symbol, close_side, pos_amt, True, product_type)
        return {"action": "close", "side": close_side, "amount": pos_amt, "orderId": order.get("id")}

    # No position yet
    if pos_amt == 0:
        if intent != "open":
            return {"action": "ignored", "reason": "no position and not open"}
        if (not ALLOW_SHORTS) and side == "sell":
            return {"action": "blocked", "reason": "shorts disabled"}
        active = await _active_symbols(ex, product_type)
        bases = {s.split("/")[0] for s in active}
        if unified_symbol.split("/")[0] not in bases and len(bases) >= MAX_COINS:
            return {"action": "blocked", "reason": "max coins reached", "active": list(bases)}
        amount = await _compute_order_qty(ex, unified_symbol) if FORCE_FIXED_SIZING else float(incoming_size or 0)
        if amount <= 0:
            return {"action": "blocked", "reason": "zero size"}
        order = await _place_market(ex, unified_symbol, side, amount, False, product_type)
        return {"action": "open", "side": side, "amount": amount, "orderId": order.get("id")}

    # Position exists
    same_dir = (pos_side == "long" and side == "buy") or (pos_side == "short" and side == "sell")
    opposite = not same_dir

    if opposite:
        if not REENTER_ON_OPPOSITE:
            return {"action": "blocked", "reason": "opposite while position open"}
        await _place_market(ex, unified_symbol, "sell" if pos_side == "long" else "buy", pos_amt, True, product_type)
        amount = await _compute_order_qty(ex, unified_symbol) if FORCE_FIXED_SIZING else float(incoming_size or 0)
        if amount <= 0:
            return {"action": "reenter_blocked", "reason": "zero size after close"}
        order = await _place_market(ex, unified_symbol, side, amount, False, product_type)
        return {"action": "reenter", "side": side, "amount": amount, "orderId": order.get("id")}

    # Same direction: allow only add/open intents
    if intent not in ("add", "open"):
        return {"action": "ignored", "reason": "position exists but no add/open intent"}
    amount = await _compute_order_qty(ex, unified_symbol) if FORCE_FIXED_SIZING else float(incoming_size or 0)
    if amount <= 0:
        return {"action": "blocked", "reason": "zero size"}
    order = await _place_market(ex, unified_symbol, side, amount, False, product_type)
    return {"action": "add", "side": side, "amount": amount, "orderId": order.get("id")}