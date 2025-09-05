import os
from decimal import Decimal
from typing import Dict, Any
from bitget_ccxt import fetch_positions_all, get_market

# --------- Static config (via env) ---------
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # USDT linear perpetual
MARGIN_COIN  = os.getenv("MARGIN_COIN", "USDT")
LEVERAGE     = os.getenv("LEVERAGE", "10")                # numeric string
FRACTION_PER_POSITION = os.getenv("FRACTION_PER_POSITION", "0.05")  # 1/20 of equity
MAX_COINS    = int(os.getenv("MAX_COINS", "5"))           # max distinct symbols with non-zero net
REQUIRE_INTENT_FOR_OPEN = (os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true")

# --------- Helpers ---------
def normalize_symbol(sym: str | None) -> str | None:
    if not sym:
        return None
    s = sym.upper().strip()
    # Accept "WUSDT", "WUSDT.P", "W/USDT", "WUSDT:USDT" → normalize to "WUSDT:USDT"
    if s.endswith(".P"):
        s = s[:-2]
    s = s.replace("/", ":")
    if ":" not in s:
        s = f"{s}:USDT"
    return s

def _dec(x) -> Decimal:
    return Decimal(str(x))

def target_qty_for_margin(target_margin_usdt: Decimal, leverage: Decimal, mark_price: Decimal) -> Decimal:
    """
    Quantity (base) so that margin ≈ target_margin_usdt at given leverage.
    margin = (qty * price) / leverage  →  qty = target_margin * leverage / price
    """
    if mark_price <= 0 or leverage <= 0 or target_margin_usdt <= 0:
        return Decimal("0")
    return (target_margin_usdt * leverage) / mark_price

async def round_size_to_step(ex, symbol: str, qty: Decimal) -> Decimal:
    """
    Rounds quantity to the exchange's lot size step.
    """
    if qty <= 0:
        return Decimal("0")
    m = await get_market(ex, symbol)
    step = Decimal(str(m.get("lot", m.get("limits", {}).get("amount", {}).get("min", "0.000001"))))
    if step <= 0:
        step = Decimal("0.000001")
    # floor to step
    return (qty / step).to_integral_value(rounding="ROUND_DOWN") * step

async def can_open_new_coin(ex, symbol: str, product_type: str, margin_coin: str) -> bool:
    """
    Enforce MAX_COINS: count distinct non-zero symbols excluding this symbol if already open.
    """
    positions = await fetch_positions_all(ex, product_type, margin_coin)
    active = set()
    for p in positions:
        amt = p.get("contracts") or p.get("amount") or 0
        side = (p.get("side") or "").lower()
        try:
            q = Decimal(str(amt))
        except Exception:
            q = Decimal("0")
        if q != 0:
            active.add(p.get("symbol"))
    if symbol in active:
        return True
    return len(active) < MAX_COINS