import os

FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.05"))  # seed × 5% per entry/DCA
MAX_OPEN_COINS     = int(os.getenv("MAX_OPEN_COINS", "5"))           # distinct symbols
DEFAULT_LEVERAGE   = int(os.getenv("MAX_LEVERAGE", "10"))
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
REFERENCE_BALANCE_USDT = float(os.getenv("REFERENCE_BALANCE_USDT", "0") or 0)

PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
MARGIN_COIN  = os.getenv("MARGIN_COIN", "USDT")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"