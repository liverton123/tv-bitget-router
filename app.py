import os, json, time
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import ccxt.async_support as ccxt

app = FastAPI()

# ------------------ ENV (둘 다 지원) ------------------
def pick(*names):
    for n in names:
        v = os.getenv(n)
        if v: return v
    return None

BITGET_KEY       = pick("BITGET_KEY", "BITGET_API_KEY")
BITGET_SECRET    = pick("BITGET_SECRET", "BITGET_API_SECRET")
BITGET_PASSWORD  = pick("BITGET_PASSWORD", "BITGET_API_PASSWORD")
PRODUCT_TYPE     = (os.getenv("BITGET_PRODUCT_TYPE") or "umcbl").lower()

# 동작 옵션(기본: 플랫에서 sell 진입 금지, 반전 금지)
ALLOW_SHORT_WHEN_FLAT = (os.getenv("ALLOW_SHORTS") or os.getenv("ALLOW_SHORT_WHEN_FLAT") or "false").lower() == "true"
ALLOW_REVERSE         = (os.getenv("ALLOW_REVERSE") or "false").lower() == "true"

used_names = {
    "apiKey":   "BITGET_KEY"      if os.getenv("BITGET_KEY")      else "BITGET_API_KEY",
    "secret":   "BITGET_SECRET"   if os.getenv("BITGET_SECRET")   else "BITGET_API_SECRET",
    "password": "BITGET_PASSWORD" if os.getenv("BITGET_PASSWORD") else "BITGET_API_PASSWORD",
}
missing = [k for k,v in {"KEY":BITGET_KEY,"SECRET":BITGET_SECRET,"PASSWORD":BITGET_PASSWORD}.items() if not v]
if missing:
    print(f"⚠️ [WARN] Bitget env missing: {missing}. 현재 Render 키 이름 확인 필요.")
else:
    print(f"✅ Bitget env OK  (using {used_names})")
print(f"➡️ productType = {PRODUCT_TYPE}, ALLOW_SHORT_WHEN_FLAT={ALLOW_SHORT_WHEN_FLAT}, ALLOW_REVERSE={ALLOW_REVERSE}")

# ------------------ CCXT ------------------
ex = ccxt.bitget({
    "apiKey": BITGET_KEY or "",
    "secret": BITGET_SECRET or "",
    "password": BITGET_PASSWORD or "",
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
        "defaultSubType": "linear",
        "productType": PRODUCT_TYPE,
    },
})

_markets_loaded = False
async def ensure_markets():
    global _markets_loaded
    if not _markets_loaded:
        await ex.load_markets()
        _markets_loaded = True

# ------------------ Utils ------------------
def normalize_symbol(tv_symbol: str) -> str:
    s = tv_symbol.upper().replace(".P", "")
    if s.endswith("USDT"):
        base = s[:-5]
        return f"{base}/USDT:USDT"
    return tv_symbol

async def get_net_position(symbol: str) -> Dict[str, Any]:
    await ensure_markets()
    positions = await ex.fetch_positions([symbol], params={"productType": PRODUCT_TYPE})
    net = 0.0
    side = "flat"
    for p in positions:
        sz = float(p.get("contracts") or p.get("size") or 0)
        if sz == 0: 
            continue
        if p.get("side") == "long":  net += sz
        if p.get("side") == "short": net -= sz
    if net > 0: side = "long"
    if net < 0: side = "short"
    return {"side": side, "net": net}

async def market_order(symbol: str, side: str, size: float):
    await ensure_markets()
    params = {"productType": PRODUCT_TYPE}
    if side == "buy":
        return await ex.create_market_buy_order(symbol, size, params)
    else:
        return await ex.create_market_sell_order(symbol, size, params)

# 간단 중복 필터
_recent = {}
def dedup_key(symbol, side, size, ttl=5):
    now = time.time()
    k = f"{symbol}:{side}:{size:.8f}"
    if k in _recent and now - _recent[k] < ttl:
        return True
    _recent[k] = now
    return False

# ------------------ Routes ------------------
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

    if not tv_symbol or side not in ("buy","sell") or orderType != "market":
        raise HTTPException(400, "missing/invalid fields")

    symbol = normalize_symbol(tv_symbol)
    if dedup_key(symbol, side, size):
        return JSONResponse({"ok": True, "skipped": "duplicate"})

    # 현재 포지션
    pos = await get_net_position(symbol)
    print(f"[{symbol}] incoming {side} size={size} | pos={pos}")

    if side == "buy":
        o = await market_order(symbol, "buy", size)
        return {"ok": True, "order": o}

    # side == sell
    if pos["side"] == "long":
        o = await market_order(symbol, "sell", size)  # 롱 청산/부분청산
        return {"ok": True, "closed_long": True, "order": o}

    if pos["side"] == "flat":
        if not ALLOW_SHORT_WHEN_FLAT:
            return {"ok": True, "skipped": "flat_and_sell"}
        o = await market_order(symbol, "sell", size)  # 숏 진입 허용 시
        return {"ok": True, "opened_short": True, "order": o}

    # 이미 숏 보유
    if not ALLOW_REVERSE:
        return {"ok": True, "skipped": "already_short"}
    # 간단 반전(우선 청산)
    o = await market_order(symbol, "buy", abs(pos["net"]))
    return {"ok": True, "reversed_to_long": True, "close_short_order": o}