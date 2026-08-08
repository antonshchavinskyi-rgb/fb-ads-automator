import os
import sys
import json
import time
import html
import re
import urllib.request
import urllib.parse
import urllib.error
from copy import deepcopy
from datetime import datetime

from fb_config import API_VER, POLAND_TZ

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MAX_TEST_ADSETS = 1
REPORT_FILE = "duplicate_test_v6_compat_report.json"

# Ordinary ads only for this phase. Catalogs are intentionally skipped.
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

# Readable fields useful for source diagnosis and post-create audit.
CREATIVE_FIELDS = [
    "id", "name", "account_id", "object_story_id", "effective_object_story_id",
    "object_story_spec", "asset_feed_spec", "degrees_of_freedom_spec",
    "creative_sourcing_spec", "format_transformation_spec", "generative_asset_spec",
    "url_tags", "instagram_user_id", "platform_customizations", "contextual_multi_ads",
    "dynamic_ad_voice", "destination_set_id", "destination_spec",
    "omnichannel_link_spec", "place_page_set_id", "product_suggestion_settings",
    "recommender_settings", "template_url", "template_url_spec",
    "use_page_actor_override", "source_facebook_post_id", "source_instagram_media_id",
    "image_hash", "image_url", "video_id", "thumbnail_url", "title", "body",
]

# Fields Meta's current create-ad-creative endpoint accepts and that can be sensibly
# cloned from an existing ordinary creative. We only send fields actually present.
CREATIVE_CREATE_ALLOWLIST = {
    "object_story_spec",
    "asset_feed_spec",
    "degrees_of_freedom_spec",
    "creative_sourcing_spec",
    "format_transformation_spec",
    "generative_asset_spec",
    "url_tags",
    "instagram_user_id",
    "platform_customizations",
    "contextual_multi_ads",
    "dynamic_ad_voice",
    "destination_set_id",
    "destination_spec",
    "omnichannel_link_spec",
    "place_page_set_id",
    "product_suggestion_settings",
    "recommender_settings",
    "template_url",
    "template_url_spec",
    "use_page_actor_override",
}

# Legacy field explicitly rejected by Meta with subcode 3858504.
LEGACY_KEYS_TO_REMOVE = {"standard_enhancements"}

RELEVANT_PERMISSION_NAMES = {
    "ads_management", "ads_read", "business_management",
    "pages_show_list", "pages_read_engagement", "pages_manage_ads",
    "instagram_basic", "instagram_manage_insights",
}

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


def serialize_param(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def graph_request(method, path, params=None, retries=0, stage=None):
    """Conservative Graph API helper.

    - POST is never retried automatically: create/copy may have succeeded even if
      the response was lost, so automatic retry could create duplicates.
    - Rate-limit errors stop immediately.
    - GET may retry only when retries>0 and Meta marks the error transient.
    """
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VER}/{path.lstrip('/')}"

    max_attempts = 1 if method != "GET" else (retries + 1)
    for attempt in range(max_attempts):
        try:
            encoded = {k: serialize_param(v) for k, v in params.items() if v is not None}
            if method == "GET":
                full_url = url + "?" + urllib.parse.urlencode(encoded)
                req = urllib.request.Request(full_url, method="GET")
            else:
                data = urllib.parse.urlencode(encoded).encode("utf-8")
                req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            info = decode_meta_error(body)
            code = info.get("code")
            if code in {4, 17, 80004}:
                raise MetaRequestError(e.code, info, stage=stage)
            transient = info.get("is_transient") or code in {1, 2}
            if method == "GET" and transient and attempt < max_attempts - 1:
                print(f"[{stage or 'request'}] Meta transient error {code}; retry in 30s", flush=True)
                time.sleep(30)
                continue
            raise MetaRequestError(e.code, info, stage=stage)


