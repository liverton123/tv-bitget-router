import os
import json
import time
import math
import asyncio
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import ccxt.async_support as ccxt  # async

# -----------------------
# ENV
# -----------------------
API_KEY  = os.getenv("BITGET_API_KEY", "")
API_PW   = os.getenv("BITGET_API_PASSWORD", "")
API_SEC  = os.getenv("BITGET_API_SECRET", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

DRY_RUN  = os.getenv("DRY_RUN", "false").lower() == "true"
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))

MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# 새 진입이 MAX_COINS로 거절된 “의도”를 몇 분 기억할지
BLOCKED_TTL_MIN = int(os.getenv("BLOCKED_TTL_MIN", "1440"))  # 24h

ORDER_LOCK = asyncio.Lock()
last_seen: Dict[str, float] = {}          # 중복/버스트 가드
blocked_open: Dict[str, Dict[str, Any]] = {}  # {symbol: {"side":"long|short","ts":float}}

# -----------------------
# Helpers
# -----------------------
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    s = tv_symbol.replace(".P", "").replace(".p", "")
    if not s.endswith("USDT"):
        raise ValueError(f"Unsupported TV symbol: {tv_symbol}")
    base = s[:-4]
    return f"{base}/USDT:USDT"

def now_s() -> float:
    return time.time()

def now_ms() -> int:
    return int(time.time() * 1000)

def opposite(side: str) -> str:
    return "long" if side == "short" else "short"

# -----------------------
# FastAPI & CCXT
# -----------------------
app = FastAPI(title="TV→Bitget Router", version="2.3")

