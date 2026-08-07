import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import html
from datetime import datetime, timedelta

from fb_config import (
    ACCOUNTS,
    OFFERS,
    API_VER,
    POLAND_TZ,
    ATTRIBUTION_WINDOW,
    MANAGER_NO_LEAD_FACTOR,
    MANAGER_HIGH_SPEND_FACTOR,
    MANAGER_TARGET_CPL_FACTOR,
    MANAGER_TWO_DAY_RULE_START_HOUR,
    MANAGER_HEARTBEAT_HOUR,
    MANAGER_HEARTBEAT_MINUTE_MAX,
    currency_rate,
    currency_symbol,
)

ACCESS_TOKEN = os.environ.get('FB_ACCESS_TOKEN')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')


def esc(text):
    return html.escape(str(text), quote=False)


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
    params = dict(params)
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
            print(f" ❌ Помилка API Meta ({e.code}): {error_body}", flush=True)
            break
        except Exception as e:
            print(f" ⚠️ Помилка з'єднання: {e}", flush=True)
            break
    return results


def get_leads(actions_list):
    for action in actions_list:
        if action.get('action_type') in ['offsite_conversion.fb_pixel_lead', 'lead']:
            return int(float(action.get('value', 0)))
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


def parse_category(campaign_name):
    """Повертає категорію BE для звичайної або каталожної кампанії."""
    parts = [p.strip().lower() for p in campaign_name.split('-')]

    # Звичайна кампанія: offer_id - category - ...
    if len(parts) >= 2 and parts[1] in OFFERS:
        return parts[1]

    # Каталог: category - ктг - ...
    if parts and parts[0] in OFFERS and 'ктг' in parts:
        return parts[0]

    # Безпечний fallback для старих назв
    for part in parts:
        if part in OFFERS:
            return part
    return None


def process_offers_logic(acc_id, currency, events):
    rate = currency_rate(currency)
    sym = currency_symbol(currency)

    endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/adsets"
    raw_adsets = fetch_data(endpoint, {
        'fields': 'id,name,status,effective_status,campaign{name}',
        'limit': 250,
    })

    adsets_data = {}
    for adset in raw_adsets:
        eff_status = adset.get('effective_status', adset.get('status'))
        if eff_status not in ['ACTIVE', 'PAUSED']:
            continue

        campaign_name = adset.get('campaign', {}).get('name', '')
        tag = parse_category(campaign_name)
        if not tag:
            continue

        base_cpl = OFFERS[tag]
        adsets_data[adset['id']] = {
            'name': adset['name'],
            'status': adset.get('status'),
            'tag': tag,
            'target_cpl': base_cpl * rate * MANAGER_TARGET_CPL_FACTOR,
            'limit_no_leads': base_cpl * rate * MANAGER_NO_LEAD_FACTOR,
            'limit_high_cpl': base_cpl * rate * MANAGER_HIGH_SPEND_FACTOR,
            'stats': {
                'today': {'s': 0.0, 'l': 0},
                'last_2d': {'s': 0.0, 'l': 0},
            },
        }

    if not adsets_data:
        return

    now_poland = datetime.now(POLAND_TZ)
    today_str = now_poland.strftime('%Y-%m-%d')
    yesterday_str = (now_poland - timedelta(days=1)).strftime('%Y-%m-%d')
    last_2d_time_range = json.dumps({'since': yesterday_str, 'until': today_str})

    insights_endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights"

    insights_today = fetch_data(insights_endpoint, {
        'level': 'adset',
        'fields': 'adset_id,spend,actions',
        'date_preset': 'today',
        'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW),
        'limit': 250,
    })
    for row in insights_today:
        aid = row.get('adset_id')
        if aid in adsets_data:
            adsets_data[aid]['stats']['today']['s'] = float(row.get('spend', 0))
            adsets_data[aid]['stats']['today']['l'] = get_leads(row.get('actions', []))

    insights_2d = fetch_data(insights_endpoint, {
        'level': 'adset',
        'fields': 'adset_id,spend,actions',
        'time_range': last_2d_time_range,
        'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW),
        'limit': 250,
    })
    for row in insights_2d:
        aid = row.get('adset_id')
        if aid in adsets_data:
            adsets_data[aid]['stats']['last_2d']['s'] = float(row.get('spend', 0))
            adsets_data[aid]['stats']['last_2d']['l'] = get_leads(row.get('actions', []))

    for aid, data in adsets_data.items():
        s_today = data['stats']['today']['s']
        l_today = data['stats']['today']['l']
        cpl_today = s_today / l_today if l_today > 0 else 0.0
        s_2d = data['stats']['last_2d']['s']
        l_2d = data['stats']['last_2d']['l']

        t_cpl = data['target_cpl']
        l_no_leads = data['limit_no_leads']
        l_high_cpl = data['limit_high_cpl']

        action = None
        reason = ""

        # 1) Швидкий стоп сьогодні без ліда
        if s_today > l_no_leads and l_today == 0:
            action = 'PAUSED'
            reason = f"Швидкий стоп без лідів (TODAY): Витрати {s_today:.2f}{sym} > {l_no_leads:.2f}{sym}"

        # 2) High-CPL stop після хоча б одного ліда
        elif s_today > l_high_cpl and l_today >= 1 and cpl_today > t_cpl:
            action = 'PAUSED'
            reason = f"Збитковий CPL (TODAY): CPL {cpl_today:.2f}{sym} > {t_cpl:.2f}{sym}"

        # 3) Сьогодні + вчора без лідів; працює після 10:00 за Польщею
        elif now_poland.hour >= MANAGER_TWO_DAY_RULE_START_HOUR and s_2d > l_no_leads and l_2d == 0:
            action = 'PAUSED'
            reason = f"Стоп без лідів (2 DAYS): Витрати {s_2d:.2f}{sym} > {l_no_leads:.2f}{sym}"

        # 4) Доліт ліда після паузи
        if not action and l_today >= 1 and cpl_today < t_cpl:
            action = 'ACTIVE'
            reason = f"Доліт ліда (TODAY): CPL {cpl_today:.2f}{sym} < {t_cpl:.2f}{sym}"

        if action and action != data['status']:
            icon = '🔴' if action == 'PAUSED' else '🟢'
            act_word = 'Вимкнено' if action == 'PAUSED' else 'Увімкнено'
            if change_entity_status(aid, action):
                print(f"   {icon} {act_word} група: [{data['tag'].upper()}] {data['name']} (ID: {aid})", flush=True)
                print(f"      ↳ Причина: {reason}", flush=True)
                events.append(
                    f"{icon} <b>{act_word}</b>: [{esc(data['tag'].upper())}] {esc(data['name'])}\n"
                    f"   ID: <code>{aid}</code>\n"
                    f"   ↳ {esc(reason)}"
                )


