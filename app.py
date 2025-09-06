# app.py
import os
import logging
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, validator
import uvicorn

from trade import smart_route

log = logging.getLogger("router.app")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI()

# --- env ---
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # <- required by smart_route

# --- models (Pydantic v2) ---
class Alert(BaseModel):
    secret: str
    symbol: str = Field(..., min_length=3)
    side: str = Field(..., pattern=r"(?i)^(buy|sell)$")
    orderType: str = Field(..., pattern=r"(?i)^(market|limit)$")
    size: float
    intent: str | None = Field(None, pattern=r"(?i)^(open|add|close|auto)$")

    @validator("symbol")
    def normalize_symbol(cls, v: str) -> str:
        # Accept "W/USDT", "WUSDT", "WUSDT:USDT" -> normalize to "WUSDT:USDT"
        s = v.replace("/", "").upper()
        if not s.endswith("USDT"):
            raise ValueError("symbol must be *USDT market")
        return f"{s}:USDT"

    @validator("side")
    def norm_side(cls, v: str) -> str:
        return v.lower()

    @validator("orderType")
    def norm_ot(cls, v: str) -> str:
        return v.lower()

    @validator("intent")
    def norm_intent(cls, v: str | None) -> str | None:
        return v.lower() if v else None


@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        alert = Alert(**body)
    except Exception as e:
        log.error("ALERT parse error: %s", e)
        raise HTTPException(status_code=400, detail=f"Bad alert: {e}")

    if WEBHOOK_SECRET and alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    symbol = alert.symbol           # e.g. WUSDT:USDT
    side = alert.side               # "buy" | "sell"
    order_type = alert.orderType    # "market" | "limit"
    size = float(alert.size)
    intent = alert.intent or "auto" # "open" | "add" | "close" | "auto"

    log.info("incoming => %s %s size=%s intent=%s", side, symbol, size, intent)

    # Disallow shorts if configured
    if not ALLOW_SHORTS and side == "sell" and intent in ("open", "auto"):
        log.info("shorts disabled: ignoring")
        return {"ok": True, "skipped": "shorts_disabled"}

    try:
        result = await smart_route(
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            intent=intent,
            reenter_on_opposite=REENTER_ON_OPPOSITE,
            product_type=PRODUCT_TYPE,  # <-- critical fix
        )
        return {"ok": True, "result": result}
    except Exception as e:
        log.error("unhandled", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))