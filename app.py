# app.py
# FastAPI + ccxt Bitget router for TradingView webhooks
# - Per-symbol USDT-M perpetuals only
# - Uses Bitget leverage set on the exchange (NOT from env)
# - Margin per entry = balance * FRACTION_PER_POSITION (e.g., 5%)
# - BUY/SELL는 포지션 상태를 조회해서 [롱 진입/추가, 숏 진입/추가, 롱 정리, 숏 정리]로 자동 판별
# - MAX_COINS 초과 시 신규 진입/추가 차단(정리 주문은 허용)
# - FORCE_EQUAL_NOTIONAL=true면 TV에서 넘어오는 size는 무시하고 항상 동일 마진으로 계산
# - 최소 수량/스텝/최소 명목가 맞춰서 양 조정
# - 에러는 로그로 남기고 TV엔 200 OK로 응답(재전송 루프 방지)

import os
import math
import time
import json
import asyncio
from typing import Dict, Any, Optional, Tuple

import ccxt.async_support as ccxt  # async ccxt
from fastapi import FastAPI, Request
from pydantic import BaseModel

# ────────────────────────────────────────────────────────────────────────────────
# 환경변수
# ────────────────────────────────────────────────────────────────────────────────
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")  # Bitget은 password(=passphrase) 필요
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 시드의 1/20
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN = "USDT"

# 옵션: 최근 신규 오픈 제한(물타기·정리와 구분)
# 막혔던 코인이 아주 짧은 시간 안에 다시 '신규 오픈' 신호를 보내더라도
# 방금 막힘 때문에 곧바로 열지는 않도록 하는 보호 타이머(초)
BLOCKED_OPEN_TTL_SEC = 60

# ────────────────────────────────────────────────────────────────────────────────
# FastAPI
# ────────────────────────────────────────────────────────────────────────────────
app = FastAPI()


class TVPayload(BaseModel):
    secret: str
    symbol: str
    side: str  # "buy" or "sell"
    orderType: Optional[str] = "market"
    size: Optional[float] = None  # 무시(동일 마진 강제 시)


# 메모리 상태(안전/보강용; 진입 판정은 항상 거래소 포지션이 우선)
recent_blocked_open: Dict[str, float] = {}  # symbol -> blocked_until_ts


# ────────────────────────────────────────────────────────────────────────────────
# Bitget/ccxt helpers
# ────────────────────────────────────────────────────────────────────────────────
def now_ts() -> float:
    return time.time()


def log(msg: str, obj: Any = None) -> None:
    s = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    if obj is not None:
        try:
            s += " | " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            s += f" | {obj}"
    print(s, flush=True)


async def create_exchange() -> ccxt.Exchange:
    ex = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASSWORD,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # USDT perpetual
        },
    })
    return ex


async def close_exchange(ex: ccxt.Exchange):
    try:
        await ex.close()
    except Exception:
        pass


async def fetch_usdt_free(ex: ccxt.Exchange) -> float:
    bal = await ex.fetch_balance()
    # ccxt 표준: bal["USDT"]["free"] 가용 / bal["USDT"]["total"] 총액
    free = 0.0
    try:
        free = float(bal.get("USDT", {}).get("free") or 0.0)
    except Exception:
        pass
    return max(free, 0.0)


async def load_markets_safe(ex: ccxt.Exchange):
    try:
        await ex.load_markets()
    except Exception as e:
        log("load_markets error", str(e))


async def get_market(ex: ccxt.Exchange, symbol: str) -> Optional[Dict[str, Any]]:
    await load_markets_safe(ex)
    markets = ex.markets or {}
    return markets.get(symbol)


async def fetch_price(ex: ccxt.Exchange, symbol: str) -> float:
    # bitget에서 markPrice를 ccxt로 표준화해주지 않을 수 있으므로 last/close 사용
    try:
        t = await ex.fetch_ticker(symbol)
        price = t.get("last") or t.get("close") or t.get("info", {}).get("last")
        return float(price)
    except Exception:
        return 0.0


async def get_current_leverage(ex: ccxt.Exchange, symbol: str) -> Optional[float]:
    """
    Bitget에 설정된 현재 레버리지 가져오기.
    - ccxt의 fetch_positions 결과에 leverage가 있으면 그 값을 쓰고,
    - 없다면 v2 API 직접 호출(실패 시 None 반환 → 이후 기본값 10으로 가드)
    """
    # 1) 포지션에서 가져오기(있을 때만)
    try:
        poss = await ex.fetch_positions([symbol], params={
            "productType": PRODUCT_TYPE,
            "marginCoin": MARGIN_COIN,
        })
        for p in poss or []:
            if p.get("symbol") == symbol:
                lev = p.get("leverage")
                if lev is not None:
                    return float(lev)
    except Exception:
        pass

    # 2) Bitget 전용 API (가능하면 시도; 실패해도 앱은 동작)
    try:
        # ccxt의 raw-call: ex.fetch2(path, api, method, params)
        # GET /api/v2/mix/account/leverage?symbol=XXX&productType=USDT-FUTURES
        res = await ex.fetch2(
            "account/leverage",
            api="v2PrivateMixGet",
            method="GET",
            params={"symbol": symbol.replace("/", ""), "productType": PRODUCT_TYPE},
        )
        # Bitget 포맷: {"code":"00000","data":{"leverage": "10", ...}}
        data = (res or {}).get("data") or {}
        lev = data.get("leverage")
        if lev:
            return float(lev)
    except Exception:
        pass

    return None  # 알 수 없으면 None


