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
REPORT_FILE = "duplicate_test_ordinary_report.json"

# Core fields we actually need to verify settings. If Meta rejects any field,
# the resilient reader splits the request and records the exact unavailable field
# instead of aborting the whole test.
ADSET_FIELDS = [
    "id", "name", "account_id", "campaign_id", "status", "effective_status",
    "bid_strategy", "bid_amount", "bid_constraints", "billing_event",
    "optimization_goal", "daily_budget", "lifetime_budget", "start_time", "end_time",
    "attribution_spec", "promoted_object", "destination_type", "pacing_type",
    "targeting", "is_dynamic_creative", "recurring_budget_semantics",
]

AD_FIELDS = [
    "id", "name", "status", "effective_status", "adset_id", "campaign_id",
    "creative", "tracking_specs", "conversion_specs", "conversion_domain",
    "source_ad_id",
]

# Fields most relevant to checking whether Meta silently changes creative automation/
# Advantage+ enhancements. These are optional probes: lack of permission for one field
# does not abort the duplicate test.
CREATIVE_FIELDS = [
    "id", "name", "object_story_id", "object_story_spec", "asset_feed_spec",
    "degrees_of_freedom_spec", "creative_sourcing_spec", "format_transformation_spec",
    "generative_asset_spec", "product_set_id", "url_tags", "instagram_user_id",
    "source_facebook_post_id", "source_instagram_media_id", "template_url",
    "template_url_spec", "platform_customizations", "dynamic_ad_voice",
    "product_suggestion_settings", "recommender_settings", "place_page_set_id",
    "destination_set_id", "destination_spec", "omnichannel_link_spec",
    "contextual_multi_ads",
]

TRANSIENT_CODES = {4, 17, 80004}
PARTIAL_RESULTS = {}


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


def decode_meta_error(body):
    try:
        parsed = json.loads(body)
        err = parsed.get("error", {})
        return {
            "message": err.get("message", body),
            "code": err.get("code"),
            "subcode": err.get("error_subcode"),
            "is_transient": err.get("is_transient", False),
            "user_title": err.get("error_user_title"),
            "user_msg": err.get("error_user_msg"),
            "fbtrace_id": err.get("fbtrace_id"),
            "raw": parsed,
        }
    except Exception:
        return {
            "message": body, "code": None, "subcode": None,
            "is_transient": False, "user_title": None, "user_msg": None,
            "fbtrace_id": None, "raw": body,
        }


class SkipSource(RuntimeError):
    """Intentional test skip, not a failure."""
    pass


class MetaRequestError(RuntimeError):
    def __init__(self, http_status, info, stage=None):
        self.http_status = http_status
        self.info = info
        self.stage = stage
        text = f"Meta HTTP {http_status}: {info.get('message')} (code={info.get('code')}, subcode={info.get('subcode')})"
        if info.get("user_title"):
            text += f" | {info.get('user_title')}"
        if info.get("user_msg"):
            text += f" | {info.get('user_msg')}"
        super().__init__(text)


def graph_request(method, path, params=None, retries=3, stage=None):
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
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            info = decode_meta_error(body)
            transient = info.get("is_transient") or info.get("code") in TRANSIENT_CODES
            if transient and attempt < retries:
                delay = delays[min(attempt, len(delays) - 1)]
                print(f"[{stage or 'request'}] Meta transient error {info.get('code')}; retry in {delay}s", flush=True)
                time.sleep(delay)
                continue
            raise MetaRequestError(e.code, info, stage=stage)


RELEVANT_PERMISSION_NAMES = {
    "ads_management", "ads_read", "business_management",
    "pages_show_list", "pages_read_engagement", "pages_manage_ads",
    "instagram_basic", "instagram_manage_insights",
}


