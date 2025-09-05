import os
import re
import math
import ccxt.async_support as ccxt

# ---------- env ----------
DEFAULTS = {
    "WEBHOOK_SECRET": "",
    "BITGET_API_KEY": "",
    "BITGET_API_SECRET": "",
    "BITGET_API_PASSWORD": "",
    "BITGET_PRODUCT_TYPE": "umcbl",
    "MARGIN_MODE": "cross",            # for reference only; Bitget USDT-M default cross
    "FRACTION_PER_POSITION": "0.05",   # 1/20 of equity
    "MAX_COINS": "5",                  # cap on simultaneous distinct coins
    "ALLOW_SHORTS": "true",
    "CLOSE_TOLERANCE_PCT": "0.02",     # +2% buffer on reduce-only close
    "DRY_RUN": "false",
    "REQUIRE_INTENT_FOR_OPEN": "true", # only open/add when intent provided
}

def load_env():
    out = {}
    for k, v in DEFAULTS.items():
        out[k] = os.getenv(k, v)
    # coercions
    out["FRACTION_PER_POSITION"] = float(out["FRACTION_PER_POSITION"])
    out["MAX_COINS"] = int(out["MAX_COINS"])
    out["ALLOW_SHORTS"] = out["ALLOW_SHORTS"].lower() == "true"
    out["DRY_RUN"] = out["DRY_RUN"].lower() == "true"
    out["CLOSE_TOLERANCE_PCT"] = float(out["CLOSE_TOLERANCE_PCT"])
    out["REQUIRE_INTENT_FOR_OPEN"] = out["REQUIRE_INTENT_FOR_OPEN"].lower() == "true"
    return out

# ---------- symbol normalize ----------
EXCHANGE_PREFIXES = ("BINANCE:", "BYBIT:", "BITGET:", "OKX:", "OKEX:", "KUCOIN:")

def normalize_symbol(sym: str) -> str:
    s = (sym or "").strip().upper()
    if not s:
        return s
    for p in EXCHANGE_PREFIXES:
        if s.startswith(p):
            s = s.split(":", 1)[1]
            break
    s = s.replace(" ", "")
    if s.endswith(":USDT"):
        s = s[:-5]
    s = s.replace("/", "")
    if s.endswith(".P"):
        s = s[:-2]
    if s.endswith("-PERP"):
        s = s[:-5]
    if s.endswith("PERPETUAL"):
        s = s[:-9]
    if s.endswith("USD") and not s.endswith("USDT"):
        s = s + "T"
    s = re.sub(r"(USDT)+$", "USDT", s)
    return s

# ---------- market helpers ----------
async def fetch_price(ex: ccxt.Exchange, symbol: str) -> float:
    t = await ex.fetch_ticker(symbol)
    px = t.get("last") or t.get("close") or t.get("ask") or t.get("bid")
    return float(px)

async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    bal = await ex.fetch_balance()
    usdt = bal.get("USDT") or {}
    free = usdt.get("free")
    total = usdt.get("total")
    return float(total if total is not None else (free or 0.0))

def round_amount(ex: ccxt.Exchange, symbol: str, amount: float) -> float:
    m = ex.markets[symbol]
    step = (m.get("precision") or {}).get("amount")
    if step is not None:
        amount = ex.amount_to_precision(symbol, amount)
    min_amt = (((m.get("limits") or {}).get("amount") or {}).get("min")) or 0.0
    if min_amt and amount < min_amt:
        amount = min_amt
    return float(amount)

def net_contracts_from_position(p) -> float:
    # ccxt bitget positions: "contracts" or "contractsSize"
    return float(p.get("contracts", p.get("contractsSize", 0.0)) or 0.0)

async def get_position_contracts(ex: ccxt.Exchange, symbol: str) -> float:
    poss = await ex.fetch_positions([symbol])
    for p in poss:
        if p.get("symbol") == symbol:
            return net_contracts_from_position(p)
    return 0.0

async def open_coin_count(ex: ccxt.Exchange) -> int:
    poss = await ex.fetch_positions()
    coins = set()
    for p in poss:
        c = net_contracts_from_position(p)
        if abs(c) > 0:
            coins.add(p.get("symbol"))
    # count distinct coins (symbols)
    return len(coins)

