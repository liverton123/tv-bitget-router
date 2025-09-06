from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
import os

from trade import smart_route, get_exchange, close_exchange  # get_exchange/close_exchange는 trade.py에 추가

app = FastAPI()

class Alert(BaseModel):
    secret: str
    symbol: str = Field(..., pattern=r"^[A-Z0-9/:\-_.]+$")
    side: str = Field(..., pattern=r"^(?i)(buy|sell|close)$")
    orderType: str = Field(..., pattern=r"^(?i)(market|limit)$")
    size: float
    intent: str | None = Field(default=None, pattern=r"^(?i)(auto|open|close|scale_in|scale_out)$")
    product_type: str | None = None  # e.g. umcbl / dmcbl

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# re-enter behavior flag from env
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"

@app.post("/webhook")
async def webhook(alert: Alert, request: Request):
    if WEBHOOK_SECRET and alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    ex = await get_exchange()  # make exchange
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
        )
        return {"ok": True, "result": result}
    finally:
        await close_exchange(ex)  # always close

@app.get("/")
async def root():
    return {"status": "ok"}