def get_api_access_diagnostics():
    diag = {
        "identity": None,
        "permissions": [],
        "granted": [],
        "declined_or_other": [],
        "relevant": {},
        "errors": [],
    }
    try:
        diag["identity"] = graph_request("GET", "me", {"fields": "id,name"}, stage="access_diag:me")
    except Exception as e:
        diag["errors"].append({"stage": "me", "error": str(e)})
    try:
        rows = graph_request("GET", "me/permissions", {}, stage="access_diag:permissions").get("data", [])
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
    return "\n".join(lines)


def get_node_fields_resilient(node_id, fields, stage_prefix):
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

    chunk_size = 8
    for i in range(0, len(fields), chunk_size):
        fetch_group(fields[i:i + chunk_size])
    return data, unavailable


def get_edge_fields_resilient(node_id, edge, fields, limit=20, stage_prefix="edge"):
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

    fields = list(dict.fromkeys(["id"] + list(fields)))
    chunk_size = 6
    for i in range(0, len(fields), chunk_size):
        fetch_group(fields[i:i + chunk_size])
    return list(rows_by_id.values()), unavailable


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        # Preserve list order; Meta list semantics may be positional.
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
        raise SystemExit("No adset ID. Pass CLI ID or DUPLICATE_TEST_ADSET_IDS.")
    if len(ids) > MAX_TEST_ADSETS:
        raise SystemExit(f"Safety limit: max {MAX_TEST_ADSETS} adset per run.")
    if not ids[0].isdigit():
        raise SystemExit(f"Invalid adset ID: {ids[0]}")
    return ids


def source_is_catalog(source):
    promoted = source.get("promoted_object") or {}
    return bool(find_value(promoted, "product_set_id"))


def remove_legacy_keys(obj, path=""):
    """Remove only explicitly known legacy keys; return sanitized object + removed paths."""
    removed = []
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            p = f"{path}.{key}" if path else key
            if key in LEGACY_KEYS_TO_REMOVE:
                removed.append(p)
                continue
            clean, nested_removed = remove_legacy_keys(value, p)
            removed.extend(nested_removed)
            # Drop empty maps/lists created only by removing a legacy field.
            if clean == {} or clean == [] or clean is None:
                continue
            out[key] = clean
        return out, removed
    if isinstance(obj, list):
        out = []
        for idx, value in enumerate(obj):
            clean, nested_removed = remove_legacy_keys(value, f"{path}[{idx}]")
            removed.extend(nested_removed)
            if clean == {} or clean == [] or clean is None:
                continue
            out.append(clean)
        return out, removed
    return obj, removed


def build_compat_creative_payload(source_creative, suffix):
    """Build a NEW creative using source object_story_spec and supported fields.

    We intentionally do NOT fall back to object_story_id. Meta documents that reusing an
    object_story_id can return the already-existing creative, which would defeat our goal
    of getting a new creative ID for every duplicate.
    """
    if not source_creative.get("object_story_spec"):
        raise SkipSource(
            "Compatibility clone needs object_story_spec to guarantee a NEW creative ID. "
            "Source only exposes an existing story/post or insufficient creative structure."
        )

    payload = {
        "name": f"{source_creative.get('name') or 'creative'}{suffix}",
    }
    removed_paths = []
    for field in CREATIVE_CREATE_ALLOWLIST:
        if field not in source_creative or source_creative.get(field) in (None, {}, []):
            continue
        clean, removed = remove_legacy_keys(deepcopy(source_creative.get(field)), field)
        removed_paths.extend(removed)
        if clean not in (None, {}, []):
            payload[field] = clean

    if not payload.get("object_story_spec"):
        raise SkipSource("object_story_spec became empty during compatibility sanitization.")

    return payload, sorted(set(removed_paths))


def build_new_ad_payload(source_ad, copied_adset_id, new_creative_id, suffix):
    payload = {
        "name": f"{source_ad.get('name') or 'ad'}{suffix}",
        "adset_id": str(copied_adset_id),
        "creative": {"creative_id": str(new_creative_id)},
        "status": "PAUSED",
    }
    for field in ("tracking_specs", "conversion_specs", "conversion_domain"):
        value = source_ad.get(field)
        if value not in (None, {}, [], ""):
            payload[field] = deepcopy(value)
    return payload


