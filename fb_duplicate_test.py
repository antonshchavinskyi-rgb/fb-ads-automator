import os
import sys
import json
import time
import html
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

from fb_config import API_VER, POLAND_TZ

ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_TEST_ADSETS = 5
MAX_SYNC_CHILD_ADS = 3  # conservative sync deep-copy guard
REPORT_FILE = "duplicate_test_report.json"

ADSET_FIELDS = [
    "id", "name", "account_id", "campaign_id", "status", "effective_status",
    "bid_strategy", "bid_amount", "bid_constraints", "bid_info", "billing_event",
    "optimization_goal", "optimization_sub_event", "daily_budget", "lifetime_budget",
    "start_time", "end_time", "attribution_spec", "promoted_object", "destination_type",
    "pacing_type", "targeting", "is_dynamic_creative", "recurring_budget_semantics",
    "dsa_beneficiary", "dsa_payor", "existing_customer_budget_percentage",
    "frequency_control_specs", "automatic_manual_state", "source_adset_id",
]

AD_FIELDS = [
    "id", "name", "status", "effective_status", "adset_id", "campaign_id",
    "creative", "tracking_specs", "conversion_specs", "conversion_domain",
    "creative_automation_spec", "creative_asset_groups_spec", "source_ad_id",
]

CREATIVE_FIELDS = [
    "id", "name", "object_story_id", "object_story_spec", "asset_feed_spec",
    "degrees_of_freedom_spec", "creative_sourcing_spec", "format_transformation_spec",
    "generative_asset_spec", "product_set_id", "url_tags", "instagram_user_id",
    "source_facebook_post_id", "source_instagram_media_id", "template_url",
    "template_url_spec", "platform_customizations", "authorization_category",
    "dynamic_ad_voice", "product_suggestion_settings", "recommender_settings",
    "place_page_set_id", "destination_set_id", "destination_spec",
    "omnichannel_link_spec", "contextual_multi_ads",
]

TRANSIENT_CODES = {4, 17, 80004}


def esc(text):
    return html.escape(str(text), quote=False)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; summary only in log.", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)


def _decode_error(body):
    try:
        parsed = json.loads(body)
        err = parsed.get("error", {})
        return {
            "message": err.get("message", body),
            "code": err.get("code"),
            "subcode": err.get("error_subcode"),
            "is_transient": err.get("is_transient", False),
            "raw": parsed,
        }
    except Exception:
        return {"message": body, "code": None, "subcode": None, "is_transient": False, "raw": body}


def graph_request(method, path, params=None, retries=3):
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VER}/{path.lstrip('/')}"
    delays = [5, 15, 45]

    for attempt in range(retries + 1):
        try:
            if method == "GET":
                full_url = url + "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(full_url, method="GET")
            else:
                data = urllib.parse.urlencode(params).encode("utf-8")
                req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            info = _decode_error(body)
            transient = info.get("is_transient") or info.get("code") in TRANSIENT_CODES
            if transient and attempt < retries:
                delay = delays[min(attempt, len(delays) - 1)]
                print(f"Meta transient error {info.get('code')}; retry in {delay}s: {info.get('message')}", flush=True)
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Meta HTTP {e.code}: {info.get('message')} (code={info.get('code')}, subcode={info.get('subcode')})"
            )
        except Exception:
            if attempt < retries:
                delay = delays[min(attempt, len(delays) - 1)]
                time.sleep(delay)
                continue
            raise


def get_node(node_id, fields):
    return graph_request("GET", str(node_id), {"fields": ",".join(fields)})


