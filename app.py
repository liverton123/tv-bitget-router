# app.py
import os, json, time, hmac, hashlib, logging, traceback
from typing import Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

# -----------------------------
# 로깅
# -----------------------------
logger = logging.getLogger("tv-bitget-router")
logger.setLevel(logging.INFO)

app = FastAPI(title="tv-bitget-router")

# -----------------------------
# 환경 변수
# -----------------------------
API_KEY         = os.getenv("BITGET_API_KEY", "")
API_SECRET      = os.getenv("BITGET_API_SECRET", "")
API_PASSWORD    = os.getenv("BITGET_API_PASSWORD", "")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET", "")

ALLOW_SHORTS    = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))
MAX_COINS       = int(os.getenv("MAX_COINS", "5"))
DRY_RUN         = os.getenv("DRY_RUN", "false").lower() == "true"

BASE_URL        = "https://api.bitget.com"

# 메모리 포지션 추적: {"SYMBOL":{"side":"long|short","size":float}}
open_positions: Dict[str, Dict[str, Any]] = {}

# -----------------------------
# 응답 헬퍼 (항상 200)
# -----------------------------
def ok(payload: Dict[str, Any]) -> JSONResponse:
    return JSONResponse({"ok": True, **payload}, status_code=200)

def fail(msg: str, extra: Dict[str, Any] = None) -> JSONResponse:
    payload = {"ok": False, "error": msg}
    if extra:
        payload.update(extra)
    # TradingView 재시도 폭주 방지를 위해 200으로 고정
    return JSONResponse(payload, status_code=200)

# -----------------------------
# 헬스 체크
# -----------------------------
@app.get("/status")
def status():
    return ok({"positions": open_positions})

# -----------------------------
# 웹훅
# -----------------------------
@app.post("/webhook")
async def webhook(request: Request):
    try:
        raw = await request.body()
        ct = request.headers.get("content-type", "")
        raw_text = raw.decode("utf-8", errors="replace") if raw else ""
        logger.info(f"[WEBHOOK] CT={ct} RAW={raw_text[:500]}")
    except Exception:
        logger.exception("read body failed")
        return fail("bad_body")

    # JSON 파싱
    try:
        data = await request.json() if "application/json" in (ct or "").lower() else json.loads(raw_text or "{}")
    except Exception:
        logger.exception("parse json failed")
        return fail("bad_json")

    # 필드 검증
    required = ("secret", "symbol", "side", "orderType", "size")
    miss = [k for k in required if k not in data]
    if miss:
        logger.error(f"missing fields: {miss} | data={data}")
        return fail("missing_fields", {"missing": miss})

    if not WEBHOOK_SECRET or data.get("secret") != WEBHOOK_SECRET:
        logger.warning("secret mismatch")
        return fail("unauthorized")

    symbol = str(data["symbol"]).strip()
    side   = str(data["side"]).lower().strip()            # "buy" | "sell"
    _otype = str(data["orderType"]).lower().strip()       # "market" 등
    try:
        size   = float(data["size"])
    except Exception:
        return fail("bad_size")

    current = open_positions.get(symbol)
    # 액션 결정
    if current:
        if current["side"] == "long":
            action = "long_add" if side == "buy" else "long_close"
        else:  # short
            action = "short_add" if side == "sell" else "short_close"
    else:
        if side == "buy":
            action = "long_open"
        else:   # sell
            if ALLOW_SHORTS:
                action = "short_open"
            else:
                logger.info(f"short blocked for {symbol}")
                return fail("short_not_allowed")

    # 신규 오픈 제한
    if action in ("long_open", "short_open") and len(open_positions) >= MAX_COINS:
        logger.warning(f"max coins reached ({MAX_COINS}), skip new entry: {symbol}")
        return fail("max_coins")

    logger.info(f"ACTION={action} symbol={symbol} size={size}")

    # 주문 실행
    if DRY_RUN:
        logger.info(f"DRY_RUN order: {action} {symbol} size={size}")
    else:
        try:
            await place_order_bitget(symbol, action, size)
        except Exception as e:
            logger.error(f"order_failed: {e}\n{traceback.format_exc()}")
            # 주문 실패해도 서버는 200으로 응답
            return fail("order_failed", {"detail": str(e)})

    # 포지션 현황 갱신
    if action == "long_open":
        open_positions[symbol] = {"side": "long", "size": size}
    elif action == "short_open":
        open_positions[symbol] = {"side": "short", "size": size}
    elif action in ("long_add", "short_add"):
        open_positions[symbol]["size"] += size
    elif action in ("long_close", "short_close"):
        open_positions.pop(symbol, None)

    return ok({"action": action, "symbol": symbol, "size": size, "positions": open_positions})

# -----------------------------
# Bitget 주문 (시장가)
# -----------------------------
async def place_order_bitget(symbol: str, action: str, size: float):
    # action -> 실제 주문 side
    if action in ("long_open", "long_add", "short_close"):
        trade_side = "buy"
    elif action in ("short_open", "short_add", "long_close"):
        trade_side = "sell"
    else:
        raise ValueError(f"unknown action {action}")

    ts = str(int(time.time() * 1000))
    method = "POST"
    path = "/api/mix/v1/order/placeOrder"

    body = {
        "symbol": symbol,
        "marginCoin": "USDT",
        "size": str(size),
        "side": trade_side,
        "orderType": "market",
        "timeInForceValue": "normal"
    }
    payload = json.dumps(body, separators=(",", ":"))

    # Bitget 서명 (문서 기준: ACCESS-SIGN = HMAC-SHA256(signStr) -> base64)
    sign_str = ts + method + path + payload
    sign = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).digest()
    sign_b64 = __import__("base64").b64encode(sign).decode()

    headers = {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign_b64,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": API_PASSWORD,
        "Content-Type": "application/json",
        "locale": "en-US",
    }

    url = BASE_URL + path
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, headers=headers, content=payload)
        text = r.text
        logger.info(f"[Bitget] {r.status_code} {text}")
        if r.status_code != 200:
            raise RuntimeError(f"bitget_status_{r.status_code}: {text}")

        # Bitget 에러코드 검사
        try:
            j = r.json()
            if str(j.get("code")) not in ("00000", "0", "200"):
                raise RuntimeError(f"bitget_resp_error: {j}")
        except Exception as e:
            raise RuntimeError(f"bitget_resp_parse_failed: {text}") from e
