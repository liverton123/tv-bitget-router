import os
import logging
from typing import Any, Dict

import ccxt.async_support as ccxt  # 비동기 ccxt
# 주의: ccxt.pro가 아닌 async_support만 사용

logger = logging.getLogger("tv-bitget-router.trade")


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name, default)
    if v is None:
        return None
    v = v.strip()
    if v == "" or v.lower() in ("none", "null"):
        return None
    return v


def _convert_tv_symbol_to_ccxt(symbol: str) -> str:
    """
    TradingView 선물표기(예: 'HBARUSDT.P') -> ccxt Bitget 표기('HBAR/USDT:USDT')
    """
    s = symbol.upper().strip()
    # '.P' 붙는 경우만 처리
    if s.endswith(".P") and "USDT" in s:
        # 예: HBARUSDT.P -> HBAR/USDT:USDT
        base = s.replace(".P", "")
        if base.endswith("USDT"):
            base_asset = base[:-4]  # 'HBAR'
            return f"{base_asset}/USDT:USDT"
    # 그대로 넘겨보고, 거래소에서 실패하면 예외 처리
    return symbol


async def get_exchange():
    """
    Bitget 인스턴스 생성. 호출자에서 반드시 await exchange.close() 호출.
    - 인증 환경변수 키 이름은 절대 변경하지 않음:
      bitget_api_key / bitget_api_secret / bitget_api_password
    - productType은 기본 USDT-FUTURES (이전 'Parameter productType cannot be empty' 방지)
    """
    api_key = _env("bitget_api_key")
    api_secret = _env("bitget_api_secret")
    api_password = _env("bitget_api_password")

    if not (api_key and api_secret and api_password):
        # app.py에서 미리 걸러주지만, 여기서도 한 번 더 검증
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    product_type = _env("bitget_product_type", "USDT-FUTURES")

    exchange = ccxt.bitget({
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_password,
        "enableRateLimit": True,
        "options": {
            # 선물(USDT Perp) 기본
            "defaultType": "swap",
            # CCXT가 Bitget에 넘길 productType (주요 에러의 원인 파라미터)
            "defaultProductType": product_type,
        },
    })

    # 네트워크 설정(필요 시)
    # exchange.aiohttp_proxy = _env("HTTP_PROXY") or None

    return exchange


async def _create_market_order(exchange, symbol: str, side: str, size: float, params: Dict[str, Any] | None = None):
    """
    Bitget 마켓 주문 (swap). side: 'buy' or 'sell'
    Bitget은 포지션 모드나 사이즈 단위(perp에서는 계약수) 등에 따라 파라미터가 필요할 수 있으니
    productType 기본값을 항상 포함.
    """
    params = dict(params or {})
    params.setdefault("productType", _env("bitget_product_type", "USDT-FUTURES"))

    ccxt_symbol = _convert_tv_symbol_to_ccxt(symbol)
    # Bitget은 amount가 '계약수' 기준. TV에서 contracts를 넘긴다고 가정.
    try:
        order = await exchange.create_order(
            symbol=ccxt_symbol,
            type="market",
            side=side,
            amount=size,
            price=None,
            params=params,
        )
        return order
    except Exception as e:
        logger.exception("create_order failed")
        raise


async def smart_route(
    exchange,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    raw: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    간단 라우터:
    - order_type == 'market' 만 지원(기존 템플릿 준수)
    - side in {'buy','sell'}
    - DCA/종료/진입 등의 상위 전략 구분은 TV 쪽에서 payload로 관리하고,
      여기서는 받은 side/size로만 체결 (불필요 변경 금지)
    """
    side = side.lower()
    order_type = order_type.lower()

    if order_type != "market":
        raise ValueError(f"Unsupported orderType: {order_type}")

    if side not in ("buy", "sell"):
        raise ValueError(f"Unsupported side: {side}")

    result = await _create_market_order(
        exchange=exchange,
        symbol=symbol,
        side=side,
        size=size,
        params=None,
    )
    return {"symbol": symbol, "side": side, "size": size, "exchange_result": result}