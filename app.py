import os, json, logging
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import ccxt.async_support as ccxt

# =========================
# 설정 로딩 (ENV)
# =========================
ALLOWED_PRODUCT_TYPES = {"umcbl", "cmcbl", "dmcbl"}  # Bitget 표준 productType (소문자)

def read_config():
    key = os.getenv("BITGET_API_KEY", "").strip()
    secret = os.getenv("BITGET_API_SECRET", "").strip()
    password = os.getenv("BITGET_API_PASSWORD", "").strip()
    product_type = (os.getenv("BITGET_PRODUCT_TYPE", "") or "umcbl").strip().lower()

    if product_type not in ALLOWED_PRODUCT_TYPES:
        raise RuntimeError(
            f"Invalid BITGET_PRODUCT_TYPE='{product_type}', allowed={sorted(ALLOWED_PRODUCT_TYPES)}"
        )

    missing = [n for n, v in [
        ("BITGET_API_KEY", key),
        ("BITGET_API_SECRET", secret),
        ("BITGET_API_PASSWORD", password),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing env: {', '.join(missing)}")

    dry_run = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
    allow_shorts = os.getenv("ALLOW_SHORTS", "true").strip().lower() in ("1", "true", "yes")
    close_tol = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.0") or 0)

    return {
        "key": key,
        "secret": secret,
        "password": password,
        "product_type": product_type,
        "dry_run": dry_run,
        "allow_shorts": allow_shorts,
        "close_tol": close_tol,
    }

# =========================
# 심볼 변환 (TV -> Bitget/ccxt)
# =========================
def tv_to_bitget(tv_symbol: str) -> str:
    """
    TV 예: 'ETHUSDT.P' or 'ETHUSDT' -> 'ETH/USDT:USDT'
    """
    if not tv_symbol:
        raise ValueError("empty symbol")

    s = tv_symbol.upper().replace("PERP", "").replace(".P", "")
    if not s.endswith("USDT"):
        raise ValueError(f"Unsupported symbol format: {tv_symbol}")
    base = s[:-4]  # strip 'USDT'
    return f"{base}/USDT:USDT"

# =========================
# Bitget 클라이언트 (요청마다 fresh)
# =========================
def make_exchange(cfg: dict):
    return ccxt.bitget({
        "apiKey": cfg["key"],
        "secret": cfg["secret"],
        "password": cfg["password"],
        "options": {
            "defaultType": "swap",
            "adjustForTimeDifference": True,
        },
        "enableRateLimit": True,
        "timeout": 15000,
    })

# =========================
# 포지션/주문 로직
# =========================
async def ensure_market(ex: ccxt.bitget, ccxt_symbol: str) -> bool:
    markets = await ex.load_markets()
    return ccxt_symbol in markets

async def fetch_net_position(ex, product_type: str, ccxt_symbol: str) -> float:
    pos_list = await ex.fetch_positions([ccxt_symbol], params={"productType": product_type})
    net = 0.0
    for p in pos_list:
        side = (p.get("side") or "").lower()
        qty = float(p.get("contracts", 0) or 0)
        if side == "long":
            net += qty
        elif side == "short":
            net -= qty
    return net

async def route_order(ex, product_type: str, ccxt_symbol: str, side: str, size: float,
                      allow_shorts: bool, close_tol_pct: float, dry_run: bool) -> Dict[str, Any]:
    """
    * buy/sell 은 '행동'이고, 청산/진입은 '현재 순포지션(net)'에 따라 결정
    * 반대방향 신호는 청산, 같은 방향은 진입(또는 물타기)
    """
    if size <= 0:
        return {"ok": False, "skipped": "size<=0"}

    net = await fetch_net_position(ex, product_type, ccxt_symbol)
    params = {"productType": product_type}

    def within_tol(a, b):
        if b == 0:
            return a == 0
        return abs(a - b) <= abs(b) * (close_tol_pct / 100.0)

    if side == "buy":
        if net < 0:  # 순숏 -> buy는 숏 청산
            amount = min(size, abs(net))
            if within_tol(amount, abs(net)):
                amount = abs(net)
            if amount <= 0:
                return {"ok": True, "skipped": "nothing_to_close"}
            if dry_run:
                return {"ok": True, "dry_run": True, "action": "close_short", "amount": amount}
            return await ex.create_order(ccxt_symbol, "market", "buy", amount, None, params)
        else:        # 무포/순롱 -> 롱 진입/물타기
            if dry_run:
                return {"ok": True, "dry_run": True, "action": "open_long", "amount": size}
            return await ex.create_order(ccxt_symbol, "market", "buy", size, None, params)

    elif side == "sell":
        if net > 0:  # 순롱 -> sell은 롱 청산
            amount = min(size, net)
            if within_tol(amount, net):
                amount = net
            if amount <= 0:
                return {"ok": True, "skipped": "nothing_to_close"}
            if dry_run:
                return {"ok": True, "dry_run": True, "action": "close_long", "amount": amount}
            return await ex.create_order(ccxt_symbol, "market", "sell", amount, None, params)
        else:        # 무포/순숏 -> 숏 진입/물타기
            if not allow_shorts and net == 0:
                return {"ok": True, "skipped": "shorts_disabled"}
            if dry_run:
                return {"ok": True, "dry_run": True, "action": "open_short", "amount": size}
            return await ex.create_order(ccxt_symbol, "market", "sell", size, None, params)

    else:
        return {"ok": False, "error": f"invalid side '{side}'"}

# =========================
# FastAPI
# =========================
app = FastAPI(title="tv-bitget-router")
logger = logging.getLogger("router")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

@app.get("/")
async def root():
    return {"ok": True, "message": "tv-bitget-router up"}

@app.post("/webhook")
async def webhook(req: Request):
    # 무조건 200 OK로 응답해 TV의 재시도 루프와 500 폭격 방지
    try:
        payload = await req.json()
    except Exception:
        body = await req.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            logger.exception("[PAYLOAD_PARSE_ERROR]")
            return JSONResponse({"ok": False, "error": "bad_json"}, status_code=200)

    try:
        cfg = read_config()  # 요청마다 최신 ENV 읽기
    except Exception as e:
        logger.exception("[CONFIG_ERROR] %s", e)
        return JSONResponse({"ok": False, "error": f"config_error: {e}"}, status_code=200)

    # 필드 파싱
    try:
        tv_symbol = (payload.get("symbol") or "").strip()
        side = (payload.get("side") or "").strip().lower()  # buy|sell
        size = float(payload.get("size", 0) or 0)
        order_type = (payload.get("orderType") or "").strip().lower()
    except Exception as e:
        logger.exception("[FIELD_ERROR] %s", e)
        return JSONResponse({"ok": False, "error": "invalid_fields"}, status_code=200)

    if order_type and order_type != "market":
        return JSONResponse({"ok": True, "skipped": "only_market_supported"}, status_code=200)

    # 심볼 변환
    try:
        ccxt_symbol = tv_to_bitget(tv_symbol)
    except Exception as e:
        logger.warning("[SYMBOL_MAP_SKIP] %s", e)
        return JSONResponse({"ok": True, "skipped": "bad_symbol_format"}, status_code=200)

    ex = make_exchange(cfg)
    try:
        if not await ensure_market(ex, ccxt_symbol):
            logger.warning("[SKIP_UNLISTED] %s", ccxt_symbol)
            return JSONResponse({"ok": True, "skipped": "unlisted_symbol"}, status_code=200)

        res = await route_order(
            ex,
            cfg["product_type"],
            ccxt_symbol,
            side,
            size,
            cfg["allow_shorts"],
            cfg["close_tol"],
            cfg["dry_run"],
        )

        logger.info({"event": "order_result", "symbol": ccxt_symbol, "side": side, "size": size, "res": res})
        return JSONResponse({"ok": True, "result": res}, status_code=200)

    except ccxt.BaseError as e:
        logger.exception("[CCXT_ERROR] %s", e)
        return JSONResponse({"ok": False, "error": f"ccxt:{str(e)}"}, status_code=200)
    except Exception as e:
        logger.exception("[UNEXPECTED] %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    finally:
        try:
            await ex.close()
        except Exception:
            pass