def compare_adset(src, dst):
    return diff_dict(src, dst, ignore={
        "id", "name", "status", "effective_status", "start_time", "end_time"
    })


def compare_ad(src, dst):
    return diff_dict(src or {}, dst or {}, ignore={
        "id", "name", "status", "effective_status", "adset_id", "campaign_id",
        "source_ad_id", "creative"
    })


def compare_expected_creative_payload(expected_payload, created_creative):
    expected = {k: v for k, v in expected_payload.items() if k != "name"}
    actual = {k: created_creative.get(k) for k in expected.keys()}
    return diff_dict(expected, actual)


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


def audit_result(source, source_ad, source_creative, copied_adset_id, copied_ad_id, copied_creative_id,
                 expected_creative_payload, mode, removed_legacy_paths):
    time.sleep(4)
    copied, copied_unavail = get_node_fields_resilient(
        copied_adset_id, ADSET_FIELDS, stage_prefix=f"audit:copied_adset:{copied_adset_id}"
    )
    copied_ad, ad_unavail = get_node_fields_resilient(
        copied_ad_id, AD_FIELDS, stage_prefix=f"audit:copied_ad:{copied_ad_id}"
    )
    copied_creative, creative_unavail = get_node_fields_resilient(
        copied_creative_id, CREATIVE_FIELDS, stage_prefix=f"audit:copied_creative:{copied_creative_id}"
    )

    adset_diffs = compare_adset(source, copied)
    ad_diffs = compare_ad(source_ad, copied_ad)
    creative_payload_diffs = compare_expected_creative_payload(expected_creative_payload, copied_creative)

    source_pixel = find_value(source.get("promoted_object"), "pixel_id")
    copied_pixel = find_value(copied.get("promoted_object"), "pixel_id")

    result = {
        "mode": mode,
        "account_id": source.get("account_id"),
        "campaign_id": source.get("campaign_id"),
        "source_adset_id": source.get("id"),
        "copied_adset_id": copied_adset_id,
        "source_ad_id": source_ad.get("id"),
        "copied_ad_id": copied_ad_id,
        "source_creative_id": (source_ad.get("creative") or {}).get("id"),
        "copied_creative_id": copied_creative_id,
        "creative_id_changed": str((source_ad.get("creative") or {}).get("id")) != str(copied_creative_id),
        "source_effective_story_id": source_creative.get("effective_object_story_id"),
        "copied_effective_story_id": copied_creative.get("effective_object_story_id"),
        "pixel_source": source_pixel,
        "pixel_copy": copied_pixel,
        "pixel_match": source_pixel == copied_pixel,
        "adset_diffs": adset_diffs,
        "ad_diffs": ad_diffs,
        "creative_payload_diffs": creative_payload_diffs,
        "removed_legacy_paths": removed_legacy_paths,
        "unavailable_fields": {
            "copied_adset": copied_unavail,
            "copied_ad": ad_unavail,
            "copied_creative": creative_unavail,
        },
        "source_adset": source,
        "copied_adset": copied,
        "source_ad": source_ad,
        "copied_ad": copied_ad,
        "source_creative": source_creative,
        "copied_creative": copied_creative,
        "expected_creative_payload": expected_creative_payload,
    }
    result["critical_ok"] = (
        copied.get("status") == "PAUSED"
        and copied_ad.get("status") == "PAUSED"
        and result["creative_id_changed"]
        and result["pixel_match"]
        and not adset_diffs
        and not ad_diffs
        and not creative_payload_diffs
    )
    return result