def get_api_access_diagnostics():
    """Read token identity + granted/declined OAuth permissions. No extra secret required."""
    diag = {
        "identity": None,
        "permissions": [],
        "granted": [],
        "declined_or_other": [],
        "relevant": {},
        "errors": [],
    }
    try:
        diag["identity"] = graph_request(
            "GET", "me", {"fields": "id,name"}, stage="access_diag:me"
        )
    except Exception as e:
        diag["errors"].append({"stage": "me", "error": str(e)})

    try:
        rows = graph_request(
            "GET", "me/permissions", {}, stage="access_diag:permissions"
        ).get("data", [])
        diag["permissions"] = rows
        for row in rows:
            name = row.get("permission")
            status = row.get("status")
            if status == "granted":
                diag["granted"].append(name)
            else:
                diag["declined_or_other"].append({"permission": name, "status": status})
        diag["granted"] = sorted(x for x in diag["granted"] if x)
        diag["relevant"] = {
            name: ("granted" if name in diag["granted"] else "not_granted_or_not_returned")
            for name in sorted(RELEVANT_PERMISSION_NAMES)
        }
    except Exception as e:
        diag["errors"].append({"stage": "permissions", "error": str(e)})
    return diag


def access_diag_summary(diag):
    identity = diag.get("identity") or {}
    relevant = diag.get("relevant") or {}
    granted_relevant = [k for k, v in relevant.items() if v == "granted"]
    missing_relevant = [k for k, v in relevant.items() if v != "granted"]
    lines = [
        "🔐 <b>Meta API access diagnostics</b>",
        f"API identity: {esc(identity.get('name') or 'unknown')} • <code>{esc(identity.get('id') or 'unknown')}</code>",
        f"Relevant granted: {esc(', '.join(granted_relevant) or 'none detected')}",
        f"Relevant not granted/not returned: {esc(', '.join(missing_relevant) or 'none')}",
    ]
    if diag.get("errors"):
        lines.append(f"Diagnostic errors: {len(diag['errors'])}")
    lines.append("ℹ️ OAuth scopes ≠ asset-level access. Page/Instagram access is probed separately per source adset.")
    return "\n".join(lines)


def extract_identity_ids_from_ads(ads):
    page_ids = set()
    instagram_ids = set()
    creative_ids = []
    for ad in ads or []:
        creative_ref = ad.get("creative") or {}
        cid = creative_ref.get("id") if isinstance(creative_ref, dict) else None
        if not cid:
            continue
        creative_ids.append(str(cid))
        creative, _ = get_node_fields_resilient(
            cid, ["id", "object_story_id", "object_story_spec", "instagram_user_id"],
            stage_prefix=f"access_diag:creative:{cid}"
        )
        spec = creative.get("object_story_spec") or {}
        if isinstance(spec, dict) and spec.get("page_id"):
            page_ids.add(str(spec.get("page_id")))
        story_id = creative.get("object_story_id")
        if story_id and "_" in str(story_id):
            # object_story_id normally starts with the Page ID. Use as fallback only.
            page_ids.add(str(story_id).split("_", 1)[0])
        if creative.get("instagram_user_id"):
            instagram_ids.add(str(creative.get("instagram_user_id")))
    return sorted(page_ids), sorted(instagram_ids), creative_ids


def probe_asset(node_id, kind):
    try:
        data = graph_request(
            "GET", str(node_id), {"fields": "id,name"}, stage=f"access_diag:{kind}:{node_id}"
        )
        return {"id": str(node_id), "kind": kind, "accessible": True, "data": data}
    except MetaRequestError as e:
        return {
            "id": str(node_id), "kind": kind, "accessible": False,
            "error": {
                "message": e.info.get("message"), "code": e.info.get("code"),
                "subcode": e.info.get("subcode"), "user_title": e.info.get("user_title"),
                "user_msg": e.info.get("user_msg"),
            },
        }


def get_source_asset_access(source_ads_basic):
    page_ids, instagram_ids, creative_ids = extract_identity_ids_from_ads(source_ads_basic)
    pages = [probe_asset(x, "page") for x in page_ids]
    instagram = [probe_asset(x, "instagram") for x in instagram_ids]
    return {
        "creative_ids": creative_ids,
        "page_ids": page_ids,
        "instagram_ids": instagram_ids,
        "pages": pages,
        "instagram": instagram,
    }


