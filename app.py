import os, json, asyncio, math
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple

import ccxt.async_support as ccxt
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "umcbl")  # bitget USDT-M perpetual
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))

app = FastAPI()
ex: ccxt.bitget = None  # type: ignore

# --- 간단 캐시/락 ---
market_cache = TTLCache(maxsize=512, ttl=60)
price_cache  = TTLCache(maxsize=1024, ttl=5)
locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------- 유틸 ----------
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼 -> CCXT 심볼
    예: 'ENAUSDT.P' / 'ENAUSDT' -> 'ENA/USDT:USDT'
    """
    s = tv_symbol.upper().replace(".P", "").replace("PERP", "").replace("-PERP", "")
    if not s.endswith("USDT"):
        raise ValueError(f"unsupported symbol: {tv_symbol}")
    base = s[:-4]
    return f"{base}/USDT:USDT"

def is_close_intent(req_amount: Optional[float], pos_abs: float,
                    lot_step: float, tol_pct: float) -> bool:
    if pos_abs <= 0:
        return False
    if req_amount is None:
        # 수량이 명시 안 됐고 반대방향이면 '청산 의도'로 본다(전량)
        return True
    abs_tol = max(lot_step, pos_abs * tol_pct)
    return abs(req_amount - pos_abs) <= abs_tol

def round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


# ---------- 거래소 초기화 ----------
@app.on_event("startup")
async def startup():
    global ex
    ex = ccxt.bitget({
        "apiKey": os.getenv("BITGET_API_KEY"),
        "secret": os.getenv("BITGET_API_SECRET"),
        "password": os.getenv("BITGET_API_PASSWORD"),
        "enableRateLimit": True,
        "options": {
            # ccxt가 bitget 선물에서 productType 넘기도록 강제
            "defaultType": "swap",
        },
    })
    await ex.load_markets()


@app.on_event("shutdown")
async def shutdown():
    if ex:
        await ex.close()


# ---------- 마켓/시세/포지션 ----------
async def fetch_market(symbol: str) -> Dict[str, Any]:
    if symbol in market_cache:
        return market_cache[symbol]
    market = ex.market(symbol)
    market_cache[symbol] = market
    return market

async def fetch_price(symbol: str) -> float:
    if symbol in price_cache:
        return price_cache[symbol]
    t = await ex.fetch_ticker(symbol)
    price = float(t["last"] or t["close"] or t["bid"] or t["ask"])
    price_cache[symbol] = price
    return price

async def fetch_positions(symbols: List[str]) -> List[Dict[str, Any]]:
    return await ex.fetch_positions(symbols, params={"productType": PRODUCT_TYPE})

async def fetch_net_position(symbol: str) -> Tuple[float, float]:
    """
    리턴: (net_amount, abs_amount)  - +롱 / -숏 (계약수량=기초자산 수량)
    """
    pos_list = await fetch_positions([symbol])
    net = 0.0
    for p in pos_list:
        if p.get("symbol") != symbol:
            continue
        side = str(p.get("side") or "").lower()  # long/short
        contracts = float(p.get("contracts") or p.get("contractSize") or 0.0)
        # 일부 ccxt 버전에선 p["contracts"]가 0이고 p["size"]가 있을 수 있음
        if contracts <= 0 and p.get("size") is not None:
            contracts = float(p["size"])
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
    return net, abs(net)


# ---------- 수량 결정 ----------
async def decide_amount(symbol: str, msg: Dict[str, Any]) -> Tuple[float, float, float, float, str]:
    """
    size 해석:
      - msg.size가 없으면 '가능한 기본 로직'(예: 일정 USDT)을 쓰고 싶다면 여기서 구현.
      - 기본은 msg.size를 '기초자산 수량'으로 해석. msg.sizeIn == "quote"면 USDT->base 변환.
    리턴: (amount_final, amount_req, price, lot_step, sizing_mode)
    """
    market = await fetch_market(symbol)
    price = await fetch_price(symbol)
    lot_step = float(
        market.get("amount_increment") or
        (market.get("precision") or {}).get("amount") or
        market.get("lot") or 0.0
    )

    req = msg.get("size")
    sizing_mode = "msg_base"
    if req is None:
        raise HTTPException(400, "size missing")
    req = float(req)

    if str(msg.get("sizeIn") or "").lower() in ("quote", "usd", "usdt"):
        # USDT 금액을 보냈다면 기초자산 수량으로 환산
        sizing_mode = "msg_quote"
        base = req / price
    else:
        base = req

    # 거래소 최소증분 맞춤
    if lot_step and lot_step > 0:
        base = round_down(base, lot_step)

    if base <= 0:
        raise HTTPException(400, "amount computed as zero")

    return base, float(req), float(price), float(lot_step), sizing_mode


# ---------- 주문 ----------
async def place_market(symbol: str, side: str, amount: float, reduce_only: bool) -> Dict[str, Any]:
    params = {"reduceOnly": reduce_only, "productType": PRODUCT_TYPE}
    order = await ex.create_order(symbol=symbol, type="market",
                                  side=side, amount=amount, params=params)
    return order


# ---------- 라우팅 로직 (핵심) ----------
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    # 1) 인증
    if WEBHOOK_SECRET and body.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(401, "bad secret")

    raw_symbol = body.get("symbol") or body.get("ticker") or ""
    if not raw_symbol:
        raise HTTPException(400, "symbol missing")

    try:
        symbol = tv_to_ccxt_symbol(raw_symbol)
    except Exception as e:
        raise HTTPException(400, f"bad symbol: {raw_symbol}") from e

    side = str(body.get("side") or "").lower()  # 'buy' or 'sell'
    if side not in ("buy", "sell"):
        raise HTTPException(400, "side must be buy/sell")

    # 동시 신호 대비 심볼별 락
    async with locks[symbol]:
        # 현재 포지션
        net, net_abs = await fetch_net_position(symbol)
        pos_side = "long" if net > 0 else ("short" if net < 0 else "flat")

        # 요청 수량 계산(기초자산)
        amount_final, amount_req, price, lot_step, sizing_mode = await decide_amount(symbol, body)

        # ---- 의도 판별 ----
        # ① 포지션 없을 때: 무조건 '진입'
        # ② 포지션 있을 때:
        #    - 요청 방향이 '반대'면 기본 '청산'
        #    - (옵션) size가 포지션 수량과 거의 같으면 청산 의도 강화
        opposite = (pos_side == "long" and side == "sell") or (pos_side == "short" and side == "buy")
        close_by_size = is_close_intent(amount_req, net_abs, lot_step, CLOSE_TOLERANCE_PCT) if net_abs > 0 else False
        force_reduce = bool(body.get("reduceOnly", False))  # TV에서 명시했다면 최우선

        intent = "open"
        reduce_only = False

        if net_abs == 0:
            intent = "open"
        else:
            if opposite:
                intent = "close"
                reduce_only = True  # 반대방향은 항상 청산 모드
            if close_by_size or force_reduce:
                intent = "close"
                reduce_only = True

        # ---- 청산 처리 보호장치 ----
        if intent == "close":
            # 포지션 수량 초과 주문 방지(역진입 차단)
            amount_final = min(amount_final, net_abs)
            # 청산인데 방향이 포지션 반대가 아니면(예: 롱인데 buy) 스킵
            if (pos_side == "long" and side != "sell") or (pos_side == "short" and side != "buy"):
                return {"ok": True, "skip": "close_side_mismatch"}
            if amount_final <= 0:
                return {"ok": True, "skip": "amount_zero_on_close"}
        else:
            # 진입인데 TV가 실수로 reduceOnly를 보냈으면 제거
            if reduce_only:
                reduce_only = False

        plan = {
            "symbol": symbol,
            "tv_symbol": raw_symbol,
            "side": side,
            "intent": intent,
            "amount_req": amount_req,
            "amount_final": amount_final,
            "price": price,
            "pos_side": pos_side,
            "pos_abs": net_abs,
            "sizing_mode": sizing_mode,
            "reduceOnly": reduce_only,
            "productType": PRODUCT_TYPE,
        }

        # 포지션 0이고 intent==close로 판정되었다면(예: 연속 종료신호 두 번째) 스킵
        if net_abs == 0 and intent == "close":
            return {"ok": True, "skip": "no_position_to_close", "plan": plan}

        # 최종 주문
        order = await place_market(symbol, side, amount_final, reduce_only)
        plan["order_id"] = order.get("id")
        return {"ok": True, "plan": plan, "order": order}


@app.get("/health")
async def health():
    return {"ok": True}