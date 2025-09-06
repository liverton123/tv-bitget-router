import os
import logging
from typing import Any, Dict, Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, validator

import ccxt.async_support as ccxt  # asyncio version
from trade import smart_route, normalize_symbol

log = logging.getLogger("router.app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# ---- env ----
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
BITGET_PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # USDT-M: umcbl
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"

if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSWORD):
    log.warning("Bitget API credentials are missing")

app = FastAPI()

class Webhook(BaseModel):
    secret: str
    symbol: str
    side: str
    orderType: str = Field("market", alias="orderType")
    size: Optional[float] = 0.0
    intent: Optional[str] = None  # "open" | "add" | "close"

    @validator("side")
    def v_side(cls, v: str) -> str:
        v = v.lower()
        if v not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        return v

    @validator("orderType")
    def v_ot(cls, v: str) -> str:
        if v.lower() != "market":
            raise ValueError("only market supported")
        return v.lower()

@app.on_event("startup")
async def _startup() -> None:
    pass

@app.on_event("shutdown")
async def _shutdown() -> None:
    pass

def build_exchange() -> ccxt.bitget:
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # futures
            "defaultSettle": "usdt",
            # bitget positions/fetch require productType when calling raw; ccxt handles it if defaultType swap
        },
    })
    return ex

@app.post("/webhook")
async def webhook(req: Request) -> Dict[str, Any]:
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        data = Webhook(**payload)
    except Exception as e:
        log.info("payload validation error: %s", e)
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    if WEBHOOK_SECRET and data.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # normalize incoming symbol to ccxt unified
    unified_symbol = normalize_symbol(data.symbol)

    # block accidental opens if the alert wasn't explicit
    if REQUIRE_INTENT_FOR_OPEN and (data.intent or "").lower() not in ("open", "add", "close"):
        log.info("[ROUTER] ignored: missing/unknown intent for %s", unified_symbol)
        return {"status": "ignored", "reason": "missing/unknown intent"}

    intent = (data.intent or "").lower() or "open"

    ex = build_exchange()
    try:
        res = await smart_route(
            ex=ex,
            unified_symbol=unified_symbol,
            side=data.side,
            order_type=data.orderType,
            incoming_size=float(data.size or 0),
            intent=intent,
            product_type=BITGET_PRODUCT_TYPE,
        )
        return {"status": "ok", "result": res}
    finally:
        try:
            await ex.close()
        except Exception:
            pass

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")