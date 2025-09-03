import os
import re
import json
import asyncio
from typing import Optional, Literal, Dict, Any

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, field_validator
import ccxt.async_support as ccxt

# -----------------------------
# 환경 설정
# -----------------------------
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "umcbl").strip().lower()  # <- 소문자 강제
MARGIN_MODE  = os.getenv("MARGIN_MODE", "cross").strip().lower()
DEFAULT_LEVERAGE = int(os.getenv("DEFAULT_LEVERAGE", "10"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if PRODUCT_TYPE not in {"umcbl", "dmcbl"}:
    # umcbl: USDT-M perpetual, dmcbl: Coin-M perpetual (필요시 추가)
    raise RuntimeError(f"Unsupported PRODUCT_TYPE: {PRODUCT_TYPE}")

# -----------------------------
# Pydantic 모델
# -----------------------------
class TVPayload(BaseModel):
    secret: str
    symbol: str               # e.g., ENAUSDT.P
    side: Literal["buy", "sell"]
    orderType: Literal["market", "limit"]
    size: float
    price: Optional[float] = None

    @field_validator("symbol")
    def normalize_symbol(cls, v: str) -> str:
        # TradingView 심볼 (예: ENAUSDT.P) → CCXT 심볼 (예: ENA/USDT:USDT or ENA/USDT)
        # Bitget USDT-M 퍼펫은 일반적으로 ENA/USDT
        s = v.strip().upper()
        # 뒤에 '.P' 붙는 건 제거
        s = re.sub(r"\.P$", "", s)
        # XXXUSDT → XXX/USDT
        if s.endswith("USDT"):
            base = s[:-4]
            quote = "USDT"
            return f"{base}/{quote}"
        return s

# -----------------------------
# FastAPI
# -----------------------------
app = FastAPI(title="tv-bitget-router")

# -----------------------------
# 거래소 인스턴스 (재사용)
# -----------------------------
_ex_lock = asyncio.Lock()
_ex: Optional[ccxt.bitget] = None

async def get_exchange() -> ccxt.bitget:
    global _ex
    async with _ex_lock:
        if _ex is None:
            _ex = ccxt.bitget({
                "apiKey": os.getenv("BITGET_API_KEY"),
                "secret": os.getenv("BITGET_API_SECRET"),
                "password": os.getenv("BITGET_API_PASSPHRASE"),
                "enableRateLimit": True,
                "options": {
                    "defaultType": "swap",  # 선물/스왑
                    "defaultMarginMode": MARGIN_MODE,
                    "defaultProductType": PRODUCT_TYPE,
                },
            })
            await _ex.load_markets()
        return _ex

# -----------------------------
# 유틸: 심볼의 정밀도/시장정보
# -----------------------------
async def get_market_info(ex: ccxt.bitget, symbol: str) -> Dict[str, Any]:
    if symbol not in ex.markets:
        await ex.load_markets()
    market = ex.market(symbol)
    return market

def amount_to_precision(ex: ccxt.bitget, symbol: str, amount: float) -> float:
    return float(ex.amount_to_precision(symbol, amount))

def price_to_precision(ex: ccxt.bitget, symbol: str, price: float) -> float:
    return float(ex.price_to_precision(symbol, price))

# -----------------------------
# 유틸: 레버리지/마진모드 보장
# -----------------------------
async def ensure_leverage_and_margin(ex: ccxt.bitget, symbol: str):
    try:
        # set_margin_mode 필요시:
        await ex.set_margin_mode(MARGIN_MODE, symbol, params={"productType": PRODUCT_TYPE})
    except Exception:
        # 일부 거래소/시장 조합에서 불필요하거나 권한 제약 가능 → 무시
        pass

    try:
        await ex.set_leverage(
            DEFAULT_LEVERAGE,
            symbol,
            params={"productType": PRODUCT_TYPE, "marginMode": MARGIN_MODE},
        )
    except Exception:
        # 이미 설정되어 있거나 코인 특성상 실패할 수 있으니 무시
        pass

# -----------------------------
# 유틸: 현재 순포지션(net) 조회
#   반환: (net_qty, entry_price)
#   net_qty > 0 → 롱, net_qty < 0 → 숏, 0 → 무포지션
# -----------------------------
async def fetch_net_position(ex: ccxt.bitget, symbol: str) -> tuple[float, Optional[float]]:
    # Bitget의 fetch_positions는 symbols 리스트 또는 None
    pos_list = await ex.fetch_positions([symbol], params={"productType": PRODUCT_TYPE})
    net_qty = 0.0
    entry_price = None

    for p in pos_list:
        if p.get("symbol") != symbol:
            continue
        # Bitget은 long / short 각각 포지션을 반환할 수 있다.
        side = p.get("side")  # 'long' or 'short'
        size = float(p.get("contracts") or p.get("contractsSize") or p.get("positionAmt") or 0)
        # ccxt 통일 필드
        amt = float(p.get("contracts") or p.get("amount") or 0)

        if side == "long":
            net_qty += amt
            if not entry_price and p.get("entryPrice"):
                entry_price = float(p["entryPrice"])
        elif side == "short":
            net_qty -= amt
            if not entry_price and p.get("entryPrice"):
                entry_price = float(p["entryPrice"])

    return net_qty, entry_price

# -----------------------------
# 핵심: 안전주문 라우터
# - opposite(반대) 방향 주문은 기본적으로 reduceOnly로 처리
# - 무포지션일 때 reduceOnly 주문은 거래소가 거절 → "평가종료 신호가 신규진입" 문제 방지
# -----------------------------
async def route_order(ex: ccxt.bitget, symbol: str, side: str, order_type: str,
                      size: float, price: Optional[float]) -> Dict[str, Any]:

    await ensure_leverage_and_margin(ex, symbol)
    market = await get_market_info(ex, symbol)

    # 수량/가격 정밀도 보정
    size = amount_to_precision(ex, symbol, float(size))
    if price is not None:
        price = price_to_precision(ex, symbol, float(price))

    # 현재 net 포지션
    net_qty, _ = await fetch_net_position(ex, symbol)

    # 현재 방향
    cur_dir = "long" if net_qty > 0 else "short" if net_qty < 0 else "flat"

    params = {
        "productType": PRODUCT_TYPE,
        "marginMode": MARGIN_MODE,
        "leverage": str(DEFAULT_LEVERAGE),
    }

    # 1) 평감지: 현재 롱인데 sell이 들어오면 reduceOnly
    #           현재 숏인데 buy가 들어오면 reduceOnly
    #           무포지션(flat)인데 reduceOnly면 자동 거절 => 신규 진입 방지
    reduce_only = False
    if cur_dir == "long" and side == "sell":
        reduce_only = True
    elif cur_dir == "short" and side == "buy":
        reduce_only = True

    if reduce_only:
        params["reduceOnly"] = True

    # 2) 주문 실행
    order = await ex.create_order(
        symbol=symbol,
        type=order_type,
        side=side,
        amount=size,
        price=price,
        params=params
    )
    return order

# -----------------------------
# Webhook
# -----------------------------
@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload_raw = await req.body()
        data = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        raise HTTPException(400, "invalid json")

    try:
        tv = TVPayload(**data)
    except Exception as e:
        raise HTTPException(400, f"bad payload: {e}")

    if not WEBHOOK_SECRET or tv.secret != WEBHOOK_SECRET:
        raise HTTPException(401, "unauthorized")

    ex = await get_exchange()

    # 심볼 변환 결과 (예: ENA/USDT)
    ccxt_symbol = tv.symbol

    # Bitget 마켓에 심볼이 없으면 오류
    await ex.load_markets()
    if ccxt_symbol not in ex.markets:
        raise HTTPException(400, f"unknown symbol on bitget: {ccxt_symbol}")

    try:
        order = await route_order(
            ex=ex,
            symbol=ccxt_symbol,
            side=tv.side,
            order_type=tv.orderType,
            size=tv.size,
            price=tv.price
        )
        return {"ok": True, "order": order}
    except ccxt.BaseError as e:
        # Bitget 에러 메세지 그대로 노출 + 500
        raise HTTPException(500, f"exchange_error: {str(e)}")
    except Exception as e:
        raise HTTPException(500, f"runtime_error: {str(e)}")

# -----------------------------
# 헬스체크
# -----------------------------
@app.get("/")
async def root():
    return {"status": "ok", "productType": PRODUCT_TYPE, "marginMode": MARGIN_MODE, "leverage": DEFAULT_LEVERAGE}

# -----------------------------
# 종료 훅
# -----------------------------
@app.on_event("shutdown")
async def shutdown_event():
    global _ex
    if _ex is not None:
        try:
            await _ex.close()
        except Exception:
            pass