import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal, Optional, Dict, Any
from trade import get_exchange, smart_route

app = FastAPI()

# Webhook secret from env (optional but recommended)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# Pydantic model for TradingView alert payload
class Alert(BaseModel):
    secret: Optional[str] = None
    symbol: str = Field(..., description="e.g. HBARUSDT.P")
    side: Literal["buy", "sell"]
    orderType: Literal["market", "limit"] = "market"
    size: float = Field(..., gt=0)
    price: Optional[float] = Field(None, gt=0)  # used only when orderType == "limit"
    # passthrough params if needed
    params: Optional[Dict[str, Any]] = None

@app.post("/webhook")
async def webhook(alert: Alert, request: Request):
    # Secret check (if WEBHOOK_SECRET is set)
    if WEBHOOK_SECRET:
        if not alert.secret or alert.secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid secret")

    try:
        ex = await get_exchange()
        result = await smart_route(ex, alert.dict())
        return {"ok": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        # make it visible in logs and as 500 to TradingView
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"ok": True}