async def fetch_positions_by_symbol(ex: ccxt.Exchange, symbol: str) -> Tuple[float, float]:
    """
    해당 심볼의 현재 포지션 수량(롱/숏)을 리턴.
    - 반환: (long_qty, short_qty)  (기본 단위: base asset 수량)
    """
    long_qty, short_qty = 0.0, 0.0
    try:
        poss = await ex.fetch_positions([symbol], params={
            "productType": PRODUCT_TYPE,
            "marginCoin": MARGIN_COIN,
        })
        for p in poss or []:
            if p.get("symbol") != symbol:
                continue
            side = (p.get("side") or p.get("info", {}).get("holdSide") or "").lower()
            amt = float(p.get("contracts") or p.get("positionAmt") or 0.0)
            # bitget: contracts>0 라면 "long", <0 라면 "short" 처리될 수도 있음
            if side == "long" or amt > 0:
                long_qty = abs(amt)
            elif side == "short" or amt < 0:
                short_qty = abs(amt)
    except Exception:
        pass
    return long_qty, short_qty


async def count_open_symbols(ex: ccxt.Exchange) -> int:
    """현재 오픈(롱/숏) 중인 심볼 개수"""
    try:
        poss = await ex.fetch_positions(params={
            "productType": PRODUCT_TYPE,
            "marginCoin": MARGIN_COIN,
        })
        symbols = set()
        for p in poss or []:
            amt = float(p.get("contracts") or p.get("positionAmt") or 0.0)
            if abs(amt) > 0:
                symbols.add(p.get("symbol"))
        return len(symbols)
    except Exception:
        return 0


def quantize_amount(mkt: Dict[str, Any], amount: float) -> float:
    """
    시장의 step/최소량에 맞게 반올림/하한 적용
    """
    if amount <= 0:
        return 0.0
    step = (mkt.get("precision") or {}).get("amount")  # 자릿수
    step_size = (mkt.get("limits") or {}).get("amount", {}).get("min", None)

    # 소수 자릿 반올림
    if step is not None:
        q = 10 ** step
        amount = math.floor(amount * q) / q

    # 최소 수량 적용
    if step_size:
        if amount < float(step_size):
            amount = 0.0
    return float(amount)


async def build_order_amount(
    ex: ccxt.Exchange,
    symbol: str,
    price: float,
    equal_notional: bool = True,
) -> Tuple[float, float]:
    """
    주문 수량 및 사용 마진 계산:
    - 마진 = free_balance * FRACTION_PER_POSITION
    - 레버리지는 Bitget 설정값을 조회(get_current_leverage)
    - 수량 = (마진 * 레버리지) / price
    """
    free = await fetch_usdt_free(ex)
    margin = max(free * FRACTION_PER_POSITION, 0.0)

    # 레버리지 조회(없으면 10으로 가드)
    lev = await get_current_leverage(ex, symbol)
    if lev is None or lev <= 0:
        lev = 10.0

    notional = margin * lev
    if notional <= 0 or price <= 0:
        return 0.0, 0.0

    raw_amount = notional / price

    mkt = await get_market(ex, symbol)
    if not mkt:
        return 0.0, 0.0

    amount = quantize_amount(mkt, raw_amount)

    # Bitget 최소 명목가(quote) 체크: markets[].limits.cost.min 있을 경우 고려
    min_cost = (((mkt.get("limits") or {}).get("cost") or {}).get("min")) or 0.0
    if min_cost and amount * price < float(min_cost):
        # 최소 명목가 미만이면 주문 무효
        return 0.0, 0.0

    return amount, margin


def is_reduce_only_for(side: str, close_long: bool, close_short: bool) -> bool:
    """
    reduceOnly 결정: 롱 정리(sell) or 숏 정리(buy)면 True
    """
    if side == "sell" and close_long:
        return True
    if side == "buy" and close_short:
        return True
    return False


async def place_order_bitget(
    ex: ccxt.Exchange,
    symbol: str,
    action_side: str,            # 'buy' or 'sell'
    amount: float,
    reduce_only: bool,
) -> Dict[str, Any]:
    if DRY_RUN:
        log("DRY_RUN: create_order skipped", {"symbol": symbol, "side": action_side, "amount": amount, "reduceOnly": reduce_only})
        return {"dry_run": True}

    params = {
        # Bitget(close) : reduceOnly=true 로 정리
        "reduceOnly": reduce_only,
        "productType": PRODUCT_TYPE,
    }

    try:
        order = await ex.create_order(symbol, "market", action_side, amount, None, params)
        return order or {}
    except ccxt.InsufficientFunds as e:
        log("order_failed: insufficient funds", {"symbol": symbol, "msg": str(e)})
        return {"error": "insufficient_funds"}
    except ccxt.ExchangeError as e:
        log("order_failed: exchange error", {"symbol": symbol, "msg": str(e)})
        return {"error": "exchange_error", "detail": str(e)}
    except Exception as e:
        log("order_failed: unknown error", {"symbol": symbol, "msg": str(e)})
        return {"error": "unknown", "detail": str(e)}


