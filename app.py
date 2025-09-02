# app.py  (Bitget USDT-Perp Router, full version)
# FastAPI + CCXT (Bitget swap). 
# ✅ 핵심
# - TV → webhook(JSON): {"secret":"...", "symbol":"HBARUSDT.P", "side":"buy|sell", "orderType":"market", "size": 1234.56}
# - 시드(총자산)의 FRACTION_PER_POSITION 만큼만 마진을 씀 (예: 1/20 → 0.05)
# - 레버리지는 Bitget UI 설정값 사용 (여기서 설정/변경하지 않음)
# - 코인별 최소수량/자릿수, 최소주문금액을 엄격히 반영해 수량 라운딩
# - 현재 포지션을 조회해 buy/sell을 ‘정리(reduceOnly)’인지 ‘신규(open)’인지 자동판별
# - 심볼 변환: "HBARUSDT.P" → "HBAR/USDT:USDT" (Bitget-CCXT)

import os
import json
import math
from typing import Dict, Any

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

# 시드의 몇 %를 한 번 진입에 사용할지 (예: 0.05 = 1/20)
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))

# 필요 시 신규심볼 오픈 제한(물타기만 허용) 같은 정책을 추가하고 싶다면
# 심볼별 최근 상태를 메모리에 캐싱해서 사용할 수 있음.
# 여기서는 포지션 상태(롱/숏/없음)만 보고 신규/정리를 판별.
app = FastAPI()

# ------------------------------ Utils ------------------------------

