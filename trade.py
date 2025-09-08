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
    if symbol in symbol_meta