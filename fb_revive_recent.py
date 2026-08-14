import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import html
from collections import defaultdict
from datetime import datetime, timedelta

from fb_config import (
    ACCOUNTS,
    OFFERS,
    API_VER,
    POLAND_TZ,
    ATTRIBUTION_WINDOW,
    REVIVE_DRY_RUN,
    REVIVE_RECENT_TWO_PLUS_CPL_FACTOR,
    REVIVE_RECENT_ONE_CPL_FACTOR,
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
    """Надсилає один або кілька компактних повідомлень без ризику перевищити Telegram limit."""
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
    Meta GET with compact transient-rate-limit handling.

    For transient application/request limits (code 4 / code 17 / subcode 1504022)
    we back off instead of immediately failing or retrying forever.
    """
    params = dict(params)
    params['access_token'] = ACCESS_TOKEN
    query_string = urllib.parse.urlencode(params)
    url = f"{endpoint}?{query_string}"

    results = []
    rate_limit_delays = [30, 60, 120]
    rate_limit_attempt = 0

    while url:
        time.sleep(0.2)
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
                msg = (
                    f"Meta rate limit після {rate_limit_attempt} повторів — {context}; "
                    f"HTTP {e.code}, code={code}, subcode={subcode}, transient={is_transient}"
                )
            else:
                msg = f"API Meta HTTP {e.code} {context}: code={code}, subcode={subcode}, message={message}"

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

def get_leads(actions_list):
    for action in actions_list:
        if action.get('action_type') in ['offsite_conversion.fb_pixel_lead', 'lead']:
            return int(float(action.get('value', 0)))
    return 0


def change_entity_status(entity_id, new_status, errors=None, context=""):
    time.sleep(0.1)
    url = f"https://graph.facebook.com/{API_VER}/{entity_id}"
    data = urllib.parse.urlencode({'status': new_status, 'access_token': ACCESS_TOKEN}).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req):
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        msg = f"HTTP {e.code} при зміні статусу {context or entity_id}: {body}"
        print(f" ❌ {msg}", flush=True)
        if errors is not None:
            errors.append(msg)
        return False
    except Exception as e:
        msg = f"Помилка зміни статусу {context or entity_id}: {e}"
        print(f" ❌ {msg}", flush=True)
        if errors is not None:
            errors.append(msg)
        return False


def build_time_ranges(now_poland):
    """Два повні попередні календарні дні за Europe/Warsaw."""
    today = now_poland.date()
    since = today - timedelta(days=2)
    until = today - timedelta(days=1)
    return {
        'recent': json.dumps({'since': since.isoformat(), 'until': until.isoformat()}),
        'recent_label': f"{since.isoformat()}–{until.isoformat()}",
    }


def get_account_state(acc_id, currency, time_ranges, errors, warnings):
    """
    RECENT 2D бере лише PAUSED-групи в ACTIVE кампаніях з активністю за два
    повні попередні дні. Старі кампанії без активності не перевіряються,
    тому не створюють UNKNOWN CAMPAIGN FORMAT шум.
    """
    campaigns_raw = fetch_data(
        f"https://graph.facebook.com/{API_VER}/act_{acc_id}/campaigns",
        {'fields': 'id,name,status,effective_status', 'limit': 250},
        errors,
        f"campaigns account {acc_id}",
    )

    active_campaigns = {
        c['id']: c
        for c in campaigns_raw
        if c.get('effective_status') == 'ACTIVE'
    }

    adsets_raw = fetch_data(
        f"https://graph.facebook.com/{API_VER}/act_{acc_id}/adsets",
        {'fields': 'id,name,status,effective_status,campaign_id', 'limit': 250},
        errors,
        f"adsets account {acc_id}",
    )

    # Спочатку збираємо тільки PAUSED адсети в ACTIVE кампаніях — без парсингу назви.
    raw_candidates = {}
    for adset in adsets_raw:
        if adset.get('status') != 'PAUSED':
            continue

        campaign_id = adset.get('campaign_id')
        campaign = active_campaigns.get(campaign_id)
        if not campaign:
            continue

        raw_candidates[adset['id']] = {
            'adset_id': adset['id'],
            'adset_name': adset.get('name', ''),
            'campaign_id': campaign_id,
            'campaign_name': campaign.get('name', ''),
            'recent_spend': 0.0,
            'recent_leads': 0,
            'recent_impressions': 0,
        }

    if not raw_candidates:
        return {}, defaultdict(list)

    insights_endpoint = f"https://graph.facebook.com/{API_VER}/act_{acc_id}/insights"

    # Recent: 2 повні попередні дні.
    recent = fetch_data(
        insights_endpoint,
        {
            'level': 'adset',
            'fields': 'adset_id,spend,actions,impressions',
            'time_range': time_ranges['recent'],
            'action_attribution_windows': json.dumps(ATTRIBUTION_WINDOW),
            'limit': 250,
        },
        errors,
        f"recent insights account {acc_id}",
    )
    for row in recent:
        aid = row.get('adset_id')
        if aid in raw_candidates:
            raw_candidates[aid]['recent_spend'] = float(row.get('spend', 0))
            raw_candidates[aid]['recent_leads'] = get_leads(row.get('actions', []))
            raw_candidates[aid]['recent_impressions'] = int(row.get('impressions', 0))

    candidates = {}
    rate = currency_rate(currency)
    unknown_campaigns = set()

    for aid, raw in raw_candidates.items():
        recent_relevant = any((
            raw['recent_impressions'] > 0,
            raw['recent_spend'] > 0,
            raw['recent_leads'] > 0,
        ))

        # Без активності у Recent-вікні цей PAUSED adset не стосується цього скрипта.
        if not recent_relevant:
            continue

        parsed = parse_campaign_name(raw['campaign_name'])
        if not parsed['valid']:
            warning_key = raw['campaign_id'] or raw['campaign_name']
            if warning_key not in unknown_campaigns:
                unknown_campaigns.add(warning_key)
                relevant_reason = ['є активність у Recent-вікні']
                warnings.append(
                    f"⚠️ <b>UNKNOWN CAMPAIGN FORMAT</b>\n"
                    f"   Campaign: {esc(raw['campaign_name'] or '—')}\n"
                    f"   CID: <code>{esc(raw['campaign_id'] or '—')}</code>\n"
                    f"   ↳ {esc(parsed['reason'])}; {esc(' / '.join(relevant_reason))}; Revive кампанію пропустив"
                )
            continue

        category = parsed['category']
        candidates[aid] = {
            'account_id': acc_id,
            'currency': currency,
            'adset_id': aid,
            'adset_name': raw['adset_name'],
            'campaign_id': raw['campaign_id'],
            'campaign_name': raw['campaign_name'],
            'offer_id': parsed['offer_id'],
            'category': category,
            'is_catalog': parsed['is_catalog'],
            'be': OFFERS[category] * rate,
            'recent_spend': raw['recent_spend'],
            'recent_leads': raw['recent_leads'],
            'recent_impressions': raw['recent_impressions'],
        }

    if not candidates:
        return {}, defaultdict(list)

    # Ads потрібні тільки для тих груп, які реально пройшли попередній activity-фільтр і парсер.
    ads_raw = fetch_data(
        f"https://graph.facebook.com/{API_VER}/act_{acc_id}/ads",
        {'fields': 'id,name,status,effective_status,adset_id', 'limit': 250},
        errors,
        f"ads account {acc_id}",
    )
    ads_by_adset = defaultdict(list)
    for ad in ads_raw:
        aid = ad.get('adset_id')
        if aid in candidates:
            ads_by_adset[aid].append(ad)

    return candidates, ads_by_adset

def qualifies_recent(candidate):
    leads = candidate['recent_leads']
    spend = candidate['recent_spend']
    be = candidate['be']

    if leads <= 0:
        return False, 0.0

    cpl = spend / leads
    if leads >= 2:
        return cpl < be * REVIVE_RECENT_TWO_PLUS_CPL_FACTOR, cpl
    if leads == 1:
        return cpl <= be * REVIVE_RECENT_ONE_CPL_FACTOR, cpl
    return False, cpl



def activate_candidate(candidate, ads_by_adset, errors, dry_run=False):
    aid = candidate['adset_id']
    paused_ads = [ad for ad in ads_by_adset.get(aid, []) if ad.get('status') == 'PAUSED']

    if dry_run:
        # У DRY RUN нічого не змінюємо в Meta. Лише рахуємо, скільки оголошень
        # довелося б увімкнути разом із групою.
        print(
            f"   🧪 DRY RUN: would activate adset {aid} + {len(paused_ads)} paused ad(s)",
            flush=True,
        )
        return True, len(paused_ads)

    # Спочатку вмикаємо adset. Якщо оголошення було вимкнене Hygiene — вмикаємо його після цього.
    if not change_entity_status(aid, 'ACTIVE', errors, f"adset {aid}"):
        return False, 0

    activated_ads = 0
    for ad in paused_ads:
        if change_entity_status(ad['id'], 'ACTIVE', errors, f"ad {ad['id']} / adset {aid}"):
            activated_ads += 1

    return True, activated_ads


def main():
    if not ACCESS_TOKEN:
        print("❌ Помилка: FB_ACCESS_TOKEN не знайдено в змінних середовища!", flush=True)
        return

    now_poland = datetime.now(POLAND_TZ)
    time_ranges = build_time_ranges(now_poland)
    mode_label = 'DRY RUN' if REVIVE_DRY_RUN else 'LIVE'
    print(f"🌅 [FB Revive RECENT 2D / {mode_label}] {now_poland.strftime('%Y-%m-%d %H:%M:%S')} (Poland Time)", flush=True)
    print(f"   Recent: {time_ranges['recent_label']}", flush=True)

    errors = []
    warnings = []
    account_states = {}
    ads_maps = {}

    for acc_id, currency in ACCOUNTS.items():
        try:
            candidates, ads_by_adset = get_account_state(acc_id, currency, time_ranges, errors, warnings)
            account_states[acc_id] = candidates
            ads_maps[acc_id] = ads_by_adset
            print(f"📊 Акаунт {acc_id} ({currency}): paused candidates = {len(candidates)}", flush=True)
        except Exception as e:
            msg = f"Акаунт {acc_id} ({currency}): {e}"
            print(f" ❌ {msg}", flush=True)
            errors.append(msg)

    # 1) Recent Restart — без cap.
    recent_selected = []
    for acc_id, candidates in account_states.items():
        for candidate in candidates.values():
            ok, cpl = qualifies_recent(candidate)
            if ok:
                candidate['recent_cpl'] = cpl
                recent_selected.append(candidate)

    recent_events = []
    for candidate in recent_selected:
        ok, activated_ads = activate_candidate(
            candidate, ads_maps[candidate['account_id']], errors, dry_run=REVIVE_DRY_RUN
        )
        if not ok:
            continue
        sym = currency_symbol(candidate['currency'])
        recent_events.append(
            f"{'🧪' if REVIVE_DRY_RUN else '🟢'} <b>{'DRY RUN • ' if REVIVE_DRY_RUN else ''}RECENT 2D</b> [{esc(candidate['category'].upper())}] {esc(candidate['adset_name'])}\n"
            f"   Rule: <b>2 повні попередні дні</b> • {esc(time_ranges['recent_label'])}\n"
            f"   Account: <code>{candidate['account_id']}</code>\n"
            f"   Campaign: {esc(candidate['campaign_name'])}\n"
            f"   Campaign ID: <code>{candidate['campaign_id']}</code> | Adset ID: <code>{candidate['adset_id']}</code>\n"
            f"   {candidate['recent_leads']} лідів • CPL {candidate['recent_cpl']:.2f}{sym} • BE {candidate['be']:.2f}{sym}"
            + (f" • ads +{activated_ads}" if activated_ads else "")
        )

    time_str = now_poland.strftime('%Y-%m-%d %H:%M')
    summary = (
        f"🌅 <b>FB Revive RECENT 2D — {'DRY RUN' if REVIVE_DRY_RUN else 'LIVE'}</b> ({time_str})\n"
        f"Restarted: <b>{len(recent_events)}</b> ({esc(time_ranges['recent_label'])})\n"
        f"Warnings: <b>{len(warnings)}</b> | Errors: <b>{len(errors)}</b>"
    )
    if REVIVE_DRY_RUN:
        summary += "\n🧪 <b>Жоден статус у Meta не змінено.</b>"

    lines = []
    lines.extend(recent_events)
    lines.extend(warnings)

    if errors:
        lines.append("⚠️ <b>Помилки</b>\n" + "\n".join(esc(e) for e in errors[:10]))

    send_telegram_lines(summary, lines)

    print(f"✅ RECENT 2D завершено ({mode_label}). Restarted={len(recent_events)}, Warnings={len(warnings)}, Errors={len(errors)}", flush=True)


if __name__ == '__main__':
    main()