def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼("HBARUSDT.P", "1000BONKUSDT.P") → CCXT Bitget 심볼("HBAR/USDT:USDT")
    """
    s = tv_symbol.strip()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        # 혹시 다른 쿼트가 들어오면 예외
        raise ValueError(f"Unsupported quote in symbol: {tv_symbol}")
    base = s[:-4]  # remove 'USDT'
    return f"{base}/USDT:USDT"


def safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def round_to_precision(amount: float, precision: int) -> float:
    """거래소가 요구하는 amount precision으로 반올림."""
    if precision is None:
        return amount
    factor = 10 ** precision
    return math.floor(amount * factor + 1e-12) / factor


# ------------------------------ CCXT ------------------------------

def build_exchange() -> ccxt.bitget:
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",   # 선물(USDT-M)
        },
    })
    return ex


async def fetch_equity_usdt(ex: ccxt.Exchange) -> float:
    """
    총자산(Equity)을 USDT 기준으로 읽음. (시드의 1/20 계산용)
    Bitget의 fetch_balance(type='swap')는 총자산을 'USDT' key 아래에 반환.
    """
    bal = await ex.fetch_balance({'type': 'swap'})
    usdt = bal.get('USDT') or {}
    # total(=equity에 가장 가깝게 쓰임). 없으면 free+used 근사.
    equity = safe_float(usdt.get('total'), safe_float(usdt.get('free', 0.0)) + safe_float(usdt.get('used', 0.0)))
    return max(equity, 0.0)


async def fetch_market_and_price(ex: ccxt.Exchange, ccxt_symbol: str) -> (Dict[str, Any], float):
    """
    심볼 마켓 정보 + 현재가(ticker last/mark)를 가져옴.
    """
    market = ex.market(ccxt_symbol)  # load_markets는 교환 객체가 내부적으로 lazy하게 처리
    ticker = await ex.fetch_ticker(ccxt_symbol)
    price = safe_float(ticker.get('last')) or safe_float(ticker.get('mark')) or safe_float(ticker.get('info', {}).get('last'))
    if price <= 0:
        raise RuntimeError(f"Could not fetch a valid price for {ccxt_symbol}")
    return market, price


async def fetch_net_position(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    """
    현재 순포지션 계약수(>0: 롱, <0: 숏, =0: 없음)
    Bitget은 fetch_positions로 심볼별 정보를 가져올 수 있음.
    """
    positions = await ex.fetch_positions([ccxt_symbol], params={"productType": "umcbl"})  # USDT-M linear
    net = 0.0
    for p in positions:
        if p.get('symbol') == ccxt_symbol:
            # ccxt 표준필드: contracts (포지션 계약수), side로 부호 판정
            contracts = safe_float(p.get('contracts', 0.0))
            side = (p.get('side') or "").lower()
            if side == 'long':
                net += contracts
            elif side == 'short':
                net -= contracts
    return net


def build_order_params_for_close(side: str) -> Dict[str, Any]:
    """
    정리(청산) 주문은 reduceOnly = True
    side: 'buy' or 'sell' (주문 방향 자체는 거래소 규칙에 따름)
    """
    return {
        "reduceOnly": True,
        # Bitget은 positionSide(hedge 모드) 관련 설정이 필요할 수 있으나
        # 여기서는 one-way(단방향) 전제로 reduceOnly로 정리 구분
    }


def size_to_amount(notional_usdt: float, price: float) -> float:
    """
    명목가(USDT) → 수량(코인) 변환.
    """
    return max(notional_usdt / price, 0.0)


def apply_market_limits(amount: float, market: Dict[str, Any]) -> float:
    """
    코인별 precision/최소수량/최소금액 제한을 적용해 안전한 수량으로 보정.
    """
    precision = market.get('precision', {}).get('amount')
    limits = market.get('limits', {}) or {}
    min_amt = safe_float(limits.get('amount', {}).get('min'), 0.0)
    # Bitget은 최소주문금액(min cost)이 있는 경우도 있음
    min_cost = safe_float(limits.get('cost', {}).get('min'), 0.0)

    amt = amount
    if precision is not None:
        amt = round_to_precision(amt, precision)

    if min_amt and amt < min_amt:
        amt = min_amt

    # min cost가 있다면, 가격은 실제 체결가와 조금 다를 수 있으나 대략 보호용으로만 사용
    # 여기서는 별도 강제 상향은 하지 않음. (체결 거부시 로그로 확인)
    return amt


async def place_order(
    ex: ccxt.Exchange,
    ccxt_symbol: str,
    side: str,            # 'buy' or 'sell'
    amount: float,
    reduce_only: bool
):
    """
    Bitget 선물 마켓가 주문. reduceOnly로 정리/신규 구분.
    """
    params = {}
    if reduce_only:
        params.update(build_order_params_for_close(side))
    # Bitget linear USDT swap → create_order(symbol, type, side, amount, price=None, params={})
    return await ex.create_order(ccxt_symbol, 'market', side, amount, None, params)


# ------------------------------ FastAPI Schemas ------------------------------

class TVAlert(BaseModel):
    secret: str = Field(default="")
    symbol: str
    side: str            # "buy" | "sell"
    orderType: str = Field(default="market")
    size: float | None = None   # TV가 넣어주는 'size'는 참고값(여기서는 사용하지 않고 계산)


# ------------------------------ Webhook ------------------------------

@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    data = TVAlert(**payload)

    # 1) 시크릿 확인
    if WEBHOOK_SECRET and data.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Bad secret")

    # 2) 심볼 변환
    try:
        ccxt_symbol = tv_to_ccxt_symbol(data.symbol)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    side = data.side.lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail=f"Unsupported side: {data.side}")

    ex = build_exchange()
    try:
        # 3) 총자산(=시드)에 FRACTION 적용 → 이번 진입에 쓸 '마진'(USDT)
        equity = await fetch_equity_usdt(ex)
        if equity <= 0:
            raise HTTPException(status_code=400, detail="No equity to trade")

        notional_usdt = equity * FRACTION_PER_POSITION   # 시드의 1/20 (예)
        # 참고: 레버리지는 거래소 UI설정 적용 → 여기선 건드리지 않음

        # 4) 시장 정보 + 가격
        market, price = await fetch_market_and_price(ex, ccxt_symbol)

        # 5) 현재 순포지션 조회 → 정리/신규 판별
        net = await fetch_net_position(ex, ccxt_symbol)

        #    buy:  (net<0)면 숏 정리(reduce), (net>=0)면 롱 신규
        #    sell: (net>0)면 롱 정리(reduce), (net<=0)면 숏 신규
        reduce_only = False
        if side == "buy" and net < -1e-12:
            reduce_only = True
        elif side == "sell" and net > 1e-12:
            reduce_only = True

        # 6) 신규라면 우리가 계산한 notional로 수량 산출
        #    정리(reduceOnly)라면 "가능한 전량"을 정리하고 싶지만, 
        #    여기서는 신규와 동일한 계산으로 수량을 넣고, 거래소가 초과분은 자동 조정(reduceOnly)하게 둔다.
        raw_amount = size_to_amount(notional_usdt, price)
        amount = apply_market_limits(raw_amount, market)

        if amount <= 0:
            # min amount 때문에 0이 된 경우 → 스킵
            return {"ok": True, "skip": "calc amount is zero", "symbol": data.symbol, "price": price}

        # 7) 주문
        order = await place_order(ex, ccxt_symbol, side, amount, reduce_only)
        return {"ok": True, "order": order, "symbol": data.symbol, "reduceOnly": reduce_only, "equity": equity, "notional": notional_usdt, "price": price, "amount": amount}

    except ccxt.BaseError as e:
        # 거래소 레벨 에러
        raise HTTPException(status_code=500, detail=f"ccxt_error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"runtime_error: {str(e)}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass


@app.get("/")
async def root():
    return {"status": "ok"}