def main():
    if not ACCESS_TOKEN:
        print("❌ Помилка: FB_ACCESS_TOKEN не знайдено в змінних середовища!", flush=True)
        return

    now_poland = datetime.now(POLAND_TZ)
    is_heartbeat_window = (
        now_poland.hour == MANAGER_HEARTBEAT_HOUR
        and now_poland.minute < MANAGER_HEARTBEAT_MINUTE_MAX
    )

    print(f"🚀 [FB Manager Monitoring] {now_poland.strftime('%Y-%m-%d %H:%M:%S')} (Poland Time)", flush=True)

    all_events = []
    all_errors = []

    for acc_id, currency in ACCOUNTS.items():
        print(f"\n📊 Акаунт: {acc_id} ({currency})", flush=True)
        acc_events = []
        try:
            process_offers_logic(acc_id, currency, acc_events)
            all_events.extend(f"Акаунт {acc_id} ({currency}):\n{e}" for e in acc_events)
        except Exception as e:
            err_msg = f"Акаунт {acc_id} ({currency}): {esc(e)}"
            print(f" ❌ Помилка під час обробки акаунта {acc_id}: {e}", flush=True)
            all_errors.append(err_msg)

    print("\n✅ Моніторинг успішно завершено.", flush=True)

    time_str = now_poland.strftime('%Y-%m-%d %H:%M')
    if all_errors:
        text = f"⚠️ <b>FB Manager: помилки</b> ({time_str})\n\n" + "\n\n".join(all_errors)
        if all_events:
            text += "\n\n" + "\n\n".join(all_events)
        send_telegram(text)
    elif all_events:
        text = f"🔔 <b>FB Manager: зміни</b> ({time_str})\n\n" + "\n\n".join(all_events)
        send_telegram(text)
    elif is_heartbeat_window:
        send_telegram(f"✅ FB Manager живий, змін не було ({time_str})")


if __name__ == '__main__':
    main()
