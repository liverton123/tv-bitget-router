# src/app.py
import os
import json
import time
from typing import Optional

import ccxt.async_support as ccxt
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
FRACTION_PER_POSITION = float(os.getenv("FRACTION_PER_POSITION", "0.05"))  # 시드의 1/20
MAX_COINS = int(os.getenv("MAX_COINS", "5"))
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

BITGET_API_KEY = os.getenv("BITGET_API_KEY")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET")
BITGET_API_PASSWORD = os.getenv("BITGET_API_PASSWORD")

if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSWORD):
    raise RuntimeError("Bitget API key/secret/password is missing in environment")

app = FastAPI()

class Alert(BaseModel):
    secret: str
    symbol: str     # e.g. "DOGEUSDT.P"
    side: str       # "buy" | "sell"
    orderType: str  # "market" expected
    size: Optional[float] = None  # TV가 넣어주지만, 우리는 무시하고 자체 계산

def tv_to_ccxt_symbol(tv_symbol: str) -> Optional[str]:
    """
    TradingView: DOGEUSDT.P -> CCXT/Bitget: DOGE/USDT:USDT (Perp)
    지원하지 않는 심볼은 None 반환
    """
    s = tv_symbol.strip().upper()
    if s.endswith(".P"):
        s = s[:-2]
    if not s.endswith("USDT"):
        return None
    base = s[:-4]
    if not base:
        return None
    # Bitget perp symbol
    return f"{base}/USDT:USDT"

async def make_exchange():
    ex = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "options": {
            "defaultType": "swap",   # ensure we talk to perpetual
        },
        "enableRateLimit": True,
    })
    await ex.load_markets()
    return ex

async def fetch_price(ex: ccxt.bitget, symbol: str) -> Optional[float]:
    """
    견고한 가격 조회: ticker.last -> mark price -> 1m ohlcv close
    """
    try:
        ticker = await ex.fetch_ticker(symbol)
        last = ticker.get("last")
        if isinstance(last, (int, float)) and last and last > 0:
            return float(last)
    except Exception:
        pass

    # mark price (if supported)
    try:
        if hasattr(ex, "publicMixGetMarketMarkPrice"):
            # not unified by ccxt, but bitget has this endpoint. Fallback try/catch.
            # symbolId like BTCUSDT? ex.markets[symbol]["id"]
            m = ex.markets.get(symbol)
            if m and "id" in m:
                data = await ex.publicMixGetMarketMarkPrice({"symbol": m["id"]})
                # data example: {"data":[{"markPrice":"..."}]}
                mp = None
                if data and isinstance(data, dict):
                    arr = data.get("data")
                    if isinstance(arr, list) and arr:
                        mp = arr[0].get("markPrice")
                if mp:
                    val = float(mp)
                    if val > 0:
                        return val
    except Exception:
        pass

    # 1m candle
    try:
        ohlcvs = await ex.fetch_ohlcv(symbol, timeframe="1m", limit=2)
        if ohlcvs and len(ohlcvs):
            close = ohlcvs[-1][4]
            if close and close > 0:
                return float(close)
    except Exception:
        pass

    return None

async def fetch_free_usdt(ex: ccxt.bitget) -> float:
    """
    현재 사용 가능한 USDT (swap 계정) 사용.
    """
    bal = await ex.fetch_balance(params={"productType": "USDT-FUTURES"})  # bitget uses this param
    # ccxt가 key를 'USDT'로 올려줌
    usdt = bal.get("USDT") or {}
    free = usdt.get("free")
    if free is None:
        # 일부 버전 호환: info/raw에서 꺼내기
        free = 0.0
    return float(free or 0.0)

async def fetch_open_positions_symbols(ex: ccxt.bitget) -> set:
    """
    현재 보유(미청산) 포지션이 있는 심볼 목록
    """
    out = set()
    try:
        positions = await ex.fetch_positions(None, params={"productType": "USDT-FUTURES"})
        for p in positions or []:
            sym = p.get("symbol")
            # size 혹은 contracts > 0 이면 보유로 판단
            contracts = p.get("contracts") or p.get("contractSize")
            pos_amt = p.get("contracts") or p.get("info", {}).get("total", {}).get("holdVol")
            # ccxt 통일이 모호해서 안전하게 수량/명목값 둘 다 체크
            size = p.get("positionAmt") or p.get("info", {}).get("total", {}).get("holdVol")
            if p.get("side") in ("long", "short") and float(p.get("contracts") or 0) > 0:
                out.add(sym)
            elif size and float(size) > 0:
                out.add(sym)
            elif contracts and float(contracts) > 0:
                out.add(sym)
    except Exception:
        pass
    return out

async def fetch_position_side_and_size(ex: ccxt.bitget, symbol: str) -> tuple[str, float]:
    """
    현재 심볼의 포지션 방향('long'|'short'|'flat')과 수량(코인 수)을 반환
    """
    side = "flat"
    size = 0.0
    try:
        positions = await ex.fetch_positions([symbol], params={"productType": "USDT-FUTURES"})
        for p in positions or []:
            if p.get("symbol") == symbol:
                s = (p.get("side") or "").lower()
                contracts = float(p.get("contracts") or 0.0)
                if s in ("long", "short") and contracts > 0:
                    side = s
                    size = contracts
                    break
    except Exception:
        pass
    return side, size

async def place_order_bitget(ex: ccxt.bitget, symbol: str, side: str, amount: float, reduce_only: bool):
    """
    Bitget 마켓 주문. reduce_only=True면 포지션 축소/정리 용도로만 실행.
    """
    params = {
        "reduceOnly": True if reduce_only else False,
        "productType": "USDT-FUTURES",
    }
    return await ex.create_order(symbol, "market", side, amount, None, params)

