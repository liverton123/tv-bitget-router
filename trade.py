import os, math, time, json, asyncio, aiohttp
from typing import Dict, Tuple, Any, Literal
from urllib.parse import urlencode

BITGET_BASE = "https://api.bitget.com"
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
MARGIN_COIN  = "USDT"

API_KEY      = os.getenv("bitget_api_key")
API_SECRET   = os.getenv("bitget_api_secret")
API_PASSWORD = os.getenv("bitget_api_password")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
MAX_COINS    = int(os.getenv("MAX_COINS", "5"))
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.02"))

# 고정 마진 $6
FORCE_FIXED_SIZING = os.getenv("FORCE_FIXED_SIZING", "true").lower() == "true"
FIXED_MARGIN_USD   = float(os.getenv("FIXED_MARGIN_USD", "6"))

_symbol_meta: Dict[str, Dict[str, float]] = {}
_position_cache: Dict[str, Tuple[str, float]] = {}
_pos_cache_ts = 0.0

def _now_ms() -> str:
    return str(int(time.time() * 1000))

async def _request(session: aiohttp.ClientSession, method: str, path: str,
                   params: Dict[str, Any] | None = None,
                   body_json: Dict[str, Any] | None = None,
                   auth: bool = False) -> Any:
    method = method.upper()
    query = "" if not params else "?" + urlencode(params, doseq=True)
    url   = BITGET_BASE + path + query
    body_str = "" if body_json is None else json.dumps(body_json, separators=(",", ":"))
    headers = {"Content-Type": "application/json"}

    if auth:
        import hmac, hashlib, base64
        ts = _now_ms()
        prehash = ts + method + path + query + ("" if method == "GET" else body_str)
        sign = base64.b64encode(hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()).decode()
        headers.update({
            "ACCESS-KEY": API_KEY,
            "ACCESS-SIGN": sign,
            "ACCESS-PASSPHRASE": API_PASSWORD,
            "ACCESS-TIMESTAMP": ts,
            "locale": "en-US",
        })

    try:
        async with session.request(method, url, data=(None if method == "GET" else body_str),
                                   headers=headers, timeout=20) as r:
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
    global _position_cache, _pos_cache_ts
    if time.time() - _pos_cache_ts < 2 and _position_cache:
        return _position_cache
    out: Dict[str, Tuple[str, float]] = {}
    data = await _request(session, "GET", "/api/v2/mix/position/all-position",
                          params={"productType": PRODUCT_TYPE}, auth=True)
    if isinstance(data, dict) and data.get("code") == "00000":
        for row in data.get("data") or []:
            sym = (row.get("symbol") or "").upper()
            sz  = float(row.get("total") or row.get("holdVol") or 0)
            side_raw = (row.get("holdSide") or "").lower()
            if sz > 0:
                side = "long" if side_raw in ("long", "buy") else "short"
                out[sym] = (side, sz)
    _position_cache = out
    _pos_cache_ts = time.time()
    return out

async def _fetch_symbol_meta(session: aiohttp.ClientSession, symbol: str) -> Dict[str, float]:
    if symbol in _symbol_meta:
        return _symbol_meta[symbol]
    data = await _request(session, "GET", "/api/v2/mix/market/contracts",
                          params={"productType": PRODUCT_TYPE})
    min_qty, qty_step, price_step = 0.0001, 0.0001, 0.0001
    if isinstance(data, dict) and data.get("code") == "00000":
        for it in data.get("data") or []:
            if (it.get("symbol") or "").upper() == symbol:
                min_qty = float(it.get("minTradeNum") or min_qty)
                qty_step = float(it.get("sizeMultiplier") or qty_step)
                pp = it.get("pricePlace")
                if pp is not None:
                    price_step = 10 ** (-int(pp))
                break
    meta = {"min_qty": min_qty, "qty_step": qty_step, "price_step": price_step}
    _symbol_meta[symbol] = meta
    return meta

async def _fetch_last_price(session: aiohttp.ClientSession, symbol: str) -> float:
    d = await _request(session, "GET", "/api/v2/mix/market/ticker",
                       params={"symbol": symbol, "productType": PRODUCT_TYPE})
    if isinstance(d, dict) and d.get("code") == "00000":
        x = d.get("data") or {}
        for k in ("lastPr", "last", "close"):
            if x.get(k):
                return float(x[k])
    return 0.0

