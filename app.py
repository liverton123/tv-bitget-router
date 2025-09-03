import os
import json
import asyncio
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

import ccxt.async_support as ccxt   # 반드시 async_support 사용
from pydantic import BaseModel

# =========================
# 환경변수
# =========================
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mySecret123!")  # TV 메시지의 "secret" 과 동일하게
BITGET_API_KEY = os.getenv("BITGET_KEY")
BITGET_API_SECRET = os.getenv("BITGET_SECRET")
BITGET_API_PASSWORD = os.getenv("BITGET_PASSWORD")  # Bitget은 비밀번호 필수

if not (BITGET_API_KEY and BITGET_API_SECRET and BITGET_API_PASSWORD):
    print("[WARN] BITGET API 환경변수가 비어있습니다. (BITGET_KEY/SECRET/PASSWORD)")

# =========================
# FastAPI
# =========================
app = FastAPI(title="tv-bitget-router", version="1.0.0")


# =========================
# 유틸: TV심볼 -> ccxt/Bitget 심볼
#   TV:  ETHUSDT.P    (U본위 퍼프)
#   ccxt: ETH/USDT:USDT  (Bitget UMCBL)
# =========================
def tv_to_ccxt_symbol(tv_symbol: str) -> str:
    """
    TradingView 심볼(예: 'ETHUSDT.P')을 ccxt/Bitget 마켓 심볼('ETH/USDT:USDT')로 변환
    """
    if not tv_symbol:
        raise ValueError("symbol empty")

    s = tv_symbol.upper().replace(".P", "")
    # USDT 마켓만 운용한다고 가정
    if not s.endswith("USDT"):
        raise ValueError(f"Unsupported symbol format: {tv_symbol}")

    base = s[:-4]  # 'ETH'
    return f"{base}/USDT:USDT"  # ccxt의 Bitget U 본위 무기한 통합 심볼


# =========================
# Bitget (ccxt) 클라이언트
# =========================
def create_bitget():
    exchange = ccxt.bitget({
        "apiKey": BITGET_API_KEY,
        "secret": BITGET_API_SECRET,
        "password": BITGET_API_PASSWORD,
        "enableRateLimit": True,
        # Bitget U본위 Perp에 필요한 파라미터
        "options": {
            # 기본 productType 설정
            "defaultType": "swap",             # ccxt 관용
            "defaultMarginMode": "cross",
            "defaultProductType": "umcbl",     # 커스텀 키 (params로 계속 넘겨줄 것)
        },
    })
    return exchange


# =========================
# 포지션 조회 (순포지션 수량)
#  - Bitget는 dual/oneway 모두 지원. ccxt는 리스트 형태로 반환.
#  - net 계약수(+)롱, (-)숏 추정
# =========================
async def fetch_net_contracts(ex: ccxt.Exchange, ccxt_symbol: str) -> float:
    try:
        positions = await ex.fetch_positions([ccxt_symbol], params={"productType": "umcbl"})
    except Exception:
        # 일부 ccxt 버전은 심볼 배열 미지원 → 전체 조회 후 필터
        positions = await ex.fetch_positions(params={"productType": "umcbl"})

    net = 0.0
    for p in positions:
        if p.get("symbol") != ccxt_symbol:
            continue
        # ccxt 표준 필드
        side = (p.get("side") or "").lower()  # "long" | "short" | ""
        contracts = float(p.get("contracts") or 0) or float(p.get("amount") or 0)
        if side == "long":
            net += contracts
        elif side == "short":
            net -= contracts
        else:
            # 일부 브로커는 sign 가 amount에 반영될 수도 있음
            net += float(p.get("positionAmt") or 0)
    return float(net)


# =========================
# 주문 헬퍼
#  - buy: 증액(물타기 포함)
#  - sell: reduceOnly (롱 청산 전용) → 순포지션 없으면 "무시"
# =========================
async def place_order(
    ex: ccxt.Exchange,
    ccxt_symbol: str,
    side: str,                 # 'buy' | 'sell'
    order_type: str,           # 'market' 권장
    size: float,
) -> Dict[str, Any]:

    side = side.lower()
    order_type = order_type.lower()

    if order_type not in ("market", "limit"):
        raise ValueError("orderType must be 'market' or 'limit'")

    # 롱-전용 전략: SELL은 reduceOnly로만; 포지션 없으면 무시
    if side == "sell":
        net = await fetch_net_contracts(ex, ccxt_symbol)
        if net <= 0:
            return {
                "status": "ignored",
                "reason": "no long position to reduce",
                "symbol": ccxt_symbol,
                "requested_size": size,
                "net_before": net,
            }
        reduce_size = min(size, net)  # 남은 수량보다 많이 던지지 않기
        params = {"reduceOnly": True, "productType": "umcbl"}
        order = await ex.create_order(ccxt_symbol, order_type, side, reduce_size, None, params)
        net_after = await fetch_net_contracts(ex, ccxt_symbol)
        return {"status": "ok", "order": order, "net_before": net, "net_after": net_after}

    elif side == "buy":
        # 롱 증액(진입/물타기)
        params = {"productType": "umcbl"}
        order = await ex.create_order(ccxt_symbol, order_type, side, size, None, params)
        net_after = await fetch_net_contracts(ex, ccxt_symbol)
        return {"status": "ok", "order": order, "net_after": net_after}

    else:
        raise ValueError("side must be 'buy' or 'sell'")


# =========================
# 요청 스키마
# =========================
class TVPayload(BaseModel):
    secret: str
    symbol: str        # ex) "ETHUSDT.P"
    side: str          # "buy" | "sell"
    orderType: str     # "market"
    size: float        # 수량 (예: 0.034)


# =========================
# 웹훅 엔드포인트
# =========================
@app.post("/webhook")
async def webhook(req: Request):
    try:
        data = await req.json()
    except Exception:
        # TV에서 text로 보내는 경우 대비
        raw = await req.body()
        try:
            data = json.loads(raw.decode())
        except Exception:
            raise HTTPException(status_code=400, detail="invalid json")

    # 유효성 검사
    try:
        p = TVPayload(**data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"payload error: {e}")

    if p.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

    # 심볼 변환
    try:
        ccxt_symbol = tv_to_ccxt_symbol(p.symbol)  # "ETH/USDT:USDT"
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"symbol convert error: {e}")

    ex = create_bitget()
    try:
        result = await place_order(
            ex=ex,
            ccxt_symbol=ccxt_symbol,
            side=p.side,
            order_type=p.orderType,
            size=float(p.size),
        )
        return JSONResponse({"ok": True, "result": result})
    except ccxt.BaseError as e:
        # Bitget/ccxt 에러 메시지 그대로 노출 + productType 보장
        raise HTTPException(status_code=500, detail=f"ccxt error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"server error: {repr(e)}")
    finally:
        try:
            await ex.close()
        except Exception:
            pass


@app.get("/")
async def health():
    return {"status": "ok"}