def clamp_amount_to_limits(market: dict, amount: float) -> float:
    """
    마켓의 최소/단위 제한에 맞춰 수량 보정
    """
    min_amt = (market.get("limits", {}).get("amount", {}) or {}).get("min")
    if min_amt:
        amount = max(amount, float(min_amt))
    # precision
    prec = market.get("precision", {}).get("amount")
    if isinstance(prec, int) and prec >= 0:
        factor = 10 ** prec
        amount = int(amount * factor) / factor
    return amount

@app.get("/status")
async def status():
    return {"ok": True, "ts": int(time.time())}

@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # 1) auth
    if WEBHOOK_SECRET and payload.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid secret")

    alert = Alert(**payload)

    tv_symbol = alert.symbol
    side_raw = alert.side.lower().strip()  # "buy" / "sell"

    # 2) symbol mapping
    ccxt_symbol = tv_to_ccxt_symbol(tv_symbol)
    if not ccxt_symbol:
        app.logger.info(f"skip: unsupported tv symbol | {tv_symbol}")
        return {"ok": True, "skipped": "unsupported_symbol"}

    ex = await make_exchange()
    try:
        # 3) market support check
        if ccxt_symbol not in ex.markets:
            app.logger.info(f"skip: not in markets | {ccxt_symbol}")
            return {"ok": True, "skipped": "not_in_markets"}

        market = ex.markets[ccxt_symbol]

        # 4) 가격
        price = await fetch_price(ex, ccxt_symbol)
        if not price or price <= 0:
            app.logger.info(f"skip: price fetch failed | {{'symbol':'{ccxt_symbol}','price':{price}}}")
            return {"ok": True, "skipped": "no_price"}

        # 5) 현재 보유 코인 수 제한 (MAX_COINS)
        open_syms = await fetch_open_positions_symbols(ex)
        # 신규 오픈이 아닌 **정리/축소**는 항상 허용
        cur_side, cur_size = await fetch_position_side_and_size(ex, ccxt_symbol)

        # 들어온 시그널이 '신규 오픈/추가' 인지 '정리/축소' 인지 판단
        # - buy: 롱 오픈/추가 또는 숏 정리
        # - sell: 숏 오픈/추가 또는 롱 정리
        reduce_only = False
        desired_side = side_raw  # ccxt order side 그대로 사용

        if side_raw == "buy":
            if cur_side == "short":
                reduce_only = True   # 숏 정리(축소)
            else:
                # 롱 신규/추가. 보유 없는 신규 오픈인 경우만 MAX_COINS 검사
                if cur_side == "flat" and len(open_syms) >= MAX_COINS:
                    app.logger.info(f"skip: max coins reached | currently={len(open_syms)} symbols={sorted(list(open_syms))}")
                    return {"ok": True, "skipped": "max_coins"}
        elif side_raw == "sell":
            if not ALLOW_SHORTS and cur_side in ("flat", "long"):
                app.logger.info("skip: shorts disabled")
                return {"ok": True, "skipped": "shorts_disabled"}
            if cur_side == "long":
                reduce_only = True   # 롱 정리(축소)
            else:
                # 숏 신규/추가. 보유 없는 신규 오픈이면 MAX_COINS 검사
                if cur_side == "flat" and len(open_syms) >= MAX_COINS:
                    app.logger.info(f"skip: max coins reached | currently={len(open_syms)} symbols={sorted(list(open_syms))}")
                    return {"ok": True, "skipped": "max_coins"}
        else:
            app.logger.info(f"skip: invalid side | {side_raw}")
            return {"ok": True, "skipped": "invalid_side"}

        # 6) 사용할 USDT (= 마진): 시드(현재 free USDT)의 FRACTION 만큼
        free_usdt = await fetch_free_usdt(ex)
        use_usdt = free_usdt * FRACTION_PER_POSITION
        if use_usdt <= 0:
            app.logger.info(f"skip: no free usdt | free={free_usdt}")
            return {"ok": True, "skipped": "no_balance"}

        # 7) 수량 계산 = (사용 USDT) / (가격)
        amount = use_usdt / price

        # 8) 마켓 제한에 맞춰 보정
        amount = clamp_amount_to_limits(market, amount)
        if amount <= 0:
            app.logger.info(f"skip: calc amount is zero | {{'symbol':'{ccxt_symbol}','price':{price}}}")
            return {"ok": True, "skipped": "zero_amount"}

        # 정리(축소) 주문이면 현재 보유 수량 초과하지 않도록 clamp
        if reduce_only and cur_size > 0:
            amount = min(amount, float(cur_size))
            if amount <= 0:
                app.logger.info("skip: nothing to reduce")
                return {"ok": True, "skipped": "nothing_to_reduce"}

        info_msg = {
            "tv_symbol": tv_symbol,
            "ccxt_symbol": ccxt_symbol,
            "signal_side": side_raw,
            "price": price,
            "free_usdt": free_usdt,
            "use_usdt": use_usdt,
            "amount": amount,
            "reduce_only": reduce_only,
            "position_side": cur_side,
            "position_size": cur_size,
        }
        app.logger.info(f"plan: {json.dumps(info_msg)}")

        if DRY_RUN:
            return {"ok": True, "dry_run": True, **info_msg}

        # 9) 주문
        result = await place_order_bitget(ex, ccxt_symbol, desired_side, amount, reduce_only)
        app.logger.info(f"order_ok: {json.dumps(result)}")
        return {"ok": True, "order": result}

    except ccxt.BaseError as e:
        app.logger.error(f"ccxt_error: {str(e)}")
        raise HTTPException(500, f"ccxt_error: {str(e)}")
    except Exception as e:
        app.logger.error(f"runtime_error: {str(e)}")
        raise HTTPException(500, f"runtime_error: {str(e)}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass
