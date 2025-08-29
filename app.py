# app.py  (full, compatible)
import os
from typing import Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel
from cachetools import TTLCache
import ccxt

# ===== Settings =====
BITGET_API_KEY     = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET  = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD= os.getenv("BITGET_API_PASSWORD", "")
# 환경변수 이름은 WEBHOOK_SECRET을 표준으로, 기존 WEBHOOK_KEY도 fallback 허용
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET") or os.getenv("WEBHOOK_KEY", "")
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS", "10"))
DRY_RUN            = os.getenv("DRY_RUN", "true").lower() == "true"

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

# === Incoming payload model ===
class TVOrder(BaseModel):
    # 심볼/행동 필드: 여러 이름을 모두 허용
    symbol_tv: Optional[str] = None
    symbol:    Optional[str] = None          # "DOGEUSDT_UMCBL" 또는 "DOGEUSDT.P"
    action:    Optional[str] = None          # buy/sell/sellshort/... (전략)
    side:      Optional[str] = None          # buy/sell (수동 테스트)
    qty:       Optional[float] = None
    size:      Optional[float] = None        # TradingView 수동 JSON에서 흔히 사용
    price:     Optional[float] = None
    order_id:  Optional[str] = None
    id:        Optional[str] = None
    ts:        Optional[str] = None
    tag:       Optional[str] = None

    # 인증 키: 여러 이름 허용
    webhook_key: Optional[str] = None
    key:         Optional[str] = None
    secret:      Optional[str] = None

    # 기타
    reduceOnly:  Optional[bool] = None
    orderType:   Optional[str] = None

# === 심볼 보정 ===
def map_symbol(s: str) -> str:
    """
    다음 입력을 모두 CCXT 통일 심볼로 변환:
      - "DOGEUSDT.P", "BTCUSDT.P" (TradingView 선물 심볼)
      - "DOGEUSDT_UMCBL", "BTCUSDT_UMCBL" (Bitget API 심볼)
      - "DOGEUSDT", "BTCUSD" 등 단순 문자열
      - 이미 통일 심볼인 "DOGE/USDT:USDT"는 그대로
    """
    if not s:
        return s

    # 이미 CCXT 통일 심볼 형태면 그대로
    if "/" in s and ":" in s:
        return s

    s0 = s.strip()

    # TV 심볼 정리
    if ":" in s0:
        s0 = s0.split(":")[-1]           # "BINANCE:BTCUSDT.P" -> "BTCUSDT.P"
    if s0.endswith(".P"):
        s0 = s0[:-2]                      # "BTCUSDT.P" -> "BTCUSDT"

    # Bitget API 심볼 → 통일 심볼
    if s0.endswith("_UMCBL") or s0.endswith("_CMCBL"):
        pair = s0.split("_")[0]           # "DOGEUSDT_UMCBL" -> "DOGEUSDT"
        if pair.endswith("USDT"):
            base, quote = pair[:-4], "USDT"
            return f"{base}/USDT:USDT"
        if pair.endswith("USD"):
            base, quote = pair[:-3], "USD"
            return f"{base}/USD:USD"
        # 예외: 변환 불가 시 원문 반환 (ccxt가 처리 못하면 이후 에러)
        return s

    # 단순 문자열 처리
    if s0.endswith("USDT"):
        base = s0[:-4]
        return f"{base}/USDT:USDT"
    if s0.endswith("USD"):
        base = s0[:-3]
        return f"{base}/USD:USD"

    return s0

async def fetch_open_symbols() -> set:
    try:
        positions = exchange.fetch_positions()
    except Exception:
        positions = []
    opened = set()
    for p in positions or []:
        sym  = p.get("symbol")
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

# 전략/수동 액션 매핑
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
    # 1) 인증 키: 헤더 또는 바디(secret/key/webhook_key) 허용
    provided_key = x_webhook_key or order.secret or order.key or order.webhook_key
    if WEBHOOK_SECRET and provided_key != WEBHOOK_SECRET:
        raise HTTPException(401, "invalid webhook key")

    # 2) 중복 방지
    oid = order.order_id or order.id or f"{(order.symbol_tv or order.symbol)}:{order.ts}"
    if not oid:
        raise HTTPException(400, "missing order id")
    if oid in dedupe:
        return {"status": "duplicate_ignored"}
    dedupe[oid] = True

    # 3) 심볼 보정
    raw_symbol = order.symbol_tv or order.symbol
    if not raw_symbol:
        raise HTTPException(400, "missing symbol")
    symbol = map_symbol(raw_symbol)

    # 4) 액션/사이드 해석
    reduce_only = bool(order.reduceOnly)
    side: Optional[str] = None

    if order.action:  # 전략용 action 우선
        act = (order.action or "").lower()
        if act in ENTRY_ACTIONS:
            side = ENTRY_ACTIONS[act]
            reduce_only = False
        elif act in EXIT_ACTIONS:
            side = EXIT_ACTIONS[act]
            reduce_only = True
        else:
            return {"status": "ignored", "reason": f"unknown action {order.action}"}
    else:
        # 수동 테스트: side 만으로 주문 (reduceOnly는 입력 시 반영)
        side = (order.side or "").lower()
        if side not in ("buy", "sell"):
            raise HTTPException(400, "missing/invalid side")

    # 5) 신규 엔트리일 때만 "동시 포지션 10개" 제한
    if not reduce_only and side in ("buy", "sell"):
        open_symbols = await fetch_open_symbols()
        already_open = symbol in open_symbols
        if not already_open and len(open_symbols) >= MAX_POSITIONS:
            return {"status": "blocked", "reason": "max positions reached", "open_symbols": list(open_symbols)}

    # 6) 수량
    amount = None
    if order.qty is not None:
        amount = float(order.qty)
    elif order.size is not None:
        amount = float(order.size)

    if reduce_only:
        if not amount or amount <= 0:
            amount = await fetch_position_size(symbol)  # 전량 RO 청산
    else:
        if not amount or amount <= 0:
            raise HTTPException(400, "qty/size required for entries")

    if DRY_RUN:
        return {"status": "ok(dry)", "symbol": symbol, "side": side, "amount": amount, "reduceOnly": reduce_only}

    # 7) 시장가 주문
    params: Dict[str, Any] = {"reduceOnly": reduce_only}
    try:
        result = exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
    except Exception as e:
        raise HTTPException(500, f"order failed: {e}")

    return {"status": "ok", "exchange_result": result}
