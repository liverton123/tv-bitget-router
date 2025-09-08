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
    # 1) 안전한 JSON 파싱
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        body = await request.body()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            logger.exception("Invalid JSON payload")
            return JSONResponse({"status": "error", "reason": "invalid_json"}, status_code=400)

    # 2) 필수 필드 검증
    required = ["secret", "symbol", "side", "orderType", "size"]
    missing = [k for k in required if k not in data]
    if missing:
        return JSONResponse({"status": "error", "reason": f"missing:{','.join(missing)}"}, 400)

    # (선택) 시크릿 체크: 환경변수 ROUTER_SECRET 설정 시에만 검사
    expected = os.getenv("ROUTER_SECRET")
    if expected and data.get("secret") != expected:
        return JSONResponse({"status": "error", "reason": "unauthorized"}, 401)

    ex = None
    try:
        # 3) 거래소 초기화: 자격증명 누락은 400으로 반환
        try:
            ex = await get_exchange()
        except ValueError as e:
            logger.error(str(e))
            return JSONResponse({"status": "error", "reason": "missing_credentials"}, 400)
        except Exception:
            logger.exception("Exchange init failed")
            return JSONResponse({"status": "error", "reason": "exchange_init_failed"}, 500)

        # 4) 라우팅 실행
        result = await smart_route(ex, data)
        return JSONResponse(result, 200)

    except Exception as e:
        logger.exception("Unhandled error in webhook")
        return JSONResponse({"status": "error", "reason": str(e)}, 500)

    finally:
        # 5) 세션/커넥터 정리 (ex가 있을 때만)
        if ex is not None:
            try:
                await ex.close()
                logger.info("Closed client session/connector")
            except Exception:
                pass