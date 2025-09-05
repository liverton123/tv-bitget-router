import os, json, logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from trade import route_signal, make_exchange, normalize_symbol

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s")
log = logging.getLogger("router.app")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
PRODUCT_TYPE   = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
MARGIN_COIN    = os.getenv("MARGIN_COIN", "USDT")
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"

app = FastAPI()

@app.get("/")
async def health():
    return {"ok": True, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN, "dryRun": DRY_RUN}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = json.loads((await request.body()).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="malformed json")

    if str(data.get("secret", "")) != str(WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="bad secret")

    raw_symbol = data.get("symbol")
    side = data.get("side")                # "buy" | "sell" | None
    action = (data.get("action") or "").lower()  # "close" | ""

    if not raw_symbol:
        raise HTTPException(status_code=400, detail="missing symbol")
    if side is not None and str(side).lower() not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="invalid side")

    symbol = normalize_symbol(raw_symbol)

    ex = await make_exchange()
    try:
        result = await route_signal(ex=ex, symbol=symbol, side=side, action=action)
        return JSONResponse({"ok": True, "result": result})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("unhandled")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try: await ex.close()
        except: pass