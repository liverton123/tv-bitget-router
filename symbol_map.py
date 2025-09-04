# Bitget 선물(USDT-M Perp)용 심볼 변환
# TradingView 예: "ETHUSDT.P"  -> CCXT 통합심볼: "ETH/USDT:USDT"
#                   "ENAUSDT.P" -> "ENA/USDT:USDT"
#                   "WLFUSDT.P" -> "WLF/USDT:USDT"
# 알파벳/숫자 티커만 남기고 USDT 앞에서 심볼 분할
import re

def tv_to_ccxt(tv_symbol: str) -> str:
    # 안전 처리
    s = (tv_symbol or "").strip().upper()

    # 끝의 .P / :P / -PERP 등 제거
    s = re.sub(r'(\.P|:P|-PERP)$', '', s, flags=re.IGNORECASE)

    # 예: ENAUSDT, ETHUSDT 등
    m = re.match(r'^([A-Z0-9]+)USDT$', s)
    if not m:
        # 이미 CCXT 형식이면 그대로(예: BTC/USDT:USDT)
        if "/" in s and ":" in s:
            return s
        # 마지막 보호장치: 그냥 원본 반환
        return tv_symbol

    base = m.group(1)
    return f"{base}/USDT:USDT"