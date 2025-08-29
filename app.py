# app.py — Bitget Futures router (robust TradingView payload parser)
import os, json, logging, traceback, math, re
from typing import Optional, Dict, Any, Set
from fastapi import FastAPI, Request, HTTPException, Header
import ccxt

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bitget-router")

BITGET_API_KEY      = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET   = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD", "")  # passphrase
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET") or os.getenv("WEBHOOK_KEY", "")
MAX_POSITIONS       = int(os.getenv("MAX_POSITIONS", "10"))
DRY_RUN             = os.getenv("DRY_RUN", "true").lower() == "true"

app = FastAPI(title="tv-bitget-router", version="2.0.0")

# ---- Bitget USDT-M Perp ----
exchange = ccxt.bitget({
    "apiKey": BITGET_API_KEY,
    "secret": BITGET_API_SECRET,
    "password": BITGET_API_PASSWORD,
    "enableRateLimit": True,
    "options": {"defaultType": "swap"},  # USDT-M perpetual
})
try:
    exchange.load_markets()
    log.info("Bitget markets loaded (defaultType=swap)")
except Exception as e:
    log.error(f"load_markets failed: {e}")

def mask(s: Optional[str]) -> str:
    if not s: return ""
    return s[:2] + "*"*(len(s)-4) + s[-2:] if len(s)>4 else "*"*len(s)

# ---------- payload parsing ----------
def try_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(text)
    except Exception:
        return None

def extract_first_json(text: str) -> Optional[Dict[str, Any]]:
    # 찾아서 처음 나오는 {...} 를 파싱
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m: return None
    return try_json(m.group(0))

def kv_to_dict(text: str) -> Optional[Dict[str, Any]]:
    # key=value 줄들 → dict
    lines = [l.strip() for l in text.splitlines() if "=" in l]
    if not lines: return None
    d: Dict[str, Any] = {}
    for ln in lines:
        k, v = ln.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if v.lower() in ("true","false"): d[k]= (v.lower()=="true")
        else:
            try: d[k]= float(v) if "." in v or v.isdigit() else v
            except: d[k]= v
    return d if d else None

def normalize_payload(raw: bytes) -> Dict[str, Any]:
    body = raw.decode("utf-8", "ignore").strip()
    # 1) 순수 JSON
    data = try_json(body)
    if data: return data
    # 2) 텍스트 + JSON 섞임
    data = extract_first_json(body)
    if data: return data
    # 3) key=value 형태
    data = kv_to_dict(body)
    if data: return data
    # 4) 못 읽으면 422 대신 에러 던지지 말고 로그 남김
    raise HTTPException(status_code=400, detail="invalid payload (expect JSON)")

# ---------- symbol mapping ----------
def to_umcbl_from_tv(s: str) -> str:
    """
    TV: 'BTCUSDT.P' / 'BINANCE:BTCUSDT.P' / 'BTCUSDT' / 'BTCUSDT_UMCBL'
     -> 'BTCUSDT_UMCBL'
    """
    t = s.strip()
    if ":" in t: t = t.split(":")[-1]
    if t.endswith(".P"): t = t[:-2]
    if t.endswith("_UMCBL") or t.endswith("_CMCBL"): return t
    if t.endswith("USDT"): return t + "_UMCBL"
    return t + "_UMCBL"

def umcbl_to_ccxt_symbol(um: str) -> str:
    # 'BTCUSDT_UMCBL' -> 'BTC/USDT:USDT'
    if um.endswith("_UMCBL"): um = um[:-6]
    if um.endswith("USDT"):
        base = um[:-4]
        return f"{base}/USDT:USDT"
    return um

# ---------- positions ----------
async def fetch_open_symbols() -> Set[str]:
    try:
        poss = exchange.fetch_positions()
    except Exception as e:
        log.error(f"fetch_positions failed: {e}")
        return set()
    res = set()
    for p in poss or []:
        sym = p.get("symbol")
        size = p.get("contracts") or p.get("size") or 0
        try:
            if sym and abs(float(size or 0))>0: res.add(sym)
        except: pass
    return res

async def fetch_position_size(symbol: str) -> float:
    try:
        poss = exchange.fetch_positions([symbol])
    except Exception:
        try:
            poss = exchange.fetch_positions()
        except Exception as e:
            log.error(f"fetch_position_size failed: {e}")
            return 0.0
    for p in poss or []:
        if p.get("symbol")==symbol:
            size = p.get("contracts") or p.get("size") or 0
            try: return abs(float(size or 0))
            except: return 0.0
    return 0.0

@app.get("/health")
async def health(): return {"ok": True}