# ────────────────────────────────────────────────────────────────────────────────
# Core: webhook
# ────────────────────────────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = TVPayload.parse_obj(await req.json())
    except Exception:
        log("bad_payload")
        return {"ok": True}

    if payload.secret != WEBHOOK_SECRET:
        log("auth_failed")
        return {"ok": True}

    symbol = payload.symbol.replace("_", "/").upper()  # "DOGEUSDT.P" → "DOGEUSDT.P" (Bitget ccxt 심볼 그대로 쓰는 경우가 많음)
    side = payload.side.lower().strip()  # 'buy' or 'sell'
    if side not in ("buy", "sell"):
        log("invalid_side", payload.dict())
        return {"ok": True}

    ex = await create_exchange()
    try:
        # 현재 포지션 상태
        long_qty, short_qty = await fetch_positions_by_symbol(ex, symbol)
        have_long = long_qty > 0
        have_short = short_qty > 0

        # 현재 오픈 중인 심볼 수
        open_cnt = await count_open_symbols(ex)

        # 가격/수량 계산(신규/추가 때만 사용)
        price = await fetch_price(ex, symbol)

        # 이번 신호의 의미 판정
        will_close_long = (side == "sell" and have_long)
        will_close_short = (side == "buy" and have_short)

        is_open_signal = (side == "buy" and not have_short and not have_long) or (side == "sell" and not have_long and not have_short)
        is_add_signal = (side == "buy" and have_long) or (side == "sell" and have_short)

        # 숏 허용 체크
        if side == "sell" and not have_long and not ALLOW_SHORTS:
            # 숏 신규 오픈 불가 → 무시
            log("skip: short not allowed", {"symbol": symbol})
            return {"ok": True}

        # 신규 오픈 차단(맥스 코인)
        if (is_open_signal or is_add_signal) and not (will_close_long or will_close_short):
            # 신규 또는 추가인데 reduce가 아닌 경우 → MAX_COINS 검사
            if open_cnt >= MAX_COINS:
                # 정리 신호는 아니므로 신규/추가 차단
                recent_blocked_open[symbol] = now_ts() + BLOCKED_OPEN_TTL_SEC
                log("blocked: max coins reached", {"open_cnt": open_cnt, "symbol": symbol})
                return {"ok": True}

        # 최근 '신규 오픈' 차단된 심볼은 잠시 받아주지 않음(물타기/정리는 허용)
        if is_open_signal and symbol in recent_blocked_open and now_ts() < recent_blocked_open[symbol]:
            log("skip: recently blocked open", {"symbol": symbol})
            return {"ok": True}
        else:
            # 만료된 블록은 정리
            if symbol in recent_blocked_open and now_ts() >= recent_blocked_open[symbol]:
                recent_blocked_open.pop(symbol, None)

        # reduceOnly 여부
        reduce_only = is_reduce_only_for(side, will_close_long, will_close_short)

        # 정리 주문이면 보유수량만큼 전량 정리
        if reduce_only:
            close_amt = long_qty if will_close_long else short_qty
            close_amt = float(close_amt)
            if close_amt <= 0:
                log("skip: nothing to close", {"symbol": symbol})
                return {"ok": True}

            # 수량 스텝 맞추기
            mkt = await get_market(ex, symbol)
            amt = quantize_amount(mkt, close_amt) if mkt else close_amt
            if amt <= 0:
                log("skip: quantized close amount is zero", {"symbol": symbol, "raw": close_amt})
                return {"ok": True}

            order = await place_order_bitget(ex, symbol, side, amt, True)
            log("close_order", {"symbol": symbol, "side": side, "amount": amt, "resp": order})
            return {"ok": True}

        # 신규/추가 주문이면 동일 마진으로 계산
        # (TV size는 FORCE_EQUAL_NOTIONAL=true면 무시)
        amount, used_margin = await build_order_amount(ex, symbol, price, equal_notional=FORCE_EQUAL_NOTIONAL)
        if amount <= 0:
            log("skip: calc amount is zero", {"symbol": symbol, "price": price})
            return {"ok": True}

        order = await place_order_bitget(ex, symbol, side, amount, False)
        log("open_or_add_order", {"symbol": symbol, "side": side, "amount": amount, "used_margin": used_margin, "resp": order})
        return {"ok": True}

    except Exception as e:
        log("webhook_error", str(e))
        return {"ok": True}
    finally:
        await close_exchange(ex)


@app.get("/status")
async def status():
    info = {
        "dry_run": DRY_RUN,
        "force_equal_notional": FORCE_EQUAL_NOTIONAL,
        "fraction_per_position": FRACTION_PER_POSITION,
        "max_coins": MAX_COINS,
        "allow_shorts": ALLOW_SHORTS,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "blocked_open_symbols": list(recent_blocked_open.keys()),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    log("status", info)
    return info
