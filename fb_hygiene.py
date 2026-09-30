import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import html
from datetime import datetime, timezone, timedelta

from fb_api_guard import MetaRateLimitError, cooldown_active, set_cooldown, clear_after_success

from fb_config import (
    ACCOUNTS,
    OFFERS,
    API_VER,
    POLAND_TZ,
    ATTRIBUTION_WINDOW,
    HYGIENE_MIN_AGE_DAYS,
    HYGIENE_NO_IMPRESSIONS_DAYS,
    HYGIENE_LOW_DELIVERY_DAYS,
    HYGIENE_LOW_DELIVERY_FACTOR,
    currency_rate,
    currency_symbol,
    parse_campaign_name,
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


def send_telegram_lines(header, lines, max_chars=3800):
    """Надсилає звіт частинами, щоб не перевищити ліміт Telegram."""
    if not lines:
        send_telegram(header)
        return

    chunks = []
    current = header
    for line in lines:
        candidate = current + "\n\n" + line
        if len(candidate) > max_chars and current != header:
            chunks.append(current)
            current = header + "\n\n" + line
        else:
            current = candidate
    chunks.append(current)

    for chunk in chunks:
        send_telegram(chunk)


def fetch_data(endpoint, params, errors=None, context=""):
    """
    Meta GET із контрольованими повторними спробами.

    Помилка завжди записується в errors. Викликаюча логіка може відрізнити
    успішну порожню відповідь (справжні 0 показів) від збою API.
    """
    params = dict(params)
    params['access_token'] = ACCESS_TOKEN
    query_string = urllib.parse.urlencode(params)
    url = f"{endpoint}?{query_string}"

    results = []
    rate_limit_delays = [15, 30, 60]
    rate_limit_attempt = 0

    while url:
        time.sleep(0.1)
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode('utf-8'))
                results.extend(data.get('data', []))
                url = data.get('paging', {}).get('next') if 'paging' in data else None
                rate_limit_attempt = 0
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            try:
                payload = json.loads(error_body)
                meta_error = payload.get('error', {})
            except Exception:
                meta_error = {}

            code = meta_error.get('code')
            subcode = meta_error.get('error_subcode')
            is_transient = bool(meta_error.get('is_transient'))
            message = meta_error.get('message') or error_body
            is_rate_limit = (
                code in (4, 17)
                or subcode == 1504022
                or 'request limit' in str(message).lower()
                or 'user request limit' in str(message).lower()
            )

            if is_rate_limit and rate_limit_attempt < len(rate_limit_delays):
                delay = rate_limit_delays[rate_limit_attempt]
                rate_limit_attempt += 1
                print(
                    f" ⏳ Meta rate limit ({context}) code={code} subcode={subcode}. "
                    f"Пауза {delay} сек, спроба {rate_limit_attempt}/{len(rate_limit_delays)}...",
                    flush=True,
                )
                time.sleep(delay)
                continue

            if is_rate_limit:
                raise MetaRateLimitError(
                    context=context,
                    code=code,
                    subcode=subcode,
                    http_status=e.code,
                )
            else:
                msg = (
                    f"API Meta HTTP {e.code} {context}: "
                    f"code={code}, subcode={subcode}, message={message}"
                )

            print(f" ❌ {msg}", flush=True)
            if errors is not None:
                errors.append(msg)
            break
        except Exception as e:
            msg = f"Помилка з'єднання {context}: {e}"
            print(f" ⚠️ {msg}", flush=True)
            if errors is not None:
                errors.append(msg)
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
        body = e.read().decode('utf-8')
        try:
            payload = json.loads(body)
            meta_error = payload.get('error', {})
        except Exception:
            meta_error = {}
        code = meta_error.get('code')
        subcode = meta_error.get('error_subcode')
        message = meta_error.get('message') or body
        is_rate_limit = (
            code in (4, 17)
            or 'request limit' in str(message).lower()
            or 'user request limit' in str(message).lower()
        )
        if is_rate_limit:
            raise MetaRateLimitError(
                context=f"change status {entity_id}",
                code=code,
                subcode=subcode,
                http_status=e.code,
            )
        print(f" ❌ HTTP {e.code} при зміні статусу ID {entity_id}: {body}", flush=True)
        return False
    except Exception as e:
        print(f" ❌ Помилка зміни статусу ID {entity_id}: {e}", flush=True)
        return False


