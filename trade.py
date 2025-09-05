import os, logging, ccxt.async_support as ccxt
from typing import Any, Dict, List

log = logging.getLogger("router.trade")

DEFAULT_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
# 1/20 고정
FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.05"))

FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "false").lower() == "true"
EQUAL_NOTIONAL_USDT  = float(os.getenv("EQUAL_NOTIONAL_USDT", "100"))
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

def normalize_symbol(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s: raise ValueError("symbol empty")
    u = s.upper()
    if u.endswith(".P"): u = u[:-2]
    if "/" in u and ":USDT" in u: return u
    if u.endswith("USDT") and "/" not in u: return f"{u[:-4]}/USDT:USDT"
    if "/" in u and u.endswith("USDT"): return f"{u}:USDT"
    if "USDT" in u and "/" not in u and ":USDT" not in u:
        base = u.replace("USDT", "")
        return f"{base}/USDT:USDT"
    return u

async def make_exchange(api_key: str, api_secret: str, password: str, dry_run: bool):
    ex = ccxt.bitget({
        "apiKey": api_key or "",
        "secret": api_secret or "",
        "password": password or "",
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    if dry_run:
        log.warning("[DRY_RUN] orders will NOT be sent")
    return ex

# --- sizing: 항상 '시드의 1/20' 만큼만 마진 사용 ---
async def calc_order_size_fixed_fraction(ex, symbol: str) -> float:
    markets = await ex.load_markets()
    m = markets.get(symbol) or {}
    if not m:
        await ex.load_markets(True)
        m = ex.markets.get(symbol) or {}
        if not m: raise ValueError(f"unknown market {symbol}")

    bal = await ex.fetch_balance()
    total = float(bal.get("USDT", {}).get("total", 0) or 0)     # 시드(총자산)
    free  = float(bal.get("USDT", {}).get("free", 0) or 0)      # 사용 가능
    ticker = await ex.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or 0)
    if price <= 0: raise ValueError("bad price")

    # 목표 마진 (시드의 1/20), free 부족하면 free의 98%로 클램프
    target_margin = total * FRACTION_PER_TRADE
    target_margin = min(target_margin, max(0.0, free) * 0.98)

    # 주문 수량 = (목표 마진 * 레버리지) / 가격
    notional = target_margin * DEFAULT_LEVERAGE
    amount = notional / price

    # 거래소 최소수량/정밀도 적용
    min_amt = float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0)
    if min_amt and amount < min_amt: amount = min_amt
    prec = int(m.get("precision", {}).get("amount", 4))
    amount = float(f"{amount:.{prec}f}")
    return amount

# --- positions (40019 회피) ---
async def _fetch_position_single(ex, symbol: str, product_type: str):
    try:
        return await ex.fetch_position(symbol, {"productType": product_type})
    except Exception as e:
        log.info("fallback to all-position: %s", e)
        return None

async def _fetch_positions_all(ex, product_type: str) -> List[Dict[str, Any]]:
    return await ex.fetch_positions(None, {"productType": product_type})

async def get_net_position(ex, symbol: str, product_type: str, margin_coin: str) -> float:
    su = symbol.upper()
    p = await _fetch_position_single(ex, su, product_type)
    if isinstance(p, dict) and p:
        qty = float(p.get("contracts") or p.get("info", {}).get("total", 0) or 0)
        side = (p.get("side") or p.get("info", {}).get("holdSide", "")).lower()
        return qty if side == "long" else (-qty if side == "short" else 0.0)

    positions = await _fetch_positions_all(ex, product_type)
    net = 0.0
    for px in positions or []:
        psym = (px.get("symbol") or px.get("info", {}).get("symbol", "")).upper()
        if psym != su: continue
        qty = float(px.get("contracts") or px.get("info", {}).get("total", 0) or 0)
        side = (px.get("side") or px.get("info", {}).get("holdSide", "")).lower()
        if side == "long":  net += qty
        if side == "short": net -= qty
    return net

# --- order ---
async def place_order(ex, symbol: str, side: str, size: float,
                      reduce_only: bool, order_type: str,
                      product_type: str, margin_coin: str):
    qty = float(size)
    if qty <= 0:
        return {"skipped": True, "reason": "non-positive size"}
    if DRY_RUN:
        log.info("[DRY_RUN] %s %s size=%s reduceOnly=%s", side, symbol, qty, reduce_only)
        return {"dry_run": True, "side": side, "symbol": symbol, "size": qty, "reduceOnly": reduce_only}
    params = {"reduceOnly": reduce_only, "productType": product_type, "marginCoin": margin_coin}
    return await ex.create_order(symbol=symbol, type=order_type, side=side,
                                 amount=qty, price=None, params=params)

async def smart_route(ex, symbol: str, side: str, order_type: str, size: float,
                      product_type: str, margin_coin: str, force_fixed_sizing: bool):
    # size 강제 재계산(시그널 값 무시)
    if force_fixed_sizing:
        size = await calc_order_size_fixed_fraction(ex, symbol)
    else:
        size = float(size) if size not in (None, "", "0") else 0.0
        if size <= 0:
            size = await calc_order_size_fixed_fraction(ex, symbol)

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