def native_deep_copy(source, source_ad, source_creative, suffix):
    source_id = str(source.get("id"))
    response = graph_request(
        "POST", f"{source_id}/copies",
        {"deep_copy": True, "status_option": "PAUSED"},
        stage="native_deep_copy_post"
    )
    copied_adset_id = str(response.get("copied_adset_id") or response.get("id") or "")
    if not copied_adset_id:
        raise RuntimeError(f"Native copy returned no copied_adset_id: {response}")
    PARTIAL_RESULTS[source_id] = {"copied_adset_id": copied_adset_id, "mode": "native"}

    graph_request(
        "POST", copied_adset_id,
        {"name": f"{source.get('name') or source_id}{suffix}", "status": "PAUSED"},
        stage="native_rename_adset"
    )
    time.sleep(4)
    copied_ads, _ = get_edge_fields_resilient(
        copied_adset_id, "ads", AD_FIELDS, limit=5, stage_prefix="native:copied_ads"
    )
    if len(copied_ads) != 1:
        raise RuntimeError(f"Expected exactly 1 copied ad, got {len(copied_ads)}")
    copied_ad = copied_ads[0]
    copied_creative_id = str((copied_ad.get("creative") or {}).get("id") or "")
    if not copied_creative_id:
        raise RuntimeError("Native copied ad has no creative id")

    # Native deep-copy expected payload is the source readable payload after removing
    # read-only/non-create fields; this is only for targeted comparison.
    expected_payload = {"name": f"{source_creative.get('name') or 'creative'}{suffix}"}
    for field in CREATIVE_CREATE_ALLOWLIST:
        if source_creative.get(field) not in (None, {}, []):
            expected_payload[field] = deepcopy(source_creative.get(field))

    return audit_result(
        source, source_ad, source_creative,
        copied_adset_id, str(copied_ad.get("id")), copied_creative_id,
        expected_payload, "NATIVE_DEEP_COPY", []
    )


def compatibility_clone(source, source_ad, source_creative, suffix):
    source_id = str(source.get("id"))
    account_id = str(source.get("account_id"))

    # Step 1: copy ONLY the adset; no child ad/creative recreation by Meta.
    response = graph_request(
        "POST", f"{source_id}/copies",
        {"deep_copy": False, "status_option": "PAUSED"},
        stage="compat_adset_only_copy_post"
    )
    copied_adset_id = str(response.get("copied_adset_id") or response.get("id") or "")
    if not copied_adset_id:
        raise RuntimeError(f"Adset-only copy returned no copied_adset_id: {response}")
    PARTIAL_RESULTS[source_id] = {"copied_adset_id": copied_adset_id, "mode": "compat"}
    graph_request(
        "POST", copied_adset_id,
        {"name": f"{source.get('name') or source_id}{suffix} [COMPAT]", "status": "PAUSED"},
        stage="compat_rename_adset"
    )

    # Step 2: create a NEW creative from supported source structure, removing only
    # the explicitly rejected legacy standard_enhancements key.
    creative_payload, removed_paths = build_compat_creative_payload(source_creative, suffix)
    creative_response = graph_request(
        "POST", f"act_{account_id}/adcreatives", creative_payload,
        stage="compat_create_creative"
    )
    new_creative_id = str(creative_response.get("id") or "")
    if not new_creative_id:
        raise RuntimeError(f"Creative create returned no id: {creative_response}")
    PARTIAL_RESULTS[source_id].update({"copied_creative_id": new_creative_id})

    source_creative_id = str((source_ad.get("creative") or {}).get("id") or "")
    if new_creative_id == source_creative_id:
        raise RuntimeError(
            "Compatibility creative returned SAME creative_id as source. "
            "Test stops because duplicate strategy requires a new creative entity."
        )

    # Step 3: create a NEW ad inside the copied adset using the NEW creative.
    ad_payload = build_new_ad_payload(source_ad, copied_adset_id, new_creative_id, suffix)
    ad_response = graph_request(
        "POST", f"act_{account_id}/ads", ad_payload,
        stage="compat_create_ad"
    )
    new_ad_id = str(ad_response.get("id") or "")
    if not new_ad_id:
        raise RuntimeError(f"Ad create returned no id: {ad_response}")
    PARTIAL_RESULTS[source_id].update({"copied_ad_id": new_ad_id})

    return audit_result(
        source, source_ad, source_creative,
        copied_adset_id, new_ad_id, new_creative_id,
        creative_payload, "COMPAT_NEW_CREATIVE", removed_paths
    )


