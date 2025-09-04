import os
import math
import logging
from typing import Dict, Any, List, Optional

import ccxt.async_support as ccxt  # 비동기 CCXT

log = logging.getLogger("router.trade")

DEFAULT_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.1"))  # 총자산 대비
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "false").lower() == "true"
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))  # 2%

# ---------------------------------------------------------------------
# 안전한 심볼 정규화
# ---------------------------------------------------------------------
def normalize_symbol(raw: Any) -> str:
    """
    허용 입력:
      - 'BTCUSDT.P', 'ETHUSDT'
      - 'BTC/USDT:USDT' (이미 정규화)
      - 'XRP/USDT' (콜론 없음)
    반환: 'BASE/USDT:USDT'
    """
    if raw is None:
        raise ValueError("symbol is missing")
    if not isinstance(raw, str):
        raw = str(raw)

    s = raw.strip()
    if not s:
        raise ValueError("symbol is empty")

    # 이미 CCXT 포맷
    if "/" in s and ":USDT" in s:
        return s.upper()

    u = s.upper()

    # TradingView .P 접미사 제거
    if u.endswith(".P"):
        u = u[:-2]

    # 'BTC/USDT' -> 'BTC/USDT:USDT'
    if "/" in u and u.endswith("USDT") and ":USDT" not in u:
        return f"{u}:USDT"

    # 'BTCUSDT' -> 'BTC/USDT:USDT'
    if u.endswith("USDT") and "/" not in u:
        base = u[:-4]
        return f"{base}/USDT:USDT"

    # 마지막 안전망
    if "USDT" in u and ":USDT" not in u and "/" not in u:
        base = u.replace("USDT", "")
        return f"{base}/USDT:USDT"

    return u


# ---------------------------------------------------------------------
# CCXT 익스체인지
# ---------------------------------------------------------------------
async def make_exchange(api_key: str, api_secret: str, password: str, dry_run: bool):
    params = {
        "apiKey": api_key or "",
        "secret": api_secret or "",
        "password": password or "",
        "options": {"defaultType": "swap"},  # 선물/스왑
        "enableRateLimit": True,
    }
    ex = ccxt.bitget(params)
    if dry_run:
        log.warning("[DRY_RUN] 실제 주문은 발생하지 않습니다.")
    return ex


# ---------------------------------------------------------------------
# Bitget 포지션/자산 조회 래퍼
# ---------------------------------------------------------------------
async def fetch_positions_all(ex, product_type: str, margin_coin: str) -> List[Dict[str, Any]]:
    """
    Bitget은 productType, marginCoin이 필요. CCXT의 fetch_positions에 파라미터 넘김.
    """
    try:
        positions = await ex.fetch_positions(
            params={"productType": product_type, "marginCoin": margin_coin}
        )
        return positions or []
    except Exception as e:
        log.error("[CCXT_ERROR] fetch_positions failed")
        raise

async def get_net_position(ex, symbol: str, product_type: str, margin_coin: str) -> float:
    """
    롱 수량(양수) + 숏 수량(음수)의 '순 포지션 계약수' 반환
    """
    positions = await fetch_positions_all(ex, product_type, margin_coin)
    net = 0.0
    for p in positions:
        # CCXT 통일 키
        s = (p.get("symbol") or p.get("info", {}).get("symbol")).upper()
        if s != symbol.upper():
            continue
        # Bitget: info 객체 안 side별 size가 있을 수 있음
        sz = float(p.get("contracts") or p.get("info", {}).get("total", 0) or 0)
        side = (p.get("side") or p.get("info", {}).get("holdSide", "")).lower()
        if side == "long":
            net += sz
        elif side == "short":
            net -= sz
    return net


# ---------------------------------------------------------------------
# 주문 배치
# ---------------------------------------------------------------------
async def place_order(
    ex,
    symbol: str,
    side: str,
    size: float,
    reduce_only: bool,
    order_type: str,
    product_type: str,
    margin_coin: str,
):
    """
    Bitget createOrder:
      params = { "productType": "umcbl", "marginCoin": "USDT", "reduceOnly": True/False }
    size는 계약수(coin-margined이 아니라 usdt-margined 스왑 기준)
    """
    # 방어: 음수/0 방지
    qty = float(size)
    if qty <= 0:
        return {"skipped": True, "reason": "non-positive size"}

    if os.getenv("DRY_RUN", "false").lower() == "true":
        log.info("[DRY_RUN] %s %s size=%s reduceOnly=%s", side, symbol, qty, reduce_only)
        return {"dry_run": True, "side": side, "symbol": symbol, "size": qty, "reduceOnly": reduce_only}

    try:
        order = await ex.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=qty,
            price=None,
            params={
                "productType": product_type,
                "marginCoin": margin_coin,
                "reduceOnly": reduce_only,
            },
        )
        return order
    except ccxt.InsufficientFunds as e:
        log.error("[CCXT_ERROR] insufficient funds: %s", e)
        raise
    except ccxt.ExchangeError as e:
        log.error("[CCXT_ERROR] %s", e)
        raise
    except Exception as e:
        log.error("[CCXT_ERROR] create_order failed: %s", e)
        raise


