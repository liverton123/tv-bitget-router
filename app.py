# app.py  — TV → Bitget (USDT-M Perp) Auto Trader
# v3.0 (coin-cap, strict block for missed adds, auto entry/exit detection)

import os, json, time, asyncio
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import ccxt.async_support as ccxt  # use async ccxt for concurrency

# ─────────────────────────
# ENV
# ─────────────────────────
API_KEY  = os.getenv("BITGET_API_KEY", "")
API_PW   = os.getenv("BITGET_API_PASSWORD", "")
API_SEC  = os.getenv("BITGET_API_SECRET", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

DRY_RUN  = os.getenv("DRY_RUN", "false").lower() == "true"

# 숏 허용 여부
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

# 균일 노출(USDT 기준) 진입
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1회 진입 = 시드 5%

# 코인(심볼) 동시 보유 상한
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# “슬롯 초과로 못 연 심볼”에 대해 신규 오픈 금지 지속시간(분)
# 0 이면 기능 해제. 기본 360(=6h) 권장. 지정 안 하면 1440(=24h).
BLOCKED_TTL_MIN = int(os.getenv("BLOCKED_TTL_MIN", "1440"))

# 동시성 가드
ORDER_LOCK = asyncio.Lock()
last_seen_ts: Dict[str, float] = {}       # 중복 가드(심볼별 0.8s)
blocked_until: Dict[str, float] = {}      # { "DOGE/USDT:USDT": unix_ts_until }

# ─────────────────────────
# Helpers
# ─────────────────────────
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    # "DOGEUSDT.P" → "DOGE/USDT:USDT"
    s = tv_symbol.replace(".P", "").replace(".p", "").strip().upper()
    if not s.endswith("USDT"):
        raise HTTPException(400, f"Unsupported TV symbol: {tv_symbol}")
    base = s[:-4]
    return f"{base}/USDT:USDT"

def now_s() -> float:
    return time.time()

def fmt(v: float, n: int = 8) -> float:
    try:
        return float(f"{float(v):.{n}f}")
    except Exception:
        return 0.0

# ─────────────────────────
# FastAPI & CCXT
# ─────────────────────────
app = FastAPI(title="TV→Bitget Router", version="3.0")

exchange = ccxt.bitget({
    "apiKey": API_KEY,
    "secret": API_SEC,
    "password": API_PW,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",       # futures
        "defaultSubType": "linear",  # USDT-M
    },
})

@app.on_event("startup")
async def _startup():
    await exchange.load_markets()

@app.on_event("shutdown")
async def _shutdown():
    try:
        await exchange.close()
    except Exception:
        pass

# ─────────────────────────
# State Readers
# ─────────────────────────
async def fetch_open_positions_map() -> Dict[str, Dict[str, Any]]:
    """
    returns: { "DOGE/USDT:USDT": {"amount": +123.0 (long) or -123.0 (short), "raw": ...}, ... }
    """
    res = {}
    try:
        positions = await exchange.fetch_positions([])
    except Exception:
        positions = []
    for p in positions:
        sym = p.get("symbol")
        amt = p.get("amount")
        if amt is None:
            amt = p.get("contracts") or 0
        try:
            amt = float(amt)
        except Exception:
            amt = 0.0
        if amt:
            res[sym] = {"amount": amt, "raw": p}
    return res

async def get_equity_usdt() -> float:
    try:
        bal = await exchange.fetch_balance()
        total = (bal.get("total") or {}).get("USDT")
        if total is None:
            free_ = (bal.get("free") or {}).get("USDT") or 0.0
            used_ = (bal.get("used") or {}).get("USDT") or 0.0
            total = free_ + used_
        return float(total or 0.0)
    except Exception:
        return 0.0

async def get_price(symbol: str) -> float:
    try:
        t = await exchange.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        return 0.0

# ─────────────────────────
# Order
# ─────────────────────────
async def place_order(symbol_ccxt: str, side: str, amount: float, reduce_only: bool) -> Dict[str, Any]:
    if DRY_RUN:
        return {"dry_run": True, "symbol": symbol_ccxt, "side": side, "amount": amount, "reduceOnly": reduce_only}
    params = {"reduceOnly": reduce_only}
    return await exchange.create_order(symbol=symbol_ccxt, type="market", side=side, amount=amount, price=None, params=params)

def purge_blocked():
    if BLOCKED_TTL_MIN <= 0:
        blocked_until.clear()
        return
    now = now_s()
    dead = [sym for sym, ts in blocked_until.items() if ts <= now]
    for k in dead:
        blocked_until.pop(k, None)

