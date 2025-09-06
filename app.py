import os
import json
import logging
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict

from trade import smart_route  # uses your existing trade.py

# ---- Logging ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("router.app")

# ---- Env ----
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123")
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"

# ---- Pydantic models (Pydantic v2) ----
class Alert(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    secret: str = Field(...)
    symbol: str = Field(..., description="e.g. BTCUSDT.P or BTC/USDT:USDT")
    side: str = Field(..., pattern="^(?i)(buy|sell)$")
    orderType: Optional[str] = Field("market", alias="orderType")
    size: Optional[float] = 0.0
    intent: Optional[str] = Field(
        None, description="open | add | close (explicit intent from strategy, optional)"
    )

# ---- FastAPI ----
app = FastAPI()


def normalize_symbol(s: str) -> str:
    """Accepts forms like LINKUSDT.P, LINK/USDT:USDT, LINKUSDT:USDT and returns CCXT market id LINK/USDT:USDT."""
    s = s.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if "/" in s and ":" in s:
        return s
    if ":" in s and "/" not in s:
        base_quote, margin = s.split(":")
        return f"{base_quote[:-4]}/{base_quote[-4:]}:{margin}"
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    return s


@app.get("/")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(req: Request) -> JSONResponse:
    try:
        payload: Any = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        alert = Alert.model_validate(payload if isinstance(payload, dict) else json.loads(payload))
    except Exception as e:
        log.exception("Alert validation failed")
        raise HTTPException(status_code=400, detail=f"Validation error: {e}")

    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    symbol = normalize_symbol(alert.symbol)
    side = alert.side.lower()  # 'buy' | 'sell'
    order_type = (alert.orderType or "market").lower()
    size = float(alert.size or 0.0)
    intent = (alert.intent or "").lower()  # '', 'open', 'add', 'close'

    log.info('router.app:[ROUTER] incoming => %s %s size=%.8g intent=%s', side, symbol, size, intent or "auto")

    try:
        # Delegate to trading logic. smart_route must:
        # - infer intent when not provided
        # - ignore pure close signals when no position exists
        # - respect sizing/limits per your trade.py
        result = await smart_route(symbol, side, order_type, size, intent, REENTER_ON_OPPOSITE)
        return JSONResponse({"ok": True, "result": result})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("router.app:[ROUTER] unhandled")
        raise HTTPException(status_code=500, detail=str(e))