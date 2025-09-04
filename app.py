import os
import json
import logging
import traceback
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, validator

from symbol_map import tv_to_ccxt
from trade import build_exchange, smart_route, PRODUCT_TYPE, DRY_RUN

# ===== 로깅 =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("router.app")

app = FastAPI(title="tv-bitget-router", version="2.0")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

# ===== Pydantic 모델 =====
class OrderIn(BaseModel):
    secret: str
    symbol: str
    side: str
    orderType: str = Field(default="market")
    size: float

    @validator("side")
    def v_side(cls, v: str):
        s = (v or "").strip().lower()
        if s not in ("buy", "sell"):
            raise ValueError("side must be buy/sell")
        return s

    @validator("orderType")
    def v_type(cls, v: str):
        if (v or "").lower() != "market":
            raise ValueError("orderType must be market")
        return v


@app.get("/health")
async def health():
    return {
        "ok": True,
        "productType": PRODUCT_TYPE,
        "dryRun": DRY_RUN,
    }


@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        body = await req.body()
        log.error(f"[WEBHOOK] invalid json body={body!r}")
        raise HTTPException(status_code=400, detail="invalid json")

    # 로우 로깅(민감정보 제외)
    safe_log = {k: payload.get(k) for k in ("secret", "symbol", "side", "orderType", "size")}
    safe_log["secret"] = "***"
    log.info(f"[WEBHOOK] recv={safe_log}")

    # 검증
    try:
        data = OrderIn(**payload)
    except Exception as e:
        log.error(f"[VALIDATE] {e}")
        raise HTTPException(status_code=400, detail=f"bad payload: {e}")

    if data.secret != WEBHOOK_SECRET:
        log.error("[AUTH] secret mismatch")
        raise HTTPException(status_code=403, detail="bad secret")

    ccxt_symbol = tv_to_ccxt(data.symbol)
    log.info(f"[SYMBOL] {data.symbol} -> {ccxt_symbol} (productType={PRODUCT_TYPE})")

    # Bitget 연결
    try:
        ex = build_exchange()
    except Exception as e:
        log.error(f"[CONFIG] {e}")
        raise HTTPException(status_code=500, detail=f"bitget config error: {e}")

    # 주문 라우트
    try:
        res = await smart_route(
            ex, symbol=ccxt_symbol, side=data.side, size=float(data.size)
        )
        log.info(f"[DONE] orders={res}")
        return {"ok": True, "orders": res, "dryRun": DRY_RUN}
    except Exception as e:
        # Bitget/ccxt 에러 메시지를 그대로 내려주어 디버깅 가능하게
        log.error(f"[CCXT_ERROR] {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"{e}")