import os
from typing import Optional, Literal
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from trade import smart_route, get_exchange

app = FastAPI()

def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
REENTER_ON_OPPOSITE = str_to_bool(os.getenv("REENTER_ON_OPPOSITE"), False)
DEFAULT_PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "USDT-FUTURES")

class Alert(BaseModel):
    secret: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    orderType: Literal["market", "limit"] = "market"
    size: float = Field(..., gt=0)
    intent: Optional[Literal["entry", "scale", "close", "auto"]] = "auto"
    product_type: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)

@app.post("/webhook")
async def webhook(alert: Alert, request: Request):
    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    product_type = alert.product_type or DEFAULT_PRODUCT_TYPE
    ex = await get_exchange()
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
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await ex.close()

@app.get("/health")
async def health():
    return {"ok": True}