import os
import json
import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from trade import (
    get_exchange,
    normalize_symbol,
    get_open_position,
    can_open_more_symbols,
    ensure_leverage_and_mode,
    usd_to_contracts,
    place_market_order,
    close_position_market,
    FRACTION_PER_POSITION,
    MAX_COINS,
    ALLOW_SHORTS,
    FIXED_ENTRY_USD,
)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"

app = FastAPI()


def _json(obj: Any) -> JSONResponse:
    return JSONResponse(content=obj)


async def _infer_intent(exchange, symbol: str, side: str) -> str:
    """
    intent 미제공 시, 현재 보유상태를 보고 자동 판정:
      - 포지션 없음  -> 'entry'
      - 같은 방향(롱/숏)으로 이미 보유 -> 'dca'
      - 반대 방향 보유 -> 'exit' (우선 정리)
    """
    pos = await get_open_position(exchange, symbol)
    if pos is None or float(pos.get("contracts", 0) or 0) == 0:
        return "entry"

    # Bitget은 posSide가 'long'/'short', 사이즈>0 로 표현되는 케이스가 일반적
    current_side = pos.get("side") or pos.get("posSide") or ""
    current_side = current_side.lower()

    desired = "long" if side.lower() == "buy" else "short"

    if current_side == desired:
        return "dca"
    else:
        return "exit"


@app.post("/webhook")
async def webhook(req: Request):
    payload: Dict[str, Any]
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 1) 시크릿 검사
    secret = str(payload.get("secret", "")).strip()
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")

    # 2) 필수 필드
    raw_symbol = str(payload.get("symbol", "")).strip()
    side = str(payload.get("side", "")).strip().lower()  # 'buy'|'sell'
    order_type = str(payload.get("orderType", "market")).strip().lower()
    size_hint = payload.get("size")  # 숫자 또는 None

    if not raw_symbol or side not in ("buy", "sell") or order_type != "market":
        raise HTTPException(status_code=400, detail="Bad payload fields")

    symbol = normalize_symbol(raw_symbol)

    # 3) 거래소 준비
    exchange = await get_exchange()
    try:
        await ensure_leverage_and_mode(exchange, symbol)

        # 4) intent 결정 (없어도 자동 판정)
        intent = str(payload.get("intent", "") or "").strip().lower()
        if not intent:
            intent = await _infer_intent(exchange, symbol, side)

        # 5) 방향/숏 허용 체크
        desired_side = "long" if side == "buy" else "short"
        if desired_side == "short" and not ALLOW_SHORTS:
            return _json({"ok": False, "reason": "shorts_not_allowed"})

        # 6) 현재 포지션 상태 파악
        pos = await get_open_position(exchange, symbol)
        has_pos = bool(pos and float(pos.get("contracts", 0) or 0) > 0)
        pos_side = (pos.get("side") or pos.get("posSide") or "").lower() if has_pos else None

        # 7) 심볼 슬롯 제한 확인 (진입/물타기 시만)
        if intent in ("entry", "dca"):
            if not await can_open_more_symbols(exchange, symbol, MAX_COINS):
                return _json({"ok": False, "reason": "max_symbols_reached"})

        # 8) 주문 수량 계산 (진입/물타기에는 고정 USD 사용, 종료는 전량)
        contracts: Optional[float] = None
        if intent in ("entry", "dca"):
            # size_hint 무시하고 고정 USD 기준으로 계약수 계산
            contracts = await usd_to_contracts(exchange, symbol, FIXED_ENTRY_USD)

        # 9) 의도별 처리
        if intent == "entry":
            # 이미 반대/같은 방향 보유 시 진입 대신 DCA/EXIT로 교정
            if has_pos:
                if pos_side == desired_side:
                    intent = "dca"
                else:
                    intent = "exit"

        if intent == "dca":
            if not has_pos:
                # 보유가 없는데 dca가 오면 진입으로 교정 (슬롯 제한은 이미 통과)
                intent = "entry"

        if intent == "exit":
            # 보유 없으면 할 일 없음
            if not has_pos:
                return _json({"ok": True, "intent": "exit", "skipped": "no_position"})
            # 반대 방향 주문으로 전량 청산
            await close_position_market(exchange, symbol)
            return _json({"ok": True, "intent": "exit", "symbol": symbol})

        # 여기까지 왔으면 entry 또는 dca
        qty = float(contracts or 0)
        if qty <= 0:
            raise HTTPException(status_code=400, detail="Contracts calc error")

        order = await place_market_order(exchange, symbol, side, qty)
        return _json(
            {
                "ok": True,
                "intent": intent,
                "symbol": symbol,
                "side": side,
                "contracts": qty,
                "order_id": order.get("id"),
            }
        )
    finally:
        try:
            await exchange.close()
        except Exception:
            pass