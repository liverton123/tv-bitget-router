import os, logging, math
import ccxt.async_support as ccxt
from typing import Any, Dict, List

log = logging.getLogger("router.trade")

DEFAULT_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
# 항상 시드의 1/20 (=5%)
FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.05"))

CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.0"))  # 0%로 고정(40804 방지)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

def normalize_symbol(raw: Any) -> str:
    s = str(raw or "").strip()
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

# ---------- sizing ----------
def _round_amount(amount: float, precision: int) -> float:
    if precision < 0: precision = 0
    q = 10 ** precision
    return math.floor(amount * q) / q  # 내림해서 초과 청산/주문 방지

async def _market_info(ex, symbol: str) -> Dict[str, Any]:
    markets = await ex.load_markets()
    m = markets.get(symbol) or {}
    if not m:
        await ex.load_markets(True)
        m = ex.markets.get(symbol) or {}
    if not m:
        raise ValueError(f"unknown market {symbol}")
    return m

async def calc_order_size_fixed_fraction(ex, symbol: str) -> float:
    m = await _market_info(ex, symbol)

    bal = await ex.fetch_balance()
    total = float(bal.get("USDT", {}).get("total", 0) or 0)
    free  = float(bal.get("USDT", {}).get("free", 0) or 0)

    ticker = await ex.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or 0)
    if price <= 0:
        raise ValueError("bad price")

    # 목표 마진 = 시드(총자산) * 5%
    desired_margin = total * FRACTION_PER_TRADE
    # 최소 주문 금액(비트겟 대부분 5 USDT) 고려
    min_cost = float(m.get("limits", {}).get("cost", {}).get("min", 5) or 5)
    min_margin = min_cost / max(1, DEFAULT_LEVERAGE)

    # 사용 가능한 범위로 클램프
    target_margin = max(min_margin, min(desired_margin, free * 0.98))
    if target_margin <= 0:
        return 0.0

    notional = target_margin * DEFAULT_LEVERAGE
    amount = notional / price

    # 최소 수량/정밀도 적용
    min_amt = float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0)
    prec = int(m.get("precision", {}).get("amount", 3))
    amount = max(amount, min_amt)
    amount = _round_amount(amount, prec)
    return amount

# ---------- positions (40019 회피용 다중 시도) ----------
async def _try_fetch_position(ex, symbol: str, product_type: str):
    # 1) 단일 심볼
    for pt in (product_type, product_type.upper()):
        try:
            return await ex.fetch_position(symbol, {"productType": pt})
        except Exception as e:
            last = str(e)
            continue
    # 2) 전체 조회 후 필터
    for pt in (product_type, product_type.upper(), None):
        try:
            positions = await ex.fetch_positions(None, {"productType": pt} if pt else {})
            return next((p for p in positions or [] if (p.get("symbol") or p.get("info", {}).get("symbol", "")).upper() == symbol.upper()), None)
        except Exception as e:
            last = str(e)
            continue
    log.info("fetch_position fallback failed; assume flat")
    return None

async def get_net_position(ex, symbol: str, product_type: str, margin_coin: str) -> float:
    p = await _try_fetch_position(ex, symbol, product_type)
    if not p:
        return 0.0
    qty = float(p.get("contracts") or p.get("info", {}).get("total", 0) or 0)
    side = (p.get("side") or p.get("info", {}).get("holdSide", "")).lower()
    if side == "long":  return qty
    if side == "short": return -qty
    return 0.0

# ---------- orders ----------
async def place_order(ex, symbol: str, side: str, size: float, reduce_only: bool,
                      order_type: str, product_type: str, margin_coin: str):
    qty = float(size)
    if qty <= 0:
        return {"skipped": True, "reason": "non-positive size"}

    if DRY_RUN:
        log.info("[DRY_RUN] %s %s size=%s reduceOnly=%s", side, symbol, qty, reduce_only)
        return {"dry_run": True, "side": side, "symbol": symbol, "size": qty, "reduceOnly": reduce_only}

    params = {"reduceOnly": reduce_only, "productType": product_type, "marginCoin": margin_coin}

    try:
        return await ex.create_order(symbol=symbol, type=order_type, side=side,
                                     amount=qty, price=None, params=params)
    except ccxt.BaseError as e:
        msg = str(e)
        # 40804: 청산 수량이 보유수량 초과 → 순수량으로 한 번 더 시도
        if "40804" in msg or "cannot exceed the number of positions held" in msg:
            try:
                net = await get_net_position(ex, symbol, product_type, margin_coin)
                m = await _market_info(ex, symbol)
                prec = int(m.get("precision", {}).get("amount", 3))
                retry_qty = _round_amount(max(0.0, min(abs(net), qty)), prec)
                if retry_qty <= 0:
                    return {"skipped": True, "reason": "nothing to close"}
                return await ex.create_order(symbol=symbol, type=order_type, side=side,
                                             amount=retry_qty, price=None, params=params)
            except Exception as e2:
                raise e2
        raise

async def smart_route(ex, symbol: str, side: str, order_type: str, size: float,
                      product_type: str, margin_coin: str, force_fixed_sizing: bool):
    # 사이즈 강제(시그널 무시)
    if force_fixed_sizing:
        size = await calc_order_size_fixed_fraction(ex, symbol)
    else:
        try:
            size = float(size)
        except:
            size = 0.0
        if size <= 0:
            size = await calc_order_size_fixed_fraction(ex, symbol)

    if size <= 0:
        return [{"skipped": True, "reason": "insufficient free margin or min size"}]

    net = await get_net_position(ex, symbol, product_type, margin_coin)
    log.info("[ROUTER] net=%s incoming=%s size=%s", net, side, size)

    out = []
    m = await _market_info(ex, symbol)
    prec = int(m.get("precision", {}).get("amount", 3))

    if net == 0:
        out.append({"entry": await place_order(ex, symbol, side, size, False, order_type, product_type, margin_coin)})
        return out

    # 청산 수량은 절대 보유수량을 넘지 않도록 '내림' 처리
    def clamp_close(q, net_abs):
        q = min(net_abs, q)
        return _round_amount(max(0.0, q), prec)

    net_abs = abs(net)

    if net > 0:  # 롱 보유
        if side == "buy":
            out.append({"add_long": await place_order(ex, symbol, "buy", size, False, order_type, product_type, margin_coin)})
        else:
            close_sz = clamp_close(size, net_abs)
            if close_sz > 0:
                out.append({"close_long": await place_order(ex, symbol, "sell", close_sz, True, order_type, product_type, margin_coin)})
            rem = _round_amount(max(0.0, size - close_sz), prec)
            if rem > 0:
                out.append({"open_short": await place_order(ex, symbol, "sell", rem, False, order_type, product_type, margin_coin)})

    else:       # 숏 보유
        if side == "sell":
            out.append({"add_short": await place_order(ex, symbol, "sell", size, False, order_type, product_type, margin_coin)})
        else:
            close_sz = clamp_close(size, net_abs)
            if close_sz > 0:
                out.append({"close_short": await place_order(ex, symbol, "buy", close_sz, True, order_type, product_type, margin_coin)})
            rem = _round_amount(max(0.0, size - close_sz), prec)
            if rem > 0:
                out.append({"open_long": await place_order(ex, symbol, "buy", rem, False, order_type, product_type, margin_coin)})

    return out