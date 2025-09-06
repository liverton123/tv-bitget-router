import os
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from trade import smart_route

app = FastAPI()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"
REQUIRE_INTENT_FOR_ADD = os.getenv("REQUIRE_INTENT_FOR_ADD", "true").lower() == "true"
IGNORE_CLOSE_WHEN_FLAT = os.getenv("IGNORE_CLOSE_WHEN_FLAT", "true").lower() == "true"
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

class Alert(BaseModel):
    secret: str
    symbol: str
    side: str = Field(..., pattern="^(?i)(buy|sell)$")
    orderType: str = Field(..., pattern="^(?i)(market|limit)$")
    size: float
    intent: Optional[str] = Field(None, pattern="^(?i)(open|add|close|flip|auto)$")

@app.post("/webhook")
async def webhook(payload: Alert, request: Request):
    if WEBHOOK_SECRET and payload.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    result = await smart_route(
        symbol=payload.symbol,
        side=payload.side.lower(),
        order_type=payload.orderType.lower(),
        size=payload.size,
        intent=(payload.intent or "auto").lower(),
        product_type=PRODUCT_TYPE,
        flags={
            "REENTER_ON_OPPOSITE": REENTER_ON_OPPOSITE,
            "REQUIRE_INTENT_FOR_OPEN": REQUIRE_INTENT_FOR_OPEN,
            "REQUIRE_INTENT_FOR_ADD": REQUIRE_INTENT_FOR_ADD,
            "IGNORE_CLOSE_WHEN_FLAT": IGNORE_CLOSE_WHEN_FLAT,
            "ALLOW_SHORTS": ALLOW_SHORTS,
        },
    )
    return {"ok": True, "result": result}