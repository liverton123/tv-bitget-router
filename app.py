import os
import json
import logging
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from trade import (
    make_exchange,
    normalize_symbol,
    smart_route,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
)
log = logging.getLogger("router.app")

# ---- 환경변수 ----
SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # USDT 무기한
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT")

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# sanity check (경고만)
for k, v in [
    ("BITGET_API_KEY", API_KEY),
    ("BITGET_API_SECRET", API_SECRET),
    ("BITGET_API_PASSWORD", API_PASSWORD),
]:
    if not v and not DRY_RUN:
        log.warning("[WARN] BITGET API 환경변수가 비어있습니다. (%s)", k)

app = FastAPI()


@app.get("/")
async def health():
    return {"ok": True, "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}


@app.post("/webhook")
async def webhook(request: Request):
    """
    TradingView 알림 JSON 예:
    {
      "secret": "mySecret123!",
      "symbol": "{{ticker}}",
      "side": "{{strategy.order.action}}",
      "orderType": "market",
      "size": {{strategy.order.contracts}}
    }
    """
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed JSON")

    # 1) secret
    if str(data.get("secret", "")) != str(SECRET):
        raise HTTPException(status_code=401, detail="Bad secret")

    # 2) 기초 파라미터 검증
    raw_symbol = data.get("symbol", None)
    if raw_symbol is None or (isinstance(raw_symbol, str) and raw_symbol.strip() == ""):
        raise HTTPException(status_code=400, detail="Missing symbol")

    side = str(data.get("side", "")).strip().lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="Missing/invalid side")

    order_type = str(data.get("orderType", "market")).strip().lower()
    size = data.get("size", None)
    if size is None:
        raise HTTPException(status_code=400, detail="Missing size")

    try:
        symbol = normalize_symbol(raw_symbol)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad symbol: {e}")

    # 숏 허용 제한
    if side == "sell" and not ALLOW_SHORTS:
        return JSONResponse({"ok": False, "reason": "shorts disabled"}, status_code=202)

    log.info("[ROUTER] incoming => %s %s size=%s", side, symbol, size)

    # 3) 거래 실행
    ex = await make_exchange(
        api_key=API_KEY, api_secret=API_SECRET, password=API_PASSWORD, dry_run=DRY_RUN
    )

    try:
        result = await smart_route(
            ex=ex,
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            product_type=PRODUCT_TYPE,
            margin_coin=MARGIN_COIN,
        )
        return JSONResponse({"ok": True, "result": result})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("[ROUTER] unhandled")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            await ex.close()
        except Exception:
            pass