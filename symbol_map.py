import re
from typing import Optional

# TV의 "ETHUSDT.P" → Bitget 선물 "ETH/USDT:USDT"
def normalize_tv_symbol(tv_symbol: str) -> Optional[str]:
    s = tv_symbol.strip().upper()

    # ...USDT.P / ...USDT (둘 다 대응)
    m = re.fullmatch(r"([A-Z0-9]+)USDT(?:\.P)?", s)
    if not m:
        return None
    base = m.group(1)
    return f"{base}/USDT:USDT"

async def is_supported_market(exchange, ccxt_symbol: str) -> bool:
    try:
        await exchange.load_markets()
        return ccxt_symbol in exchange.markets
    except Exception:
        return False