exchange = ccxt.bitget({
    "apiKey": API_KEY,
    "secret": API_SEC,
    "password": API_PW,
    "enableRateLimit": True,
    "options": {"defaultType": "swap", "defaultSubType": "linear"},
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

# -----------------------
# State readers
# -----------------------
async def fetch_open_positions_map() -> Dict[str, Dict[str, Any]]:
    res = {}
    try:
        positions = await exchange.fetch_positions([])
    except Exception:
        positions = []

    for p in positions:
        sym = p.get("symbol")
        amount = p.get("amount")
        if amount is None:
            amount = p.get("contracts") or 0
        try:
            amount = float(amount)
        except Exception:
            amount = 0.0
        if amount:
            res[sym] = {
                "amount": amount,
                "side": "long" if amount > 0 else "short",
                "raw": p,
            }
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

# -----------------------
# Order
# -----------------------
async def place_order(symbol_ccxt: str, side: str, amount: float, reduce_only: bool) -> Dict[str, Any]:
    if DRY_RUN:
        return {"dry_run": True, "symbol": symbol_ccxt, "side": side, "amount": amount, "reduceOnly": reduce_only}
    params = {"reduceOnly": reduce_only}
    return await exchange.create_order(symbol=symbol_ccxt, type="market", side=side, amount=amount, price=None, params=params)

def clamp(v: float) -> float:
    return max(0.0, float(v or 0.0))

def purge_blocked_expired():
    if BLOCKED_TTL_MIN <= 0:
        return
    now = now_s()
    dead = [k for k, v in blocked_open.items() if now - v.get("ts", 0) > BLOCKED_TTL_MIN * 60]
    for k in dead:
        blocked_open.pop(k, None)

async def decide_and_execute(payload: Dict[str, Any]) -> Dict[str, Any]:
    tv_symbol = payload.get("symbol", "")
    side = (payload.get("side") or "").lower().strip()       # "buy"|"sell"
    size_from_tv = abs(float(payload.get("size", 0) or 0.0))
    if side not in ("buy", "sell"):
        raise HTTPException(400, detail=f"invalid side: {side}")

    symbol_ccxt = tv_to_ccxt_symbol(tv_symbol)

    # 중복 가드
    if now_s() - last_seen.get(symbol_ccxt, 0) < 0.8:
        return {"skipped": "duplicate_guard", "symbol": symbol_ccxt}
    last_seen[symbol_ccxt] = now_s()

    purge_blocked_expired()

    open_map = await fetch_open_positions_map()
    pos = open_map.get(symbol_ccxt)
    cur_amt = float(pos["amount"]) if pos else 0.0
    cur_side = "long" if cur_amt > 0 else ("short" if cur_amt < 0 else None)

    # reduceOnly 판별
    reduce_only = False
    will_open_long = False
    will_open_short = False
    if side == "buy":
        if cur_amt < 0:
            reduce_only = True
        else:
            will_open_long = True
    else:  # sell
        if cur_amt > 0:
            reduce_only = True
        else:
            will_open_short = True

    if will_open_short and not ALLOW_SHORTS:
        return {"skipped": "short_not_allowed", "symbol": symbol_ccxt}

    # MAX_COINS 심볼 제한: 신규(미보유) 진입만 막음
    open_coin_set = set(open_map.keys())

    # ---- “막혀서 못 연 포지션의 반대 신호” 를 무시하는 가드 ----
    # 이전에 MAX_COINS로 “열려고 했던 방향”이 기록돼 있고,
    # 지금은 포지션이 없으며(=청산 대상 아님),
    # 지금 들어온 신호가 그 “반대 방향”이라면 → 새 포지션 절대 열지 않고 무시
    blk = blocked_open.get(symbol_ccxt)
    if blk and symbol_ccxt not in open_coin_set and not reduce_only and BLOCKED_TTL_MIN > 0:
        want_side = "long" if will_open_long else ("short" if will_open_short else None)
        if want_side and want_side != blk.get("side"):
            return {
                "skipped": "opposite_of_blocked_would_open",
                "symbol": symbol_ccxt,
                "blocked_side": blk.get("side"),
                "incoming_open_side": want_side,
            }

    # 신규 진입 검사 (락 밖 1차)
    if (will_open_long or will_open_short) and (symbol_ccxt not in open_coin_set):
        if len(open_coin_set) >= MAX_COINS:
            # “어떤 방향으로 열려고 했는지” 기록
            blocked_open[symbol_ccxt] = {
                "side": "long" if will_open_long else "short",
                "ts": now_s(),
            }
            return {"rejected": "MAX_COINS_reached", "symbol": symbol_ccxt, "blocked_side": blocked_open[symbol_ccxt]["side"],
                    "open_symbols": sorted(list(open_coin_set))}

    # 수량 계산
    if reduce_only:
        amount = abs(cur_amt)
        if size_from_tv > 0:
            amount = min(amount, size_from_tv)
    else:
        if FORCE_EQUAL_NOTIONAL:
            eq = await get_equity_usdt()
            px = await get_price(symbol_ccxt)
            if px <= 0 or eq <= 0:
                raise HTTPException(422, detail="cannot calc notional (no price/equity)")
            notional = eq * FRACTION_PER_POSITION
            amount = notional / px
        else:
            amount = size_from_tv

    amount = clamp(amount)
    if amount <= 0:
        return {"skipped": "zero_amount", "symbol": symbol_ccxt, "reduceOnly": reduce_only}

    # 주문(동시성 가드)
    async with ORDER_LOCK:
        # 동시 도착 레이스 대비 재확인
        open_map2 = await fetch_open_positions_map()
        open_coin_set2 = set(open_map2.keys())

        if (will_open_long or will_open_short) and (symbol_ccxt not in open_coin_set2):
            if len(open_coin_set2) >= MAX_COINS:
                blocked_open[symbol_ccxt] = {"side": "long" if will_open_long else "short", "ts": now_s()}
                return {"rejected": "MAX_COINS_racing_guard", "symbol": symbol_ccxt,
                        "blocked_side": blocked_open[symbol_ccxt]["side"],
                        "open_symbols": sorted(list(open_coin_set2))}

        order = await place_order(symbol_ccxt, side, amount, reduce_only)

        # 성공적으로 포지션이 열렸다면 해당 심볼의 block 기록 제거
        if not reduce_only:
            blocked_open.pop(symbol_ccxt, None)

        return {
            "ok": True,
            "symbol": symbol_ccxt,
            "side": side,
            "reduceOnly": reduce_only,
            "amount": amount,
            "order": order,
        }

# -----------------------
# Routes
# -----------------------
@app.get("/health")
async def health():
    return {"ok": True, "ts": now_ms()}

@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if WEBHOOK_SECRET and (data.get("secret") or "") != WEBHOOK_SECRET:
        raise HTTPException(401, "Unauthorized")

    try:
        return JSONResponse(await decide_and_execute(data))
    except HTTPException as he:
        raise he
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
