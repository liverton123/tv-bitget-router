import os
import math
from typing import Any, Dict, Optional, List

import ccxt.async_support as ccxt

# ===== 환경변수 =====
BITGET_API_KEY = os.getenv("bitget_api_key", "").strip()
BITGET_API_SECRET = os.getenv("bitget_api_secret", "").strip()
BITGET_API_PASSWORD = os.getenv("bitget_api_password", "").strip()
BITGET_PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl").strip()  # 선물 상품 타입 (기본: 선물 USDT)
MARGIN_MODE = os.getenv("MARGIN_MODE", "cross").strip().lower()  # 'cross' | 'isolated'
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

# 고정 진입 USD (요청사항: 6달러 고정)
FIXED_ENTRY_USD = float(os.getenv("FIXED_ENTRY_USD", "6"))

# (과거 호환) 비율 기반 진입값 - 필요할 경우만 사용
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))

CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))

# ===== 공통 =====
def _require_bitget_creds():
    if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSWORD):
        raise ValueError("Missing Bitget credentials (key/secret/password).")


async def get_exchange() -> ccxt.bitget:
    _require_bitget_creds()
    exchange = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "options": {
            "defaultType": "swap",       # 선물
            "defaultSubType": "linear",  # USDT 선물
        },
        "enableRateLimit": True,
    })
    await exchange.load_markets()
    return exchange


def normalize_symbol(symbol: str) -> str:
    """
    TradingView에서 전달되는 심볼을 Bitget 선물 심볼로 표준화.
    예) 'BTCUSDT.P' -> 'BTC/USDT:USDT'
    """
    s = symbol.replace(".P", "").replace(".p", "")
    base, quote = s[:-4], s[-4:]  # 대략적 파싱 (XXXXUSDT)
    standard = f"{base}/{quote}:USDT"
    return standard


async def get_open_position(exchange, symbol: str) -> Optional[Dict[str, Any]]:
    """
    심볼의 오픈 포지션(있으면) 정보를 반환.
    ccxt의 fetch_positions 사용 (bitget는 심볼 지정이 안전)
    """
    try:
        positions = await exchange.fetch_positions([symbol])
    except Exception:
        positions = []
    for p in positions or []:
        if (p.get("symbol") or "").lower() == symbol.lower():
            # 수량이 0이면 미보유로 간주
            contracts = float(p.get("contracts") or 0)
            if contracts != 0:
                return p
    return None


async def list_open_symbols(exchange) -> List[str]:
    try:
        positions = await exchange.fetch_positions()
    except Exception:
        positions = []
    out = []
    for p in positions or []:
        contracts = float(p.get("contracts") or 0)
        if contracts != 0:
            out.append(p.get("symbol"))
    return out


async def can_open_more_symbols(exchange, symbol: str, max_coins: int) -> bool:
    """
    심볼 슬롯 제한 체크:
      - 새로 진입하는 심볼이 기존 오픈 심볼에 포함되면 OK
      - 포함되지 않으면, 현재 오픈 심볼 수가 max_coins 미만이어야 OK
    """
    open_syms = await list_open_symbols(exchange)
    if symbol in open_syms:
        return True
    return len(open_syms) < max_coins


async def ensure_leverage_and_mode(exchange, symbol: str):
    """
    Bitget 레버리지/마진모드 보정.
    (여기서는 서버측 기본세팅 유지: 레버리지는 거래소 앱에서 설정된 값 사용)
    마진모드는 cross/isolated 환경변수로 적용 시도.
    """
    try:
        if MARGIN_MODE in ("cross", "isolated"):
            # Bitget ccxt는 setMarginMode(symbol 단위) 지원
            await exchange.set_margin_mode(MARGIN_MODE, symbol=symbol)
    except Exception:
        # 실패해도 트레이딩 진행은 가능하므로 무시
        pass


async def fetch_ticker_price(exchange, symbol: str) -> float:
    ticker = await exchange.fetch_ticker(symbol)
    return float(ticker["last"])


async def usd_to_contracts(exchange, symbol: str, usd: float) -> float:
    """
    고정 USD 기준으로 시장가 계약수로 변환.
    - 레버리지는 거래소(앱) 설정값을 따름 -> 여기서는 단순히 '원금 USD / 가격'
    - 최소 수량/스텝은 exchange.market의 limits를 참고하여 맞춤.
    """
    price = await fetch_ticker_price(exchange, symbol)
    if price <= 0:
        return 0.0

    market = exchange.market(symbol)
    amount_step = market.get("limits", {}).get("amount", {}).get("step") or market.get("precision", {}).get("amount")
    min_amount = market.get("limits", {}).get("amount", {}).get("min") or 0

    qty = usd / price  # 레버리지는 거래소가 알아서 반영 (증거금 6$ 기준)

    # 스텝/최소치 보정
    if amount_step:
        step = float(amount_step)
        if step > 0:
            qty = math.floor(qty / step) * step
    if min_amount and qty < float(min_amount):
        qty = float(min_amount)

    return max(0.0, float(exchange.amount_to_lots(symbol, qty) if hasattr(exchange, "amount_to_lots") else qty))


async def place_market_order(exchange, symbol: str, side: str, amount: float) -> Dict[str, Any]:
    """
    시장가 주문 실행. side: 'buy' | 'sell'
    """
    order = await exchange.create_order(symbol, "market", side, amount)
    return order


async def close_position_market(exchange, symbol: str) -> None:
    """
    전량 청산 (반대 방향 시장가)
    """
    pos = await get_open_position(exchange, symbol)
    if not pos:
        return

    side = (pos.get("side") or pos.get("posSide") or "").lower()
    contracts = float(pos.get("contracts") or 0)
    if contracts <= 0:
        return

    close_side = "sell" if side == "long" else "buy"
    # 일부 거래소는 reduceOnly 지원, bitget은 'reduceOnly' 옵션 지원 여부 제한적 -> 단순 반대주문
    await place_market_order(exchange, symbol, close_side, abs(contracts))