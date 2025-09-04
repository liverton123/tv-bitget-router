# app.py
import os
import json
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from trade import (
    normalize_symbol,
    smart_route,
    get_exchange_singleton,
)

app = FastAPI()
log = logging.getLogger("router.app")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.body()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # 1) 시크릿 체크
        if str(data.get("secret", "")) != str(SECRET):
            raise HTTPException(status_code=401, detail="Bad secret")

        raw_symbol = str(data.get("symbol", "")).strip()
        side = str(data.get("side", "")).strip().lower()  # buy/sell
        order_type = str(data.get("orderType", "market")).strip().lower()
        size = data.get("size", None)  # float or int(contracts); optional

        if not raw_symbol or side not in ("buy", "sell"):
            raise HTTPException(status_code=400, detail="Missing symbol/side")

        symbol = normalize_symbol(raw_symbol)

        log.info("[ROUTER] incoming => %s %s size=%s", side, symbol, size)

        # 2) 거래 라우팅
        ex = await get_exchange_singleton()
        result = await smart_route(ex, symbol, side, order_type, size)

        return JSONResponse({"ok": True, "result": result})

    except HTTPException as e:
        log.error("[ROUTER] HTTP %s: %s", e.status_code, e.detail)
        raise
    except Exception as e:
        log.exception("[ROUTER] unhandled")
        raise HTTPException(status_code=500, detail=str(e))