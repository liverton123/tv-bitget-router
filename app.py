import os
import json
from typing import Any, Dict

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from trade import (
    get_exchange,
    get_position_info,
    get_open_positions_count,
    place_order_market,
    ensure_leverage,
    to_exchange_symbol,
)

app = FastAPI(title="tv-bitget-router")

# ---- env ----
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"
FORCE_FIXED_SIZING = os.getenv("FORCE_FIXED_SIZING", "true").lower() == "true"
FIXED_MARGIN_USDT = float(os.getenv("FIXED_MARGIN_USDT", "6"))
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "cross").lower()  # cross | isolated
MAX_COINS = int(os.getenv("MAX_COINS", "5"))


def _safe(payload: Dict[str, Any], key: str, default=None):
    v = payload.get(key, default)
    if isinstance(v, str):
        v = v.strip()
    return v


def _dir_from_side(side: str) -> str:
    return "long" if side.lower() == "buy" else "short"


def approx_equal(a: float, b: float, tol: float) -> bool:
    if b == 0:
        return abs(a) < 1e-8
    return abs(a - b) <= abs(b) * tol


@app.post("/webhook")
async def webhook(request: Request):
    # ---- parsing ----
    try:
        payload = await request.json()
    except Exception:
        body = await request.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(400, "Invalid JSON")

    # ---- auth ----
    incoming_secret = str(_safe(payload, "secret", ""))
    if WEBHOOK_SECRET and incoming_secret != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden (secret mismatch)")

    raw_symbol = str(_safe(payload, "symbol", "") or _safe(payload, "ticker", ""))
    if not raw_symbol:
        raise HTTPException(400, "Missing symbol")

    side = str(_safe(payload, "side", "")).lower()  # buy/sell
    if side not in ("buy", "sell"):
        raise HTTPException(400, "Missing/invalid side")

    order_type = str(_safe(payload, "orderType", "market")).lower()
    if order_type != "market":
        raise HTTPException(400, "Only market orders supported")

    incoming_size = float(_safe(payload, "size", 0) or 0)
    intent = str(_safe(payload, "intent", "") or "").lower()  # 'open'|'add'|'close'|''

    ex = await get_exchange()
    ex_symbol = to_exchange_symbol(raw_symbol)

    # 현재 심볼 포지션
    pos = await get_position_info(ex, ex_symbol)
    pos_side = pos["side"]             # 'none'|'long'|'short'
    pos_size = pos["contracts"]        # >0 일 때만 의미
    incoming_dir = _dir_from_side(side)

    # ---- 분류 ----
    action = None           # 'OPEN'|'ADD'|'CLOSE'|'FLIP'
    reduce_only = False

    if intent == "close":
        action = "CLOSE"
        reduce_only = True

    elif pos_side != "none":
        # 포지션 있음
        if incoming_dir == pos_side:
            action = "ADD"
        else:
            if incoming_size == 0 or approx_equal(incoming_size, pos_size, CLOSE_TOLERANCE_PCT):
                action = "CLOSE"
                reduce_only = True
            else:
                action = "FLIP" if REENTER_ON_OPPOSITE else "CLOSE"
                reduce_only = True

    else:
        # 포지션 없음
        if REQUIRE_INTENT_FOR_OPEN and intent != "open":
            await ex.close()
            return JSONResponse({"status": "ignored", "reason": "no position & no open intent"}, 200)

        # MAX_COINS 제한 체크 (심볼 신규 진입만 제한)
        open_count = await get_open_positions_count(ex)
        if open_count >= MAX_COINS:
            await ex.close()
            return JSONResponse({"status": "ignored", "reason": "max coins reached"}, 200)

        action = "OPEN"

    # ---- 실행 ----
    qty = None
    if action in ("OPEN", "ADD"):
        # 레버리지/마진모드 보정
        await ensure_leverage(ex, ex_symbol, DEFAULT_LEVERAGE, MARGIN_MODE)

        if FORCE_FIXED_SIZING:
            qty = await place_order_market(
                ex, ex_symbol, side,
                fixed_margin_usdt=FIXED_MARGIN_USDT,
                reduce_only=False,
            )
        else:
            if incoming_size <= 0:
                await ex.close()
                raise HTTPException(400, "Missing size")
            qty = await place_order_market(
                ex, ex_symbol, side,
                contracts=incoming_size,
                reduce_only=False,
            )

    elif action == "CLOSE":
        if pos_side == "none":
            await ex.close()
            return JSONResponse({"status": "ignored", "reason": "no position to close"}, 200)
        close_side = "sell" if pos_side == "long" else "buy"
        qty = await place_order_market(
            ex, ex_symbol, close_side,
            contracts=pos_size,
            reduce_only=True,
        )

    elif action == "FLIP":
        close_side = "sell" if pos_side == "long" else "buy"
        await place_order_market(
            ex, ex_symbol, close_side,
            contracts=pos_size,
            reduce_only=True,
        )
        await ensure_leverage(ex, ex_symbol, DEFAULT_LEVERAGE, MARGIN_MODE)
        qty = await place_order_market(
            ex, ex_symbol,
            "buy" if pos_side == "short" else "sell",
            fixed_margin_usdt=FIXED_MARGIN_USDT,
            reduce_only=False,
        )

    await ex.close()
    return JSONResponse(
        {"status": "ok", "symbol": ex_symbol, "classified": action, "reduceOnly": reduce_only, "qty": qty},
        200,
    )


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))