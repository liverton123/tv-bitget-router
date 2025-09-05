import os
import json
from decimal import Decimal
from fastapi import FastAPI, Body
import uvicorn

from trade import smart_route, get_net_position
from bitget_ccxt import make_exchange
from risk import (
    normalize_symbol,
    MAX_COINS,
    REQUIRE_INTENT_FOR_OPEN,
    PRODUCT_TYPE,
    MARGIN_COIN,
)

app = FastAPI()

# Exchange instance (CCXT, single session for the process)
ex = make_exchange(
    api_key=os.getenv("BITGET_API_KEY", ""),
    api_secret=os.getenv("BITGET_API_SECRET", ""),
    api_password=os.getenv("BITGET_API_PASSWORD", ""),
    enable_rate_limit=True,
)

@app.get("/")
def ping():
    return {"ok": True}

@app.post("/webhook")
async def webhook(payload: dict = Body(...)):
    # Normalize/validate inputs
    symbol = normalize_symbol(payload.get("symbol"))
    side = (payload.get("side") or "").lower()  # "buy" | "sell"
    order_type = (payload.get("orderType") or "market").lower()
    intent = (payload.get("intent") or "").lower()  # "open" | "dca" | "close" | ""
    raw_size = payload.get("size")

    # Hard guards
    if not symbol or side not in ("buy", "sell"):
        return {"ok": True, "ignored": "bad symbol/side"}

    try:
        size = Decimal(str(raw_size))
    except Exception:
        size = Decimal("0")

    if size <= 0:
        return {"ok": True, "ignored": "non-positive size"}

    # Current net position (signed base amount)
    net = await get_net_position(ex, symbol, PRODUCT_TYPE, MARGIN_COIN)

    # Safety: when flat, do not open unless the intent explicitly allows it
    if net == 0 and REQUIRE_INTENT_FOR_OPEN and intent not in {"open", "dca"}:
        return {"ok": True, "ignored": "flat_and_no_open_intent"}

    # Route
    result = await smart_route(
        ex=ex,
        symbol=symbol,
        side=side,
        order_type=order_type,
        size=size,
        product_type=PRODUCT_TYPE,
        margin_coin=MARGIN_COIN,
        intent=intent,               # used to disambiguate open/dca/close
    )
    return {"ok": True, "result": result}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))