def parse_iso_time(time_str):
    try:
        return datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return None


def get_leads(actions_list):
    for action in actions_list:
        if action.get('action_type') in ['offsite_conversion.fb_pixel_lead', 'lead']:
            return int(float(action.get('value', 0)))
    return 0


def process_hygiene_logic(acc_id, events, errors):
    now_utc = datetime.now(timezone.utc)
    min_age_seconds = HYGIENE_MIN_AGE_DAYS * 24 * 60 * 60
    date_preset = f"last_{HYGIENE_NO_IMPRESSIONS_DAYS}d"
    currency = ACCOUNTS[acc_id]
    rate = currency_rate(currency)
    sym = currency_symbol(currency)

    # LOW DELIVERY: два повні попередні календарні дні за Europe/Warsaw.
    today_poland = datetime.now(POLAND_TZ).date()
    low_since = today_poland - timedelta(days=HYGIENE_LOW_DELIVERY_DAYS)
    low_until = today_poland - timedelta(days=1)
    low_range = json.dumps({
        'since': low_since.isoformat(),
        'until': low_until.isoformat(),
    })

    # 1. Адсети: власний status ACTIVE, старші за 3 дні, 0 показів за last_7d.
    # effective_status не використовуємо як фільтр: Hygiene має чистити також
    # ACTIVE-адсети всередині PAUSED кампаній.
    raw_adsets = fetch_data(
        f"https://graph.facebook.com/{API_VER}/act_{acc_id}/adsets",
        {'fields': 'id,name,status,effective_status,created_time,start_time,campaign_id,campaign{name}', 'limit': 250},
        errors,
        f"adsets account {acc_id}",
    )

    adset_meta = {}
    active_adsets = {}
    for adset in raw_adsets:
        meta = {
            'name': adset.get('name', ''),
            'campaign_id': adset.get('campaign_id', ''),
            'campaign_name': adset.get('campaign', {}).get('name', ''),
        }
        adset_meta[adset['id']] = meta

        if adset.get('status') != 'ACTIVE' or not adset.get('created_time'):
            continue
        created = parse_iso_time(adset['created_time'])
        if created and (now_utc - created).total_seconds() > min_age_seconds:
            active_adsets[adset['id']] = meta

    paused_adset_ids = set()

    if active_adsets:
        insight_errors_before = len(errors)
        insights = fetch_data(
            f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights",
            {
                'level': 'adset',
                'fields': 'adset_id,impressions',
                'date_preset': date_preset,
                'limit': 250,
            },
            errors,
            f"adset insights {date_preset} account {acc_id}",
        )

        if len(errors) > insight_errors_before:
            print(
                f"   🛡️ Hygiene fail-closed: AdSet-паузи для акаунта {acc_id} пропущено — "
                f"статистику {date_preset} не підтверджено.",
                flush=True,
            )
        else:
            with_impressions = {
                row.get('adset_id') for row in insights if int(row.get('impressions', 0)) > 0
            }

            for adset_id, meta in active_adsets.items():
                if adset_id not in with_impressions and change_entity_status(adset_id, 'PAUSED'):
                    paused_adset_ids.add(adset_id)
                    print(f"   🧹 Гігієна: Вимкнено неактивну групу [{meta['name']}] | ID: {adset_id}", flush=True)
                    events.append(
                        f"🧹 <b>Групу вимкнено</b>: {esc(meta['name'])}\n"
                        f"   Campaign: {esc(meta['campaign_name'] or '—')}\n"
                        f"   CID: <code>{esc(meta['campaign_id'] or '—')}</code> | AID: <code>{adset_id}</code>"
                    )

    # 1.5. LOW DELIVERY
    # Група мала два повні попередні дні для роботи, але не набрала delivery:
    # 0 лідів і Spend < 0.40 × BE -> PAUSED.
    #
    # Для цього правила вимагаємо effective_status == ACTIVE, щоб PAUSED кампанія
    # не виглядала як low delivery лише через те, що вона вимкнена.
    low_candidates = {}
    for adset in raw_adsets:
        aid = adset.get('id')
        if (
            not aid
            or aid in paused_adset_ids
            or adset.get('status') != 'ACTIVE'
            or adset.get('effective_status') != 'ACTIVE'
        ):
            continue

        started = parse_iso_time(adset.get('start_time') or adset.get('created_time'))
        if not started:
            continue

        # Група повинна існувати не пізніше початку двохденного вікна.
        if started.astimezone(POLAND_TZ).date() > low_since:
            continue

        parsed = parse_campaign_name(adset.get('campaign', {}).get('name', ''))
        category = parsed.get('category')
        if category not in OFFERS:
            continue

        low_candidates[aid] = {
            'name': adset.get('name', ''),
            'campaign_id': adset.get('campaign_id', ''),
            'campaign_name': adset.get('campaign', {}).get('name', ''),
            'be': OFFERS[category] * rate,
            'spend': 0.0,
            'leads': 0,
        }

    if low_candidates:
        insight_errors_before = len(errors)
        low_insights = fetch_data(
            f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights",
            {
                'level': 'adset',
                'fields': 'adset_id,spend,actions',
                'time_range': low_range,
                'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW),
                'limit': 250,
            },
            errors,
            f"low delivery insights {low_since.isoformat()}–{low_until.isoformat()} account {acc_id}",
        )

        if len(errors) > insight_errors_before:
            print(
                f"   🛡️ Hygiene fail-closed: LOW DELIVERY для акаунта {acc_id} пропущено — статистику не підтверджено.",
                flush=True,
            )
        else:
            for row in low_insights:
                aid = row.get('adset_id')
                if aid in low_candidates:
                    low_candidates[aid]['spend'] = float(row.get('spend', 0))
                    low_candidates[aid]['leads'] = get_leads(row.get('actions', []))

            for aid, meta in low_candidates.items():
                limit = meta['be'] * HYGIENE_LOW_DELIVERY_FACTOR
                if meta['leads'] == 0 and meta['spend'] < limit:
                    if change_entity_status(aid, 'PAUSED'):
                        paused_adset_ids.add(aid)
                        print(
                            f"   🐢 LOW DELIVERY: Вимкнено групу [{meta['name']}] | "
                            f"Spend {meta['spend']:.2f}{sym} < {limit:.2f}{sym} | ID: {aid}",
                            flush=True,
                        )
                        events.append(
                            f"🐢 <b>LOW DELIVERY — групу вимкнено</b>: {esc(meta['name'])}\n"
                            f"   Campaign: {esc(meta['campaign_name'] or '—')}\n"
                            f"   CID: <code>{esc(meta['campaign_id'] or '—')}</code> | AID: <code>{aid}</code>\n"
                            f"   {low_since.isoformat()}–{low_until.isoformat()}: 0 лідів • "
                            f"Spend {meta['spend']:.2f}{sym} < {limit:.2f}{sym} ({HYGIENE_LOW_DELIVERY_FACTOR:.2f}×BE)"
                        )

    # 2. Оголошення: власний status ACTIVE, старші за 3 дні, 0 показів за last_7d.
    # effective_status не використовуємо як фільтр: Hygiene має чистити також
    # ACTIVE-оголошення всередині PAUSED кампаній/адсетів.
    raw_ads = fetch_data(
        f"https://graph.facebook.com/{API_VER}/act_{acc_id}/ads",
        {'fields': 'id,name,status,effective_status,created_time,adset_id', 'limit': 250},
        errors,
        f"ads account {acc_id}",
    )

    active_ads = {}
    for ad in raw_ads:
        if ad.get('status') != 'ACTIVE' or not ad.get('created_time'):
            continue
        created = parse_iso_time(ad['created_time'])
        if created and (now_utc - created).total_seconds() > min_age_seconds:
            active_ads[ad['id']] = {
                'name': ad.get('name', ''),
                'adset_id': ad.get('adset_id', ''),
            }

    if active_ads:
        insight_errors_before = len(errors)
        insights = fetch_data(
            f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights",
            {
                'level': 'ad',
                'fields': 'ad_id,impressions',
                'date_preset': date_preset,
                'limit': 250,
            },
            errors,
            f"ad insights {date_preset} account {acc_id}",
        )

        if len(errors) > insight_errors_before:
            print(
                f"   🛡️ Hygiene fail-closed: Ad-паузи для акаунта {acc_id} пропущено — "
                f"статистику {date_preset} не підтверджено.",
                flush=True,
            )
        else:
            with_impressions = {
                row.get('ad_id') for row in insights if int(row.get('impressions', 0)) > 0
            }

            for ad_id, meta in active_ads.items():
                if ad_id not in with_impressions and change_entity_status(ad_id, 'PAUSED'):
                    parent = adset_meta.get(meta['adset_id'], {})
                    print(f"   🧹 Гігієна: Вимкнено неактивне оголошення [{meta['name']}] | ID: {ad_id}", flush=True)
                    events.append(
                        f"🧹 <b>Оголошення вимкнено</b>: {esc(meta['name'])}\n"
                        f"   Group: {esc(parent.get('name', '—'))}\n"
                        f"   Campaign: {esc(parent.get('campaign_name', '—'))}\n"
                        f"   CID: <code>{esc(parent.get('campaign_id', '—'))}</code> | AID: <code>{esc(meta['adset_id'] or '—')}</code>"
                    )