def get_node_fields_resilient(node_id, fields, stage_prefix):
    """Fetch as many requested fields as possible without one protected field killing the test."""
    data = {"id": str(node_id)}
    unavailable = []

    def fetch_group(group):
        if not group:
            return
        try:
            result = graph_request(
                "GET", str(node_id), {"fields": ",".join(group)},
                stage=f"{stage_prefix}:{','.join(group)}"
            )
            data.update(result)
        except MetaRequestError as e:
            if len(group) == 1:
                unavailable.append({"field": group[0], "error": e.info})
                return
            mid = len(group) // 2
            fetch_group(group[:mid])
            fetch_group(group[mid:])

    # Fetch in moderate chunks first; split only on failure.
    chunk_size = 8
    for i in range(0, len(fields), chunk_size):
        fetch_group(fields[i:i + chunk_size])
    return data, unavailable


def get_edge_fields_resilient(node_id, edge, fields, limit=50, stage_prefix="edge"):
    """Same idea as resilient node fetch, but returns rows merged by id."""
    rows_by_id = {}
    unavailable = []

    def fetch_group(group):
        if not group:
            return
        try:
            result = graph_request(
                "GET", f"{node_id}/{edge}",
                {"fields": ",".join(group), "limit": limit},
                stage=f"{stage_prefix}:{','.join(group)}"
            )
            for row in result.get("data", []):
                rid = str(row.get("id"))
                rows_by_id.setdefault(rid, {}).update(row)
        except MetaRequestError as e:
            if len(group) == 1:
                unavailable.append({"field": group[0], "error": e.info})
                return
            mid = len(group) // 2
            fetch_group(group[:mid])
            fetch_group(group[mid:])

    # Always include id so rows can be merged.
    fields = list(dict.fromkeys(["id"] + list(fields)))
    chunk_size = 6
    for i in range(0, len(fields), chunk_size):
        fetch_group(fields[i:i + chunk_size])
    return list(rows_by_id.values()), unavailable


def get_ads_with_creatives_resilient(adset_id, stage_prefix):
    ads, ad_unavailable = get_edge_fields_resilient(
        adset_id, "ads", AD_FIELDS, limit=50, stage_prefix=f"{stage_prefix}:ads"
    )
    creative_unavailable = []
    for ad in ads:
        creative_ref = ad.get("creative") or {}
        creative_id = creative_ref.get("id") if isinstance(creative_ref, dict) else None
        if creative_id:
            creative, unavailable = get_node_fields_resilient(
                creative_id, CREATIVE_FIELDS, stage_prefix=f"{stage_prefix}:creative:{creative_id}"
            )
            ad["_creative_full"] = creative
            for item in unavailable:
                item = dict(item)
                item["creative_id"] = creative_id
                creative_unavailable.append(item)
        else:
            ad["_creative_full"] = None
    return ads, ad_unavailable, creative_unavailable


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
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
            diffs.extend(diff_dict(a, b, path=p))
        elif normalized(a) != normalized(b):
            diffs.append({"field": p, "source": a, "copy": b})
    return diffs


