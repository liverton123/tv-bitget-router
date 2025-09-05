import os

# risk/policy
FRACTION_PER_TRADE = float(os.getenv("FRACTION_PER_TRADE", "0.05"))  # 1/20
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
DEFAULT_LEVERAGE   = int(os.getenv("MAX_LEVERAGE", "10"))
REENTER_ON_OPPOSITE = os.getenv("REENTER_ON_OPPOSITE", "false").lower() == "true"
REFERENCE_BALANCE_USDT = float(os.getenv("REFERENCE_BALANCE_USDT", "0") or 0)

# venue
PRODUCT_TYPE = os.getenv("BITGET_PRODUCT_TYPE", "umcbl")
MARGIN_COIN  = os.getenv("MARGIN_COIN", "USDT")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"