def main():
    if not ACCESS_TOKEN:
        print("❌ Помилка: FB_ACCESS_TOKEN не знайдено в змінних середовища!", flush=True)
        return

    cooldown, cooldown_state = cooldown_active("main")
    if cooldown:
        print(
            f"⏸️ FB Hygiene пропущено: Meta API cooldown до {cooldown_state.get('until')} "
            f"| {cooldown_state.get('reason', '')}",
            flush=True,
        )
        return

    now_poland = datetime.now(POLAND_TZ)
    print(f"🧹 [FB Ads Hygiene Start] {now_poland.strftime('%Y-%m-%d %H:%M:%S')} (Poland Time)", flush=True)

    all_events = []
    all_errors = []
    rate_limited = False

    for acc_id, currency in ACCOUNTS.items():
        print(f"\n📊 Акаунт: {acc_id} ({currency})", flush=True)
        acc_events = []
        acc_errors = []
        try:
            process_hygiene_logic(acc_id, acc_events, acc_errors)
        except MetaRateLimitError as e:
            rate_limited = True
            reason = f"Code {e.code}" + (f"/{e.subcode}" if e.subcode else "")
            print(f" 🚨 {e}", flush=True)
            try:
                set_cooldown("main", reason, "fb_hygiene")
            except Exception as guard_error:
                print(f" ⚠️ Не вдалося зберегти API cooldown: {guard_error}", flush=True)
            all_errors.append(f"Акаунт {acc_id} ({currency}): {esc(e)}")
        except Exception as e:
            err_msg = f"Акаунт {acc_id} ({currency}): {esc(e)}"
            print(f" ❌ Помилка під час обробки гігієни акаунта {acc_id}: {e}", flush=True)
            all_errors.append(err_msg)

        all_events.extend(f"Акаунт {acc_id} ({currency}): {e}" for e in acc_events)
        all_errors.extend(f"Акаунт {acc_id} ({currency}): {esc(e)}" for e in acc_errors)

        if rate_limited:
            print("⏸️ Решту акаунтів пропущено через Meta API cooldown.", flush=True)
            break

    if not rate_limited:
        try:
            clear_after_success("main")
        except Exception as guard_error:
            print(f" ⚠️ Не вдалося очистити API cooldown: {guard_error}", flush=True)

    print("\n✅ Денне чищення завершено.", flush=True)

    time_str = now_poland.strftime('%Y-%m-%d %H:%M')
    if all_errors:
        header = f"⚠️ <b>FB Hygiene: помилки</b> ({time_str})"
        send_telegram_lines(header, all_errors + all_events)
    elif all_events:
        header = f"🧹 <b>FB Hygiene: очищено</b> ({time_str})"
        send_telegram_lines(header, all_events)
    else:
        send_telegram(f"✅ FB Hygiene живий, чистити не було чого ({time_str})")


if __name__ == '__main__':
    main()
