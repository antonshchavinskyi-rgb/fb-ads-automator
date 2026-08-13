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

# Затверджені бізнес-параметри запуску та Scaler
COST_GOAL_FACTOR = 0.85
BID_CAP_FACTOR = 2.20
DAILY_BUDGET_FACTOR = 1.70
SCALER_CAMPAIGN_CAP = 20
SCALER_ACCOUNT_CAP = 150
SCALER_COST_GOAL_DUPLICATE_MULTIPLIER = 1.00
SCALER_BID_DUPLICATE_MULTIPLIER = 0.50

# (min leads, max leads or None, ((max CPL/BE, duplicates), ...))
SCALER_SOURCE_MATRIX = (
    (1, 1, ((0.60, 1), (0.90, 0))),
    (2, 3, ((0.75, 3), (0.90, 1))),
    (4, 6, ((0.75, 6), (0.90, 3))),
    (7, 9, ((0.75, 9), (0.90, 6))),
    (10, None, ((0.60, 18), (0.75, 15), (0.90, 12))),
)
SCALER_OFFER_TIERS = (
    (0.90, 1.00, False),
    (0.95, 0.75, False),
    (1.00, 0.50, False),
)
SCALER_BID_JITTER_MIN = 0.005
SCALER_BID_JITTER_MAX = 0.010
SCALER_START_HOUR = 5
SCALER_START_MINUTE_FROM = 35
SCALER_START_MINUTE_TO = 55

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

# Безпечний тестовий режим Revive: True = лише показати, що БИ увімкнули, без змін у Meta
REVIVE_DRY_RUN = False

# fb_revive.py — Recent Restart
REVIVE_RECENT_TWO_PLUS_CPL_FACTOR = 1.00  # strict <
REVIVE_RECENT_ONE_CPL_FACTOR = 0.70       # <=

# fb_revive.py — Deep Revive
REVIVE_DEEP_IDLE_DAYS = 7
REVIVE_DEEP_HISTORY_DAYS = 14
REVIVE_DEEP_TWO_PLUS_CPL_FACTOR = 0.80
REVIVE_DEEP_ONE_CPL_FACTOR = 0.60
REVIVE_DEEP_CAP_PER_OFFER = 10
REVIVE_DEEP_CAP_PER_CATALOG_CAMPAIGN = 10


def currency_rate(currency: str) -> float:
    return USD_PLN_RATE if currency == 'PLN' else 1.0


def currency_symbol(currency: str) -> str:
    return CURRENCY_SYMBOLS.get(currency, currency + ' ')

def parse_campaign_name(campaign_name: str) -> dict:
    """
    Єдиний толерантний парсер назв кампаній для всіх FB-скриптів.

    Правила:
    - категорія BE шукається як ТОЧНИЙ токен між дефісами, а не як підрядок;
    - зайві дефіси/пробіли та повтори того самого тегу не заважають;
    - якщо знайдено кілька РІЗНИХ BE-категорій, назва вважається неоднозначною;
    - каталог визначається маркером `ктг` або `каталог` як окремим токеном;
    - для звичайної кампанії offer_id = перший чисто цифровий токен;
    - для каталогу offer_id не потрібен.
    """
    raw = str(campaign_name or '')
    normalized = raw.replace('—', '-').replace('–', '-').replace('−', '-')
    parts = [p.strip().lower() for p in normalized.split('-') if p.strip()]

    catalog_markers = {'ктг', 'каталог'}
    is_catalog = any(p in catalog_markers for p in parts)
    category_hits = [p for p in parts if p in OFFERS]
    unique_categories = list(dict.fromkeys(category_hits))

    if not unique_categories:
        return {
            'offer_id': None,
            'category': None,
            'is_catalog': is_catalog,
            'valid': False,
            'reason': 'не знайдено тег BE (пла/зол/сер/бро/зал)',
            'tokens': parts,
        }

    if len(set(unique_categories)) > 1:
        return {
            'offer_id': None,
            'category': None,
            'is_catalog': is_catalog,
            'valid': False,
            'reason': 'знайдено кілька різних тегів BE: ' + ', '.join(unique_categories),
            'tokens': parts,
        }

    category = unique_categories[0]

    if is_catalog:
        return {
            'offer_id': None,
            'category': category,
            'is_catalog': True,
            'valid': True,
            'reason': '',
            'tokens': parts,
        }

    offer_id = next((p for p in parts if p.isdigit()), None)
    if not offer_id:
        return {
            'offer_id': None,
            'category': category,
            'is_catalog': False,
            'valid': False,
            'reason': 'не знайдено offer_id як окремий цифровий токен',
            'tokens': parts,
        }

    return {
        'offer_id': offer_id,
        'category': category,
        'is_catalog': False,
        'valid': True,
        'reason': '',
        'tokens': parts,
    }