# ---------- order helpers ----------
async def create_order(
    ex: ccxt.Exchange,
    symbol: str,
    side: str,
    amount: float,
    reduce_only: bool,
    env: dict,
    order_type: str = "market",
):
    params = {
        "productType": env["BITGET_PRODUCT_TYPE"],
        "reduceOnly": reduce_only,
    }
    if env["DRY_RUN"]:
        return {"dry_run": True, "symbol": symbol, "side": side, "amount": amount, "reduce_only": reduce_only}
    return await ex.create_order(symbol, "market", side, amount, None, params)

async def close_all(ex: ccxt.Exchange, symbol: str, env: dict):
    pos = await get_position_contracts(ex, symbol)
    if abs(pos) < 1e-12:
        return {"closed": 0.0, "reason": "no position"}
    side = "sell" if pos > 0 else "buy"
    amount = abs(pos) * (1.0 + env["CLOSE_TOLERANCE_PCT"])
    amount = round_amount(ex, symbol, amount)
    return await create_order(ex, symbol, side, amount, True, env)

# ---------- sizing: fixed fraction of equity (1/20) ----------
async def calc_open_amount(ex: ccxt.Exchange, symbol: str, env: dict) -> float:
    eq = await fetch_equity_usdt(ex)
    usd = max(0.0, eq * env["FRACTION_PER_POSITION"])
    if usd <= 0:
        return 0.0
    px = await fetch_price(ex, symbol)
    amt = usd / max(px, 1e-12)
    amt = round_amount(ex, symbol, amt)
    return float(amt)

# ---------- intent router ----------
def _side_dir(side: str) -> int:
    if side == "buy":
        return +1
    if side == "sell":
        return -1
    return 0

async def smart_route(
    ex: ccxt.Exchange,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    intent: str,
    env: dict,
):
    if side not in ("buy", "sell"):
        raise ValueError("invalid side")
    await ex.load_markets()
    if symbol not in ex.markets:
        raise ValueError(f"unsupported symbol: {symbol}")

    pos = await get_position_contracts(ex, symbol)
    pos_dir = 1 if pos > 0 else (-1 if pos < 0 else 0)
    side_dir = _side_dir(side)

    # classify when intent is missing
    inferred = ""
    if not intent:
        if pos_dir == 0:
            # no position: treat as CLOSE-only (ignore) unless opening explicitly allowed
            inferred = "open"
        else:
            if pos_dir == side_dir:
                inferred = "add"
            else:
                inferred = "close"
        intent = inferred

    # guard: allow shorts or not
    if not env["ALLOW_SHORTS"] and side == "sell" and pos_dir <= 0 and intent in ("open", "add"):
        return {"ignored": True, "reason": "shorts disabled"}

    # require explicit open/add when configured
    if env["REQUIRE_INTENT_FOR_OPEN"] and intent in ("open", "add") and inferred != "add" and pos_dir == 0 and inferred != "open":
        # shouldn't happen, keep safe
        intent = "open"

    # max coins guard (only for opening a new coin)
    if intent == "open" and pos_dir == 0:
        if await open_coin_count(ex) >= env["MAX_COINS"]:
            return {"ignored": True, "reason": "max coins reached"}

    # close intent: close all regardless of payload size
    if intent == "close":
        return await close_all(ex, symbol, env)

    # reduce intent (explicit): reduce by given size (contracts); if size is 0 or invalid, close all
    if intent == "reduce":
        if abs(pos) < 1e-12:
            return {"ignored": True, "reason": "no position"}
        if size <= 0:
            return await close_all(ex, symbol, env)
        reduce_side = "sell" if side_dir > 0 else "buy"
        amt = min(abs(pos), abs(size))
        amt = amt * (1.0 + env["CLOSE_TOLERANCE_PCT"])
        amt = round_amount(ex, symbol, amt)
        return await create_order(ex, symbol, reduce_side, amt, True, env)

    # open/add: ignore incoming size and use fixed-fraction sizing
    if intent in ("open", "add"):
        amt = await calc_open_amount(ex, symbol, env)
        if amt <= 0:
            return {"ignored": True, "reason": "sizing=0"}
        return await create_order(ex, symbol, side, amt, False, env)

    # fallback: if side opposes existing position, treat as close
    if pos_dir != 0 and pos_dir != side_dir:
        return await close_all(ex, symbol, env)

    # otherwise open safely
    amt = await calc_open_amount(ex, symbol, env)
    if amt <= 0:
        return {"ignored": True, "reason": "sizing=0"}
    return await create_order(ex, symbol, side, amt, False, env)