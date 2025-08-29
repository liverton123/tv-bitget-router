import os, json, time, traceback
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn

import ccxt  # ccxt==4.x

app = FastAPI(title="TV-Bitget Router", version="1.0")

# ──────────────────────────────────────────────────────────────────────────────
# 환경변수
# ──────────────────────────────────────────────────────────────────────────────
API_KEY     = os.getenv("BITGET_API_KEY", "")
API_SECRET  = os.getenv("BITGET_API_SECRET", "")
API_PASS    = os.getenv("BITGET_API_PASSWORD", "")  # Bitget Passphrase
WEBHOOK_KEY = os.getenv("WEBHOOK_SECRET", "mySecret123!")

# 선물 전용
DEFAULT_TYPE = "swap"  # Bitget USDT-M Perp
MAX_OPEN     = int(os.getenv("MAX_OPEN_POSITIONS", "10"))  # 동시에 허용할 심볼 개수

# 균일 노출(“현재 시드의 1/10 × 레버리지 10배”)
FORCE_EQUAL  = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.10"))  # 1/10
LEVERAGE     = float(os.getenv("LEVERAGE", "10"))  # 노출 환산용(참고)

# 최소 주문 금액 기본값(심볼별 limits에 없으면 이 값을 사용)
DEFAULT_MIN_NOTIONAL = float(os.getenv("DEFAULT_MIN_NOTIONAL", "5"))

# ──────────────────────────────────────────────────────────────────────────────
# ccxt Bitget 인스턴스
# ──────────────────────────────────────────────────────────────────────────────
def make_exchange() -> ccxt.bitget:
    ex = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASS,
        "enableRateLimit": True,
        "options": {
            "defaultType": DEFAULT_TYPE,    # swap
        },
    })
    ex.load_markets()
    return ex

exchange = make_exchange()

# ──────────────────────────────────────────────────────────────────────────────
# 유틸: 심볼 변환
# TradingView 예: "SUIUSDT.P" → Bitget market id "SUIUSDT_UMCBL" → ccxt 심볼 "SUI/USDT:USDT"
# ──────────────────────────────────────────────────────────────────────────────
def tv_to_umcbl(tv_symbol: str) -> str:
    sym = tv_symbol.strip().upper()
    if sym.endswith(".P"):
        sym = sym[:-2]
    # 이미 UMCBL 형태로 들어오면 그대로
    if sym.endswith("_UMCBL"):
        return sym
    return f"{sym}_UMCBL"

def umcbl_to_ccxt(umcbl_symbol: str) -> str:
    # "BTCUSDT_UMCBL" → "BTC/USDT:USDT"
    if not umcbl_symbol.endswith("_UMCBL"):
        raise ValueError(f"unexpected umcbl symbol: {umcbl_symbol}")
    core = umcbl_symbol[:-6]  # remove "_UMCBL"
    if core.endswith("USDT"):
        base = core[:-4]
        quote = "USDT"
    else:
        # fallback
        base, quote = core, "USDT"
    return f"{base}/{quote}:USDT"

# ──────────────────────────────────────────────────────────────────────────────
# 유틸: 현재 열린 선물 포지션 수(심볼 개수) 계산
# ──────────────────────────────────────────────────────────────────────────────
def count_open_positions(ex: ccxt.bitget) -> int:
    try:
        poss = ex.fetch_positions()
        cnt = 0
        seen = set()
        for p in poss:
            if p.get("contract", "") and abs(float(p.get("contracts", 0))) > 0:
                # 심볼 기준 unique
                seen.add(p.get("symbol"))
        cnt = len(seen)
        return cnt
    except Exception:
        return 0

