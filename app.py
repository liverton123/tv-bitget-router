import os
from fastapi import FastAPI, Request
from trade import smart_route

app = FastAPI()

SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    if data.get("secret") != SECRET:
        return {"status": "error", "message": "invalid secret"}

    symbol = data.get("symbol")
    side = data.get("side")
    order_type = data.get("orderType", "market")
    size = float(data.get("size", 0))
    intent = data.get("intent", "auto")

    product_type = "UMCBL"  # Bitget USDT-M perpetual

    await smart_route(symbol, side, order_type, size, intent, product_type)
    return {"status": "success"}