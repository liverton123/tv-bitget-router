# app.py
import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from trade import smart_route  # 기존 그대로 사용

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # e.g., umcbl, dmcbl
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"

app = FastAPI(title="tv-bitget-router")

def _clean_symbol(raw: str) -> str:
    s = (raw or "").upper().strip()
    # strip common TV futures/perp decorations
    for suf in (".P", ".PERP", "-PERP", "PERP", ":USDT", "/USDT"):
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    # normalize separators inside, then ensure USDT suffix
    s = s.replace("/", "").replace(":", "").replace("-", "")
    if not s.endswith("USDT"):
        # handle cases like "BTC" -> "BTCUSDT" if missing by mistake
        if s and not s.endswith("USDC") and not s.endswith("USD"):
            s = f"{s}USDT"
    return s

class Alert(BaseModel):
    secret: str
    symbol: str
    side: Literal["buy", "sell"] = Field(...)
    orderType: Literal["market", "limit"] = Field(..., alias="orderType")
    size: float
    intent: Optional[Literal["open", "close", "add", "auto"]] = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        return _clean_symbol(v)

    @field_validator("symbol")
    @classmethod
    def enforce_usdt(cls, v: str) -> str:
        if not v.endswith("USDT"):
            raise ValueError("symbol must be *USDT market")
        return v

@app.post("/webhook")
async def webhook(a: Alert, request: Request):
    if WEBHOOK_SECRET and a.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    symbol = a.symbol  # already normalized like 'TAOUSDT'
    side = a.side
    order_type = a.orderType
    size = float(a.size)
    intent = a.intent or "auto"

    try:
        result = await smart_route(
            symbol,
            side,
            order_type,
            size,
            intent,
            REENTER_ON_OPPOSITE,
            PRODUCT_TYPE,
        )
        return {"ok": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        # keep prior logging behavior
        import logging
        logging.getLogger("router.app").error("unhandled", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))