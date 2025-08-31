import os
import json
import logging
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import hmac
import hashlib
import time

# 로거 세팅
logger = logging.getLogger("webhook")
logger.setLevel(logging.INFO)

app = FastAPI()

# 환경 변수
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Bitget API URL
BASE_URL = "https://api.bitget.com"

# 현재 보유 심볼 추적 (메모리 상)
open_positions = {}  # {"BTCUSDT.P": {"side": "long", "size": 123.4}, ...}

# 상태 체크
@app.get("/status")
def status():
    return {"ok": True, "positions": open_positions}


@app.post("/webhook")
async def webhook(request: Request):
    # 1) 원문 로깅
    try:
        raw = await request.body()
        raw_text = raw.decode("utf-8", errors="replace")
        ct = request.headers.get("content-type", "")
        logger.info(f"[WEBHOOK] CT={ct} RAW={raw_text[:500]}")
    except Exception:
        logger.exception("Failed to read request body")
        return JSONResponse({"ok": False, "error": "bad_body"}, status_code=400)

    # 2) JSON 파싱
    try:
        if "application/json" in ct.lower():
            data = await request.json()
        else:
            data = json.loads(raw_text)
    except Exception:
        logger.exception("Failed to parse JSON")
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    # 3) 필수 필드 검증
    required = ("secret", "symbol", "side", "orderType", "size")
    missing = [k for k in required if k not in data]
    if missing:
        logger.error(f"Missing fields: {missing} | data={data}")
        return JSONResponse({"ok": False, "error": f"missing:{missing}"}, status_code=400)

    # 4) 비밀키 검증
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET not set")
    if data.get("secret") != WEBHOOK_SECRET:
        logger.warning("Secret mismatch")
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    symbol = data["symbol"]
    side = data["side"].lower()
    order_type = data["orderType"].lower()
    try:
        size = float(data["size"])
    except Exception:
        logger.exception("Invalid size format")
        return JSONResponse({"ok": False, "error": "bad_size"}, status_code=400)

    # 5) 포지션 상태 기반 매핑
    # 이미 포지션이 있는 경우: 반대 side는 "종료", 같은 side는 "물타기"
    # 포지션이 없는 경우: buy=롱 진입, sell=숏 진입
    pos = open_positions.get(symbol)

    action = None
    if pos:
        if pos["side"] == "long":
            if side == "buy":
                action = "long_add"
            else:
                action = "long_close"
        elif pos["side"] == "short":
            if side == "sell":
                action = "short_add"
            else:
                action = "short_close"
    else:
        if side == "buy":
            action = "long_open"
        else:
            if ALLOW_SHORTS:
                action = "short_open"
            else:
                logger.info("Shorts not allowed, ignoring")
                return {"ok": False, "skipped": "short_not_allowed"}

    logger.info(f"Symbol={symbol}, Side={side}, Action={action}, Size={size}")

    # 6) 최대 코인 제한 확인
    if action in ("long_open", "short_open") and len(open_positions) >= MAX_COINS:
        logger.warning("Max coins reached, skipping new entry")
        return {"ok": False, "skipped": "max_coins"}

    # 7) 주문 실행
    if DRY_RUN:
        logger.info(f"DRY_RUN: would execute {action} {symbol} size={size}")
    else:
        try:
            await place_order(symbol, action, size)
        except Exception:
            logger.exception("Order placement failed")
            return JSONResponse({"ok": False, "error": "order_failed"}, status_code=500)

    # 8) 포지션 상태 갱신
    if action == "long_open":
        open_positions[symbol] = {"side": "long", "size": size}
    elif action == "short_open":
        open_positions[symbol] = {"side": "short", "size": size}
    elif action in ("long_add", "short_add"):
        open_positions[symbol]["size"] += size
    elif action in ("long_close", "short_close"):
        open_positions.pop(symbol, None)

    return {"ok": True, "action": action, "symbol": symbol, "size": size}


async def place_order(symbol: str, action: str, size: float):
    """
    Bitget 주문 실행 (시장가)
    """
    timestamp = str(int(time.time() * 1000))
    method = "POST"
    request_path = "/api/mix/v1/order/placeOrder"

    # 방향 결정
    if action in ("long_open", "long_add"):
        side = "buy"
    elif action in ("short_open", "short_add"):
        side = "sell"
    elif action == "long_close":
        side = "sell"
    elif action == "short_close":
        side = "buy"
    else:
        raise ValueError("Unknown action")

    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "orderType": "market",
        "timeInForceValue": "normal"
    }
    body_json = json.dumps(body)

    pre_hash = timestamp + method + request_path + body_json
    sign = hmac.new(API_SECRET.encode("utf-8"), pre_hash.encode("utf-8"), hashlib.sha256).digest()
    sign_b64 = sign.hex()

    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-PASSPHRASE": API_PASSWORD,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(BASE_URL + request_path, headers=headers, content=body_json, timeout=10.0)
        if r.status_code != 200:
            raise Exception(f"Bitget API error {r.status_code}: {r.text}")

    logger.info(f"Order placed: {body}")
    return r.json()
