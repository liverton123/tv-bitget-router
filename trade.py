import os
from typing import Optional, Dict, Any

import ccxt.async_support as ccxt

PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")  # ★ 필수
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
CLOSE_TOLERANCE_PCT = float(os.getenv("CLOSE_TOLERANCE_PCT", "0.05"))
FORCE_EQUAL_NOTIONAL = os.getenv("FORCE_EQUAL_NOTIONAL", "true").lower() == "true"

class BitgetTrader:
    def __init__(self) -> None:
        api_key = os.getenv("BITGET_API_KEY", "")
        api_secret = os.getenv("BITGET_API_SECRET", "")
        api_password = os.getenv("BITGET_API_PASSWORD", "")

        self.exchange = ccxt.bitget({
            "apiKey": api_key,
            "secret": api_secret,
            "password": api_password,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # USDT-M Perp
            },
        })

    # ---------- helpers ----------
    async def _market(self, symbol: str) -> Dict[str, Any]:
        await self.exchange.load_markets()
        return self.exchange.market(symbol)

    async def _amount_to_precision(self, symbol: str, amount: float) -> float:
        m = await self._market(symbol)
        return float(self.exchange.amount_to_precision(symbol, amount))

    async def fetch_net_position(self, symbol: str) -> Dict[str, Any]:
        """
        Bitget 포지션 요약:
        - size: >0 롱, <0 숏, ==0 무포지션
        - abs_size: 절대 수량
        """
        positions = await self.exchange.fetch_positions(
            [symbol], params={"productType": PRODUCT_TYPE}
        )
        # ccxt 표준화 값 사용(가능한 경우)
        long_sz = 0.0
        short_sz = 0.0
        for p in positions or []:
            if p.get("symbol") != symbol:
                continue
            side = p.get("side") or p.get("positionSide")
            amt = float(p.get("contracts") or p.get("amount") or 0)
            if amt <= 0:
                continue
            if (side or "").lower().startswith("long"):
                long_sz += amt
            elif (side or "").lower().startswith("short"):
                short_sz += amt

        net = long_sz - short_sz
        return {
            "net": net,
            "abs_size": abs(net),
            "side": "long" if net > 0 else "short" if net < 0 else "flat",
            "long": long_sz,
            "short": short_sz,
        }

    # ---------- core routing ----------
    async def route_order(self, ccxt_symbol: str, tv_side: str, size: float, order_type: str = "market"):
        """
        규칙:
        - tv_side=buy -> long 의도, tv_side=sell -> short 의도(전략 신호 의미)
        - 현재 포지션과 같은 방향: 증액(물타기)
        - 현재 포지션 반대 방향: reduce-only (청산/감소). 절대 flip 금지
        - DRY_RUN=True 이면 주문 미전송, payload만 리턴
        """
        tv_side = tv_side.lower()
        if tv_side not in ("buy", "sell"):
            raise ValueError(f"invalid side {tv_side}")

        # 숏 금지 옵션
        if tv_side == "sell" and not ALLOW_SHORTS:
            return {"skipped": "shorts not allowed"}

        # 현재 포지션
        pos = await self.fetch_net_position(ccxt_symbol)
        curr_side = pos["side"]
        curr_abs = pos["abs_size"]

        # Bitget 최소수량 보정
        size = await self._amount_to_precision(ccxt_symbol, size)

        # 주문 파라미터 공통(★ productType 필수)
        base_params = {"productType": PRODUCT_TYPE}

        # 동일 방향이면 증액
        if (tv_side == "buy" and curr_side in ("flat", "long")) or (tv_side == "sell" and curr_side in ("flat", "short")):
            if DRY_RUN:
                return {"dry_run": True, "action": "increase", "symbol": ccxt_symbol, "side": tv_side, "size": size}
            order = await self.exchange.create_order(
                symbol=ccxt_symbol,
                type=order_type,
                side="buy" if tv_side == "buy" else "sell",
                amount=size,
                params=base_params,  # ★
            )
            return {"executed": "increase", "order": order}

        # 반대 방향 → reduce-only (청산/감소), flip 금지
        else:
            # 목표 청산수량 계산
            if FORCE_EQUAL_NOTIONAL and abs(size - curr_abs) <= curr_abs * CLOSE_TOLERANCE_PCT:
                # 전량 청산
                close_amt = curr_abs
            else:
                # 부분 청산(지시 수량만큼)
                close_amt = min(size, curr_abs)

            close_amt = await self._amount_to_precision(ccxt_symbol, close_amt)

            if close_amt == 0:
                return {"skipped": "nothing to close"}

            params = {**base_params, "reduceOnly": True}

            side_to_send = "sell" if curr_side == "long" else "buy"  # 포지션 반대측으로 청산

            if DRY_RUN:
                return {"dry_run": True, "action": "reduce", "symbol": ccxt_symbol, "side": side_to_send, "size": close_amt}

            order = await self.exchange.create_order(
                symbol=ccxt_symbol,
                type=order_type,
                side=side_to_send,
                amount=close_amt,
                params=params,  # ★ reduceOnly + productType
            )
            return {"executed": "reduce", "order": order}