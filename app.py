# == 새로 추가할 유틸 함수들 ==

def _round_step(value: float, step: float) -> float:
    # stepSize(=lotSize) 보정용 (0이면 보정 없이 통과)
    if step and step > 0:
        return ( (value // step) * step )
    return value

def compute_equal_notional_qty(
    exchange,                 # ccxt 인스턴스
    symbol: str,              # ccxt 심볼 (예: 'SPKUSDT:USDT' 또는 'SPKUSDT_UMCBL' 등 네 코드 방식)
    price: float,             # 현재가 (없으면 fetch_ticker로 가져오기)
    equity_usdt: float,       # 현재 총 시드(USDT)
    fraction_per_position: float,  # 환경변수 FRACTION_PER_POSITION (예: 0.05)
    leverage: float           # 레버리지 (비트겟에서 Already-Set 일 때도 계산용으로만 사용)
) -> float:
    """
    목표: 모든 코인이 동일 '마진(증거금)'이 들어가도록 qty를 계산한다.
    - target_margin = equity * fraction
    - notional = target_margin * leverage
    - qty_raw = notional / price  = (target_margin * leverage) / price
    - 마지막에 market.limits / precision을 참고해 lotSize에 맞춰 아래로 반올림한다.
    """
    markets = exchange.markets
    m = markets.get(symbol)
    if not m:
        # 심볼이 미리 로드 안되어 있으면 한 번 로드
        exchange.load_markets()
        m = exchange.markets.get(symbol)
        if not m:
            raise Exception(f"Unknown market: {symbol}")

    # 1) 목표 마진(증거금, USDT)
    target_margin = equity_usdt * fraction_per_position

    # 2) 목표 명목가치(USDT)
    target_notional = target_margin * leverage

    # 3) 원시 수량
    qty_raw = target_notional / price

    # 4) lotSize/stepSize 보정(아래로 반올림)
    step = None
    # ccxt bitget 선물은 보통 'limits' 혹은 'precision'에 step 정보가 들어있음
    # 가장 안전한 접근:
    #  - m.get('limits', {}).get('amount', {}).get('min')/('max') 및
    #  - m.get('precision', {}).get('amount') or m['info']['sizePlace'] 등을 체크
    # 거래소 마다 표현이 달라서, 우선 step 후보들을 탐색:
    step_candidates = [
        (m.get('limits', {}).get('amount', {}) or {}).get('step'),
        m.get('precision', {}).get('amount'),
        m.get('info', {}).get('sizeStep'),
        m.get('info', {}).get('lotSz'),
        m.get('info', {}).get('minSz'),
    ]
    for sc in step_candidates:
        if sc:
            try:
                s = float(sc)
                if s > 0:
                    step = s
                    break
            except Exception:
                pass

    qty = qty_raw
    if step:
        qty = _round_step(qty_raw, step)

    # 5) 최소수량/최소명목가(있으면) 확인
    min_amt = (m.get('limits', {}).get('amount', {}) or {}).get('min')
    if min_amt:
        try:
            if qty < float(min_amt):
                # 최소수량 미만이면 주문 스킵(0 리턴)
                return 0.0
        except Exception:
            pass

    # 비트겟 일부 심볼은 최소 명목가 요구가 있을 수 있으므로 체크
    min_cost = (m.get('limits', {}).get('cost', {}) or {}).get('min')
    if min_cost:
        try:
            if (qty * price) < float(min_cost):
                return 0.0
        except Exception:
            pass

    return float(qty)


# == 주문 직전 로직 예시 (기존 place_order 부분을 이런 식으로 바꾼다) ==

def place_equal_notional_order(exchange, tv_symbol, side, allow_shorts=True):
    """
    - tv_symbol: 트뷰에서 넘어온 심볼(예: 'SPKUSDT.P') -> 내부 변환함수로 비트겟 심볼로 바꾼 뒤 사용
    - side: 'buy' or 'sell'
    """
    # 0) 트뷰 심볼 -> 비트겟 선물 심볼 변환
    ccxt_symbol = map_tv_to_bitget_symbol(tv_symbol)  # 네 코드에 이미 있는 변환기 사용

    # 1) 현재가
    ticker = exchange.fetch_ticker(ccxt_symbol)
    price  = float(ticker['last'] or ticker['close'])

    # 2) 현재 지갑(또는 사용 기준) 잔고
    bal = exchange.fetch_balance()
    equity = float(bal['USDT']['total'])  # 필요 시 free/total 중 선택

    frac = float(os.getenv("FRACTION_PER_POSITION", "0.05"))
    lev  = float(os.getenv("LEVERAGE", "20"))  # 비트겟에서 수동설정 사용하는 경우라도 계산용으로 쓴다

    # 3) 동일 USD 노출을 위한 수량 산출
    qty = compute_equal_notional_qty(exchange, ccxt_symbol, price, equity, frac, lev)
    if qty <= 0:
        log.info(f"[SKIP] {ccxt_symbol} qty too small after lotSize rounding")
        return None

    # 4) 포지션 방향 분기 (롱/숏 허용)
    params = {}
    if side == "buy":
        # 롱 오픈 or 숏 클로즈(네 로직에서 구분되어 있을 것)
        if not allow_shorts:
            # allow_shorts=False라도 buy는 허용(롱 오픈/숏 청산 포함)
            pass
        order = exchange.create_market_buy_order(ccxt_symbol, qty, params)
    else:  # side == "sell"
        if not allow_shorts:
            # 숏 오픈이 금지라면, 이 sell이 '롱 청산'인지 '숏 오픈'인지 네 측 포지션/알고리즘에서 구분해서만 호출
            pass
        order = exchange.create_market_sell_order(ccxt_symbol, qty, params)

    return order