# ---------------------------------------------------------------------
# 수량 산정 (가벼운 보호 로직)
# ---------------------------------------------------------------------
async def calc_order_size(
    ex,
    symbol: str,
    fraction_per_trade: float = FRACTION_PER_TRADE,
    leverage: int = DEFAULT_LEVERAGE,
) -> float:
    """
    간단 버전: 계정 USDT 잔고 * fraction * leverage / 마크가격
    FORCE_EQUAL_NOTIONAL=True면 심볼 무관 동일 명목가로 맞춤.
    """
    markets = await ex.load_markets()
    m = markets.get(symbol)
    if not m:
        await ex.load_markets(True)
        m = ex.markets.get(symbol)
    if not m:
        raise ValueError(f"Unknown market {symbol}")

    # 잔고
    bal = await ex.fetch_balance()
    usdt = float(bal.get("USDT", {}).get("free", 0) or bal.get("USDT", {}).get("total", 0) or 0)

    ticker = await ex.fetch_ticker(symbol)
    price = float(ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("last", 0))

    if price <= 0:
        raise ValueError("bad price")

    notional = usdt * fraction_per_trade * leverage
    if FORCE_EQUAL_NOTIONAL:
        # 예: 고정 100 USDT 명목
        fixed = float(os.getenv("EQUAL_NOTIONAL_USDT", "100"))
        notional = min(notional, fixed)

    amount = max(0.0, notional / price)

    # 시장의 최소수량 반영
    min_amt = float(m.get("limits", {}).get("amount", {}).get("min", 0) or 0)
    if min_amt and amount < min_amt:
        amount = min_amt

    precision = int(m.get("precision", {}).get("amount", 4))
    amount = float(f"{amount:.{precision}f}")
    return amount


# ---------------------------------------------------------------------
# 엔트리/청산 자동 라우팅
# ---------------------------------------------------------------------
async def smart_route(
    ex,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    product_type: str,
    margin_coin: str,
):
    """
    1) 현재 순포지션 확인
    2) 같은 방향이면 '신규 진입'
       반대 방향이면 먼저 reduceOnly로 닫고 남으면 신규 반전
    """
    # 0) 사이즈 보정(0 또는 None이면 우리는 계산해서 사용)
    if size in (None, 0, "0"):
        size = await calc_order_size(ex, symbol)

    net = await get_net_position(ex, symbol, product_type, margin_coin)
    log.info("[ROUTER] net=%s incoming side=%s size=%s", net, side, size)

    results = []

    if net == 0:
        # 그냥 신규
        res = await place_order(
            ex, symbol, side, size, False, order_type, product_type, margin_coin
        )
        results.append({"entry": res})
        return results

    if net > 0:  # 현재 롱
        if side == "buy":
            # 롱 추가
            res = await place_order(
                ex, symbol, "buy", size, False, order_type, product_type, margin_coin
            )
            results.append({"add_long": res})
        else:
            # 숏 신호 → 먼저 롱 청산
            close_size = min(abs(net) * (1 + CLOSE_TOLERANCE_PCT), size)
            res1 = await place_order(
                ex, symbol, "sell", close_size, True, order_type, product_type, margin_coin
            )
            results.append({"close_long": res1})
            # 남은 사이즈 있으면 반전 진입
            remaining = max(0.0, size - close_size)
            if remaining > 0:
                res2 = await place_order(
                    ex, symbol, "sell", remaining, False, order_type, product_type, margin_coin
                )
                results.append({"open_short": res2})

    else:  # net < 0, 현재 숏
        if side == "sell":
            # 숏 추가
            res = await place_order(
                ex, symbol, "sell", size, False, order_type, product_type, margin_coin
            )
            results.append({"add_short": res})
        else:
            # 롱 신호 → 먼저 숏 청산
            close_size = min(abs(net) * (1 + CLOSE_TOLERANCE_PCT), size)
            res1 = await place_order(
                ex, symbol, "buy", close_size, True, order_type, product_type, margin_coin
            )
            results.append({"close_short": res1})
            remaining = max(0.0, size - close_size)
            if remaining > 0:
                res2 = await place_order(
                    ex, symbol, "buy", remaining, False, order_type, product_type, margin_coin
                )
                results.append({"open_long": res2})

    return results