# trade.py
import os
import asyncio
import logging
from typing import Optional, Dict, Any

import ccxt.async_support as ccxt

log = logging.getLogger("router.trade")

BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")

# Bitget USDT-M 선물
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl").lower()  # 'umcbl'
MARGIN_COIN = os.getenv("MARGIN_COIN", "USDT").upper()            # 'USDT'

# 리스크/포지션 설정
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.10"))  # 계정 순자산의 n%
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# singleton exchange
_exchange_singleton = None

async def get_exchange_singleton():
    global _exchange_singleton
    if _exchange_singleton is None:
        if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSWORD):
            raise RuntimeError("Bitget API envs missing. Set BITGET_API_KEY/SECRET/PASSWORD")

        ex = ccxt.bitget({
            "apiKey": BITGET_API_KEY,
            "secret": BITGET_API_SECRET,
            "password": BITGET_API_PASSWORD,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # linear swap
            }
        })
        await ex.load_markets()
        _exchange_singleton = ex
    return _exchange_singleton

# ===== Symbol helpers =====
def normalize_symbol(raw: str) -> str:
    """
    TV에서 오는 예: 'BTCUSDT.P', 'VINEUSDT.P'
    CCXT 통일 심볼: 'BTC/USDT:USDT'
    """
    s = raw.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if s.endswith(":USDT"):
        # 이미 'BTC/USDT:USDT' 형태일 수 있음
        return s
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT:USDT"
    # 최후의 보정
    return s.replace("USDT.P", "/USDT:USDT").replace("USDT", "/USDT:USDT")

# ===== Position helpers =====
async def fetch_positions_all(ex) -> list:
    # Bitget은 fetch_positions에 productType, marginCoin 필요
    params = {"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}
    return await ex.fetch_positions(params)

async def get_net_position(ex, symbol: str) -> float:
    """
    해당 심볼의 순포지션 계약수(롱=+, 숏=-).
    ccxt bitget의 amount(contracts)가 양수(롱)/음수(숏)로 제공된다.
    """
    positions = await fetch_positions_all(ex)
    net = 0.0
    for p in positions or []:
        if p.get("symbol") == symbol and p.get("marginCoin", MARGIN_COIN) == MARGIN_COIN:
            amt = float(p.get("contracts", p.get("amount", 0.0)) or 0.0)
            net += amt
    return net

async def get_price(ex, symbol: str) -> float:
    t = await ex.fetch_ticker(symbol)
    return float(t.get("last") or t.get("close") or 0.0)

async def get_free_usdt(ex) -> float:
    bal = await ex.fetch_balance()
    usdt = bal.get("USDT", {}) or {}
    # futures에서는 'free' 대신 'total'/'used'일 수 있음 → 안전하게
    free = usdt.get("free", None)
    if free is None:
        free = float(usdt.get("total", 0.0)) - float(usdt.get("used", 0.0))
    return float(free or 0.0)

async def ensure_leverage(ex, symbol: str):
    # Bitget은 심볼별 레버리지 설정 필요할 수 있음
    try:
        lev = MAX_LEVERAGE
        await ex.set_leverage(lev, symbol, params={"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN})
    except Exception as e:
        log.info("set_leverage skip/warn: %s", e)

def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

async def compute_amount(ex, symbol: str, side: str, incoming_size: Optional[float]) -> float:
    """
    주문 수량(contracts) 산출.
    - FORCE_EQUAL_NOTIONAL=True 이면: (free * FRACTION * MAX_LEVERAGE) / price
      → 계정 일부만 사용하고 과도한 진입 방지
    - False면: TV에서 주는 size(contracts) 사용
    """
    if not FORCE_EQUAL_NOTIONAL and incoming_size:
        return float(incoming_size)

    price = await get_price(ex, symbol)
    free = await get_free_usdt(ex)
    if price <= 0:
        raise RuntimeError(f"Bad price for {symbol}")
    margin_budget = free * FRACTION_PER_TRADE  # 사용할 증거금
    notional = margin_budget * MAX_LEVERAGE
    contracts = notional / price
    # 최소 1 계약 보장
    return max(1.0, contracts)

async def place_order(
    ex,
    symbol: str,
    side: str,          # 'buy' or 'sell'
    order_type: str,    # 'market' only here
    amount: float,
    reduce_only: bool = False,
) -> Dict[str, Any]:

    params = {
        "reduceOnly": reduce_only,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
    }

    if DRY_RUN:
        log.info("[DRY] %s %s amt=%.6f reduceOnly=%s", side, symbol, amount, reduce_only)
        return {"dryRun": True, "side": side, "symbol": symbol, "amount": amount, "reduceOnly": reduce_only}

    # Bitget은 create_order 사용 (type='market')
    ord = await ex.create_order(symbol, order_type, side, amount, None, params)
    return ord

async def smart_route(
    ex,
    symbol: str,
    side: str,              # 'buy'/'sell'
    order_type: str = "market",
    incoming_size: Optional[float] = None
) -> Dict[str, Any]:
    """
    - 현재 포지션(net) 기준으로 reduceOnly/새 진입 자동판단
    - 과도한 수량 방지(계정 일부만 사용)
    - Bitget fetch_positions 40019 방지(필수 파라미터 항상 전달)
    """
    side = side.lower()
    order_type = order_type.lower()

    await ensure_leverage(ex, symbol)

    # 현재 순포지션
    net = await get_net_position(ex, symbol)
    log.info("[ROUTER] %s net=%.6f incoming side=%s", symbol, net, side)

    # 들어온 주문 의도
    want_long = (side == "buy")
    want_short = (side == "sell")

    # 숏 금지 설정
    if want_short and not ALLOW_SHORTS and net <= 0:
        return {"skipped": True, "reason": "shorts_not_allowed", "symbol": symbol}

    # 수량 계산
    amt = await compute_amount(ex, symbol, side, incoming_size)

    # 1) 기존 포지션과 반대면 우선 청산(reduceOnly)
    if (net > 0 and want_short) or (net < 0 and want_long):
        close_size = min(abs(net), amt)
        if close_size > 0:
            log.info("[ROUTER] reduce-only close %.6f on %s", close_size, symbol)
            res1 = await place_order(ex, symbol, side, close_size, reduce_only=True)
            # 남은 양이 있고 반대 방향 신규 진입 허용이면 이어서 진입
            remain = amt - close_size
            if remain > 0:
                log.info("[ROUTER] new entry remain %.6f on %s", remain, symbol)
                res2 = await place_order(ex, symbol, side, remain, reduce_only=False)
                return {"close": res1, "entry": res2}
            return {"close": res1}

    # 2) 같은 방향이면 단순 증액(피라미딩) or 신규 진입
    log.info("[ROUTER] entry %.6f on %s", amt, symbol)
    res = await place_order(ex, symbol, side, amt, reduce_only=False)
    return {"entry": res}