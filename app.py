import os
from typing import Optional, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator

# --- Safe import: import 오류가 나더라도 app 심볼은 항상 존재하게 ---
try:
    from trade import (
        smart_route,
        get_exchange,
        normalize_symbol,
        normalize_product_type,
    )
    _IMPORT_ERROR: Optional[str] = None
except Exception as e:
    # uvicorn이 "Attribute 'app' not found"로 뭉뚱그리지 않도록 원인 보존
    smart_route = get_exchange = normalize_symbol = normalize_product_type = None  # type: ignore
    _IMPORT_ERROR = f"import error in trade: {e!r}"

app = FastAPI()
application = app  # 일부 환경에서 application을 찾는 경우가 있어 함께 노출
__all__ = ["app", "application"]

def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
REENTER_ON_OPPOSITE = str_to_bool(os.getenv("REENTER_ON_OPPOSITE"), False)

# trade가 임포트 실패 시에도 서버는 기동되지만 /webhook 호출 시 상세 에러 반환
def _guard_import():
    if _IMPORT_ERROR is not None:
        raise HTTPException(status_code=500, detail=_IMPORT_ERROR)

DEFAULT_PRODUCT_TYPE = None
try:
    # normalize_product_type이 임포트에 실패했을 수 있으므로 가드
    if normalize_product_type:
        DEFAULT_PRODUCT_TYPE = normalize_product_type(os.getenv("PRODUCT_TYPE") or "umcbl")
    else:
        DEFAULT_PRODUCT_TYPE = os.getenv("PRODUCT_TYPE") or "umcbl"
except Exception:
    DEFAULT_PRODUCT_TYPE = os.getenv("PRODUCT_TYPE") or "umcbl"

class Alert(BaseModel):
    secret: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: Literal["buy", "sell"]
    orderType: Literal["market", "limit"] = "market"
    size: float = Field(..., gt=0)
    intent: Optional[Literal["entry", "scale", "close", "auto"]] = "auto"
    product_type: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)

    @validator("symbol")
    def _strip_symbol(cls, v: str) -> str:
        return v.strip()

@app.post("/webhook")
async def webhook(alert: Alert):
    _guard_import()
    if alert.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    product_type = (normalize_product_type(alert.product_type) if normalize_product_type else alert.product_type) \
                   or DEFAULT_PRODUCT_TYPE
    if not product_type:
        raise HTTPException(status_code=400, detail="product_type is required")

    symbol_unified = normalize_symbol(alert.symbol) if normalize_symbol else alert.symbol

    ex = await get_exchange()  # type: ignore
    try:
        result = await smart_route(  # type: ignore
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
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            await ex.close()
        except Exception:
            pass

@app.get("/health")
async def health():
    if _IMPORT_ERROR:
        return {"ok": False, "detail": _IMPORT_ERROR}
    return {"ok": True}