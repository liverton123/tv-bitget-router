import os, json, asyncio, time, hmac, hashlib
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import ccxt.async_support as ccxt

app = FastAPI()

# ==== 1) ENV ====
BITGET_KEY = os.getenv("BITGET_KEY")
BITGET_SECRET = os.getenv("BITGET_SECRET")
BITGET_PASSWORD = os.getenv("BITGET_PASSWORD")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl").lower()

# 운영 보호 옵션
ALLOW_REVERSE = os.getenv("ALLOW_REVERSE", "false").lower() == "true"   # 포지션 반전 허용?
ALLOW_SHORT_WHEN_FLAT = os.getenv("ALLOW_SHORT_WHEN_FLAT", "false").lower() == "true"  # 플랫에서 sell로 숏 진입 허용?

# 시작 시 검증/로그
missing = [k for k,v in {
    "BITGET_KEY":BITGET_KEY, "BITGET_SECRET":BITGET_SECRET, "BITGET_PASSWORD":BITGET_PASSWORD
}.items() if not v]
if missing:
    print(f"⚠️ [WARN] Bitget API env missing: {missing} (KEY/SECRET/PASSWORD)")
else:
    print("✅ Bitget API env OK")

print(f"➡️ productType = {PRODUCT_TYPE}")

# ==== 2) CCXT INIT ====
ex = ccxt.bitget({
    "apiKey": BITGET_KEY or "",
    "secret": BITGET_SECRET or "",
    "password": BITGET_PASSWORD or "",
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",            # 선물
        "defaultSubType": "linear",       # USDT 선물
        "productType": PRODUCT_TYPE,      # 중요
    },
})

# ccxt는 심볼 정보를 알아야 내부 포맷으로 변환 가능
markets_loaded = False

async def ensure_markets():
    global markets_loaded
    if not markets_loaded:
        await ex.load_markets()
        markets_loaded = True

# ==== 3) 유틸 ====
def normalize_symbol(tv_symbol: str) -> str:
    """
    TradingView: 'ETHUSDT.P' 같은 형식을 Bitget ccxt 심볼 'ETH/USDT:USDT' 로 변환
    """
    s = tv_symbol.upper().replace(".P", "")
    if s.endswith("USDT"):
        base = s[:-5]
        return f"{base}/USDT:USDT"
    # fallback: 그대로
    return tv_symbol

async def get_net_position(symbol: str) -> Dict[str, Any]:
    """
    현재 심볼 NET 포지션(롱/숏/플랫)과 크기 반환
    """
    await ensure_markets()
    # v2 mix 포지션 조회 시 productType 반드시
    positions = await ex.fetch_positions([symbol], params={"productType": PRODUCT_TYPE})
    net = 0.0
    side = "flat"
    for p in positions:
        # ccxt bitget: 'side' 와 'contracts'/'size' 등의 필드가 들어옴
        sz = float(p.get("contracts") or p.get("size") or 0)
        if sz == 0:
            continue
        if p.get("side") == "long":
            net += sz
        elif p.get("side") == "short":
            net -= sz
    if net > 0:
        side = "long"
    elif net < 0:
        side = "short"
    return {"side": side, "net": net}

async def market_order(symbol: str, side: str, size: float):
    """
    side: 'buy' 또는 'sell'
    size: 계약수(coin 수량). TradingView에서 온 값을 그대로 사용하되 float 처리
    """
    await ensure_markets()
    # bitget의 경우 주문에도 productType 전달 안정적
    params = {"productType": PRODUCT_TYPE}
    if side == "buy":
        return await ex.create_market_buy_order(symbol, size, params)
    else:
        return await ex.create_market_sell_order(symbol, size, params)

# 간단 중복 방지 (symbol+size+side 5초 내 중복 무시)
_recent: Dict[str, float] = {}
def dedup(key: str, ttl=5.0) -> bool:
    now = time.time()
    last = _recent.get(key, 0)
    if now - last < ttl:
        return True
    _recent[key] = now
    return False

# ==== 4) Webhook ====
@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.body()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(400, "invalid json")

        # 필드 꺼내기
        tv_symbol = str(data.get("symbol") or "").strip()
        side      = str(data.get("side") or "").lower().strip()     # 'buy' | 'sell'
        order_type= str(data.get("orderType") or "").lower().strip()
        size_raw  = data.get("size")
        try:
            size = float(size_raw)
        except Exception:
            raise HTTPException(400, f"invalid size: {size_raw}")

        if not tv_symbol or side not in ("buy","sell") or order_type != "market":
            raise HTTPException(400, "missing/invalid fields")

        symbol = normalize_symbol(tv_symbol)

        # 중복 방지
        if dedup(f"{symbol}:{side}:{size}"):
            return JSONResponse({"ok": True, "skipped": "duplicate"})

        # 현재 포지션 파악
        pos = await get_net_position(symbol)
        pos_side = pos["side"]
        print(f"[{symbol}] incoming {side} size={size} | pos={pos_side}")

        # ===== 포지션/종료 규칙 =====
        # 1) buy : 무조건 롱 진입/증액
        if side == "buy":
            order = await market_order(symbol, "buy", size)
            return JSONResponse({"ok": True, "order": order})

        # 2) sell :
        #   - 롱 보유면 -> 종료(혹은 부분청산)
        #   - 플랫이면 -> 기본은 '무시' (ALLOW_SHORT_WHEN_FLAT=true 인 경우에만 숏진입)
        #   - 숏 보유면 -> (a) 반전 허용 시 롱으로 반전, (b) 미허용 시 무시
        if side == "sell":
            if pos_side == "long":
                order = await market_order(symbol, "sell", size)
                return JSONResponse({"ok": True, "closed_long": True, "order": order})
            elif pos_side == "flat":
                if not ALLOW_SHORT_WHEN_FLAT:
                    return JSONResponse({"ok": True, "skipped": "flat_and_sell"})
                # 숏 진입 허용 시
                order = await market_order(symbol, "sell", size)
                return JSONResponse({"ok": True, "opened_short": True, "order": order})
            else:  # short 보유
                if not ALLOW_REVERSE:
                    return JSONResponse({"ok": True, "skipped": "already_short"})
                # 반전: 우선 숏 청산 사이즈만큼 buy 후 필요시 추가 buy
                order = await market_order(symbol, "buy", abs(pos["net"]))  # 청산
                # 남는 사이즈만큼 buy 로 롱 만들기(여기선 간단화: 추가주문 생략 가능)
                return JSONResponse({"ok": True, "reversed_to_long": True, "order_close": order})

    except HTTPException as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status_code)
    except ccxt.BaseError as e:
        print("CCXT ERROR:", repr(e))
        return JSONResponse({"ok": False, "error": "exchange_error", "detail": str(e)}, status_code=500)
    except Exception as e:
        print("UNCAUGHT:", repr(e))
        return JSONResponse({"ok": False, "error": "server_error", "detail": str(e)}, status_code=500)

@app.get("/")
async def root():
    return {"status": "ok"}