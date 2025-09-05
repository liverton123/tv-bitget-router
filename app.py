import os
from fastapi import FastAPI, HTTPException, Request
import uvicorn
import ccxt.async_support as ccxt

from trade import (
    normalize_symbol,
    smart_route,
    load_env,
)

app = FastAPI()

ENV = load_env()
ex = ccxt.bitget({
    "apiKey": ENV["BITGET_API_KEY"],
    "secret": ENV["BITGET_API_SECRET"],
    "password": ENV["BITGET_API_PASSWORD"],
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",
        "productType": ENV["BITGET_PRODUCT_TYPE"],  # umcbl(usdt-m), dmcbl(usdc-m) etc.
    },
})

@app.on_event("startup")
async def _startup():
    await ex.load_markets()

@app.on_event("shutdown")
async def _shutdown():
    try:
        await ex.close()
    except Exception:
        pass

@app.post("/webhook")
async def webhook(payload: dict):
    try:
        if ENV["WEBHOOK_SECRET"] and payload.get("secret") != ENV["WEBHOOK_SECRET"]:
            raise HTTPException(status_code=401, detail="unauthorized")

        raw_symbol = payload.get("symbol")
        if not raw_symbol:
            raise HTTPException(status_code=400, detail="symbol is required")

        symbol = normalize_symbol(raw_symbol)
        await ex.load_markets()
        if symbol not in ex.markets:
            raise HTTPException(status_code=400, detail=f"unsupported symbol: {raw_symbol} -> {symbol}")

        side = str(payload.get("side", "")).lower()  # "buy" | "sell"
        order_type = str(payload.get("orderType", "market")).lower()  # force market later anyway
        intent = (payload.get("intent") or "").lower()  # "open" | "add" | "reduce" | "close" | ""
        try:
            size = float(payload.get("size", 0) or 0.0)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid size")

        result = await smart_route(
            ex=ex,
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            intent=intent,
            env=ENV,
        )
        return {"ok": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad request: {e}")

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")