@app.get("/status")
async def status():
    return {
        "live": True,
        "dry_run": DRY_RUN,
        "max_positions": MAX_POSITIONS,
        "has_api_key": bool(BITGET_API_KEY),
        "has_api_secret": bool(BITGET_API_SECRET),
        "has_api_password": bool(BITGET_API_PASSWORD),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "default_type": "swap",
    }

@app.post("/webhook")
async def webhook(request: Request, x_webhook_key: Optional[str] = Header(default=None)):
    try:
        raw = await request.body()
        data = normalize_payload(raw)

        # ---- auth ----
        provided = data.get("secret") or data.get("key") or data.get("webhook_key") or x_webhook_key
        log.info(f"Auth check | provided={mask(provided)} | expected_set={bool(WEBHOOK_SECRET)}")
        if WEBHOOK_SECRET and provided != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="invalid webhook key")

        # ---- symbol ----
        raw_sym = str(data.get("symbol") or data.get("ticker") or data.get("symbol_tv") or "")
        if not raw_sym:
            raise HTTPException(status_code=400, detail="missing symbol")
        umcbl = to_umcbl_from_tv(raw_sym)
        symbol = umcbl_to_ccxt_symbol(umcbl)
        log.info(f"Symbol map | in='{raw_sym}' -> umcbl='{umcbl}' -> ccxt='{symbol}'")

        # ---- side / action ----
        # 허용: side: buy/sell 또는 open_long/open_short/close_long/close_short
        side_raw = str(data.get("side") or data.get("action") or "").lower()
        reduce_only = bool(data.get("reduceOnly", False))

        if side_raw in ("open_long", "long", "buy"):
            side = "buy"; reduce_only = False
        elif side_raw in ("open_short", "short", "sellshort"):
            side = "sell"; reduce_only = False
        elif side_raw in ("close_long","exit_long","sell"):
            side = "sell"; reduce_only = True
        elif side_raw in ("close_short","exit_short","buy_to_cover","buytocover","buy"):
            side = "buy"; reduce_only = True
        else:
            # 전략 알림에서 action 텍스트만 올 수 있어 방어
            if "sellshort" in side_raw: side="sell"; reduce_only=False
            elif "exit_short" in side_raw: side="buy"; reduce_only=True
            elif "exit_long" in side_raw: side="sell"; reduce_only=True
            elif "buy" in side_raw: side="buy"
            elif "sell" in side_raw: side="sell"
            else:
                raise HTTPException(status_code=400, detail=f"invalid side/action: {side_raw}")

        # ---- open positions limit (entries only) ----
        if not reduce_only:
            opens = await fetch_open_symbols()
            already = symbol in opens
            log.info(f"Open symbols={list(opens)} (count={len(opens)}) | already_open={already}")
            if not already and len(opens) >= MAX_POSITIONS:
                return {"status": "blocked", "reason": "max positions reached", "open_symbols": list(opens)}

        # ---- amount ----
        amount = data.get("size") or data.get("qty") or data.get("amount")
        if amount is None:
            # 감소 주문이면 현 포지션 수량 조회
            if reduce_only:
                amount = await fetch_position_size(symbol)
                log.info(f"ReduceOnly true -> fetched pos size={amount}")
            else:
                raise HTTPException(status_code=400, detail="qty/size required")
        amount = float(amount)

        # ---- min notional adjust (>= 5 USDT by default) ----
        try:
            mkt = exchange.market(symbol)
            limits = (mkt.get("limits") or {})
            min_cost = None
            if limits.get("cost") and limits["cost"].get("min") is not None:
                min_cost = float(limits["cost"]["min"])
            if not min_cost: min_cost = 5.0
            last = exchange.fetch_ticker(symbol)["last"]
            notional = amount * float(last)
            if notional < min_cost:
                needed = min_cost / float(last)
                prec = (mkt.get("precision") or {}).get("amount")
                if prec is not None:
                    step = 10 ** (-int(prec))
                    amount = math.ceil(needed / step) * step
                else:
                    amount = math.ceil(needed * 1e6)/1e6
                log.info(f"Adjusted amount | last={last}, min_cost={min_cost} -> amount={amount}")
        except Exception as e:
            log.warning(f"min-notional adjust skipped: {e}")

        if DRY_RUN:
            log.info(f"[DRY] {symbol} {side} amount={amount} reduceOnly={reduce_only}")
            return {"status": "ok(dry)", "symbol": symbol, "side": side, "amount": amount, "reduceOnly": reduce_only}

        params = {"reduceOnly": reduce_only}
        log.info(f"Placing order | symbol={symbol} side={side} amount={amount} params={params}")
        result = exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
        log.info(f"Order result: {json.dumps(result, default=str)[:1200]}")
        return {"status": "ok", "result": result}

    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Unhandled error: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"order failed: {str(e)}")