def find_value(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj.get(key)
        for v in obj.values():
            found = find_value(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_value(v, key)
            if found is not None:
                return found
    return None


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


def pair_ads(source_ads, copied_ads):
    """Prefer source_ad_id mapping; then same creative id; then position."""
    copy_by_source = {str(a.get("source_ad_id")): a for a in copied_ads if a.get("source_ad_id")}
    used = set()
    pairs = []
    for src in source_ads:
        src_id = str(src.get("id"))
        dst = copy_by_source.get(src_id)
        if dst:
            used.add(str(dst.get("id")))
        pairs.append([src, dst])

    leftovers = [a for a in copied_ads if str(a.get("id")) not in used]
    for pair in pairs:
        if pair[1] is None and leftovers:
            pair[1] = leftovers.pop(0)
    return [(a, b) for a, b in pairs]


def compare_adset(src, dst):
    return diff_dict(src, dst, ignore={
        "id", "name", "status", "effective_status", "start_time", "end_time"
    })


def compare_ad(src, dst):
    src_core = {k: v for k, v in (src or {}).items() if k != "_creative_full"}
    dst_core = {k: v for k, v in (dst or {}).items() if k != "_creative_full"}
    return diff_dict(src_core, dst_core, ignore={
        "id", "name", "status", "effective_status", "adset_id", "source_ad_id", "creative"
    })


def compare_creative(src, dst):
    return diff_dict(src or {}, dst or {}, ignore={"id", "name"})


def meta_error_summary(e):
    info = e.info if isinstance(e, MetaRequestError) else {}
    parts = [str(e)]
    if info.get("user_title"):
        parts.append(f"Title: {info['user_title']}")
    if info.get("user_msg"):
        parts.append(f"Detail: {info['user_msg']}")
    if info.get("fbtrace_id"):
        parts.append(f"fbtrace_id: {info['fbtrace_id']}")
    return " | ".join(parts)


def source_is_catalog(source):
    promoted = source.get("promoted_object") or {}
    return bool(find_value(promoted, "product_set_id"))


def creative_id_pairs(ad_reports):
    rows = []
    for row in ad_reports:
        rows.append({
            "source_ad_id": row.get("source_ad_id"),
            "copied_ad_id": row.get("copied_ad_id"),
            "source_creative_id": row.get("source_creative_id"),
            "copied_creative_id": row.get("copied_creative_id"),
            "creative_id_changed": bool(row.get("source_creative_id") and row.get("copied_creative_id") and str(row.get("source_creative_id")) != str(row.get("copied_creative_id"))),
        })
    return rows


def test_one(source_id, suffix):
    result = {
        "source_adset_id": source_id,
        "copy_created": False,
        "copied_adset_id": None,
        "stages": [],
        "warnings": [],
        "errors": [],
    }

    def stage(name, status="ok", detail=None):
        row = {"stage": name, "status": status}
        if detail is not None:
            row["detail"] = detail
        result["stages"].append(row)
        print(f"[{source_id}] {name}: {status}" + (f" | {detail}" if detail else ""), flush=True)

    # 1) Minimal/core source read. No creative inspection yet.
    source, source_unavailable = get_node_fields_resilient(
        source_id, ADSET_FIELDS, stage_prefix=f"source_adset:{source_id}"
    )
    if not source.get("campaign_id") or not source.get("account_id"):
        raise RuntimeError("Cannot read source account_id/campaign_id; aborting before copy.")
    if source_is_catalog(source):
        raise SkipSource("Catalog/DPA source detected by product_set_id. Ordinary-ad test intentionally skips catalogs.")
    stage("source_adset_read")
    if source_unavailable:
        result["warnings"].append({"source_adset_unavailable_fields": source_unavailable})
        stage("source_adset_optional_fields", "warning", f"{len(source_unavailable)} unavailable")

    # 2) Count/read source child ads without creative deep inspection.
    source_ads_basic, source_ad_unavailable = get_edge_fields_resilient(
        source_id, "ads", AD_FIELDS, limit=50, stage_prefix=f"source_ads:{source_id}"
    )
    if not source_ads_basic:
        raise RuntimeError("Source adset has no readable child ads; aborting before copy.")
    stage("source_ads_read", detail=f"{len(source_ads_basic)} ad(s)")
    if source_ad_unavailable:
        result["warnings"].append({"source_ads_unavailable_fields": source_ad_unavailable})

    # Asset-level diagnostics: OAuth scopes alone do not prove access to the Page/IG identity
    # used by the ad. Probe those identities before attempting the copy.
    asset_access = get_source_asset_access(source_ads_basic)
    result["asset_access"] = asset_access
    PARTIAL_RESULTS[source_id] = {
        "source_adset_id": source_id,
        "account_id": source.get("account_id"),
        "campaign_id": source.get("campaign_id"),
        "asset_access": asset_access,
    }
    inaccessible_assets = [x for x in asset_access.get("pages", []) + asset_access.get("instagram", []) if not x.get("accessible")]
    if inaccessible_assets:
        result["warnings"].append({"inaccessible_assets": inaccessible_assets})
        stage("source_identity_access", "warning", f"{len(inaccessible_assets)} inaccessible Page/IG asset(s)")
    else:
        stage("source_identity_access", detail=f"pages={len(asset_access.get('page_ids', []))}, ig={len(asset_access.get('instagram_ids', []))}")

    # 3) Native Meta deep-copy with the smallest supported parameter set.
    # Same campaign is implicit; no rename_options in the copy request. This isolates
    # the actual /copies capability from optional parameters.
    copy_response = graph_request(
        "POST", f"{source_id}/copies",
        {"deep_copy": "true", "status_option": "PAUSED"},
        stage="adset_copy_post"
    )
    copied_id = str(copy_response.get("copied_adset_id") or copy_response.get("id") or "")
    if not copied_id:
        raise RuntimeError(f"Copy endpoint returned no copied_adset_id: {copy_response}")
    result["copy_created"] = True
    result["copied_adset_id"] = copied_id
    PARTIAL_RESULTS[source_id] = {"copied_adset_id": copied_id, "copy_created": True}
    result["copy_response"] = copy_response
    stage("copy_created", detail=copied_id)

    # 4) Rename in a separate call so rename_options cannot break the copy itself.
    source_name = source.get("name") or f"source_{source_id}"
    desired_name = f"{source_name}{suffix}"
    try:
        graph_request("POST", copied_id, {"name": desired_name, "status": "PAUSED"}, stage="rename_copy")
        stage("copy_renamed")
    except Exception as e:
        result["warnings"].append({"rename_error": str(e)})
        stage("copy_renamed", "warning", str(e))

    # Give child ads a moment to materialize.
    time.sleep(4)

    # 5) Full resilient audit after copy. Optional inaccessible fields are warnings.
    copied, copied_unavailable = get_node_fields_resilient(
        copied_id, ADSET_FIELDS, stage_prefix=f"copied_adset:{copied_id}"
    )
    source_ads, src_ad_unavail, src_creative_unavail = get_ads_with_creatives_resilient(
        source_id, stage_prefix=f"source_full:{source_id}"
    )
    copied_ads, dst_ad_unavail, dst_creative_unavail = get_ads_with_creatives_resilient(
        copied_id, stage_prefix=f"copy_full:{copied_id}"
    )
    stage("post_copy_audit", detail=f"ads {len(source_ads)}→{len(copied_ads)}")

    audit_unavailable = {
        "copied_adset": copied_unavailable,
        "source_ads": src_ad_unavail,
        "source_creatives": src_creative_unavail,
        "copied_ads": dst_ad_unavail,
        "copied_creatives": dst_creative_unavail,
    }
    if any(audit_unavailable.values()):
        result["warnings"].append({"audit_unavailable_fields": audit_unavailable})

    adset_diffs = compare_adset(source, copied)
    ad_reports = []
    for src_ad, dst_ad in pair_ads(source_ads, copied_ads):
        if dst_ad is None:
            ad_reports.append({
                "source_ad_id": src_ad.get("id"), "copied_ad_id": None,
                "missing_copy": True, "ad_diffs": [], "creative_diffs": []
            })
            continue
        ad_reports.append({
            "source_ad_id": src_ad.get("id"),
            "copied_ad_id": dst_ad.get("id"),
            "missing_copy": False,
            "ad_diffs": compare_ad(src_ad, dst_ad),
            "creative_diffs": compare_creative(src_ad.get("_creative_full"), dst_ad.get("_creative_full")),
            "source_creative_id": (src_ad.get("creative") or {}).get("id"),
            "copied_creative_id": (dst_ad.get("creative") or {}).get("id"),
        })

    source_pixel = find_value(source.get("promoted_object"), "pixel_id")
    copied_pixel = find_value(copied.get("promoted_object"), "pixel_id")
    source_product_set = find_value(source.get("promoted_object"), "product_set_id")
    copied_product_set = find_value(copied.get("promoted_object"), "product_set_id")
    creative_pairs = creative_id_pairs(ad_reports)
    all_creatives_new = bool(creative_pairs) and all(x.get("creative_id_changed") for x in creative_pairs)

    result.update({
        "account_id": source.get("account_id"),
        "campaign_id": source.get("campaign_id"),
        "source_name": source.get("name"),
        "copied_name": copied.get("name"),
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
        "creative_id_pairs": creative_pairs,
        "all_creatives_new": all_creatives_new,
        "source_targeting": source.get("targeting"),
        "copied_targeting": copied.get("targeting"),
        "adset_diffs": adset_diffs,
        "ads": ad_reports,
        "source_adset": source,
        "copied_adset": copied,
    })

    result["critical_ok"] = (
        copied.get("status") == "PAUSED"
        and len(source_ads) == len(copied_ads)
        and result["pixel_match"]
        and source_product_set is None
        and copied_product_set is None
        and result["all_creatives_new"]
        and not adset_diffs
        and all(
            not x.get("missing_copy") and not x.get("ad_diffs") and not x.get("creative_diffs")
            for x in ad_reports
        )
    )
    return result


def compact_summary(result):
    adset_diff_count = len(result.get("adset_diffs", []))
    ad_diff_count = sum(len(x.get("ad_diffs", [])) for x in result.get("ads", []))
    creative_diff_count = sum(len(x.get("creative_diffs", [])) for x in result.get("ads", []))
    icon = "✅" if result.get("critical_ok") else "⚠️"
    pairs = result.get("creative_id_pairs") or []
    pair_lines = []
    for x in pairs[:3]:
        pair_lines.append(
            f"Ad <code>{esc(x.get('source_ad_id'))}</code> → <code>{esc(x.get('copied_ad_id'))}</code>; "
            f"Creative <code>{esc(x.get('source_creative_id'))}</code> → <code>{esc(x.get('copied_creative_id'))}</code> "
            f"{'✅ NEW' if x.get('creative_id_changed') else '❌ SAME'}"
        )
    placement = result.get("source_targeting") or {}
    placement_bits = []
    for key in ("publisher_platforms", "facebook_positions", "instagram_positions", "messenger_positions", "device_platforms"):
        if key in placement:
            placement_bits.append(f"{key}={placement.get(key)}")
    return (
        f"{icon} <b>Ordinary duplicate test v4</b>\n"
        f"Account: <code>{esc(result.get('account_id'))}</code>\n"
        f"Source Adset: <code>{esc(result.get('source_adset_id'))}</code>\n"
        f"Copy Adset: <code>{esc(result.get('copied_adset_id'))}</code> (PAUSED)\n"
        f"Ads: {result.get('source_ads_count')} → {result.get('copied_ads_count')}\n"
        + ("\n".join(pair_lines) + "\n" if pair_lines else "")
        + f"Pixel: <code>{esc(result.get('source_pixel_id'))}</code> → <code>{esc(result.get('copied_pixel_id'))}</code> {'✅' if result.get('pixel_match') else '❌'}\n"
        + (f"Placements: {esc('; '.join(placement_bits))}\n" if placement_bits else "")
        + f"Diffs: adset={adset_diff_count}, ad={ad_diff_count}, creative={creative_diff_count}\n"
        f"Warnings: {len(result.get('warnings', []))}"
    )


def error_summary(source_id, e, partial=None):
    partial = partial or {}
    copy_line = ""
    if partial.get("copied_adset_id"):
        copy_line = f"\n⚠️ Copy may already exist: <code>{esc(partial['copied_adset_id'])}</code> (should be PAUSED)"
    access_line = ""
    asset_access = partial.get("asset_access") or {}
    if asset_access:
        page_bits = [f"{x.get('id')}={'OK' if x.get('accessible') else 'NO'}" for x in asset_access.get("pages", [])]
        ig_bits = [f"{x.get('id')}={'OK' if x.get('accessible') else 'NO'}" for x in asset_access.get("instagram", [])]
        if page_bits or ig_bits:
            access_line = "\nAsset access: " + esc("; ".join((["Page " + ", ".join(page_bits)] if page_bits else []) + (["IG " + ", ".join(ig_bits)] if ig_bits else [])))
    stage = e.stage if isinstance(e, MetaRequestError) else None
    detail = meta_error_summary(e) if isinstance(e, MetaRequestError) else str(e)
    return (
        f"❌ <b>Ordinary duplicate test v4 error</b>\n"
        f"Source Adset: <code>{esc(source_id)}</code>\n"
        f"Stage: <b>{esc(stage or 'unknown')}</b>"
        f"{copy_line}{access_line}\n"
        f"{esc(detail)}"
    )


def main():
    if not ACCESS_TOKEN:
        raise SystemExit("FB_ACCESS_TOKEN is missing")

    ids = parse_ids()
    now = datetime.now(POLAND_TZ)
    suffix = f" [PYTEST {now.strftime('%Y%m%d-%H%M')}]"
    access_diag = get_api_access_diagnostics()
    report = {
        "created_at": now.isoformat(),
        "api_version": API_VER,
        "mode": "ORDINARY_NATIVE_DEEP_COPY_PAUSED_V4",
        "source_ids": ids,
        "api_access": access_diag,
        "results": [],
        "errors": [],
    }

    print("FB ordinary duplicate test v4: native deep-copy, always PAUSED; catalogs skipped.", flush=True)
    print(json.dumps({"api_access": access_diag}, ensure_ascii=False, indent=2), flush=True)
    send_telegram(access_diag_summary(access_diag))
    print("Important: ordinary ads only. Catalog sources are skipped. Creative IDs are audited and expected to be NEW.", flush=True)
    print(f"IDs: {ids}", flush=True)

    for source_id in ids:
        partial = {"source_adset_id": source_id, "copied_adset_id": None}
        try:
            result = test_one(source_id, suffix)
            partial.update(result)
            report["results"].append(result)
            print(json.dumps({
                "source": source_id,
                "copy": result.get("copied_adset_id"),
                "critical_ok": result.get("critical_ok"),
                "pixel_match": result.get("pixel_match"),
                "product_set_match": result.get("product_set_match"),
                "adset_diffs": len(result.get("adset_diffs", [])),
                "warnings": len(result.get("warnings", [])),
            }, ensure_ascii=False, indent=2), flush=True)
            send_telegram(compact_summary(result))
        except SkipSource as e:
            skip_row = {"source_adset_id": source_id, "skipped": True, "reason": str(e)}
            report["results"].append(skip_row)
            print(f"SKIP [{source_id}]: {e}", flush=True)
            send_telegram(
                f"⏭ <b>Ordinary duplicate test v4 — skipped</b>\n"
                f"Source Adset: <code>{esc(source_id)}</code>\n{esc(e)}"
            )
        except Exception as e:
            partial.update(PARTIAL_RESULTS.get(source_id, {}))
            err_row = {
                "source_adset_id": source_id,
                "error": str(e),
                "stage": e.stage if isinstance(e, MetaRequestError) else None,
                "meta": e.info if isinstance(e, MetaRequestError) else None,
                "copied_adset_id": partial.get("copied_adset_id"),
            }
            report["errors"].append(err_row)
            print(f"ERROR [{source_id}]: {err_row}", flush=True)
            send_telegram(error_summary(source_id, e, partial=partial))

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nReport saved: {REPORT_FILE}", flush=True)
    print("Any copy created by this test is PAUSED. Inspect in Ads Manager before deleting.", flush=True)

    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