# ─────────────────────────
# Core Decision
# ─────────────────────────
async def decide_and_execute(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    TV payload(예):
      {"secret":"...","symbol":"DOGEUSDT.P","side":"buy","orderType":"market","size":1266.5}
    - buy/sell은 '현재 보유 상태'로 자동 해석:
        미보유+buy → 롱 진입, 미보유+sell → 숏 진입(ALLOW_SHORTS 필요)
        롱 보유+sell → 롱 청산(reduceOnly)
        숏 보유+buy → 숏 청산(reduceOnly)
        롱 보유+buy, 숏 보유+sell → '추가진입'(물타기)
    - 신규 진입만 MAX_COINS로 제한 (물타기/청산은 제한 없음)
    - 슬롯 초과로 거절된 심볼은 BLOCKED_TTL_MIN 동안 '신규 오픈 금지'(청산은 허용)
      → 슬롯이 비어도 그 사이 들어오는 신호가 '물타기'였던 것을 새 진입으로 오해하지 않도록 보수적으로 차단
    """
    # 보안키
    sec = (payload.get("secret") or "").strip()
    if WEBHOOK_SECRET and sec != WEBHOOK_SECRET:
        raise HTTPException(401, "Unauthorized")

    tv_symbol = payload.get("symbol", "")
    side = (payload.get("side") or "").lower().strip()
    size_from_tv = abs(float(payload.get("size", 0) or 0.0))

    if side not in ("buy", "sell"):
        raise HTTPException(400, f"invalid side: {side}")

    symbol_ccxt = tv_to_ccxt_symbol(tv_symbol)

    # 중복 가드(동일 심볼 0.8s 내 중복 알림 무시)
    tnow = now_s()
    if tnow - last_seen_ts.get(symbol_ccxt, 0) < 0.8:
        return {"skipped": "duplicate_guard", "symbol": symbol_ccxt}
    last_seen_ts[symbol_ccxt] = tnow

    # 상태 읽기
    purge_blocked()
    open_map = await fetch_open_positions_map()
    coin_set = set(open_map.keys())

    pos_amt = float(open_map.get(symbol_ccxt, {}).get("amount", 0.0))
    have_long = pos_amt > 0
    have_short = pos_amt < 0
    have_none = pos_amt == 0

    # 진입/청산/추가 판단
    reduce_only = False
    will_open_new_long = False
    will_open_new_short = False

    if side == "buy":
        if have_short:
            reduce_only = True                    # 숏 청산
        elif have_none:
            will_open_new_long = True            # 신규 롱
        else:
            # have_long
            # 롱 추가진입(물타기)
            pass
    else:  # "sell"
        if have_long:
            reduce_only = True                   # 롱 청산
        elif have_none:
            will_open_new_short = True           # 신규 숏
        else:
            # have_short
            # 숏 추가진입(물타기)
            pass

    # 숏 신규 진입 허용 여부
    if will_open_new_short and not ALLOW_SHORTS:
        return {"skipped": "short_not_allowed", "symbol": symbol_ccxt}

    # ── 신규 진입 “차단 상태(strict)” 검사
    # 슬롯 초과로 이전에 거절된 심볼은 TTL 동안 신규 오픈 금지 (청산/물타기는 허용)
    blk_until = blocked_until.get(symbol_ccxt, 0)
    if have_none and (will_open_new_long or will_open_new_short) and blk_until > now_s():
        return {"rejected": "blocked_new_open_until", "symbol": symbol_ccxt, "until": blk_until}

    # ── MAX_COINS 1차 검사 (신규 심볼만)
    if have_none and (will_open_new_long or will_open_new_short):
        if len(coin_set) >= MAX_COINS and symbol_ccxt not in coin_set:
            if BLOCKED_TTL_MIN > 0:
                blocked_until[symbol_ccxt] = now_s() + BLOCKED_TTL_MIN * 60
            return {
                "rejected": "MAX_COINS_reached",
                "symbol": symbol_ccxt,
                "open_symbols": sorted(list(coin_set)),
                "blocked_until": blocked_until.get(symbol_ccxt)
            }

    # 수량 계산
    if reduce_only:
        amount = abs(pos_amt)
        if size_from_tv > 0:
            # 트뷰가 더 작은 청산수량을 보냈다면 그만큼만
            amount = min(amount, size_from_tv)
    else:
        # 신규/추가 진입
        if FORCE_EQUAL_NOTIONAL:
            equity = await get_equity_usdt()
            price = await get_price(symbol_ccxt)
            if equity <= 0 or price <= 0:
                raise HTTPException(422, "cannot calc notional (no equity/price)")
            notional = equity * max(min(FRACTION_PER_POSITION, 1.0), 0.0)
            amount = notional / price
        else:
            amount = size_from_tv

    amount = fmt(max(0.0, amount))
    if amount <= 0:
        return {"skipped": "zero_amount", "symbol": symbol_ccxt, "reduceOnly": reduce_only}

    # ── 주문 (락으로 레이스 방지 + MAX_COINS 재확인)
    async with ORDER_LOCK:
        # 재확인
        open_map2 = await fetch_open_positions_map()
        coin_set2 = set(open_map2.keys())
        pos_amt2 = float(open_map2.get(symbol_ccxt, {}).get("amount", 0.0))
        none2 = pos_amt2 == 0

        # strict block 재확인
        blk_until2 = blocked_until.get(symbol_ccxt, 0)
        if none2 and (will_open_new_long or will_open_new_short) and blk_until2 > now_s():
            return {"rejected": "blocked_new_open_until", "symbol": symbol_ccxt, "until": blk_until2}

        # MAX_COINS 재확인
        if none2 and (will_open_new_long or will_open_new_short) and symbol_ccxt not in coin_set2:
            if len(coin_set2) >= MAX_COINS:
                if BLOCKED_TTL_MIN > 0:
                    blocked_until[symbol_ccxt] = now_s() + BLOCKED_TTL_MIN * 60
                return {"rejected": "MAX_COINS_racing_guard",
                        "symbol": symbol_ccxt,
                        "open_symbols": sorted(list(coin_set2)),
                        "blocked_until": blocked_until.get(symbol_ccxt)}

        # 주문 실행
        order = await place_order(symbol_ccxt, side, amount, reduce_only)

        # 신규 오픈에 성공하면 이 심볼의 block 해제
        if (will_open_new_long or will_open_new_short) and not reduce_only:
            blocked_until.pop(symbol_ccxt, None)

        return {
            "ok": True,
            "symbol": symbol_ccxt,
            "side": side,
            "reduceOnly": reduce_only,
            "amount": amount,
            "order": order,
        }

# ─────────────────────────
# Routes
# ─────────────────────────
@app.get("/health")
async def health():
    return {"ok": True, "ts": int(now_s()*1000)}

@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    try:
        return JSONResponse(await decide_and_execute(data))
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
