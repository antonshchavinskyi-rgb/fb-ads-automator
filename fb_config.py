from zoneinfo import ZoneInfo

# Meta / accounts
API_VER = "v25.0"
POLAND_TZ = ZoneInfo("Europe/Warsaw")

ACCOUNTS = {
    '1271459967771680': 'USD',  # 1
    '746852230541150': 'USD',   # 2
    # '269403135857791': 'USD',   # 3
    '1732457457319086': 'PLN',  # 4
    # '1117620796468102': 'USD',  # 5
}

# Єдина курсова константа для бізнес-логіки
USD_PLN_RATE = 3.8

# Break-even CPL, USD
OFFERS = {
    'пла': 16.0,
    'зол': 14.0,
    'сер': 13.0,
    'бро': 11.0,
    'зал': 9.0,
}

# Атрибуція лідів у Meta Insights
ATTRIBUTION_WINDOW = ['1d_click']

CURRENCY_SYMBOLS = {
    'USD': '$',
    'PLN': 'zł',
}

# Затверджені стартові параметри (поки Scaler не реалізований — зберігаємо як source of truth)
COST_GOAL_FACTOR = 0.85
BID_CAP_FACTOR = 2.20
DAILY_BUDGET_FACTOR = 1.70
SCALER_CAMPAIGN_CAP = 50
SCALER_ACCOUNT_CAP = 150
SCALER_BID_DUPLICATE_MULTIPLIER = 0.50

# fb_manager.py
MANAGER_NO_LEAD_FACTOR = 0.60
MANAGER_HIGH_SPEND_FACTOR = 1.30
MANAGER_TARGET_CPL_FACTOR = 1.00
MANAGER_TWO_DAY_RULE_START_HOUR = 10
MANAGER_HEARTBEAT_HOUR = 12
MANAGER_HEARTBEAT_MINUTE_MAX = 15

# fb_hygiene.py
HYGIENE_MIN_AGE_DAYS = 3
HYGIENE_NO_IMPRESSIONS_DAYS = 7

# fb_revive.py — Recent Restart
REVIVE_RECENT_TWO_PLUS_CPL_FACTOR = 1.00  # strict <
REVIVE_RECENT_ONE_CPL_FACTOR = 0.70       # <=

# fb_revive.py — Deep Revive
REVIVE_DEEP_IDLE_DAYS = 7
REVIVE_DEEP_HISTORY_DAYS = 14
REVIVE_DEEP_TWO_PLUS_CPL_FACTOR = 0.80
REVIVE_DEEP_ONE_CPL_FACTOR = 0.60
REVIVE_DEEP_CAP_PER_OFFER = 10


def currency_rate(currency: str) -> float:
    return USD_PLN_RATE if currency == 'PLN' else 1.0


def currency_symbol(currency: str) -> str:
    return CURRENCY_SYMBOLS.get(currency, currency + ' ')
