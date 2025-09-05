import os, json, logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from trade import make_exchange, normalize_symbol, smart_route

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s")
log = logging.getLogger("router.app")

SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD")

PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
MARGIN_COIN  = os.getenv("MARGIN_COIN", "USDT")

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
DRY_RUN      = os.getenv("DRY_RUN", "false").lower() == "true"

for k, v in [("BITGET_API_KEY", API_KEY),
             ("BITGET_API_SECRET", API_SECRET),
             ("BITGET_API_PASSWORD", API_PASSWORD)]:
    if not v and not DRY_RUN:
        log.warning("[WARN] %s is empty", k)

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

    if str(data.get("secret", "")) != str(SECRET):
        raise HTTPException(status_code=401, detail="bad secret")

    raw_symbol = data.get("symbol")
    side = str(data.get("side", "")).lower()
    order_type = str(data.get("orderType", "market")).lower()
    size = data.get("size")

    if not raw_symbol:
        raise HTTPException(status_code=400, detail="missing symbol")
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="missing/invalid side")
    if size is None:
        raise HTTPException(status_code=400, detail="missing size")
    if side == "sell" and not ALLOW_SHORTS:
        return JSONResponse({"ok": False, "reason": "shorts disabled"}, status_code=202)

    symbol = normalize_symbol(raw_symbol)
    log.info("[ROUTER] incoming => %s %s size=%s", side, symbol, size)

    ex = await make_exchange(API_KEY, API_SECRET, API_PASSWORD, DRY_RUN)
    try:
        result = await smart_route(
            ex=ex, symbol=symbol, side=side, order_type=order_type, size=size,
            product_type=PRODUCT_TYPE, margin_coin=MARGIN_COIN
        )
        return JSONResponse({"ok": True, "result": result})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("[ROUTER] unhandled")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try: await ex.close()
        except: pass