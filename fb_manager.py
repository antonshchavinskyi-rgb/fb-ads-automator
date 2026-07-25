import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Токен безпечно зчитується із секретів GitHub
ACCESS_TOKEN = os.environ.get('FB_ACCESS_TOKEN')

ACCOUNTS = {
   # '1271459967771680': 'USD',  # 1
    '746852230541150': 'USD',   # 2
   # '269403135857791': 'USD',   # 3
    '1732457457319086': 'PLN',  # 4
   # '1117620796468102': 'USD',  # 5
}

OFFERS = {
    'пла': 16.0,
    'зол': 14.0,
    'сер': 13.0,
    'бро': 11.0,
    'зал': 9.0
}

API_VER = "v25.0"
POLAND_TZ = ZoneInfo("Europe/Warsaw")

# Атрибуція, яку використовуємо для підрахунку лідів у insights
ATTRIBUTION_WINDOW = ['1d_click']

CURRENCY_SYMBOLS = {
    'USD': '$',
    'PLN': 'zł',
}

def cur_symbol(currency):
    return CURRENCY_SYMBOLS.get(currency, currency + ' ')

def fetch_data(endpoint, params):
    params['access_token'] = ACCESS_TOKEN
    query_string = urllib.parse.urlencode(params)
    url = f"{endpoint}?{query_string}"
    
    results = []
    while url:
        time.sleep(0.1)
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode('utf-8'))
                results.extend(data.get('data', []))
                url = data.get('paging', {}).get('next') if 'paging' in data else None
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            if 'User request limit reached' in error_body or '"code":17' in error_body:
                print(" ⏳ Ліміт запитів Meta (Code 17). Пауза 15 сек...", flush=True)
                time.sleep(15)
                continue
            else:
                print(f" ❌ Помилка API Meta ({e.code}): {error_body}", flush=True)
            break
        except Exception as e:
            print(f" ⚠️ Помилка з'єднання: {e}", flush=True)
            break
    return results

def get_leads(actions_list):
    for action in actions_list:
        if action.get('action_type') in ['offsite_conversion.fb_pixel_lead', 'lead']:
            return int(action.get('value', 0))
    return 0

def change_entity_status(entity_id, new_status):
    time.sleep(0.1)
    url = f"https://graph.facebook.com/{API_VER}/{entity_id}"
    data = urllib.parse.urlencode({'status': new_status, 'access_token': ACCESS_TOKEN}).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req):
            return True
    except urllib.error.HTTPError as e:
        print(f" ❌ HTTP {e.code} при зміні статусу ID {entity_id}: {e.read().decode('utf-8')}", flush=True)
        return False
    except Exception as e:
        print(f" ❌ Помилка зміни статусу ID {entity_id}: {e}", flush=True)
        return False

