import os
import time
from typing import Literal, Optional

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, field_validator

from trade import BitgetTrader
from symbol_map import normalize_tv_symbol, is_supported_market

app = FastAPI(title="TV → Bitget Router", version="1.0.0")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ALLOWED_SYMBOLS = set(s.strip() for s in os.getenv("ALLOWED_SYMBOLS", "").split(",") if s.strip())

# --- 단순 중복 방지(몇 초 내 동일 payload drop) ---
_seen = {}
DEDUP_WINDOW_SEC = 6.0

class TVPayload(BaseModel):
    secret: str
    symbol: str      # e.g. "ETHUSDT.P"
    side: Literal["buy","sell"]
    orderType: Literal["market","limit"] = "market"
    size: float

    @field_validator("size")
    @classmethod
    def _size_pos(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("size must be positive")
        return v

trader = BitgetTrader()  # ccxt 초기화 포함

@app.get("/health")
async def health():
    return {"ok": True, "ts": int(time.time())}

@app.post("/webhook")
async def webhook(req: Request, payload: TVPayload):
    # 1) secret 검증
    if WEBHOOK_SECRET and payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    # 2) 간단 중복방지
    sig = (payload.symbol, payload.side, round(payload.size, 8), payload.orderType)
    now = time.monotonic()
    if sig in _seen and now - _seen[sig] < DEDUP_WINDOW_SEC:
        return {"status": "skipped-duplicate"}
    _seen[sig] = now

    # 3) 화이트리스트(옵션)
    if ALLOWED_SYMBOLS and payload.symbol not in ALLOWED_SYMBOLS:
        return {"status": "ignored", "reason": "symbol not allowed"}

    # 4) TV 심볼 → Bitget 선물 심볼
    ccxt_symbol = normalize_tv_symbol(payload.symbol)  # "ETH/USDT:USDT" 등
    if not ccxt_symbol:
        raise HTTPException(status_code=400, detail=f"unsupported tv symbol: {payload.symbol}")

    # 5) 시장 지원여부 확인
    supported = await is_supported_market(trader.exchange, ccxt_symbol)
    if not supported:
        # 마켓이 Bitget에 없음
        return {"status": "ignored", "reason": f"unsupported market {ccxt_symbol}"}

    # 6) 주문 라우팅(규칙: 반대 신호 = reduce-only 청산, flip 금지)
    try:
        result = await trader.route_order(
            ccxt_symbol=ccxt_symbol,
            tv_side=payload.side,
            size=payload.size,
            order_type=payload.orderType,
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        # FastAPI가 stacktrace까지 반환하지 않도록 메시지 정리
        raise HTTPException(status_code=500, detail=f"exchange_error: {e}") from None