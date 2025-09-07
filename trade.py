import os
import re
import ccxt.async_support as ccxt

# ---------- Helpers ----------
def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    # 허용 포맷 예: BTCUSDT.P, BTCUSDT_UMCBL 등 -> BTC/USDT:USDT (ccxt bitget 포맷)
    if s.endswith(".P"):
        s = s[:-2]  # HBARUSDT.P -> HBARUSDT
    base_quote = s.replace("_UMCBL", "")
    m = re.match(r"^([A-Z0-9]+)USDT$", base_quote)
    if not m:
        return symbol  # 원본 유지(추후 ccxt에서 실패 시 에러 반환)
    base = m.group(1)
    return f"{base}/USDT:USDT"

def normalize_product_type(v: str) -> str:
    x = (v or "").strip().lower()
    # 비트겟 USDT 무기한: umcbl, 코인 마진: dmcbl 등
    mapping = {
        "umcbl": "umcbl",
        "usdt": "umcbl",
        "usdt_perp": "umcbl",
        "perp": "umcbl",
        "um": "umcbl",
        "dmcbl": "dmcbl",
        "coin": "dmcbl",
    }
    return mapping.get(x, x or "umcbl")

async def get_exchange():
    key = os.getenv("BITGET_KEY", "").strip()
    secret = os.getenv("BITGET_SECRET", "").strip()
    password = os.getenv("BITGET_PASSWORD", "").strip()

    if not key or not secret or not password:
        raise ValueError("Missing Bitget credentials (key/secret/password).")

    ex = ccxt.bitget({
        "apiKey": key,
        "secret": secret,
        "password": password,
        "options": {
            "defaultType": "swap",  # 선물/무기한
            "defaultSubType": "linear",  # USDT margined
        },
        "enableRateLimit": True,
    })
    await ex.load_markets()
    return ex

async def fetch_symbol_positions(ex, symbol: str, product_type: str):
    # bitget은 심볼 미지정으로 전체 조회하는 편이 더 견고함
    params = {"productType": normalize_product_type(product_type)}
    return await ex.fetch_positions(None, params)

def get_net_position(positions, symbol: str, product_type: str):
    unified = normalize_symbol(symbol)
    pt = normalize_product_type(product_type)
    qty = 0.0
    side = None
    for p in positions:
        if p.get("symbol") == unified and p.get("info", {}).get("productType", "").lower() == pt:
            amt = float(p.get("contracts", 0) or 0)
            if amt > 0:
                side = p.get("side") or ("long" if amt > 0 else "short")
            qty += amt if (p.get("side") in {"long", "buy", "open_long"}) else -amt
    return qty, side

async def place_order(ex, symbol: str, side: str, order_type: str, size: float, price=None, product_type="umcbl"):
    unified = normalize_symbol(symbol)
    pt = normalize_product_type(product_type)
    params = {"productType": pt}
    if order_type == "market":
        return await ex.create_order(unified, "market", side, size, None, params)
    elif order_type == "limit":
        if not price:
            raise ValueError("price is required for limit orders")
        return await ex.create_order(unified, "limit", side, size, price, params)
    else:
        raise ValueError(f"unsupported orderType: {order_type}")

async def smart_route(
    ex,
    symbol: str,
    side: str,
    order_type: str,
    size: float,
    intent: str = "auto",
    reenter_on_opposite: bool = False,
    product_type: str = "umcbl",
    price=None,
):
    # 1) 포지션 조회
    pos_list = await fetch_symbol_positions(ex, symbol, product_type)
    net_qty, pos_side = get_net_position(pos_list, symbol, product_type)

    # 2) intent == auto: 사이드/포지션 기준 자동 판단
    action = intent
    if intent == "auto":
        if net_qty == 0:
            action = "entry"  # 신규 진입
        elif pos_side == "long":
            if side == "sell":
                action = "close" if not reenter_on_opposite else "entry"
            else:
                action = "scale"
        elif pos_side == "short":
            if side == "buy":
                action = "close" if not reenter_on_opposite else "entry"
            else:
                action = "scale"

    # 3) 실행
    if action == "close":
        # 반대 주문으로 size 만큼 청산
        close_side = "buy" if side == "sell" else "sell"
        return await place_order(ex, symbol, close_side, order_type, size, price, product_type)
    elif action in {"entry", "scale"}:
        return await place_order(ex, symbol, side, order_type, size, price, product_type)
    else:
        raise ValueError(f"unsupported intent: {intent}")