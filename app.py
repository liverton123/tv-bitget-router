from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import os

from trade import (
    get_exchange,
    close_exchange,
    smart_route,
)

app = FastAPI()

class Alert(BaseModel):
    secret: str
    symbol: str = Field(..., pattern=r"^[A-Z0-9/:\-_.]+$")
    side: str = Field(..., pattern=r"^(?i)(buy|sell|close)$")
    orderType: str = Field(..., pattern=r"^(?i)(market|limit)$")
    size: float
    intent: str | None = Field(default=None, pattern=r"^(?i)(open|close|scale_in|scale_out|auto)$")
    product_type: str | None = None  # e.g. umcbl / dmcbl (case-insensitive)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"

@app.post("/webhook")
async def webhook(alert: Alert):
    if WEBHOOK_SECRET and alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    ex = await get_exchange()
    try:
        product_type = alert.product_type or os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
        result = await smart_route(
            ex=ex,
            symbol=alert.symbol,
            side=alert.side,
            order_type=alert.orderType,
            size=alert.size,
            intent=alert.intent,
            reenter_on_opposite=REENTER_ON_OPPOSITE,
            product_type=product_type,
            require_intent_for_open=REQUIRE_INTENT_FOR_OPEN,
        )
        return {"ok": True, "result": result}
    finally:
        await close_exchange(ex)

@app.get("/")
async def root():
    return {"status": "ok"}