async def _get_user_leverage(session: aiohttp.ClientSession, symbol: str, default_lev: float = 10.0) -> float:
    d = await _request(session, "GET", "/api/v2/mix/account/account",
                       params={"productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN}, auth=True)
    if isinstance(d, dict) and d.get("code") == "00000":
        for row in d.get("data") or []:
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

def _decide_intent(current: Dict[str, Tuple[str, float]],
                   symbol: str, side: Literal["buy","sell"]) -> Literal["entry","dca","exit"]:
    have = current.get(symbol)
    if not have:
        return "entry"
    pos_side, _ = have
    if (pos_side == "long" and side == "buy") or (pos_side == "short" and side == "sell"):
        return "dca"
    return "exit"

async def _place_market(session: aiohttp.ClientSession, symbol: str,
                        side: Literal["buy","sell"], qty: float, reduce_only: bool) -> Any:
    body = {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "marginCoin": MARGIN_COIN,
        "size": str(qty),
        "orderType": "market",
        "side": side,
        "reduceOnly": True if reduce_only else False,
    }
    print(f"[ORDER] place {symbol} {side} qty={qty} reduceOnly={reduce_only}")
    return await _request(session, "POST", "/api/v2/mix/order/place-order", body_json=body, auth=True)

async def handle_signal(payload: Dict[str, Any]) -> Dict[str, Any]:
    # 1) secret
    if str(payload.get("secret", "")) != WEBHOOK_SECRET:
        return {"ok": False, "reason": "bad_secret"}

    raw_symbol = str(payload.get("symbol", ""))
    side_raw   = str(payload.get("side", "")).lower()
    if side_raw not in ("buy","sell"):
        return {"ok": False, "reason": f"bad_side:{side_raw}"}
    side: Literal["buy","sell"] = "buy" if side_raw == "buy" else "sell"
    symbol = _normalize_symbol(raw_symbol)

    async with aiohttp.ClientSession() as session:
        positions = await _fetch_positions(session)
        intent = _decide_intent(positions, symbol, side)

        # 신규 진입만 MAX_COINS 제한 적용
        if intent == "entry":
            if len(positions) >= MAX_COINS:
                print(f"[SKIP] max_coins: {len(positions)} >= {MAX_COINS}")
                return {"ok": True, "skipped": "max_coins", "intent": intent, "symbol": symbol}
            if side == "sell" and not ALLOW_SHORTS:
                print(f"[SKIP] shorts disabled")
                return {"ok": True, "skipped": "shorts_disabled", "intent": intent, "symbol": symbol}

        last = await _fetch_last_price(session, symbol)
        meta = await _fetch_symbol_meta(session, symbol)
        min_qty, qty_step = meta["min_qty"], meta["qty_step"]

        if FORCE_FIXED_SIZING:
            lev = await _get_user_leverage(session, symbol, default_lev=10.0)
            qty = _qty_from_margin(last, lev, FIXED_MARGIN_USD, min_qty, qty_step)
        else:
            try:
                qty = float(payload.get("size") or 0.0)
            except Exception:
                qty = 0.0
            qty = max(min_qty, _round_step(qty, qty_step))

        if qty <= 0:
            print(f"[SKIP] qty_zero price={last} min={min_qty} step={qty_step}")
            return {"ok": False, "reason": "qty_zero", "price": last}

        reduce_only = (intent == "exit")
        res = await _place_market(session, symbol, side, qty, reduce_only)
        code = (isinstance(res, dict) and res.get("code")) or "?"
        if code != "00000":
            print(f"[REJECT] {symbol} {side} qty={qty} code={code} msg={res}")
            return {"ok": False, "reason": "rejected", "intent": intent, "symbol": symbol, "code": code, "resp": res}

        print(f"[FILLED?] req accepted {symbol} {side} qty={qty} intent={intent}")
        return {"ok": True, "intent": intent, "symbol": symbol, "side": side,
                "qty": qty, "price": last, "resp": res}