def compact_summary(result):
    icon = "✅" if result.get("critical_ok") else "⚠️"
    story_note = ""
    if result.get("source_effective_story_id") or result.get("copied_effective_story_id"):
        story_note = (
            f"\nStory: <code>{esc(result.get('source_effective_story_id'))}</code> → "
            f"<code>{esc(result.get('copied_effective_story_id'))}</code>"
        )
    removed = result.get("removed_legacy_paths") or []
    removed_note = f"\nRemoved legacy: {esc(', '.join(removed))}" if removed else ""
    return (
        f"{icon} <b>Duplicate test v6 • {esc(result.get('mode'))}</b>\n"
        f"Account: <code>{esc(result.get('account_id'))}</code>\n"
        f"Source Adset: <code>{esc(result.get('source_adset_id'))}</code>\n"
        f"Copy Adset: <code>{esc(result.get('copied_adset_id'))}</code> (PAUSED)\n"
        f"Ad: <code>{esc(result.get('source_ad_id'))}</code> → <code>{esc(result.get('copied_ad_id'))}</code>\n"
        f"Creative: <code>{esc(result.get('source_creative_id'))}</code> → "
        f"<code>{esc(result.get('copied_creative_id'))}</code> "
        f"{'✅ NEW' if result.get('creative_id_changed') else '❌ SAME'}\n"
        f"Pixel: <code>{esc(result.get('pixel_source'))}</code> → <code>{esc(result.get('pixel_copy'))}</code> "
        f"{'✅' if result.get('pixel_match') else '❌'}"
        f"{story_note}{removed_note}\n"
        f"Diffs: adset={len(result.get('adset_diffs', []))}, "
        f"ad={len(result.get('ad_diffs', []))}, "
        f"creative_payload={len(result.get('creative_payload_diffs', []))}"
    )


def error_summary(source_id, e, partial=None):
    partial = partial or {}
    copy_lines = []
    for label, key in (
        ("Copy Adset", "copied_adset_id"),
        ("Copy Ad", "copied_ad_id"),
        ("Copy Creative", "copied_creative_id"),
    ):
        if partial.get(key):
            copy_lines.append(f"{label}: <code>{esc(partial[key])}</code> (PAUSED where applicable)")
    stage = e.stage if isinstance(e, MetaRequestError) else None
    detail = meta_error_summary(e) if isinstance(e, MetaRequestError) else str(e)
    return (
        f"❌ <b>Duplicate test v6 error</b>\n"
        f"Source Adset: <code>{esc(source_id)}</code>\n"
        f"Stage: <b>{esc(stage or 'unknown')}</b>\n"
        + ("\n".join(copy_lines) + "\n" if copy_lines else "")
        + f"{esc(detail)}"
    )


def test_one(source_id, suffix):
    source, source_unavailable = get_node_fields_resilient(
        source_id, ADSET_FIELDS, stage_prefix=f"source_adset:{source_id}"
    )
    if not source.get("campaign_id") or not source.get("account_id"):
        raise RuntimeError("Cannot read source account_id/campaign_id")
    if source_is_catalog(source):
        raise SkipSource("Catalog/DPA source detected. Catalog tests are intentionally postponed.")

    source_ads, ad_unavailable = get_edge_fields_resilient(
        source_id, "ads", AD_FIELDS, limit=5, stage_prefix=f"source_ads:{source_id}"
    )
    if len(source_ads) != 1:
        raise SkipSource(f"This test expects exactly 1 source ad; found {len(source_ads)}")
    source_ad = source_ads[0]
    source_creative_id = str((source_ad.get("creative") or {}).get("id") or "")
    if not source_creative_id:
        raise RuntimeError("Source ad has no creative id")
    source_creative, creative_unavailable = get_node_fields_resilient(
        source_creative_id, CREATIVE_FIELDS, stage_prefix=f"source_creative:{source_creative_id}"
    )

    base_report = {
        "source_unavailable": source_unavailable,
        "source_ad_unavailable": ad_unavailable,
        "source_creative_unavailable": creative_unavailable,
        "source_adset": source,
        "source_ad": source_ad,
        "source_creative": source_creative,
    }

    # First choice: Meta native deep-copy. If it works, that is the cleanest path.
    try:
        result = native_deep_copy(source, source_ad, source_creative, suffix)
        result.update(base_report)
        result["native_error"] = None
        return result
    except MetaRequestError as e:
        if e.info.get("subcode") != 3858504:
            raise
        native_error = {
            "stage": e.stage,
            "message": str(e),
            "meta": e.info,
        }
        print(
            f"[{source_id}] native deep-copy rejected by legacy standard_enhancements; switching to compatibility clone",
            flush=True,
        )

    # Fallback ONLY for the known legacy creative problem 3858504.
    result = compatibility_clone(source, source_ad, source_creative, suffix)
    result.update(base_report)
    result["native_error"] = native_error
    return result


