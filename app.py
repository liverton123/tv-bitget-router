import os
import math
import time
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import ccxt


# -----------------------------
# 환경변수
# -----------------------------
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")

# true/false 문자열을 깔끔히 처리
def as_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y")

DRY_RUN = as_bool(os.getenv("DRY_RUN"), False)
ALLOW_SHORTS = as_bool(os.getenv("ALLOW_SHORTS"), True)
FORCE_EQUAL_NOTIONAL = as_bool(os.getenv("FORCE_EQUAL_NOTIONAL"), True)  # true 권장
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 5%
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# Bitget USDT-M futures 코드 접미사
MIX_SUFFIX = "_UMCBL"

# -----------------------------
# FastAPI
# -----------------------------
app = FastAPI(title="TV→Bitget Router", version="1.0.0")


# -----------------------------
# CCXT Bitget 인스턴스
# -----------------------------
def get_exchange() -> ccxt.bitget:
    exchange = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASSWORD,
        "enableRateLimit": True,
        # 선물(USDT-M) 마켓
        "options": {"defaultType": "swap"},   # ccxt에서 bitget-perp는 swap
    })
    return exchange


# -----------------------------
# 도우미: 심볼 매핑 (TV "DOGEUSDT.P" → Bitget "DOGEUSDT_UMCBL")
# -----------------------------
def to_bitget_symbol(tv_symbol: str) -> str:
    # TV 심볼은 대개 "XXXUSDT.P"
    core = tv_symbol.replace(".P", "").replace(".p", "")
    return f"{core}{MIX_SUFFIX}"


# -----------------------------
# 포지션 조회 (심볼→순포지션 수량)
#   > 0 : 롱 보유수량,  < 0 : 숏 보유수량,  ==0 : 없음
# -----------------------------
def get_positions_map(exchange: ccxt.bitget) -> Dict[str, float]:
    pos_map: Dict[str, float] = {}
    # Bitget의 fetch_positions()는 전체 포지션을 반환
    positions = exchange.fetch_positions()
    for p in positions:
        sym = p.get("symbol")  # 예: "DOGEUSDT_UMCBL"
        contracts = float(p.get("contracts", 0.0))  # 절대값 수량
        side = p.get("side")  # "long"/"short" 또는 None
        if contracts <= 0 or sym is None:
            continue
        # ccxt bitget는 각각의 방향 포지션을 아이템으로 따로 줄 수 있음
        if side == "long":
            pos_map[sym] = pos_map.get(sym, 0.0) + contracts
        elif side == "short":
            pos_map[sym] = pos_map.get(sym, 0.0) - contracts
    return pos_map


def count_open_symbols(pos_map: Dict[str, float]) -> int:
    # 실제로 순포지션이 0이 아닌 심볼 수
    return sum(1 for _, qty in pos_map.items() if abs(qty) > 1e-9)


# -----------------------------
# 동일 명목금액(USDT) 기준 수량 계산
# -----------------------------
def calc_contract_amount_by_notional(exchange: ccxt.bitget, bgt_symbol: str, fraction: float) -> float:
    # 지갑 잔고(USDT)
    bal = exchange.fetch_balance({"type": "swap"})  # futures 계정
    total_usdt = bal.get("USDT", {}).get("total", None)
    if total_usdt is None:
        # 일부 계정은 "USDT" 키가 없을 수도 있으니, 총계로 보정
        total_usdt = bal.get("total", {}).get("USDT", 0.0)

    notional = float(total_usdt) * fraction
    if notional <= 0:
        return 0.0

    # 현재 가격
    ticker = exchange.fetch_ticker(bgt_symbol)
    price = float(ticker["last"])

    # 마켓 최소수량/단위
    market = exchange.market(bgt_symbol)
    min_qty = market.get("limits", {}).get("amount", {}).get("min", 0.0)
    step = market.get("precision", {}).get("amount", None)

    # 선물에서는 "계약수량"이지만 ccxt 통일로 amount=contracts 사용
    raw_amount = notional / price  # 대략적인 계약수량
    if step is not None and step > 0:
        # 소수 자리수 precision 적용
        q = int(raw_amount / (10**(-step))) * (10**(-step))
        amount = max(q, min_qty)
    else:
        # fallback
        amount = max(raw_amount, min_qty)

    return float(amount)


