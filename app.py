import os
import json
import logging
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from trade import get_exchange, smart_route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("router")

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        body = await request.body()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            logger.exception("Invalid JSON payload")
            return JSONResponse({"status": "error", "reason": "invalid_json"}, status_code=400)

    required = ["secret", "symbol", "side", "orderType", "size"]
    missing = [k for k in required if k not in data]
    if missing:
        return JSONResponse({"status": "error", "reason": f"missing:{','.join(missing)}"}, 400)

    # Secret check (optional): set ROUTER_SECRET to enforce
    expected = os.getenv("ROUTER_SECRET")
    if expected and data.get("secret") != expected:
        return JSONResponse({"status": "error", "reason": "unauthorized"}, 401)

    ex = await get_exchange()
    try:
        result = await smart_route(ex, data)
        return JSONResponse(result, 200)
    except Exception as e:
        logger.exception("Unhandled error in webhook")
        return JSONResponse({"status": "error", "reason": str(e)}, 500)
    finally:
        try:
            await ex.close()
        except Exception:
            logger.info("Closed client session/connector")