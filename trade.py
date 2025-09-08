import os
import logging
import ccxt.async_support as ccxt

log = logging.getLogger("router")

# 내부 사용: 키 마스킹
def _mask(s: str) -> str:
    if not s:
        return ""
    s = s.strip()
    return ("*" * max(0, len(s) - 4)) + s[-4:]


async def get_exchange():
    """
    Bitget용 ccxt 인스턴스 생성.
    - 반드시 BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSWORD 를 읽는다.
    - 누락 시 어떤 항목이 비었는지 로깅하고 ValueError를 던진다.
    """
    api_key = (os.getenv("BITGET_API_KEY") or os.getenv("BITGET_KEY") or "").strip()
    api_secret = (os.getenv("BITGET_API_SECRET") or os.getenv("BITGET_SECRET") or "").strip()
    api_password = (
        os.getenv("BITGET_PASSWORD")
        or os.getenv("BITGET_PASSPHRASE")
        or os.getenv("BITGET_API_PASSPHRASE")
        or ""
    ).strip()

    missing = []
    if not api_key:
        missing.append("BITGET_API_KEY")
    if not api_secret:
        missing.append("BITGET_API_SECRET")
    if not api_password:
        missing.append("BITGET_PASSWORD")

    if missing:
        log.error("Missing Bitget credentials: %s", ", ".join(missing))
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    exchange = ccxt.bitget({
        "apiKey": api_key,
        "secret": api_secret,
        "password": api_password,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "defaultSubType": "linear",
            "productType": "USDT-FUTURES",
        },
    })

    try:
        await exchange.load_markets()
        log.info(
            "Bitget creds loaded (key=%s, secret=%s, pass=%s)",
            _mask(api_key), _mask(api_secret), _mask(api_password)
        )
        return exchange
    except Exception:
        try:
            await exchange.close()
        finally:
            raise


# --------------------------------------------------
# 기존에 있던 나머지 주문/포지션 함수들
# --------------------------------------------------

async def get_position(exchange, symbol):
    positions = await exchange.fetch_positions([symbol])
    for p in positions:
        if p["symbol"] == symbol:
            return p
    return None


async def create_order(exchange, symbol, side, amount, price=None, params=None):
    if params is None:
        params = {}
    try:
        if price:
            order = await exchange.create_order(symbol, "limit", side, amount, price, params)
        else:
            order = await exchange.create_order(symbol, "market", side, amount, None, params)
        log.info("Order created: %s", order)
        return order
    except Exception as e:
        log.error("Order failed: %s", str(e))
        raise


async def close_position(exchange, symbol, side, amount, params=None):
    if params is None:
        params = {}
    try:
        order = await exchange.create_order(symbol, "market", side, amount, None, params)
        log.info("Position closed: %s", order)
        return order
    except Exception as e:
        log.error("Close position failed: %s", str(e))
        raise


async def cancel_all_orders(exchange, symbol):
    try:
        result = await exchange.cancel_all_orders(symbol)
        log.info("All orders cancelled for %s", symbol)
        return result
    except Exception as e:
        log.error("Cancel all orders failed: %s", str(e))
        raise


async def get_balance(exchange):
    try:
        balance = await exchange.fetch_balance()
        log.info("Balance fetched")
        return balance
    except Exception as e:
        log.error("Balance fetch failed: %s", str(e))
        raise