# -----------------------------
# 주문(개시/청산) 빌더
# -----------------------------
def place_order(
    exchange: ccxt.bitget,
    bgt_symbol: str,
    side: str,              # "buy"|"sell"
    amount: float,
    reduce_only: bool,
) -> Dict[str, Any]:
    if DRY_RUN:
        return {
            "dry_run": True,
            "symbol": bgt_symbol,
            "side": side,
            "amount": amount,
            "reduce_only": reduce_only,
        }

    params = {}
    # Bitget 선물 reduceOnly는 다음 파라미터로 전달
    # ccxt 최신 버전: params={"reduceOnly": True/False}
    params["reduceOnly"] = reduce_only

    # 시장가
    order = exchange.create_order(
        symbol=bgt_symbol,
        type="market",
        side=side,
        amount=amount,
        params=params
    )
    return order


# -----------------------------
# 웹훅 본체
# -----------------------------
@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON")

    # 시크릿 체크
    secret = payload.get("secret")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    tv_symbol = payload.get("symbol")
    side = str(payload.get("side", "")).lower()  # "buy"/"sell"
    if not tv_symbol or side not in ("buy", "sell"):
        raise HTTPException(status_code=422, detail="Missing symbol/side")

    bgt_symbol = to_bitget_symbol(tv_symbol)

    exch = get_exchange()

    # 마켓 존재 확인(초기 로딩 시점)
    try:
        exch.load_markets()
        if bgt_symbol not in exch.markets:
            raise HTTPException(422, detail=f"Unsupported futures symbol: {bgt_symbol}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"load_markets failed: {repr(e)}")

    # 현재 포지션 맵
    pos_map = get_positions_map(exch)
    cur_qty = float(pos_map.get(bgt_symbol, 0.0))  # >0 롱, <0 숏, 0 없음

    # 현재 오픈 심볼 수
    open_cnt = count_open_symbols(pos_map)

    # 이번 주문이 "청산"인가 "신규/증액"인가 판단
    # - buy : 숏보유중이면 청산(reduceOnly); 롱 또는 무포지션이면 신규/증액
    # - sell: 롱보유중이면 청산(reduceOnly); 숏 또는 무포지션이면 신규/증액
    is_reduce = False
    if side == "buy" and cur_qty < 0:
        is_reduce = True
    elif side == "sell" and cur_qty > 0:
        is_reduce = True

    # 신규/증액(open)인데 MAX_COINS 초과/ALLOW_SHORTS 고려
    if not is_reduce:
        # 숏 신규는 옵션에 따라 제한
        if side == "sell" and not ALLOW_SHORTS:
            return JSONResponse({"status": "ignored", "reason": "shorts_disabled"}, 200)

        # 신규 오픈인데, 이 심볼이 현재 완전 무포지션이고, 이미 MAX를 채웠으면 무시
        if abs(cur_qty) <= 1e-9 and open_cnt >= MAX_COINS:
            return JSONResponse({"status": "ignored", "reason": "max_coins_reached"}, 200)

    # 수량 계산: reduceOnly면 가능한 한 "현재 반대방향 수량만큼" 줄이도록
    # → ccxt/bitget은 시장가 청산 시 amount=청산수량으로 전달
    if is_reduce:
        # 현재 포지션 크기만큼만 청산(지나친 과청산 방지)
        amount = abs(cur_qty)
        if amount <= 0:
            # 방어적(이상치): 포지션이 없는데 reduceOnly 판단된 케이스 → 무시
            return JSONResponse({"status": "ignored", "reason": "no_position_to_reduce"}, 200)
        order = place_order(exch, bgt_symbol, side, amount, reduce_only=True)
        return JSONResponse({"status": "ok", "mode": "reduce", "order": order}, 200)

    # 신규/증액(open) 수량 계산
    if FORCE_EQUAL_NOTIONAL:
        amount = calc_contract_amount_by_notional(exch, bgt_symbol, FRACTION_PER_POSITION)
    else:
        # 비권장: TV size를 사용하려면 payload["size"] 사용
        # 단위 혼동 위험이 있어 기본은 equal notional 권장
        raw = float(payload.get("size", 0.0) or 0.0)
        amount = max(raw, 0.0)

    if amount <= 0:
        return JSONResponse({"status": "ignored", "reason": "amount_is_zero"}, 200)

    order = place_order(exch, bgt_symbol, side, amount, reduce_only=False)
    return JSONResponse({"status": "ok", "mode": "open_or_scale", "order": order}, 200)


# 헬스체크
@app.get("/health")
def health():
    return {"status": "ok"}
