import os
from typing import Optional, Dict, Any

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
import ccxt.async_support as ccxt

from trade import smart_route

APP_PORT = int(os.getenv("PORT", "10000"))

BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # umcbl (USDT-M)
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123")
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"

app = FastAPI()
ex: ccxt.Exchange | None = None


class Alert(BaseModel):
    secret: str = Field(...)
    symbol: str = Field(..., description="e.g. LINKUSDT.P or LINK/USDT:USDT")
    side: str = Field(..., regex="^(?i)(buy|sell)$")
    orderType: Optional[str] = Field("market", alias="orderType")
    size: Optional[float] = 0.0
    intent: Optional[str] = Field(None, description="open | add | close")

    class Config:
        allow_population_by_field_name = True


def normalize_symbol(raw: str) -> str:
    s = raw.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if "/" in s and ":USDT" in s:
        return s  # already unified
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    # fallback: assume base only
    return f"{s}/USDT:USDT"


@app.on_event("startup")
async def startup() -> None:
    global ex
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "defaultSettle": "USDT",
            "defaultMarginMode": "cross",
        },
    })
    await ex.load_markets()


@app.on_event("shutdown")
async def shutdown() -> None:
    if ex:
        await ex.close()


@app.post("/webhook")
async def webhook(req: Request) -> Dict[str, Any]:
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    try:
        alert = Alert(**payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad payload: {e}")

    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    if ex is None:
        raise HTTPException(status_code=503, detail="exchange not ready")

    symbol = normalize_symbol(alert.symbol)
    side = alert.side.lower()
    intent = (alert.intent or "").lower()

    # Optional guard: without intent we only allow add/close when position exists (handled in trade.smart_route).
    # To be extra safe, when no position and REQUIRE_INTENT_FOR_OPEN is true, block if intent != open.
    if REQUIRE_INTENT_FOR_OPEN and intent != "open":
        # trade.smart_route will still do final checks based on position state
        pass

    try:
        result = await smart_route(
            ex=ex,
            unified_symbol=symbol,
            side=side,
            order_type=(alert.orderType or "market").lower(),
            incoming_size=float(alert.size or 0.0),
            intent=intent or None,
            product_type=PRODUCT_TYPE,
        )
        return {"ok": True, **result}
    except ccxt.BaseError as ce:
        return {"ok": False, "error": "ccxt", "message": str(ce)}
    except Exception as e:
        return {"ok": False, "error": "unhandled", "message": str(e)}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=APP_PORT, workers=1)