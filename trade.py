import os
import math
import time
from typing import Optional, Tuple, Literal, Dict, Any

import asyncio
import aiohttp

BITGET_BASE = "https://api.bitget.com"
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # U本位 永续
MARGIN_COIN = "USDT"

API_KEY = os.getenv("bitget_api_key")
API_SECRET = os.getenv("bitget_api_secret")
API_PASSWORD = os.getenv("bitget_api_password")

# 고정 마진(USDT)
FORCE_FIXED_SIZING = os.getenv("FORCE_FIXED_SIZING", "true").lower() == "true"
FIXED_MARGIN_USD = float(os.getenv("FIXED_MARGIN_USD", "6"))

# 재진입/반대 신호 처리
REQUIRE_INTENT_FOR_OPEN = os.getenv("REQUIRE_INTENT_FOR_OPEN", "true").lower() == "true"
MAX_COINS = int(os.getenv("MAX_COINS", "5"))

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))

# 최소/스텝 정보 캐시
symbol_meta_cache: Dict[str, Dict[str, float]] = {}
# 현재 보유(심볼→("long"|"short", qty))
position_cache: Dict[str, Tuple[str, float]] = {}
pos_cache_ts = 0.0


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_symbol(tv_symbol: str) -> str:
    # VINEUSDT.P, SUIUSDT.P 등 접미사 제거
    s = tv_symbol.upper().strip()
    for suf in (".P", ".PERP", "-PERP"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def sign(params: Dict[str, str]) -> str:
    import hmac, hashlib

    # Bitget v2 서명 규칙 (query/body + timestamp)
    ts = str(now_ms())
    params["timestamp"] = ts
    message = ts + "POST" + "/api/v2/mix/order/place-order" + (params.get("body") or "")
    return ts, hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


async def http(session: aiohttp.ClientSession, method: str, path: str, params=None, json=None, auth=False):
    url = BITGET_BASE + path
    headers = {}
    if auth:
        # v2 공통 헤더 (Bitget 최신 사양)
        ts = str(now_ms())
        body = "" if json is None else aiohttp.payload.JsonPayload(json).buffer.decode()
        prehash = ts + method.upper() + path + (("" if method.upper() == "GET" else body))
        import hmac, hashlib, base64

        sign = base64.b64encode(hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
        headers.update(
            {
                "ACCESS-KEY": API_KEY,
                "ACCESS-SIGN": sign,
                "ACCESS-PASSPHRASE": API_PASSWORD,
                "ACCESS-TIMESTAMP": ts,
                "Content-Type": "application/json",
                "locale": "en-US",
            }
        )

    async with session.request(method, url, params=params, json=json, headers=headers, timeout=20) as r:
        data = await r.json(content_type=None)
        return data


async def fetch_positions(session: aiohttp.ClientSession) -> Dict[str, Tuple[str, float]]:
    """
    심볼별 현재 포지션 (방향, 수량) 반환. 없으면 미포함.
    """
    global position_cache, pos_cache_ts
    if time.time() - pos_cache_ts < 2.0 and position_cache:
        return position_cache

    out: Dict[str, Tuple[str, float]] = {}

    # Bitget v2 positions
    params = {"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}
    data = await http(session, "GET", "/api/v2/mix/position/single-position-v2", params=params, auth=True)
    # 일부 계정에서 single-position-v2가 아닌 전체목록 엔드포인트 필요
    if not isinstance(data, dict) or data.get("code") != "00000":
        data = await http(session, "GET", "/api/v2/mix/position/all-position", params={"productType": PRODUCT_TYPE}, auth=True)

    if isinstance(data, dict) and data.get("code") == "00000":
        rows = data.get("data") or []
        for row in rows:
            symbol = (row.get("symbol") or row.get("instId") or "").upper()
            size = float(row.get("total") or row.get("holdVol") or 0)
            side = "long" if (row.get("holdSide") in ("long", "BUY", "buy")) else "short" if size > 0 else ""
            if size > 0 and side:
                out[symbol] = (side, size)

    position_cache = out
    pos_cache_ts = time.time()
    return out


async def fetch_symbol_meta(session: aiohttp.ClientSession, symbol: str) -> Dict[str, float]:
    """
    최소 수량, 수량 스텝, 가격 스텝 정보
    """
    if symbol in symbol_meta_cache:
        return symbol_meta_cache[symbol]

    params = {"productType": PRODUCT_TYPE}
    data = await http(session, "GET", "/api/v2/mix/market/contracts", params=params)
    min_qty = 0.0001
    qty_step = 0.0001
    price_step = 0.0001

    if isinstance(data, dict) and data.get("code") == "00000":
        for it in data.get("data", []):
            if (it.get("symbol") or it.get("instId") or "").upper() == symbol:
                min_qty = float(it.get("minTradeNum") or min_qty)
                qty_step = float(it.get("sizeMultiplier") or qty_step)
                price_step = float(it.get("pricePlace") or 4)
                # price_place 가 자리수면 스텝으로 변환
                if price_step > 1:
                    price_step = 10 ** (-int(price_step))
                break

    meta = {"min_qty": min_qty, "qty_step": qty_step, "price_step": price_step}
    symbol_meta_cache[symbol] = meta
    return meta


def round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step


def compute_qty_from_margin(price: float, leverage: float, margin_usd: float, min_qty: float, qty_step: float) -> float:
    notional = leverage * margin_usd
    qty = max(min_qty, notional / max(price, 1e-12))
    qty = round_step(qty, qty_step)
    return qty


async def get_symbol_leverage(session: aiohttp.ClientSession, symbol: str, default_leverage: float = 10.0) -> float:
    """
    심볼별 현재 레버리지(사용자가 거래소 UI에서 설정한 값). 조회 실패시 기본값 10.
    """
    data = await http(session, "GET", "/api/v2/mix/account/account", params={"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}, auth=True)
    if isinstance(data, dict) and data.get("code") == "00000":
        for row in data.get("data", []):
            if (row.get("symbol") or "").upper() == symbol:
                try:
                    lev = float(row.get("leverage") or row.get("crossLeverage") or default_leverage)
                    if lev > 0:
                        return lev
                except:
                    pass
    return default_leverage


async def fetch_last_price(session: aiohttp.ClientSession, symbol: str) -> float:
    data = await http(session, "GET", "/api/v2/mix/market/ticker", params={"symbol": symbol, "productType": PRODUCT_TYPE})
    if isinstance(data, dict) and data.get("code") == "00000":
        d = (data.get("data") or {})
        return float(d.get("lastPr") or d.get("last") or d.get("close") or 0.0)
    return 0.0


def decide_intent(current: Dict[str, Tuple[str, float]], symbol: str, side: Literal["buy", "sell"]) -> Literal["entry", "dca", "exit"]:
    s = symbol.upper()
    have = current.get(s)
    if not have:
        return "entry"
    pos_side, _ = have
    if (pos_side == "long" and side == "buy") or (pos_side == "short" and side == "sell"):
        return "dca"
    return "exit"


async def count_distinct_symbols(session: aiohttp.ClientSession) -> int:
    pos = await fetch_positions(session)
    return len(pos.keys())


async def place_market_order(session: aiohttp.ClientSession, symbol: str, side: Literal["buy", "sell"], qty: float, reduce_only: bool):
    body = {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "size": str(qty),
        "price": None,
        "orderType": "market",
        "side": "buy" if side == "buy" else "sell",
        "reduceOnly": True if reduce_only else False,
    }
    data = await http(session, "POST", "/api/v2/mix/order/place-order", json=body, auth=True)
    return data


async def handle_signal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload: { secret, symbol, side, orderType, size }
    """
    secret = str(payload.get("secret", ""))
    if secret != os.getenv("WEBHOOK_SECRET"):
        return {"ok": False, "reason": "bad secret"}

    raw_symbol = str(payload.get("symbol", ""))
    side_raw = str(payload.get("side", "")).lower().strip()
    side: Literal["buy", "sell"]
    if side_raw not in ("buy", "sell"):
        return {"ok": False, "reason": f"bad side {side_raw}"}
    side = "buy" if side_raw == "buy" else "sell"

    symbol = normalize_symbol(raw_symbol)

    async with aiohttp.ClientSession() as session:
        # 현재 포지션 & 의도 판정
        cur_pos = await fetch_positions(session)
        intent = decide_intent(cur_pos, symbol, side)

        # MAX_COINS 제한 (진입만 막음, 물타기/종료는 허용)
        if intent == "entry":
            if await count_distinct_symbols(session) >= MAX_COINS:
                return {"ok": True, "skipped": "max_coins_reached"}

            if REQUIRE_INTENT_FOR_OPEN and not ALLOW_SHORTS and side == "sell":
                return {"ok": True, "skipped": "shorts_disabled"}

        # 수량 계산
        last_price = await fetch_last_price(session, symbol)
        meta = await fetch_symbol_meta(session, symbol)
        min_qty = meta["min_qty"]
        qty_step = meta["qty_step"]

        if FORCE_FIXED_SIZING:
            lev = await get_symbol_leverage(session, symbol, default_leverage=10.0)
            qty = compute_qty_from_margin(last_price, lev, FIXED_MARGIN_USD, min_qty, qty_step)
        else:
            # 백업 경로: TV가 보내는 size 사용 (하지만 기본은 고정마진 사용)
            tv_size = float(payload.get("size") or 0)
            qty = max(min_qty, round_step(float(tv_size), qty_step))

        if qty <= 0:
            return {"ok": False, "reason": "qty_zero"}

        reduce_only = (intent == "exit")
        res = await place_market_order(session, symbol, side, qty, reduce_only=reduce_only)

        return {
            "ok": True,
            "symbol": symbol,
            "side": side,
            "intent": intent,
            "qty": qty,
            "price": last_price,
            "response": res,
        }