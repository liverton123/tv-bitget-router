import os, json, time
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import ccxt.async_support as ccxt

app = FastAPI()

# ---------- ENV: 옛/새 이름 모두 지원 ----------
def pick(*names):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None

BITGET_KEY       = pick("BITGET_KEY", "BITGET_API_KEY")
BITGET_SECRET    = pick("BITGET_SECRET", "BITGET_API_SECRET")
BITGET_PASSWORD  = pick("BITGET_PASSWORD", "BITGET_API_PASSWORD")
PRODUCT_TYPE     = (os.getenv("BITGET_PRODUCT_TYPE") or "umcbl").lower()

ALLOW_SHORT_WHEN_FLAT = (os.getenv("ALLOW_SHORTS") or os.getenv("ALLOW_SHORT_WHEN_FLAT") or "false").lower() == "true"
ALLOW_REVERSE         = (os.getenv("ALLOW_REVERSE") or "false").lower() == "true"

missing = [n for n,v in {"KEY":BITGET_KEY,"SECRET":BITGET_SECRET,"PASSWORD":BITGET_PASSWORD}.items() if not v]
if missing:
    print(f"⚠️  Bitget env missing: {missing}")
else:
    print(f"✅ Bitget env loaded. productType={PRODUCT_TYPE}, allow_short_when_flat={ALLOW_SHORT_WHEN_FLAT}, allow_reverse={ALLOW_REVERSE}")

# ---------- CCXT ----------
ex = ccxt.bitget({
    "apiKey": BITGET_KEY or "",
    "secret": BITGET_SECRET or "",
    "password": BITGET_PASSWORD or "",
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
        "defaultSubType": "linear",
        "productType": PRODUCT_TYPE,   # ccxt bitget params
    },
})

_markets_loaded = False
_symbol_cache: Dict[str, str] = {}  # tv_symbol -> ccxt_symbol

async def ensure_markets():
    global _markets_loaded
    if not _markets_loaded:
        await ex.load_markets()
        _markets_loaded = True

def parse_tv_base(tv_symbol: str) -> Optional[str]:
    """ 'VIRTUAUSDT.P' -> 'VIRTUA' """
    if not tv_symbol:
        return None
    s = tv_symbol.upper().replace(".P", "")
    if s.endswith("USDT"):
        return s[:-5]
    return None

async def resolve_symbol_from_markets(tv_symbol: str) -> Optional[str]:
    """마켓 테이블에서 실제 스왑(USDT 선물) 심볼을 찾아 반환"""
    await ensure_markets()
    base = parse_tv_base(tv_symbol)
    if not base:
        return None

    # 캐시 우선
    if tv_symbol in _symbol_cache:
        return _symbol_cache[tv_symbol]

    # 조건: swap, linear, quote=USDT, base 일치
    for m in ex.markets.values():
        try:
            if not m.get("swap"):
                continue
            if m.get("linear") is False:
                continue
            if (m.get("quote") or "").upper() != "USDT":
                continue
            if (m.get("base") or "").upper() != base:
                continue
            ccxt_symbol = m.get("symbol")
            if ccxt_symbol:
                _symbol_cache[tv_symbol] = ccxt_symbol
                return ccxt_symbol
        except Exception:
            continue

    # 못 찾은 경우: 후보 출력(디버그용)
    print(f"⚠️  No matching USDT-swap market for base='{base}'. tv_symbol='{tv_symbol}'")
    return None

async def get_net_position_by_tv(tv_symbol: str) -> Dict[str, Any]:
    ccxt_symbol = await resolve_symbol_from_markets(tv_symbol)
    if not ccxt_symbol:
        raise HTTPException(400, f"unknown_symbol_for_swap: {tv_symbol}")

    positions = await ex.fetch_positions([ccxt_symbol], params={"productType": PRODUCT_TYPE})
    net = 0.0
    side = "flat"
    for p in positions or []:
        sz = float(p.get("contracts") or p.get("size") or 0)
        if sz == 0:
            continue
        if p.get("side") == "long":  net += sz
        if p.get("side") == "short": net -= sz
    if net > 0: side = "long"
    if net < 0: side = "short"
    return {"side": side, "net": net, "symbol": ccxt_symbol}

async def market_order_by_tv(tv_symbol: str, side: str, size: float):
    ccxt_symbol = await resolve_symbol_from_markets(tv_symbol)
    if not ccxt_symbol:
        raise HTTPException(400, f"unknown_symbol_for_swap: {tv_symbol}")
    params = {"productType": PRODUCT_TYPE}
    if side == "buy":
        return await ex.create_market_buy_order(ccxt_symbol, size, params)
    else:
        return await ex.create_market_sell_order(ccxt_symbol, size, params)

# 간단 중복 방지
_recent = {}
def is_dup(symbol, side, size, ttl=5):
    k = f"{symbol}:{side}:{size:.8f}"
    now = time.time()
    last = _recent.get(k)
    _recent[k] = now
    return last is not None and (now - last) < ttl

# ---------- Routes ----------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(400, "invalid json")

    tv_symbol = str(data.get("symbol") or "").strip()
    side      = str(data.get("side") or "").lower().strip()
    orderType = str(data.get("orderType") or "").lower().strip()
    try:
        size = float(data.get("size"))
    except Exception:
        raise HTTPException(400, "invalid size")

    if not tv_symbol or side not in ("buy", "sell") or orderType != "market":
        raise HTTPException(400, "missing/invalid fields")

    # 심볼을 먼저 해석해서 중복키에도 실제 CCXT 심볼을 사용
    ccxt_symbol = await resolve_symbol_from_markets(tv_symbol)
    if not ccxt_symbol:
        raise HTTPException(400, f"unknown_symbol_for_swap: {tv_symbol}")

    if is_dup(ccxt_symbol, side, size):
        return {"ok": True, "skipped": "duplicate"}

    pos = await get_net_position_by_tv(tv_symbol)
    print(f"[{ccxt_symbol}] incoming {side} size={size} | pos={pos}")

    if side == "buy":
        o = await market_order_by_tv(tv_symbol, "buy", size)
        return {"ok": True, "order": o}

    # side == sell
    if pos["side"] == "long":
        o = await market_order_by_tv(tv_symbol, "sell", size)  # 롱 청산/부분청산
        return {"ok": True, "closed_long": True, "order": o}

    if pos["side"] == "flat":
        if not ALLOW_SHORT_WHEN_FLAT:
            return {"ok": True, "skipped": "flat_and_sell"}
        o = await market_order_by_tv(tv_symbol, "sell", size)  # 숏 진입 허용 시
        return {"ok": True, "opened_short": True, "order": o}

    # 이미 숏 보유
    if not ALLOW_REVERSE:
        return {"ok": True, "skipped": "already_short"}
    # 반전(간단히 전량 청산 후 롱 진입까지 하려면 여기에 buy size 추가)
    o = await market_order_by_tv(tv_symbol, "buy", abs(pos["net"]))
    return {"ok": True, "reversed_to_long": True, "close_short_order": o}