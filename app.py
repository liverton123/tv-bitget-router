import os
import json
import logging
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from trade import get_exchange, smart_route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("tv-bitget-router")

app = FastAPI()


def _env(name: str, default: str | None = None) -> str | None:
    """환경변수 헬퍼 (공백·'null' 같은 값도 비어있는 것으로 취급)."""
    v = os.getenv(name, default)
    if v is None:
        return None
    v = v.strip()
    if v == "" or v.lower() in ("none", "null"):
        return None
    return v


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    """
    TradingView 웹훅을 받아 Bitget에 라우팅.
    - 요청 본문은 JSON
    - 필수: secret (옵션), symbol, side, orderType, size
    """
    body: Dict[str, Any]
    try:
        body = await request.json()
    except Exception:
        # TV가 가끔 text로 보낼 때 대비
        raw = await request.body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info("webhook payload: %s", body)

    # (선택) 시크릿 검증: 환경변수 SECRET이 설정된 경우에만 검사
    expected_secret = _env("SECRET")
    if expected_secret is not None:
        recv_secret = str(body.get("secret", "")).strip()
        if recv_secret != expected_secret:
            raise HTTPException(status_code=401, detail="Invalid secret")

    # 필드 정규화 (TradingView 템플릿 가정)
    symbol = str(body.get("symbol") or body.get("ticker") or "").strip()
    side = str(body.get("side") or body.get("action") or "").strip().lower()
    order_type = str(body.get("orderType") or "market").strip().lower()
    size = body.get("size") or body.get("contracts") or body.get("qty") or 0

    if not symbol or not side or not order_type:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # 숫자 변환 실패 방지
    try:
        size = float(size)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid size")

    if size <= 0:
        raise HTTPException(status_code=400, detail="Size must be > 0")

    # Bitget 인증값 검증을 일찍 수행해 400 반복로그 방지
    api_key = _env("bitget_api_key")
    api_secret = _env("bitget_api_secret")
    api_password = _env("bitget_api_password")
    if not (api_key and api_secret and api_password):
        logger.error("Missing Bitget credentials (key/secret/password).")
        raise HTTPException(status_code=400, detail="Missing Bitget credentials")

    # 거래 실행
    exchange = None
    try:
        exchange = await get_exchange()  # 내부에서 env 사용
        result = await smart_route(
            exchange=exchange,
            symbol=symbol,
            side=side,
            order_type=order_type,
            size=size,
            raw=body,
        )
        logger.info("order result: %s", result)
        return JSONResponse({"ok": True, "result": result})
    except HTTPException:
        # FastAPI 예외는 그대로
        raise
    except Exception as e:
        logger.exception("Unhandled error while routing order")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # ccxt async 자원 해제 (로그에서 반복 경고 나왔던 부분)
        if exchange is not None:
            try:
                await exchange.close()
                logger.info("Closed client session & connector")
            except Exception:
                logger.warning("exchange.close() failed", exc_info=True)