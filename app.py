# app.py — TV → Bitget (USDT-M Perp) Auto Trader
# v3.1 (no TTL; entry/add/exit by live position; safe re-open rule)

import os, json, time, asyncio
from typing import Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

import ccxt.async_support as ccxt  # async ccxt

# ─────────────────────────
# ENV
# ─────────────────────────
API_KEY  = os.getenv("BITGET_API_KEY", "")
API_PW   = os.getenv("BITGET_API_PASSWORD", "")
API_SEC  = os.getenv("BITGET_API_SECRET", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

DRY_RUN  = os.getenv("DRY_RUN", "false").lower() == "true"

# 숏 신규 진입 허용 여부
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

# 균일 노출(USDT 기준) 진입: 시드 × FRACTION_PER_POSITION
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 1회 진입 = 시드 5%

# 동시에 보유할 수 있는 서로 다른 코인(심볼) 수
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# 동시성/중복 가드
ORDER_LOCK = asyncio.Lock()
last_seen_ts: Dict[str, float] = {}      # {symbol: last_ts} — 0.8s 이내 중복 알림 무시

# 상태 기억(간단/안전)
last_closed_at: Dict[str, float] = {}    # {ccxt_symbol: unix_ts} — 우리가 reduceOnly로 실제 청산한 마지막 시각
blocked_since_attempt: Dict[str, bool] = {}  # {ccxt_symbol: True} — 슬롯 초과로 신규 진입을 거절한 적이 있으면 True

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

def f8(v: float) -> float:
    try:
        return float(f"{float(v):.8f}")
    except Exception:
        return 0.0

# ─────────────────────────
# FastAPI & CCXT
# ─────────────────────────
app = FastAPI(title="TV→Bitget Router", version="3.1")

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
# State readers
# ─────────────────────────
async def fetch_open_positions_map() -> Dict[str, Dict[str, Any]]:
    """
    returns: { "DOGE/USDT:USDT": {"amount": +123.0(long)/-123.0(short), "raw": {...}}, ... }
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
# Orders
# ─────────────────────────
async def place_order(symbol_ccxt: str, side: str, amount: float, reduce_only: bool) -> Dict[str, Any]:
    if DRY_RUN:
        return {"dry_run": True, "symbol": symbol_ccxt, "side": side, "amount": amount, "reduceOnly": reduce_only}
    params = {"reduceOnly": reduce_only}
    return await exchange.create_order(symbol=symbol_ccxt, type="market", side=side, amount=amount, price=None, params=params)

# ─────────────────────────
# Core Decision
# ─────────────────────────
async def decide_and_execute(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    TV payload(예):
      {"secret":"...","symbol":"DOGEUSDT.P","side":"buy","orderType":"market","size":1266.5}

    규칙(간단/안전):
    - 현재 포지션으로 buy/sell을 자동 해석:
        미보유+buy → 롱 신규 / 미보유+sell → 숏 신규(ALLOW_SHORTS 필요)
        롱 보유+sell → 롱 청산(reduceOnly)
        숏 보유+buy → 숏 청산(reduceOnly)
        롱 보유+buy, 숏 보유+sell → 물타기(추가 진입)
    - 신규 진입은 아래에서만 허용:
        (A) 해당 심볼을 우리가 과거에 reduceOnly로 '실제 청산' 한 기록이 존재하거나
        (B) 그 심볼이 지금까지 '신규 진입 시도 후 거절(blocked_since_attempt)' 된 적이 전혀 없는 '완전 첫 진입'
      → 이렇게 하면 '슬롯 초과로 놓쳤던 물타기 신호'가 나중에 새 진입으로 열리는 사고를 방지.
    - MAX_COINS는 '새 심볼 신규 진입'에만 적용(물타기/청산은 제한 없음).
    """
    # 보안키
    sec = (payload.get("secret") or "").strip()
    if WEBHOOK_SECRET and sec != WEBHOOK_SECRET:
        raise HTTPException(401, "Unauthorized")

    tv_symbol = (payload.get("symbol") or "").strip()
    side = (payload.get("side") or "").lower().strip()
    size_from_tv = abs(float(payload.get("size", 0) or 0.0))

    if side not in ("buy", "sell"):
        raise HTTPException(400, f"invalid side: {side}")

    symbol_ccxt = tv_to_ccxt_symbol(tv_symbol)

    # 중복 가드(0.8s)
    tnow = now_s()
    if tnow - last_seen_ts.get(symbol_ccxt, 0) < 0.8:
        return {"skipped": "duplicate_guard", "symbol": symbol_ccxt}
    last_seen_ts[symbol_ccxt] = tnow

    # 현재 상태
    open_map = await fetch_open_positions_map()
    open_coins = set(open_map.keys())
    pos_amt = float(open_map.get(symbol_ccxt, {}).get("amount", 0.0))
    have_long = pos_amt > 0
    have_short = pos_amt < 0
    have_none = pos_amt == 0

    # 자동 판정
    reduce_only = False
    will_open_new_long = False
    will_open_new_short = False
    if side == "buy":
        if have_short:
            reduce_only = True                     # 숏 청산
        elif have_none:
            will_open_new_long = True              # 신규 롱
        else:
            pass                                   # 롱 물타기
    else:  # sell
        if have_long:
            reduce_only = True                     # 롱 청산
        elif have_none:
            will_open_new_short = True             # 신규 숏
        else:
            pass                                   # 숏 물타기

    # 숏 신규 허용 여부
    if will_open_new_short and not ALLOW_SHORTS:
        return {"skipped": "short_not_allowed", "symbol": symbol_ccxt}

    # ── 신규 진입 허용 조건(안전 규칙)
    #  - 우리가 '실제로 reduceOnly로 닫은 적이 있는(symbol in last_closed_at)' 이거나
    #  - '아직 한 번도 슬롯 초과로 거절된 적이 없는(= 완전 첫 진입)' 경우에만 신규 오픈 허용
    #  - 이전에 슬롯 초과로 거절된 적(blocked_since_attempt=True)이 있다면, 이후 청산 이벤트가 발생해 last_closed_at 기록이 생길 때까지 신규 오픈 금지
    def allow_new_open(sym: str) -> bool:
        if sym in last_closed_at:
            return True
        # 완전 첫 진입(= 과거에 거절 기록조차 없음)일 때만 허용
        return not blocked_since_attempt.get(sym, False)

    # 신규 진입 1차 판단
    if have_none and (will_open_new_long or will_open_new_short):
        if not allow_new_open(symbol_ccxt):
            return {"rejected": "open_requires_prior_close_or_initial", "symbol": symbol_ccxt}

    # ── MAX_COINS 1차 검사(새 심볼 신규 진입만)
    if have_none and (will_open_new_long or will_open_new_short):
        if len(open_coins) >= MAX_COINS and symbol_ccxt not in open_coins:
            blocked_since_attempt[symbol_ccxt] = True
            return {"rejected": "MAX_COINS_reached", "symbol": symbol_ccxt, "open_symbols": sorted(list(open_coins))}

    # 수량 계산
    if reduce_only:
        amount = abs(pos_amt)
        if size_from_tv > 0:
            amount = min(amount, size_from_tv)     # 트뷰가 더 작은 청산 수량을 보냈다면 그만큼만
    else:
        if FORCE_EQUAL_NOTIONAL:
            equity = await get_equity_usdt()
            price = await get_price(symbol_ccxt)
            if equity <= 0 or price <= 0:
                raise HTTPException(422, "cannot calc notional (no equity/price)")
            notional = equity * max(min(FRACTION_PER_POSITION, 1.0), 0.0)
            amount = notional / price
        else:
            amount = size_from_tv

    amount = f8(max(0.0, amount))
    if amount <= 0:
        return {"skipped": "zero_amount", "symbol": symbol_ccxt, "reduceOnly": reduce_only}

    # ── 주문 (락으로 레이스 방지 + MAX_COINS 재확인)
    async with ORDER_LOCK:
        # 상태 재확인
        open_map2 = await fetch_open_positions_map()
        open_coins2 = set(open_map2.keys())
        pos_amt2 = float(open_map2.get(symbol_ccxt, {}).get("amount", 0.0))
        none2 = pos_amt2 == 0

        # 신규 진입 재확인(규칙 + MAX_COINS)
        if none2 and (will_open_new_long or will_open_new_short):
            if not allow_new_open(symbol_ccxt):
                return {"rejected": "open_requires_prior_close_or_initial", "symbol": symbol_ccxt}
            if symbol_ccxt not in open_coins2 and len(open_coins2) >= MAX_COINS:
                blocked_since_attempt[symbol_ccxt] = True
                return {"rejected": "MAX_COINS_racing_guard", "symbol": symbol_ccxt,
                        "open_symbols": sorted(list(open_coins2))}

        # 주문
        order = await place_order(symbol_ccxt, side, amount, reduce_only)

        # 상태 업데이트
        if reduce_only:
            last_closed_at[symbol_ccxt] = now_s()
        else:
            # 신규 오픈/물타기 성공 시, 해당 심볼은 더 이상 '완전 첫 진입'이 아님
            # (여기서 blocked_since_attempt 플래그는 건드리지 않음; 거절 기록은 그대로 남겨 두고,
            #  실제 청산(reduceOnly) 발생 시에만 last_closed_at이 생기므로 그때부터 정상 재진입 허용)
            pass

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
    return {"ok": True, "ts": int(now_s() * 1000)}

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
