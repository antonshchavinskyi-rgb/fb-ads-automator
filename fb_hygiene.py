import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ACCESS_TOKEN = os.environ.get('FB_ACCESS_TOKEN')

ACCOUNTS = {
   # '1271459967771680': 'USD',  # 1
    '746852230541150': 'USD',   # 2
    #'269403135857791': 'USD',   # 3
    '1732457457319086': 'PLN',  # 4
    #'1117620796468102': 'USD',  # 5
}

API_VER = "v25.0"
POLAND_TZ = ZoneInfo("Europe/Warsaw")

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(" ⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не задані — сповіщення пропущено.", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f" ⚠️ Не вдалося надіслати Telegram-повідомлення: {e}", flush=True)

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

def parse_iso_time(time_str):
    try:
        return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S%z")
    except:
        return None

def process_hygiene_logic(acc_id, events):
    now_utc = datetime.now(timezone.utc)
    
    # 1. Гігієна груп (старіші за 3 дні без показів за останні 7 днів)
    adsets_endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/adsets"
    raw_adsets = fetch_data(adsets_endpoint, {'fields': 'id,name,effective_status,created_time', 'limit': 250})
    
    active_adsets = {}
    for a in raw_adsets:
        if a.get('effective_status') == 'ACTIVE' and a.get('created_time'):
            p_time = parse_iso_time(a['created_time'])
            # Захист від None, якщо час не вдалося розпарсити
            if p_time and (now_utc - p_time).total_seconds() > 259200:
                active_adsets[a['id']] = a['name']
                
    if active_adsets:
        adset_insights = fetch_data(f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights", {'level': 'adset', 'fields': 'adset_id,impressions', 'date_preset': 'last_7d', 'limit': 250})
        adsets_with_impressions = {r.get('adset_id') for r in adset_insights if int(r.get('impressions', 0)) > 0}
        
        for aid, name in active_adsets.items():
            if aid not in adsets_with_impressions and change_entity_status(aid, 'PAUSED'):
                print(f"   🧹 Гігієна: Вимкнено неактивну групу [{name}] | ID: {aid}", flush=True)
                events.append(f"🧹 Група: {name}")

    # 2. Гігієна оголошень (старіші за 3 дні без показів за останні 7 днів)
    ads_endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/ads"
    raw_ads = fetch_data(ads_endpoint, {'fields': 'id,name,effective_status,created_time', 'limit': 250})
    
    active_ads = {}
    for a in raw_ads:
        if a.get('effective_status') == 'ACTIVE' and a.get('created_time'):
            p_time = parse_iso_time(a['created_time'])
            # Захист від None, якщо час не вдалося розпарсити
            if p_time and (now_utc - p_time).total_seconds() > 259200:
                active_ads[a['id']] = a['name']
                
    if active_ads:
        ad_insights = fetch_data(f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights", {'level': 'ad', 'fields': 'ad_id,impressions', 'date_preset': 'last_7d', 'limit': 250})
        ads_with_impressions = {r.get('ad_id') for r in ad_insights if int(r.get('impressions', 0)) > 0}
        
        for ad_id, name in active_ads.items():
            if ad_id not in ads_with_impressions and change_entity_status(ad_id, 'PAUSED'):
                print(f"   🧹 Гігієна: Вимкнено неактивне оголошення [{name}] | ID: {ad_id}", flush=True)
                events.append(f"🧹 Оголошення: {name}")

def main():
    if not ACCESS_TOKEN:
        print("❌ Помилка: FB_ACCESS_TOKEN не знайдено в змінних середовища!", flush=True)
        return

    now_poland = datetime.now(POLAND_TZ)
    print(f"🧹 [FB Ads Hygiene Start] {now_poland.strftime('%Y-%m-%d %H:%M:%S')} (Poland Time)", flush=True)

    all_events = []
    all_errors = []

    for acc_id, currency in ACCOUNTS.items():
        print(f"\n📊 Акаунт: {acc_id} ({currency})", flush=True)
        acc_events = []
        try:
            process_hygiene_logic(acc_id, acc_events)
            all_events.extend(f"Акаунт {acc_id} ({currency}): {e}" for e in acc_events)
        except Exception as e:
            err_msg = f"Акаунт {acc_id} ({currency}): {e}"
            print(f" ❌ Помилка під час обробки гігієни акаунта {acc_id}: {e}", flush=True)
            all_errors.append(err_msg)

    print("\n✅ Денне чищення успішно завершено.", flush=True)

    time_str = now_poland.strftime('%Y-%m-%d %H:%M')
    if all_errors:
        text = f"⚠️ <b>FB Hygiene: помилки</b> ({time_str})\n\n" + "\n".join(all_errors)
        if all_events:
            text += "\n\n" + "\n".join(all_events)
        send_telegram(text)
    elif all_events:
        text = f"🧹 <b>FB Hygiene: очищено</b> ({time_str})\n\n" + "\n".join(all_events)
        send_telegram(text)
    else:
        send_telegram(f"✅ FB Hygiene живий, чистити не було чого ({time_str})")

if __name__ == '__main__':
    main()