def process_offers_logic(acc_id, currency, is_morning_restart):
    rate = 3.8 if currency == 'PLN' else 1.0
    sym = cur_symbol(currency)
    endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/adsets"
    # ФІКС 1: Додано campaign{name}, щоб Meta віддавала назву кампанії
    params = {'fields': 'id,name,status,effective_status,campaign{name}', 'limit': 250}
    raw_adsets = fetch_data(endpoint, params)
    
    adsets_data = {}
    for adset in raw_adsets:
        camp_name = adset.get('campaign', {}).get('name', '').lower()
        eff_status = adset.get('effective_status', adset.get('status'))
        
        if eff_status not in ['ACTIVE', 'PAUSED']:
            continue
            
        for tag, base_cpl in OFFERS.items():
            if tag in camp_name:
                adsets_data[adset['id']] = {
                    'name': adset['name'],
                    'status': adset.get('status'),
                    'tag': tag,
                    'target_cpl': base_cpl * rate * 1.0,
                    'limit_no_leads': base_cpl * rate * 0.6,
                    'limit_high_cpl': base_cpl * rate * 1.3,
                    'stats': {'today': {'s':0, 'l':0}, 'last_2d': {'s':0, 'l':0}, 'last_3d': {'s':0, 'l':0}}
                }
                break
                
    if not adsets_data:
        return

    # ФІКС 2: Час за Польщею
    now_poland = datetime.now(POLAND_TZ)
    today_str = now_poland.strftime('%Y-%m-%d')
    yesterday_str = (now_poland - timedelta(days=1)).strftime('%Y-%m-%d')
    last_2d_time_range = json.dumps({'since': yesterday_str, 'until': today_str})

    insights_endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights"
    
    insights_today = fetch_data(insights_endpoint, {'level': 'adset', 'fields': 'adset_id,spend,actions', 'date_preset': 'today', 'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW), 'limit': 250})
    for row in insights_today:
        aid = row.get('adset_id')
        if aid in adsets_data:
            adsets_data[aid]['stats']['today']['s'] = float(row.get('spend', 0))
            adsets_data[aid]['stats']['today']['l'] = get_leads(row.get('actions', []))

    insights_2d = fetch_data(insights_endpoint, {'level': 'adset', 'fields': 'adset_id,spend,actions', 'time_range': last_2d_time_range, 'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW), 'limit': 250})
    for row in insights_2d:
        aid = row.get('adset_id')
        if aid in adsets_data:
            adsets_data[aid]['stats']['last_2d']['s'] = float(row.get('spend', 0))
            adsets_data[aid]['stats']['last_2d']['l'] = get_leads(row.get('actions', []))

    insights_3d = fetch_data(insights_endpoint, {'level': 'adset', 'fields': 'adset_id,spend,actions', 'date_preset': 'last_3d', 'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW), 'limit': 250})
    for row in insights_3d:
        aid = row.get('adset_id')
        if aid in adsets_data:
            adsets_data[aid]['stats']['last_3d']['s'] = float(row.get('spend', 0))
            adsets_data[aid]['stats']['last_3d']['l'] = get_leads(row.get('actions', []))

    for aid, data in adsets_data.items():
        s_today, l_today = data['stats']['today']['s'], data['stats']['today']['l']
        cpl_today = s_today / l_today if l_today > 0 else 0
        s_2d, l_2d = data['stats']['last_2d']['s'], data['stats']['last_2d']['l']
        s_3d, l_3d = data['stats']['last_3d']['s'], data['stats']['last_3d']['l']
        cpl_3d = s_3d / l_3d if l_3d > 0 else 0
        
        t_cpl, l_no_leads, l_high_cpl = data['target_cpl'], data['limit_no_leads'], data['limit_high_cpl']
        action, reason = None, ""
        
        if s_today > l_no_leads and l_today == 0:
            action, reason = 'PAUSED', f"Швидкий стоп без лідів (TODAY): Витрати {s_today:.2f}{sym} > {l_no_leads:.2f}{sym}"
        elif s_today > l_high_cpl and l_today >= 1 and cpl_today > t_cpl:
            action, reason = 'PAUSED', f"Збитковий CPL (TODAY): CPL {cpl_today:.2f}{sym} > {t_cpl:.2f}{sym}"
        elif s_2d > l_no_leads and l_2d == 0:
            action, reason = 'PAUSED', f"Стоп без лідів (2 DAYS): Витрати {s_2d:.2f}{sym} > {l_no_leads:.2f}{sym}"
            
        if not action:
            if l_today >= 1 and cpl_today < t_cpl:
                action, reason = 'ACTIVE', f"Доліт ліда (TODAY): CPL {cpl_today:.2f}{sym} < {t_cpl:.2f}{sym}"
            elif is_morning_restart and l_3d > 0 and cpl_3d < t_cpl:
                action, reason = 'ACTIVE', f"Ранковий рестарт 05:30 (LAST 3 DAYS): CPL {cpl_3d:.2f}{sym} < {t_cpl:.2f}{sym}"
                
        if action and action != data['status']:
            icon = '🔴' if action == 'PAUSED' else '🟢'
            act_word = 'Вимкнено' if action == 'PAUSED' else 'Увімкнено'
            if change_entity_status(aid, action):
                print(f"   {icon} {act_word} група: [{data['tag'].upper()}] {data['name']} (ID: {aid})", flush=True)
                print(f"      ↳ Причина: {reason}", flush=True)

def main():
    if not ACCESS_TOKEN:
        print("❌ Помилка: FB_ACCESS_TOKEN не знайдено в змінних середовища!", flush=True)
        return

    now_poland = datetime.now(POLAND_TZ)
    is_morning_restart = now_poland.hour == 5 and 30 <= now_poland.minute <= 59
    
    print(f"🚀 [FB Manager Monitoring] {now_poland.strftime('%Y-%m-%d %H:%M:%S')} (Poland Time)", flush=True)
    
    # ФІКС 3: Ізоляція обробки кожного акаунта через try/except
    for acc_id, currency in ACCOUNTS.items():
        print(f"\n📊 Акаунт: {acc_id} ({currency})", flush=True)
        try:
            process_offers_logic(acc_id, currency, is_morning_restart)
        except Exception as e:
            print(f" ❌ Помилка під час обробки акаунта {acc_id}: {e}", flush=True)
        
    print("\n✅ Моніторинг успішно завершено.", flush=True)

if __name__ == '__main__':
    main()