# ──────────────────────────────────────────────────────────────────────────────
# 유틸: 균일 노출 수량 계산 (신규 진입시에만)
#  - 청산/감소 주문(reduceOnly=True)은 None 반환하여 기존 수량 유지
# ──────────────────────────────────────────────────────────────────────────────
def compute_uniform_amount(ex: ccxt.bitget, ccxt_symbol: str, reduce_only: bool) -> Optional[float]:
    if reduce_only or not FORCE_EQUAL:
        return None
    try:
        bal = ex.fetch_balance()
        eq = None
        if "USDT" in bal:
            eq = bal["USDT"].get("total") or bal["USDT"].get("free")
        if not eq or eq <= 0:
            return None

        ticker = ex.fetch_ticker(ccxt_symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price or price <= 0:
            return None

        per_pos = eq * max(min(FRACTION_PER_POSITION, 1.0), 0.0)
        target_notional = per_pos * max(LEVERAGE, 1.0)
        raw_amount = target_notional / price

        market = ex.market(ccxt_symbol)
        amt = float(ex.amount_to_precision(ccxt_symbol, raw_amount))

        # 최소 주문 금액 보정
        min_cost = float(market.get("limits", {}).get("cost", {}).get("min", DEFAULT_MIN_NOTIONAL))
        if (amt * price) < min_cost:
            amt = float(ex.amount_to_precision(ccxt_symbol, min_cost / price))

        return max(amt, 0.0)
    except Exception as e:
        print(f"[uniform] skip (reason={e})")
        return None

# ──────────────────────────────────────────────────────────────────────────────
# 유틸: 액션/방향 판정
# ──────────────────────────────────────────────────────────────────────────────
def parse_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    다양한 포맷을 허용:
      1) 우리 pine alert_message 포맷:
         {"webhook_key":"...","action":"buy|sellshort|exit","symbol":"SUIUSDT.P","qty":123, ...}
      2) 단순 템플릿:
         {"secret":"...","symbol":"SUIUSDT_UMCBL","side":"buy|sell","orderType":"market","size":0.1}
    """
    # 인증키
    secret = payload.get("webhook_key") or payload.get("secret") or ""
    if WEBHOOK_KEY and secret != WEBHOOK_KEY:
        raise HTTPException(status_code=401, detail="invalid webhook key")

    # 심볼
    sym_in = payload.get("symbol") or payload.get("ticker") or ""
    if not sym_in:
        raise HTTPException(status_code=400, detail="missing symbol")

    # 수량
    qty = payload.get("qty")
    if qty is None:
        qty = payload.get("size")
    try:
        qty = float(qty) if qty is not None else None
    except Exception:
        qty = None

    # 액션/사이드
    action = (payload.get("action") or payload.get("side") or "").lower()

    # 표준화
    if action in ("buy", "long"):
        side = "buy"
        reduce_only = False
    elif action in ("sellshort", "short"):
        side = "sell"
        reduce_only = False
    elif action in ("sell", "exit", "close", "flat"):
        # 포지션 청산 의도
        # (pine에서 exit은 qty=0 으로 오도록 해두었음)
        side = "sell"  # 기본값 (롱 청산은 sell, 숏 청산은 buy로 바꿔 처리)
        reduce_only = True
    else:
        # 기본: 전략 알림 템플릿에서 side가 buy/sell 로 올 수도 있음
        if payload.get("side", "").lower() == "buy":
            side = "buy"; reduce_only = False
        elif payload.get("side", "").lower() == "sell":
            side = "sell"; reduce_only = False
        else:
            raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    return {
        "symbol_in": sym_in,
        "side": side,
        "qty": qty,
        "reduce_only": reduce_only
    }

# ──────────────────────────────────────────────────────────────────────────────
# 주문 실행
# ──────────────────────────────────────────────────────────────────────────────
def place_order(ex: ccxt.bitget, symbol_in: str, side: str, qty: Optional[float], reduce_only: bool) -> Dict[str, Any]:
    # TV → Bitget 심볼 변환
    umcbl = tv_to_umcbl(symbol_in)  # e.g., SUIUSDT_UMCBL
    ccxt_symbol = umcbl_to_ccxt(umcbl)  # e.g., SUI/USDT:USDT

    # 포지션 제한(신규 진입만 체크)
    if not reduce_only:
        open_cnt = count_open_positions(ex)
        if open_cnt >= MAX_OPEN:
            return {"status": "ignored", "reason": f"max open positions reached ({open_cnt}/{MAX_OPEN})"}

    # 현재 포지션 조회 (청산 시 사이드 반전용)
    pos_side_needed = side
    try:
        poss = ex.fetch_positions([ccxt_symbol])
        if poss:
            p = poss[0]
            sz = float(p.get("contracts", 0) or 0)
            if reduce_only:
                # 롱(>0) 청산이면 매도, 숏(<0) 청산이면 매수
                if sz > 0:
                    pos_side_needed = "sell"
                elif sz < 0:
                    pos_side_needed = "buy"
                else:
                    return {"status": "ignored", "reason": "no open position to close"}
    except Exception:
        pass

    # 수량 결정
    amount: Optional[float] = None
    if not reduce_only:
        # 균일 노출 강제
        amount = compute_uniform_amount(ex, ccxt_symbol, reduce_only)
        if amount is None:
            # 실패시, 들어온 qty 사용 (없으면 에러)
            if qty is None:
                raise HTTPException(status_code=422, detail="missing qty for entry")
            amount = float(qty)
    else:
        # 청산: 포지션 전량 close → reduceOnly + 큰 수량
        # Bitget은 reduceOnly면 남은 수량만큼만 닫히므로 크게 줘도 안전
        amount = 1e9

    params = {
        "reduceOnly": bool(reduce_only),
        # 필요 시 timeInForce, positionMode 등 추가 가능
    }

    # 주문
    print(f"Placing order | in='{symbol_in}' umcbl='{umcbl}' ccxt='{ccxt_symbol}' side='{pos_side_needed}' amount={amount} reduceOnly={reduce_only}")
    order = ex.create_order(ccxt_symbol, "market", pos_side_needed, amount, None, params)
    return {"status": "ok", "order": order, "ccxt_symbol": ccxt_symbol}

# ──────────────────────────────────────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return PlainTextResponse("tv-bitget-router alive. use POST /webhook")

@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok")

@app.get("/status")
def status():
    try:
        poss = exchange.fetch_positions()
        open_symbols = sorted({p.get("symbol") for p in poss if abs(float(p.get("contracts", 0) or 0)) > 0})
        return JSONResponse({"ok": True, "open_symbols": open_symbols, "max_open": MAX_OPEN})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})

@app.post("/webhook")
async def webhook(request: Request):
    t0 = time.time()
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    try:
        parsed = parse_action(payload)
        res = place_order(
            exchange,
            symbol_in=parsed["symbol_in"],
            side=parsed["side"],
            qty=parsed["qty"],
            reduce_only=parsed["reduce_only"],
        )
        dt = round((time.time() - t0) * 1000)
        print(f"[webhook] done in {dt}ms | result={res.get('status')}")
        return JSONResponse(res)
    except HTTPException as he:
        print(f"[webhook] http error {he.status_code}: {he.detail}")
        raise
    except Exception as e:
        print("[webhook] error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