def main():
    if not ACCESS_TOKEN:
        raise SystemExit("FB_SCALER_ACCESS_TOKEN is missing")

    ids = parse_ids()
    source_id = ids[0]
    now = datetime.now(POLAND_TZ)
    suffix = f" [PYTEST6 {now.strftime('%Y%m%d-%H%M')}]"
    access_diag = get_api_access_diagnostics()

    report = {
        "created_at": now.isoformat(),
        "api_version": API_VER,
        "mode": "ORDINARY_NATIVE_THEN_COMPAT_V6",
        "source_ids": ids,
        "api_access": access_diag,
        "results": [],
        "errors": [],
    }

    print("FB duplicate test v6: ordinary ad only; native deep-copy first, 3858504 => compatibility clone. All new entities PAUSED.", flush=True)
    print(json.dumps({"api_access": access_diag}, ensure_ascii=False, indent=2), flush=True)
    send_telegram(access_diag_summary(access_diag))

    partial = {"source_adset_id": source_id}
    try:
        result = test_one(source_id, suffix)
        partial.update(result)
        report["results"].append(result)
        print(json.dumps({
            "source": source_id,
            "mode": result.get("mode"),
            "copy_adset": result.get("copied_adset_id"),
            "copy_ad": result.get("copied_ad_id"),
            "copy_creative": result.get("copied_creative_id"),
            "creative_id_changed": result.get("creative_id_changed"),
            "critical_ok": result.get("critical_ok"),
            "removed_legacy_paths": result.get("removed_legacy_paths"),
            "adset_diffs": len(result.get("adset_diffs", [])),
            "ad_diffs": len(result.get("ad_diffs", [])),
            "creative_payload_diffs": len(result.get("creative_payload_diffs", [])),
        }, ensure_ascii=False, indent=2), flush=True)
        send_telegram(compact_summary(result))
    except SkipSource as e:
        row = {"source_adset_id": source_id, "skipped": True, "reason": str(e)}
        report["results"].append(row)
        print(f"SKIP [{source_id}]: {e}", flush=True)
        send_telegram(
            f"⏭ <b>Duplicate test v6 — skipped</b>\n"
            f"Source Adset: <code>{esc(source_id)}</code>\n{esc(e)}"
        )
    except Exception as e:
        partial.update(PARTIAL_RESULTS.get(source_id, {}))
        row = {
            "source_adset_id": source_id,
            "error": str(e),
            "stage": e.stage if isinstance(e, MetaRequestError) else None,
            "meta": e.info if isinstance(e, MetaRequestError) else None,
            "partial": partial,
        }
        report["errors"].append(row)
        print(f"ERROR [{source_id}]: {json.dumps(row, ensure_ascii=False)}", flush=True)
        send_telegram(error_summary(source_id, e, partial=partial))

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Report saved: {REPORT_FILE}", flush=True)
    print("Any entity created by this test is PAUSED. Inspect it in Ads Manager before deleting.", flush=True)

    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
