import os
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from trade import smart_route, get_exchange

app = FastAPI()

# --- config ---
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
REENTER_ON_OPPOSITE = bool(int(os.getenv("REENTER_ON_OPPOSITE", "0")))  # 0/1

# Default Bitget futures product type (USDT-M perpetual/futures).
DEFAULT_PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "USDT-FUTURES")


class Alert(BaseModel):
    secret: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    orderType: Literal["market", "limit"] = "market"
    size: float = Field(..., gt=0)

    # optional; if omitted we infer automatically from current net position
    intent: Optional[Literal["entry", "scale", "close", "auto"]] = "auto"

    # allow override from TradingView; if omitted we use DEFAULT_PRODUCT_TYPE
    product_type: Optional[str] = None

    # optional price for limit orders (ignored for market)
    price: Optional[float] = Field(default=None, gt=0)


@app.post("/webhook")
async def webhook(alert: Alert, request: Request):
    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    product_type = alert.product_type or DEFAULT_PRODUCT_TYPE

    ex = await get_exchange()  # ccxt.async_support.bitget instance
    try:
        result = await smart_route(
            ex=ex,
            symbol=alert.symbol,
            side=alert.side,
            order_type=alert.orderType,
            size=alert.size,
            intent=alert.intent or "auto",
            reenter_on_opposite=REENTER_ON_OPPOSITE,
            product_type=product_type,
            price=alert.price,
        )
        return {"ok": True, "result": result}
    except HTTPException:
        # re-raise FastAPI HTTP errors unchanged
        raise
    except Exception as e:
        # bubble up a clear message but keep 500 for unexpected errors
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Bitget requires explicit close to release resources
        await ex.close()


@app.get("/health")
async def health():
    return {"ok": True}