import os
import math
import time
import json
from typing import Dict, Tuple, Literal, Any

import asyncio
import aiohttp
from urllib.parse import urlencode

# ===== 환경 =====
BITGET_BASE = "https://api.bitget.com"
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # USDT-M perpetual
MARGIN_COIN = "USDT"

API_KEY = os.getenv("bitget_api_key")
API_SECRET = os.getenv("bitget_api_secret")
API_PASSWORD = os.getenv("bitget_api_password")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))

# 고정 마진 $6
FORCE_FIXED_SIZING = os.getenv("FORCE_FIXED_SIZING", "true").lower() == "true"
FIXED_MARGIN_USD = float(os.getenv("FIXED_MARGIN_USD", "6"))

# ===== 캐시 =====
_symbol_meta: Dict[str, Dict[str, float]] = {}
_position_cache: Dict[str, Tuple[str, float]] = {}
_pos_cache_ts = 0.0


def _now_ms() -> str:
    return str(int(time.time() * 1000))


async def _request(
    session: aiohttp.ClientSession,
    method: Literal["GET", "POST"],
    path: str,
    params: Dict[str, Any] | None = None,
    body_json: Dict[str, Any] | None = None,
    auth: bool = False,
) -> Any:
    """
    Bitget v2 표준 서명. (문제 없게 단순화)
    prehash = ts + method + requestPath(+query) + body
    sign   = Base64(HMAC_SHA256(secret, prehash))
    """
    method = method.upper()
    query = "" if not params else "?" + urlencode(params, doseq=True)
    request_path = path + query
    url = BITGET_BASE + request_path

    headers = {"Content-Type": "application/json"}
    body_str = "" if body_json is None else json.dumps(body_json, separators=(",", ":"))

    if auth:
        import hmac, hashlib, base64

        ts = _now_ms()
        prehash = ts + method + path + query + ("" if method == "GET" else body_str)
        sign = base64.b64encode(hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
        headers.update(
            {
                "ACCESS-KEY": API_KEY,
                "ACCESS-SIGN": sign,
                "ACCESS-PASSPHRASE": API_PASSWORD,
                "ACCESS-TIMESTAMP": ts,
                "locale": "en-US",
            }
        )

    try:
        async with session.request(method, url, data=(None if method == "GET" else body_str), headers=headers, timeout=20) as r:
            # Bitget는 종종 text로 돌려주므로 안전 파싱
            text = await r.text()
            try:
                data = json.loads(text)
            except Exception:
                data = {"code": str(r.status), "raw": text}
            return data
    except asyncio.TimeoutError:
        return {"code": "timeout", "msg": "request timeout"}
    except Exception as e:
        return {"code": "error", "msg": f"{type(e).__name__}"}


async def _fetch_positions(session: aiohttp.ClientSession) -> Dict[str, Tuple[str, float]]:
    """심볼 -> (방향, 수량)"""
    global _position_cache, _pos_cache_ts
    if time.time() - _pos_cache_ts < 2 and _position_cache:
        return _position_cache

    out: Dict[str, Tuple[str, float]] = {}

    data = await _request(session, "GET", "/api/v2/mix/position/all-position", params={"productType": PRODUCT_TYPE}, auth=True)
    if isinstance(data, dict) and data.get("code") == "00000":
        for row in data.get("data") or []:
            sym = (row.get("symbol") or "").upper()
            sz = float(row.get("total") or row.get("holdVol") or 0)
            side_raw = (row.get("holdSide") or "").lower()
            if sz > 0:
                side = "long" if side_raw in ("long", "buy") else "short"
                out[sym] = (side, sz)

    _position_cache = out
    _pos_cache_ts = time.time()
    return out


async def _fetch_symbol_meta(session: aiohttp.ClientSession, symbol: str) -> Dict[str, float]:
    """최소 수량/스텝/가격 스텝"""
    if symbol in _symbol_meta:
        return _symbol_meta[symbol]

    data = await _request(session, "GET", "/api/v2/mix/market/contracts", params={"productType": PRODUCT_TYPE})
    min_qty, qty_step, price_step = 0.0001, 0.0001, 0.0001
    if isinstance(data, dict) and data.get("code") == "00000":
        for it in data.get("data") or []:
            if (it.get("symbol") or "").upper() == symbol:
                min_qty = float(it.get("minTradeNum") or min_qty)
                # sizeMultiplier 가 수량 스텝 역할
                qty_step = float(it.get("sizeMultiplier") or qty_step)
                pp = it.get("pricePlace")
                if pp is not None:
                    price_step = 10 ** (-int(pp))
                break
    meta = {"min_qty": min_qty, "qty_step": qty_step, "price_step": price_step}
    _symbol_meta[symbol] = meta
    return meta


async def _fetch_last_price(session: aiohttp.ClientSession, symbol: str) -> float:
    data = await _request(session, "GET", "/api/v2/mix/market/ticker", params={"symbol": symbol, "productType": PRODUCT_TYPE})
    if isinstance(data, dict) and data.get("code") == "00000":
        d = data.get("data") or {}
        for k in ("lastPr", "last", "close"):
            if d.get(k):
                return float(d[k])
    return 0.0


async def _get_user_leverage(session: aiohttp.ClientSession, symbol: str, default_lev: float = 10.0) -> float:
    """조회 실패해도 절대 예외 안나게"""
    data = await _request(session, "GET", "/api/v2/mix/account/account", params={"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}, auth=True)
    if isinstance(data, dict) and data.get("code") == "00000":
        for row in data.get("data") or []:
            if (row.get("symbol") or "").upper() == symbol:
                for k in ("leverage", "crossLeverage", "fixLeverage"):
                    try:
                        v = float(row.get(k) or 0)
                        if v > 0:
                            return v
                    except Exception:
                        pass
    return default_lev


def _round_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step


def _qty_from_margin(price: float, leverage: float, margin_usd: float, min_qty: float, qty_step: float) -> float:
    notional = leverage * margin_usd
    qty = max(min_qty, notional / max(price, 1e-12))
    return _round_step(qty, qty_step)


def _normalize_symbol(tv_symbol: str) -> str:
    s = tv_symbol.upper().strip()
    for suf in (".P", ".PERP", "-PERP"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def _decide_intent(current: Dict[str, Tuple[str, float]], symbol: str, side: Literal["buy", "sell"]) -> Literal["entry", "dca", "exit"]:
    have = current.get(symbol)
    if not have:
        return "entry"
    pos_side, _ = have
    if (pos_side == "long" and side == "buy") or (pos_side == "short" and side == "sell"):
        return "dca"
    return "exit"


async def _place_market(
    session: aiohttp.ClientSession,
    symbol: str,
    side: Literal["buy", "sell"],
    qty: float,
    reduce_only: bool,
) -> Any:
    body = {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "size": str(qty),
        "orderType": "market",
        "side": side,
        "reduceOnly": True if reduce_only else False,
    }
    return await _request(session, "POST", "/api/v2/mix/order/place-order", body_json=body, auth=True)


async def handle_signal(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload 예: {secret, symbol, side, orderType, size}
    어떤 경우에도 예외를 밖으로 던지지 않고 dict로 반환.
    """
    # 1) 시크릿 체크
    if str(payload.get("secret", "")) != WEBHOOK_SECRET:
        return {"ok": False, "reason": "bad secret"}

    raw_symbol = str(payload.get("symbol", ""))
    side_raw = str(payload.get("side", "")).lower()
    if side_raw not in ("buy", "sell"):
        return {"ok": False, "reason": f"bad side {side_raw}"}
    side: Literal["buy", "sell"] = "buy" if side_raw == "buy" else "sell"
    symbol = _normalize_symbol(raw_symbol)

    async with aiohttp.ClientSession() as session:
        # 2) 현재 포지션 & 의도
        current = await _fetch_positions(session)
        intent = _decide_intent(current, symbol, side)

        # 3) MAX_COINS 제한: 신규 진입만 차단
        if intent == "entry":
            if len(current) >= MAX_COINS:
                return {"ok": True, "skipped": "max_coins_reached", "intent": intent, "symbol": symbol}
            if side == "sell" and not ALLOW_SHORTS:
                return {"ok": True, "skipped": "shorts_disabled", "intent": intent, "symbol": symbol}

        # 4) 수량 계산 (고정 마진: 6 USDT)
        last_price = await _fetch_last_price(session, symbol)
        meta = await _fetch_symbol_meta(session, symbol)
        min_qty, qty_step = meta["min_qty"], meta["qty_step"]

        if FORCE_FIXED_SIZING:
            lev = await _get_user_leverage(session, symbol, default_lev=10.0)
            qty = _qty_from_margin(last_price, lev, FIXED_MARGIN_USD, min_qty, qty_step)
        else:
            # fallback: TV에서 온 size 사용
            try:
                qty = float(payload.get("size") or 0.0)
            except Exception:
                qty = 0.0
            qty = max(min_qty, _round_step(qty, qty_step))

        if qty <= 0:
            return {"ok": False, "reason": "qty_zero", "price": last_price}

        # 5) 주문
        reduce_only = (intent == "exit")
        res = await _place_market(session, symbol, side, qty, reduce_only)

        return {
            "ok": True,
            "intent": intent,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": last_price,
            "response": res,
        }