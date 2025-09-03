# app.py (FastAPI/Starlette 기준)
import os, json, math
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import ccxt.async_support as ccxt

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")  # 느낌표 포함 정확히
PRODUCT_TYPE = "umcbl"  # Bitget USDT-M Perp
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "10"))

app = FastAPI()

def tv_to_bitget_symbol(tv_ticker: str) -> str:
    """
    TradingView의 `SUIUSDT.P`, `XRPUSDT.P` → ccxt 심볼 `SUI/USDT:USDT` 로 변환
    (Bitget USDT-M perpetual의 ccxt 심볼 포맷)
    """
    t = tv_ticker.upper().strip()
    if t.endswith(".P"):
        t = t[:-2]
    # 현물처럼 보이나 선물로 강제
    base_quote = t.replace("USDT", "/USDT")
    # Bitget USDT-M Perp 는 ':USDT' 컨트랙트 식별자 필요
    if "/USDT" in base_quote and not base_quote.endswith(":USDT"):
        base_quote = base_quote + ":USDT"
    return base_quote

async def get_exchange():
    ex = ccxt.bitget({
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
            "productType": PRODUCT_TYPE,  # 중요!
        }
    })
    await ex.load_markets()
    return ex

def to_reduce_only(side: str, have_pos_side: str) -> bool:
    """
    현재 보유 포지션 방향과 들어온 side로 reduceOnly 여부 결정.
    - have_pos_side: 'long' | 'short' | 'flat'
    """
    if have_pos_side == "flat":
        return False
    if have_pos_side == "long" and side == "sell":
        return True
    if have_pos_side == "short" and side == "buy":
        return True
    return False

async def get_position_side(ex, market):
    """
    Bitget에서 해당 심볼 포지션 가져와 long/short/flat 판별
    """
    try:
        pos_list = await ex.fetch_positions([market["symbol"]], params={"productType": PRODUCT_TYPE})
        long_sz = 0.0
        short_sz = 0.0
        for p in pos_list or []:
            if p.get("contracts") and float(p["contracts"]) > 0:
                if p.get("side") == "long":
                    long_sz += float(p["contracts"])
                elif p.get("side") == "short":
                    short_sz += float(p["contracts"])
        if long_sz > 0 and short_sz == 0:
            return "long"
        if short_sz > 0 and long_sz == 0:
            return "short"
        return "flat"
    except Exception:
        # 포지션 조회 실패 시엔 안전하게 flat로 취급하고 로그로만 남김
        return "flat"

def amount_to_precision(ex, market, amount_float):
    prec = market.get("precision", {}).get("amount")
    if prec is None:
        return ex.amount_to_precision(market["symbol"], amount_float)
    step = 10 ** (-prec)
    return math.floor(amount_float / step) * step

@app.post("/webhook")
async def webhook(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "err": "invalid_json"}, status_code=200)

    # 1) 시크릿 확인
    try:
        if body.get("secret") != WEBHOOK_SECRET:
            return JSONResponse({"ok": False, "err": "bad_secret"}, status_code=200)
    except Exception:
        return JSONResponse({"ok": False, "err": "no_secret"}, status_code=200)

    # 2) 필드 추출
    raw_symbol = (body.get("symbol") or "").strip()
    side      = (body.get("side") or "").strip().lower()   # buy/sell
    otype     = (body.get("orderType") or "market").strip().lower()
    size_raw  = body.get("size")

    if not raw_symbol or side not in ("buy","sell") or not size_raw:
        return JSONResponse({"ok": False, "err": "missing_fields"}, status_code=200)

    tv_symbol = raw_symbol
    ccxt_symbol = tv_to_bitget_symbol(tv_symbol)

    ex = None
    try:
        ex = await get_exchange()

        # 3) 심볼/상장 확인
        if ccxt_symbol not in ex.markets:
            # 상장 안 됨 → 200으로 응답, 로그만 남김
            app.logger.info(f"UNSUPPORTED_SYMBOL tv={tv_symbol} ccxt={ccxt_symbol}")
            return JSONResponse({"ok": False, "err": "unsupported_symbol", "symbol": tv_symbol}, status_code=200)

        market = ex.markets[ccxt_symbol]

        # 4) 수량 정규화 (TradingView는 '코인 수량'을 보냄)
        try:
            amount = float(size_raw)
        except Exception:
            return JSONResponse({"ok": False, "err": "bad_size"}, status_code=200)

        amount = amount_to_precision(ex, market, amount)
        if amount <= 0:
            return JSONResponse({"ok": False, "err": "zero_amount"}, status_code=200)

        # 5) 레버리지 설정(최초 1회만 성공해도 OK, 실패해도 주문은 진행)
        try:
            await ex.set_leverage(DEFAULT_LEVERAGE, market["symbol"], params={"productType": PRODUCT_TYPE})
        except Exception as e:
            app.logger.info(f"set_leverage_fail {market['symbol']} {e}")

        # 6) 포지션 방향 파악 & reduceOnly 결정
        have_side = await get_position_side(ex, market)
        reduce_only = to_reduce_only(side, have_side)

        # 7) 주문 생성
        params = {"productType": PRODUCT_TYPE, "reduceOnly": reduce_only}
        order = await ex.create_order(market["symbol"], otype, side, amount, None, params)

        return JSONResponse({
            "ok": True,
            "symbol": tv_symbol,
            "ccxt_symbol": market["symbol"],
            "side": side,
            "reduceOnly": reduce_only,
            "amount": amount,
            "orderId": order.get("id"),
        }, status_code=200)

    except Exception as e:
        # 모든 예외는 200으로 소비 + 상세 로그
        try:
            app.logger.error(f"order_fail tv={tv_symbol} ccxt={ccxt_symbol} side={side} size={size_raw} err={repr(e)}")
        except Exception:
            pass
        return JSONResponse({"ok": False, "err": "exception", "msg": str(e)}, status_code=200)
    finally:
        if ex:
            try:
                await ex.close()
            except Exception:
                pass

@app.get("/health")
async def health():
    return {"ok": True}