def get_edge(node_id, edge, fields, limit=100):
    results = []
    path = f"{node_id}/{edge}"
    data = graph_request("GET", path, {"fields": ",".join(fields), "limit": limit})
    results.extend(data.get("data", []))
    next_url = data.get("paging", {}).get("next")
    while next_url:
        # paging URLs already contain token/params; use directly
        try:
            with urllib.request.urlopen(next_url, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results.extend(data.get("data", []))
            next_url = data.get("paging", {}).get("next")
        except Exception as e:
            raise RuntimeError(f"Paging error for {path}: {e}")
    return results


def get_ads_with_creatives(adset_id):
    ads = get_edge(adset_id, "ads", AD_FIELDS, limit=50)
    out = []
    for ad in ads:
        ad = dict(ad)
        creative_ref = ad.get("creative") or {}
        creative_id = creative_ref.get("id") if isinstance(creative_ref, dict) else None
        creative = None
        if creative_id:
            try:
                creative = get_node(creative_id, CREATIVE_FIELDS)
            except Exception as e:
                creative = {"id": creative_id, "_fetch_error": str(e)}
        ad["_creative_full"] = creative
        out.append(ad)
    return out


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        # preserve list semantics but normalize items
        return [normalized(v) for v in value]
    return value


def diff_dict(src, dst, ignore=None, path=""):
    ignore = set(ignore or [])
    diffs = []
    src = src or {}
    dst = dst or {}
    keys = set(src.keys()) | set(dst.keys())
    for key in sorted(keys):
        if key in ignore or key.startswith("_"):
            continue
        p = f"{path}.{key}" if path else key
        a, b = src.get(key), dst.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            diffs.extend(diff_dict(a, b, ignore=set(), path=p))
        elif normalized(a) != normalized(b):
            diffs.append({"field": p, "source": a, "copy": b})
    return diffs


def adset_diffs(src, dst):
    return diff_dict(src, dst, ignore={
        "id", "name", "status", "effective_status", "source_adset_id", "start_time"
    })


def ad_diffs(src, dst):
    # creative refs are compared through full creative payload below
    s = {k: v for k, v in src.items() if k != "_creative_full"}
    d = {k: v for k, v in dst.items() if k != "_creative_full"}
    return diff_dict(s, d, ignore={
        "id", "name", "status", "effective_status", "adset_id", "source_ad_id", "creative"
    })


def creative_diffs(src, dst):
    return diff_dict(src or {}, dst or {}, ignore={"id", "name"})


def parse_ids():
    raw = os.environ.get("DUPLICATE_TEST_ADSET_IDS", "")
    if len(sys.argv) > 1:
        raw = " ".join(sys.argv[1:])
    ids = [x for x in re.split(r"[\s,;]+", raw.strip()) if x]
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise SystemExit("No adset IDs. Pass CLI IDs or DUPLICATE_TEST_ADSET_IDS.")
    if len(ids) > MAX_TEST_ADSETS:
        raise SystemExit(f"Safety limit: max {MAX_TEST_ADSETS} adsets per test run.")
    for x in ids:
        if not x.isdigit():
            raise SystemExit(f"Invalid adset ID: {x}")
    return ids


def wait_for_ads(adset_id, expected_min=1, tries=8):
    for i in range(tries):
        ads = get_ads_with_creatives(adset_id)
        if len(ads) >= expected_min:
            return ads
        time.sleep(3 + i * 2)
    return get_ads_with_creatives(adset_id)


def map_ads(copy_response, source_ads, copied_ads):
    mappings = {}
    for row in copy_response.get("ad_object_ids", []) or []:
        if row.get("ad_object_type") == "ad":
            mappings[str(row.get("source_id"))] = str(row.get("copied_id"))

    copied_by_id = {str(a.get("id")): a for a in copied_ads}
    pairs = []
    used = set()
    for s in source_ads:
        sid = str(s.get("id"))
        cid = mappings.get(sid)
        c = copied_by_id.get(cid) if cid else None
        if c:
            used.add(str(c.get("id")))
        pairs.append((s, c))

    # fallback by position for anything not mapped
    leftovers = [a for a in copied_ads if str(a.get("id")) not in used]
    for idx, (s, c) in enumerate(pairs):
        if c is None and leftovers:
            pairs[idx] = (s, leftovers.pop(0))
    return pairs


def find_value(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj.get(key)
        for v in obj.values():
            found = find_value(v, key)
            if found is not None:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = find_value(v, key)
            if found is not None:
                return found
    return None


def test_one(source_id, test_suffix):
    print(f"\n=== SOURCE ADSET {source_id} ===", flush=True)
    source = get_node(source_id, ADSET_FIELDS)
    source_ads = get_ads_with_creatives(source_id)
    if not source_ads:
        raise RuntimeError("Source adset has no ads; deep-copy test aborted.")
    if len(source_ads) > MAX_SYNC_CHILD_ADS:
        raise RuntimeError(
            f"Source has {len(source_ads)} child ads. Safety stop: sync deep-copy test allows <= {MAX_SYNC_CHILD_ADS}."
        )

    params = {
        "campaign_id": source.get("campaign_id"),
        "deep_copy": "true",
        "status_option": "PAUSED",
        "rename_options": json.dumps({
            "rename_strategy": "DEEP_RENAME",
            "rename_suffix": test_suffix,
        }, ensure_ascii=False),
    }
    copy_response = graph_request("POST", f"{source_id}/copies", params)
    copied_id = str(copy_response.get("copied_adset_id") or "")
    if not copied_id:
        raise RuntimeError(f"Copy response has no copied_adset_id: {copy_response}")

    # read-after-write is supported, but child ads can take a few seconds to materialize
    time.sleep(2)
    copied = get_node(copied_id, ADSET_FIELDS)
    copied_ads = wait_for_ads(copied_id, expected_min=len(source_ads))

    a_diffs = adset_diffs(source, copied)
    ad_pairs = map_ads(copy_response, source_ads, copied_ads)
    ad_reports = []
    for src_ad, dst_ad in ad_pairs:
        if dst_ad is None:
            ad_reports.append({
                "source_ad_id": src_ad.get("id"),
                "copied_ad_id": None,
                "missing_copy": True,
                "ad_diffs": [],
                "creative_diffs": [],
            })
            continue
        ad_reports.append({
            "source_ad_id": src_ad.get("id"),
            "copied_ad_id": dst_ad.get("id"),
            "missing_copy": False,
            "ad_diffs": ad_diffs(src_ad, dst_ad),
            "creative_diffs": creative_diffs(src_ad.get("_creative_full"), dst_ad.get("_creative_full")),
            "source_creative_id": (src_ad.get("creative") or {}).get("id"),
            "copied_creative_id": (dst_ad.get("creative") or {}).get("id"),
        })

    source_pixel = find_value(source.get("promoted_object"), "pixel_id")
    copied_pixel = find_value(copied.get("promoted_object"), "pixel_id")
    source_product_set = find_value(source.get("promoted_object"), "product_set_id")
    copied_product_set = find_value(copied.get("promoted_object"), "product_set_id")

    result = {
        "source_adset_id": source_id,
        "copied_adset_id": copied_id,
        "source_name": source.get("name"),
        "copied_name": copied.get("name"),
        "account_id": source.get("account_id"),
        "campaign_id": source.get("campaign_id"),
        "source_status": source.get("status"),
        "copied_status": copied.get("status"),
        "source_ads_count": len(source_ads),
        "copied_ads_count": len(copied_ads),
        "source_pixel_id": source_pixel,
        "copied_pixel_id": copied_pixel,
        "pixel_match": source_pixel == copied_pixel,
        "source_product_set_id": source_product_set,
        "copied_product_set_id": copied_product_set,
        "product_set_match": source_product_set == copied_product_set,
        "adset_diffs": a_diffs,
        "ads": ad_reports,
        "copy_response": copy_response,
        "source_adset": source,
        "copied_adset": copied,
    }
    result["critical_ok"] = (
        copied.get("status") == "PAUSED"
        and len(source_ads) == len(copied_ads)
        and result["pixel_match"]
        and result["product_set_match"]
        and not a_diffs
        and all(not x.get("missing_copy") and not x.get("ad_diffs") and not x.get("creative_diffs") for x in ad_reports)
    )
    return result


def compact_summary(result):
    diff_count = len(result.get("adset_diffs", []))
    creative_diff_count = sum(len(x.get("creative_diffs", [])) for x in result.get("ads", []))
    ad_diff_count = sum(len(x.get("ad_diffs", [])) for x in result.get("ads", []))
    icon = "✅" if result.get("critical_ok") else "⚠️"
    return (
        f"{icon} <b>Duplicate test</b>\n"
        f"Account: <code>{esc(result.get('account_id'))}</code>\n"
        f"Source Adset: <code>{esc(result.get('source_adset_id'))}</code>\n"
        f"Copy Adset: <code>{esc(result.get('copied_adset_id'))}</code> (PAUSED)\n"
        f"Ads: {result.get('source_ads_count')} → {result.get('copied_ads_count')}\n"
        f"Pixel: <code>{esc(result.get('source_pixel_id'))}</code> → <code>{esc(result.get('copied_pixel_id'))}</code> {'✅' if result.get('pixel_match') else '❌'}\n"
        f"Product set: <code>{esc(result.get('source_product_set_id'))}</code> → <code>{esc(result.get('copied_product_set_id'))}</code> {'✅' if result.get('product_set_match') else '❌'}\n"
        f"Diffs: adset={diff_count}, ad={ad_diff_count}, creative={creative_diff_count}"
    )


def main():
    if not ACCESS_TOKEN:
        raise SystemExit("FB_ACCESS_TOKEN is missing")
    ids = parse_ids()
    now = datetime.now(POLAND_TZ)
    suffix = f" [PYTEST {now.strftime('%Y%m%d-%H%M')}]"
    report = {
        "created_at": now.isoformat(),
        "api_version": API_VER,
        "mode": "REAL_COPY_PAUSED",
        "source_ids": ids,
        "results": [],
        "errors": [],
    }

    print("FB duplicate test: REAL copies will be created, but always PAUSED.", flush=True)
    print(f"IDs: {ids}", flush=True)
    for source_id in ids:
        try:
            result = test_one(source_id, suffix)
            report["results"].append(result)
            print(json.dumps({
                "source": source_id,
                "copy": result.get("copied_adset_id"),
                "critical_ok": result.get("critical_ok"),
                "pixel_match": result.get("pixel_match"),
                "product_set_match": result.get("product_set_match"),
                "adset_diffs": len(result.get("adset_diffs", [])),
            }, ensure_ascii=False, indent=2), flush=True)
            send_telegram(compact_summary(result))
        except Exception as e:
            msg = f"Source adset {source_id}: {e}"
            report["errors"].append(msg)
            print(f"ERROR: {msg}", flush=True)
            send_telegram(f"❌ <b>Duplicate test error</b>\nAdset: <code>{esc(source_id)}</code>\n{esc(e)}")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nReport saved: {REPORT_FILE}", flush=True)
    print("All created test copies are PAUSED. Inspect them in Ads Manager before deleting.", flush=True)
    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
