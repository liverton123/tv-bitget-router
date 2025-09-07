import os
import traceback
from typing import Optional, Literal
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, validator

# ---- import trade (원인 보존) ----
_IMPORT_ERROR: Optional[str] = None
try:
    from trade import (
        smart_route,
        get_exchange,
        normalize_symbol,
        normalize_product_type,
    )
except Exception as e:
    _IMPORT_ERROR = f"import error in trade: {e!r}"

app = FastAPI()
application = app  # uvicorn이 application을 찾는 환경 대비

def str_to_bool(s: Optional[str], default: bool = False) -> bool:
    if s is None:
        return default
    v = s.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
REENTER_ON_OPPOSITE = str_to_bool(os.getenv("REENTER_ON_OPPOSITE"), False)

# PRODUCT_TYPE 기본값(비트겟 USDT 무기한: umcbl)
_RAW_PT = os.getenv("PRODUCT_TYPE", "umcbl")
DEFAULT_PRODUCT_TYPE = None
try:
    DEFAULT_PRODUCT_TYPE = normalize_product_type(_RAW_PT) if _IMPORT_ERROR is None else _RAW_PT
except Exception:
    DEFAULT_PRODUCT_TYPE = _RAW_PT

class Alert(BaseModel):
    secret: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    orderType: Literal["market", "limit"] = "market"
    size: float = Field(..., gt=0)
    # auto: (entry/scale/close 자동판단)
    intent: Optional[Literal["entry", "scale", "close", "auto"]] = "auto"
    product_type: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)

    @validator("symbol")
    def _strip_symbol(cls, v: str) -> str:
        return v.strip()

@app.post("/webhook")
async def webhook(alert: Alert, request: Request):
    if _IMPORT_ERROR:
        raise HTTPException(status_code=500, detail=_IMPORT_ERROR)

    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    # product_type 확정
    product_type = (alert.product_type or DEFAULT_PRODUCT_TYPE)
    try:
        product_type = normalize_product_type(product_type)  # type: ignore
    except Exception:
        pass
    if not product_type:
        raise HTTPException(status_code=400, detail="product_type is required")

    # 심볼 정규화
    symbol_unified = alert.symbol
    try:
        symbol_unified = normalize_symbol(alert.symbol)  # type: ignore
    except Exception:
        pass

    # 실행
    ex = await get_exchange()  # env 검증 포함
    try:
        result = await smart_route(
            ex=ex,
            symbol=symbol_unified,
            side=alert.side,
            order_type=alert.orderType,
            size=alert.size,
            intent=alert.intent or "auto",
            reenter_on_opposite=REENTER_ON_OPPOSITE,
            product_type=product_type,
            price=alert.price,
        )
        return {"ok": True, "result": result}
    except HTTPException:
        raise
    except ValueError as ve:
        # 사용자가 바로 알 수 있게 400으로 내려줌
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        # 원인 텍스트와 스택을 동시에 남김
        tb = traceback.format_exc()
        print(f"[WEBHOOK ERROR] {e}\n{tb}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            await ex.close()
        except Exception:
            pass

@app.get("/health")
async def health():
    return {
        "ok": _IMPORT_ERROR is None,
        "import_error": _IMPORT_ERROR,
        "product_type": DEFAULT_PRODUCT_TYPE,
        "reenter_on_opposite": REENTER_ON_OPPOSITE,
    }