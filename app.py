# app.py
import os
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
from cachetools import TTLCache
import ccxt

# ===== Settings =====
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
WEBHOOK_KEY = os.getenv("WEBHOOK_KEY", "")              # TV와 동일값
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "10"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

app = FastAPI()

# CCXT (Bitget USDT-M Perp)
exchange = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_API_PASSWORD,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},
})
try:
    exchange.load_markets()
except Exception as e:
    print("load_markets failed:", e)

# 중복 알림 방지 (5분 TTL)
dedupe = TTLCache(maxsize=2000, ttl=60*5)

class TVOrder(BaseModel):
    symbol_tv: Optional[str] = None
    symbol: Optional[str] = None
    action: Optional[str] = None   # buy/sell/sellshort/buy_to_cover/exit_*
    qty: Optional[float] = None
    price: Optional[float] = None
    order_id: Optional[str] = None
    id: Optional[str] = None
    ts: Optional[str] = None
    tag: Optional[str] = None
    webhook_key: Optional[str] = None  # 바디에 키 넣어서 검증

def map_symbol(symbol_tv_or_plain: str) -> str:
    s = symbol_tv_or_plain
    if ":" in s:
        s = s.split(":")[-1]
    s = s.replace(".P", "")
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    if s.endswith("USD"):
        base = s[:-3]
        return f"{base}/USD:USD"
    return s

async def fetch_open_symbols() -> set:
    try:
        positions = exchange.fetch_positions()
    except Exception:
        positions = []
    opened = set()
    for p in positions or []:
        sym = p.get("symbol")
        size = p.get("contracts") or p.get("size") or 0
        if sym and float(size or 0) != 0:
            opened.add(sym)
    return opened

async def fetch_position_size(symbol: str) -> float:
    try:
        positions = exchange.fetch_positions([symbol])
    except Exception:
        positions = exchange.fetch_positions()
    for p in positions or []:
        if p.get("symbol") == symbol:
            size = p.get("contracts") or p.get("size") or 0
            try:
                return abs(float(size or 0))
            except Exception:
                return 0.0
    return 0.0

ENTRY_ACTIONS = {"buy": "buy", "sellshort": "sell", "sell_short": "sell"}
EXIT_ACTIONS  = {
    "sell": "sell", "buy_to_cover": "buy",
    "exit_long_stop": "sell", "exit_short_stop": "buy",
    "exit_long_channel": "sell", "exit_short_channel": "buy"
}

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def webhook(order: TVOrder, x_webhook_key: Optional[str] = Header(default=None)):
    # 1) webhook 키 검증(헤더 또는 바디)
    provided_key = x_webhook_key or order.webhook_key
    if WEBHOOK_KEY and provided_key != WEBHOOK_KEY:
        raise HTTPException(401, "invalid webhook key")

    # 2) 중복 방지
    oid = order.order_id or order.id or f"{(order.symbol_tv or order.symbol)}:{order.ts}"
    if not oid:
        raise HTTPException(400, "missing order id")
    if oid in dedupe:
        return {"status": "duplicate_ignored"}
    dedupe[oid] = True

    # 3) 심볼/액션 해석
    raw_symbol = order.symbol_tv or order.symbol
    if not raw_symbol:
        raise HTTPException(400, "missing symbol")
    symbol = map_symbol(raw_symbol)

    act = (order.action or "").lower()
    if act in ENTRY_ACTIONS:
        side = ENTRY_ACTIONS[act]
        reduce_only = False
    elif act in EXIT_ACTIONS:
        side = EXIT_ACTIONS[act]
        reduce_only = True
    else:
        return {"status": "ignored", "reason": f"unknown action {order.action}"}

    # 4) 신규 엔트리만 "동시 포지션 10개" 제한
    if not reduce_only and side in ("buy", "sell"):
        open_symbols = await fetch_open_symbols()
        already_open = symbol in open_symbols
        if not already_open and len(open_symbols) >= MAX_POSITIONS:
            return {"status": "blocked", "reason": "max positions reached", "open_symbols": list(open_symbols)}

    # 5) 수량
    amount = float(order.qty or 0)
    if reduce_only:
        if amount <= 0:
            amount = await fetch_position_size(symbol)  # 전량 RO 청산
    else:
        if amount <= 0:
            raise HTTPException(400, "qty required for entries")

    if DRY_RUN:
        return {"status": "ok(dry)", "symbol": symbol, "side": side, "amount": amount, "reduceOnly": reduce_only}

    # 6) 시장가 주문
    params: Dict[str, Any] = {"reduceOnly": reduce_only}
    try:
        result = exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
    except Exception as e:
        raise HTTPException(500, f"order failed: {e}")

    return {"status": "ok", "exchange_result": result}
