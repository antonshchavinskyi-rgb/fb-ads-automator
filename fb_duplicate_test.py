import os
import sys
import json
import time
import html
import random
import mimetypes
import re
import urllib.request
import urllib.parse
import urllib.error
from copy import deepcopy
from datetime import datetime

from fb_config import POLAND_TZ

# Isolated duplicate-test uses the current Marketing API without changing production scripts.
API_VER = "v26.0"

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
REPORT_FILE = "duplicate_test_v16_source_video_image_url_report.json"

# This test intentionally handles ONE ordinary adset per run and creates PAUSED objects only.
# Catalog adsets are skipped in this phase.

ADSET_FIELDS = [
    "id", "name", "account_id", "campaign_id", "status", "effective_status",
    "bid_strategy", "bid_amount", "bid_constraints", "billing_event",
    "optimization_goal", "daily_budget", "lifetime_budget", "start_time", "end_time",
    "attribution_spec", "promoted_object", "destination_type", "pacing_type",
    "targeting", "is_dynamic_creative", "recurring_budget_semantics",
]

AD_FIELDS = [
    "id", "name", "status", "configured_status", "effective_status", "adset_id", "campaign_id",
    "creative", "tracking_specs", "conversion_specs", "conversion_domain", "source_ad_id",
    "issues_info", "failed_delivery_checks", "ad_review_feedback", "updated_time",
]

CREATIVE_FIELDS = [
    "id", "name", "account_id", "status", "object_story_id", "effective_object_story_id",
    "object_story_spec", "url_tags", "image_hash", "image_url", "thumbnail_url", "video_id",
    "contextual_multi_ads", "asset_feed_spec", "degrees_of_freedom_spec", "creative_sourcing_spec",
    "format_transformation_spec", "generative_asset_spec", "platform_customizations", "destination_spec",
]

RELEVANT_PERMISSION_NAMES = {
    "ads_management", "ads_read", "business_management",
    "pages_show_list", "pages_read_engagement", "pages_manage_ads",
}

PARTIAL = {}


def esc(text):
    return html.escape(str(text), quote=False)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; message skipped.", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15)
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
        return {"message": body, "code": None, "subcode": None, "raw": body}


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


class SkipSource(RuntimeError):
    pass


def serialize_param(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def graph_request(method, path, params=None, stage=None, get_retry=False):
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VER}/{path.lstrip('/')}"
    attempts = 2 if (method == "GET" and get_retry) else 1

    for attempt in range(attempts):
        try:
            encoded = {k: serialize_param(v) for k, v in params.items() if v is not None}
            if method == "GET":
                req = urllib.request.Request(url + "?" + urllib.parse.urlencode(encoded), method="GET")
            else:
                req = urllib.request.Request(
                    url,
                    data=urllib.parse.urlencode(encoded).encode("utf-8"),
                    method="POST",
                )
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            info = decode_meta_error(e.read().decode("utf-8", errors="replace"))
            # Never retry POST: if response is lost after success, retry could create a duplicate.
            if method == "GET" and attempt == 0 and (info.get("is_transient") or info.get("code") in {1, 2}):
                time.sleep(30)
                continue
            raise MetaRequestError(e.code, info, stage=stage)


def graph_get_all(path, params=None, stage=None):
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VER}/{path.lstrip('/')}?" + urllib.parse.urlencode(
        {k: serialize_param(v) for k, v in params.items() if v is not None}
    )
    rows = []
    while url:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                rows.extend(payload.get("data", []))
                url = payload.get("paging", {}).get("next")
        except urllib.error.HTTPError as e:
            info = decode_meta_error(e.read().decode("utf-8", errors="replace"))
            raise MetaRequestError(e.code, info, stage=stage)
    return rows


def get_permissions_diag():
    identity = graph_request("GET", "me", {"fields": "id,name"}, stage="diag:me")
    permissions = graph_request("GET", "me/permissions", {}, stage="diag:permissions").get("data", [])
    granted = sorted([x.get("permission") for x in permissions if x.get("status") == "granted" and x.get("permission")])
    relevant = {k: ("granted" if k in granted else "not_granted_or_not_returned") for k in sorted(RELEVANT_PERMISSION_NAMES)}
    return {"identity": identity, "permissions": permissions, "granted": granted, "relevant": relevant}


def send_access_diag(diag):
    identity = diag.get("identity", {})
    granted = [k for k, v in diag.get("relevant", {}).items() if v == "granted"]
    missing = [k for k, v in diag.get("relevant", {}).items() if v != "granted"]
    send_telegram(
        "🔐 <b>Meta API access diagnostics</b>\n"
        f"API identity: {esc(identity.get('name'))} • <code>{esc(identity.get('id'))}</code>\n"
        f"Relevant granted: {esc(', '.join(granted) or 'none')}\n"
        f"Relevant not granted/not returned: {esc(', '.join(missing) or 'none')}"
    )


def get_node(node_id, fields, stage):
    return graph_request("GET", str(node_id), {"fields": ",".join(fields)}, stage=stage, get_retry=True)


def download_bytes(url, stage):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
            content_type = resp.headers.get_content_type() or "image/jpeg"
            return content, content_type
    except Exception as e:
        raise RuntimeError(f"{stage}: could not download image: {e}")


def multipart_encode(fields, file_field, filename, content, content_type):
    boundary = "----FBTEST" + str(int(time.time() * 1000))
    chunks = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode())
    chunks.append(content)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload_image_to_ad_account(account_id, image_bytes, content_type, stage):
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    filename = f"pytest_thumb_{int(time.time())}{ext}"
    body, multipart_type = multipart_encode(
        {"access_token": ACCESS_TOKEN}, "filename", filename, image_bytes, content_type
    )
    url = f"https://graph.facebook.com/{API_VER}/act_{account_id}/adimages"
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": multipart_type})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        info = decode_meta_error(e.read().decode("utf-8", errors="replace"))
        raise MetaRequestError(e.code, info, stage=stage)

    # Typical response: {"images":{"filename.jpg":{"hash":"...", ...}}}
    images = payload.get("images", {}) if isinstance(payload, dict) else {}
    for _, info in images.items():
        if isinstance(info, dict) and info.get("hash"):
            return str(info["hash"]), payload
    # fallback recursive hash search
    def find_hash(obj):
        if isinstance(obj, dict):
            if obj.get("hash"):
                return obj.get("hash")
            for v in obj.values():
                h = find_hash(v)
                if h:
                    return h
        elif isinstance(obj, list):
            for v in obj:
                h = find_hash(v)
                if h:
                    return h
        return None
    h = find_hash(payload)
    if not h:
        raise RuntimeError(f"{stage}: upload succeeded but no image hash returned: {payload}")
    return str(h), payload


def fresh_image_hash(source_creative, link_data, account_id):
    # We do not reuse the source image_hash. Download the rendered/source image and upload again.
    candidate_urls = [
        link_data.get("picture"),
        source_creative.get("image_url"),
        source_creative.get("thumbnail_url"),
    ]
    url = next((x for x in candidate_urls if x), None)
    if not url:
        raise SkipSource("Image ad has no downloadable picture/image_url/thumbnail_url; cannot create fresh image asset safely.")
    image_bytes, content_type = download_bytes(url, "image_download")
    new_hash, upload_raw = upload_image_to_ad_account(account_id, image_bytes, content_type, "image_upload")
    return new_hash, {"source_url": url, "upload": upload_raw}


def clean_dict(d):
    return {k: deepcopy(v) for k, v in d.items() if v not in (None, "", {}, [])}


def clean_copy_suffixes(name):
    """Remove Facebook's trailing Copy/Копия/Копія suffixes without touching the creative key."""
    value = (name or "").strip()
    # Facebook can stack suffixes: "— Копия — Копия", "- Copy", etc.
    pattern = re.compile(r"\s*(?:—|–|-)\s*(?:копия|копія|copy)(?:\s*\d+)?\s*$", re.IGNORECASE)
    while True:
        cleaned = pattern.sub("", value).rstrip()
        if cleaned == value:
            break
        value = cleaned
    return re.sub(r"\s{2,}", " ", value).strip()


def explicit_creative_optouts(mode):
    """Explicitly disable documented Advantage+ creative features for API v26.0.

    Important:
    - Do NOT send legacy standard_enhancements; Meta has rejected it in our account.
    - Use the current documented field name video_filtering (not video_filter).
    - Multi-advertiser ads are controlled separately via contextual_multi_ads.
    - We intentionally keep the list to fields documented by Meta; unknown fields are NOT assumed to be ignored.
    """
    opt_out = {"enroll_status": "OPT_OUT"}
    common = {
        "site_extensions": deepcopy(opt_out),
        "text_optimizations": deepcopy(opt_out),
        "enhance_cta": deepcopy(opt_out),
        "description_automation": deepcopy(opt_out),
    }
    if mode == "VIDEO":
        common.update({
            "video_auto_crop": deepcopy(opt_out),
            "video_filtering": deepcopy(opt_out),
            "video_uncrop": deepcopy(opt_out),
        })
    elif mode == "IMAGE":
        common.update({
            "image_templates": deepcopy(opt_out),
            "image_touchups": deepcopy(opt_out),
            "image_background_gen": deepcopy(opt_out),
            "image_uncrop": deepcopy(opt_out),
            "image_animation": deepcopy(opt_out),
        })
    return common


def get_feature_enroll_statuses(creative):
    dof = creative.get("degrees_of_freedom_spec") or {}
    features = dof.get("creative_features_spec") or {}
    statuses = {}
    for key, value in features.items():
        if isinstance(value, dict) and value.get("enroll_status") is not None:
            statuses[key] = str(value.get("enroll_status")).upper()
    return statuses


def poll_ad_post_processing(ad_id):
    """Check asynchronous Meta post-processing so UI delivery errors are visible in the report."""
    snapshots = []
    # Two gentle checks; this is a test user, but we still avoid hammering the API.
    for delay in (10, 20):
        time.sleep(delay)
        snap = get_node(ad_id, AD_FIELDS, "audit_ad_post_processing")
        snapshots.append(snap)
        if snap.get("issues_info") or snap.get("failed_delivery_checks"):
            break
    return snapshots[-1] if snapshots else {}, snapshots


def build_clean_creative(source_creative, account_id, suffix):
    """Build a NEW ordinary creative from essential business fields only.

    Controls applied explicitly:
    - multi-advertiser ads: OPT_OUT via contextual_multi_ads;
    - Creative Setup / Advantage+ controls: explicit OPT_OUT for the relevant v25 features;
    - legacy enhancement containers from the source are NOT copied;
    - for video, resolve the AdVideo through the owning ad account and use its Meta-generated
      `picture` URL directly as `video_data.image_url`; no generic ad-image hash is created.
      Ads Manager may still label this as Manual because the public API does not expose
      the UI's Automatic selector as a documented switch.
    """
    oss = deepcopy(source_creative.get("object_story_spec") or {})
    page_id = oss.get("page_id")
    if not page_id:
        raise SkipSource("Source creative has no page_id in object_story_spec.")

    mode = "VIDEO" if oss.get("video_data") else "IMAGE" if oss.get("link_data") else None
    if not mode:
        raise SkipSource("Only ordinary single video_data or link_data creatives are supported in v16.")

    payload = {
        "name": f"{clean_copy_suffixes(source_creative.get('name') or 'creative')}{suffix}",
        "contextual_multi_ads": {"enroll_status": "OPT_OUT"},
        "degrees_of_freedom_spec": {
            "creative_features_spec": explicit_creative_optouts(mode),
        },
    }
    url_tags = source_creative.get("url_tags")
    if url_tags:
        payload["url_tags"] = url_tags

    audit = {
        "mode": mode,
        "preview_strategy": None,
        "source_image_hash": source_creative.get("image_hash"),
        "new_image_hash": None,
        "preview_meta": None,
        "requested_multi_advertiser": "OPT_OUT",
        "requested_feature_optouts": sorted(explicit_creative_optouts(mode).keys()),
        "omitted_source_containers": [
            "asset_feed_spec", "creative_sourcing_spec",
            "format_transformation_spec", "generative_asset_spec", "platform_customizations",
        ],
    }

    if mode == "VIDEO":
        vd = oss.get("video_data") or {}
        video_id = vd.get("video_id") or source_creative.get("video_id")
        if not video_id:
            raise SkipSource("Video creative has no video_id.")

        minimal_vd = clean_dict({
            "video_id": str(video_id),
            "message": vd.get("message"),
            "title": vd.get("title"),
            "link_description": vd.get("link_description"),
            "call_to_action": vd.get("call_to_action"),
        })
        # v16: do NOT reuse the source image_hash. Our v15 test showed that the old hash can
        # be accepted at creative-create time but still fail during Meta's asynchronous publish
        # processing (#2643026). The source object_story_spec for this ad exposes image_url as
        # well. Meta documents video_data.image_url as a supported thumbnail input and says the
        # image at that URL is saved to the ad account image library. We therefore pass ONLY the
        # source video_data.image_url and let Meta create a fresh backing image asset.
        # Never send image_hash and image_url together: Meta rejects that as redundant.
        source_video_image_url = vd.get("image_url")
        if not source_video_image_url:
            raise SkipSource(
                "Source video_data has no image_url. v16 intentionally skips instead of reusing "
                "the old image_hash that previously produced publishing error #2643026."
            )
        minimal_vd["image_url"] = str(source_video_image_url)
        audit.update({
            "preview_strategy": "SOURCE_VIDEO_IMAGE_URL_ONLY",
            "new_image_hash": None,
            "preview_meta": {
                "source_image_url": str(source_video_image_url),
                "note": "Only source video_data.image_url is sent; old image_hash is intentionally omitted."
            },
            "thumbnail_ui_automatic": False,
            "thumbnail_no_manual_intervention": True,
        })

        payload["object_story_spec"] = {"page_id": str(page_id), "video_data": minimal_vd}
        return payload, audit

    ld = oss.get("link_data") or {}
    new_hash, image_meta = fresh_image_hash(source_creative, ld, str(account_id))
    minimal_ld = clean_dict({
        "link": ld.get("link"),
        "message": ld.get("message"),
        "name": ld.get("name"),
        "description": ld.get("description"),
        "call_to_action": ld.get("call_to_action"),
        "image_hash": new_hash,
    })
    if not minimal_ld.get("link"):
        raise SkipSource("Image/link creative has no destination link.")
    payload["object_story_spec"] = {"page_id": str(page_id), "link_data": minimal_ld}
    audit.update({
        "preview_strategy": "FRESH_IMAGE_REUPLOAD",
        "new_image_hash": new_hash,
        "preview_meta": image_meta,
        "thumbnail_ui_automatic": True,
        "thumbnail_no_manual_intervention": True,
    })
    return payload, audit


def find_nested(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj.get(key)
        for v in obj.values():
            found = find_nested(v, key)
            if found is not None:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = find_nested(v, key)
            if found is not None:
                return found
    return None


def extract_minimal_semantics(creative):
    oss = creative.get("object_story_spec") or {}
    if oss.get("video_data"):
        vd = oss.get("video_data") or {}
        cta = vd.get("call_to_action") or {}
        return {
            "mode": "VIDEO",
            "page_id": oss.get("page_id"),
            "video_id": vd.get("video_id"),
            "message": vd.get("message"),
            "headline": vd.get("title"),
            "description": vd.get("link_description"),
            "cta": cta,
            "url": find_nested(cta, "link"),
            "url_tags": creative.get("url_tags"),
            "image_hash": vd.get("image_hash") or creative.get("image_hash"),
        }
    if oss.get("link_data"):
        ld = oss.get("link_data") or {}
        return {
            "mode": "IMAGE",
            "page_id": oss.get("page_id"),
            "message": ld.get("message"),
            "headline": ld.get("name"),
            "description": ld.get("description"),
            "cta": ld.get("call_to_action"),
            "url": ld.get("link"),
            "url_tags": creative.get("url_tags"),
            "image_hash": ld.get("image_hash") or creative.get("image_hash"),
        }
    return {"mode": "UNKNOWN"}


def normalize_for_compare(value):
    if isinstance(value, dict):
        return {k: normalize_for_compare(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [normalize_for_compare(v) for v in value]
    return value


def diff_fields(src, dst, fields):
    diffs = []
    for f in fields:
        a, b = src.get(f), dst.get(f)
        if normalize_for_compare(a) != normalize_for_compare(b):
            diffs.append({"field": f, "source": a, "copy": b})
    return diffs


def build_new_ad_payload(source_ad, copied_adset_id, new_creative_id, suffix):
    payload = {
        "name": f"{clean_copy_suffixes(source_ad.get('name') or 'ad')}{suffix}",
        "adset_id": str(copied_adset_id),
        "creative": {"creative_id": str(new_creative_id)},
        "status": "PAUSED",
    }
    for field in ("tracking_specs", "conversion_specs", "conversion_domain"):
        if source_ad.get(field) not in (None, "", {}, []):
            payload[field] = deepcopy(source_ad.get(field))
    return payload


def is_catalog(source_adset, source_creative):
    promoted = source_adset.get("promoted_object") or {}
    if find_nested(promoted, "product_set_id"):
        return True
    if find_nested(source_creative, "product_set_id"):
        return True
    return False


def create_clean_clone(source_adset_id):
    source = get_node(source_adset_id, ADSET_FIELDS, "source_adset_read")
    ads = graph_get_all(source_adset_id + "/ads", {"fields": ",".join(AD_FIELDS), "limit": 5}, "source_ads_read")
    if len(ads) != 1:
        raise SkipSource(f"Expected exactly 1 source ad, got {len(ads)}.")
    source_ad = ads[0]
    creative_id = str((source_ad.get("creative") or {}).get("id") or "")
    if not creative_id:
        raise SkipSource("Source ad has no creative ID.")
    source_creative = get_node(creative_id, CREATIVE_FIELDS, "source_creative_read")

    if is_catalog(source, source_creative):
        raise SkipSource("Catalog source detected. Catalogs are intentionally skipped in ordinary-ad phase.")

    now = datetime.now(POLAND_TZ)
    suffix = f" [PYTEST-V16 {now.strftime('%Y%m%d-%H%M%S')}]"
    account_id = str(source.get("account_id"))

    # 1) Copy only the adset. Child ad/creative are rebuilt from a minimal schema.
    copy_resp = graph_request(
        "POST", f"{source_adset_id}/copies",
        {"deep_copy": False, "status_option": "PAUSED"},
        stage="adset_copy_only",
    )
    copied_adset_id = str(copy_resp.get("copied_adset_id") or copy_resp.get("id") or "")
    if not copied_adset_id:
        raise RuntimeError(f"No copied_adset_id returned: {copy_resp}")
    PARTIAL["copied_adset_id"] = copied_adset_id
    graph_request(
        "POST", copied_adset_id,
        {"name": f"{clean_copy_suffixes(source.get('name') or source_adset_id)}{suffix}", "status": "PAUSED"},
        stage="rename_copied_adset",
    )

    # 2) Create NEW creative from business-essential fields only.
    # For video, resolve the source AdVideo through the ad account /advideos (or /video_ads) edge
    # For video, use only the source object_story_spec.video_data.image_url.
    # The old image_hash is intentionally not reused because it produced publish error #2643026.
    creative_payload, media_audit = build_clean_creative(source_creative, account_id, suffix)
    new_creative = graph_request(
        "POST", f"act_{account_id}/adcreatives", creative_payload,
        stage="create_clean_creative_v26_source_video_image_url"
    )
    new_creative_id = str(new_creative.get("id") or "")
    if not new_creative_id:
        raise RuntimeError(f"Creative create returned no id: {new_creative}")
    PARTIAL["new_creative_id"] = new_creative_id

    # 3) Create NEW ad pointing to the NEW creative.
    ad_payload = build_new_ad_payload(source_ad, copied_adset_id, new_creative_id, suffix)
    new_ad = graph_request("POST", f"act_{account_id}/ads", ad_payload, stage="create_new_ad")
    new_ad_id = str(new_ad.get("id") or "")
    if not new_ad_id:
        raise RuntimeError(f"Ad create returned no id: {new_ad}")
    PARTIAL["new_ad_id"] = new_ad_id

    copied_adset = get_node(copied_adset_id, ADSET_FIELDS, "audit_adset")
    copied_creative = get_node(new_creative_id, CREATIVE_FIELDS, "audit_creative")
    copied_ad, postprocess_snapshots = poll_ad_post_processing(new_ad_id)

    source_sem = extract_minimal_semantics(source_creative)
    copy_sem = extract_minimal_semantics(copied_creative)
    # Thumbnail hash is expected to differ; compare all other creative semantics.
    creative_fields_to_compare = ["mode", "page_id", "video_id", "message", "headline", "description", "cta", "url", "url_tags"]
    creative_diffs = diff_fields(source_sem, copy_sem, creative_fields_to_compare)

    adset_fields = [
        "bid_strategy", "bid_amount", "bid_constraints", "billing_event", "optimization_goal",
        "daily_budget", "lifetime_budget", "attribution_spec", "promoted_object", "destination_type",
        "pacing_type", "targeting", "is_dynamic_creative", "recurring_budget_semantics",
    ]
    ad_fields = ["tracking_specs", "conversion_specs", "conversion_domain"]
    adset_diffs = diff_fields(source, copied_adset, adset_fields)
    ad_diffs = diff_fields(source_ad, copied_ad, ad_fields)

    enhancement_fields = {
        k: copied_creative.get(k)
        for k in [
            "asset_feed_spec", "degrees_of_freedom_spec", "creative_sourcing_spec",
            "format_transformation_spec", "generative_asset_spec", "platform_customizations",
        ]
        if copied_creative.get(k) not in (None, {}, [])
    }

    def collect_enroll_statuses(obj, path=""):
        rows = []
        if isinstance(obj, dict):
            if "enroll_status" in obj:
                rows.append({"path": path or "$", "enroll_status": obj.get("enroll_status")})
            for k, v in obj.items():
                child = f"{path}.{k}" if path else str(k)
                rows.extend(collect_enroll_statuses(v, child))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                rows.extend(collect_enroll_statuses(v, f"{path}[{i}]"))
        return rows

    enhancement_statuses = collect_enroll_statuses(enhancement_fields)
    enhancement_opt_ins = [
        x for x in enhancement_statuses
        if str(x.get("enroll_status") or "").upper() == "OPT_IN"
    ]

    requested_feature_keys = set((media_audit.get("requested_feature_optouts") or []))
    returned_feature_statuses = get_feature_enroll_statuses(copied_creative)
    requested_feature_statuses = {k: returned_feature_statuses.get(k) for k in sorted(requested_feature_keys)}
    missing_or_not_optout = {
        k: v for k, v in requested_feature_statuses.items()
        if v != "OPT_OUT"
    }
    contextual_multi = copied_creative.get("contextual_multi_ads") or {}
    multi_advertiser_status = str(contextual_multi.get("enroll_status") or "").upper() or None
    ad_issues = copied_ad.get("issues_info") or []
    failed_delivery_checks = copied_ad.get("failed_delivery_checks") or []

    source_pixel = find_nested(source.get("promoted_object") or {}, "pixel_id")
    copied_pixel = find_nested(copied_adset.get("promoted_object") or {}, "pixel_id")

    result = {
        "mode": "CLEAN_REBUILD",
        "account_id": account_id,
        "source_adset_id": source_adset_id,
        "copied_adset_id": copied_adset_id,
        "source_ad_id": source_ad.get("id"),
        "copied_ad_id": new_ad_id,
        "source_creative_id": creative_id,
        "copied_creative_id": new_creative_id,
        "creative_id_changed": creative_id != new_creative_id,
        "pixel_source": source_pixel,
        "pixel_copy": copied_pixel,
        "pixel_match": source_pixel == copied_pixel,
        "source_semantics": source_sem,
        "copy_semantics": copy_sem,
        "media_audit": media_audit,
        "adset_diffs": adset_diffs,
        "ad_diffs": ad_diffs,
        "creative_diffs": creative_diffs,
        "enhancement_fields_after_create": enhancement_fields,
        "enhancement_enroll_statuses": enhancement_statuses,
        "enhancement_opt_ins": enhancement_opt_ins,
        "requested_feature_statuses": requested_feature_statuses,
        "missing_or_not_optout": missing_or_not_optout,
        "multi_advertiser_status": multi_advertiser_status,
        "ad_issues": ad_issues,
        "failed_delivery_checks": failed_delivery_checks,
        "postprocess_snapshots": postprocess_snapshots,
        "source_adset": source,
        "copied_adset": copied_adset,
        "source_ad": source_ad,
        "copied_ad": copied_ad,
        "source_creative": source_creative,
        "copied_creative": copied_creative,
        "creative_create_payload": creative_payload,
    }
    result["core_ok"] = (
        copied_adset.get("status") == "PAUSED"
        and copied_ad.get("status") == "PAUSED"
        and result["creative_id_changed"]
        and result["pixel_match"]
        and not creative_diffs
        and not enhancement_opt_ins
        and multi_advertiser_status == "OPT_OUT"
        and not missing_or_not_optout
        and not ad_issues
        and not failed_delivery_checks
    )
    # Separate the UI label from the operational goal. The public API does not expose
    # a documented equivalent of Ads Manager's "Automatic" thumbnail selector. For
    # automation, the critical requirement is that the script can choose a Meta-generated ad-video preview URL with no human intervention and the ad publishes cleanly.
    result["thumbnail_ui_automatic"] = bool(media_audit.get("thumbnail_ui_automatic"))
    result["thumbnail_no_manual_intervention"] = bool(media_audit.get("thumbnail_no_manual_intervention"))
    result["scaler_ready"] = result["core_ok"] and result["thumbnail_no_manual_intervention"]
    return result


def format_error(e, source_id):
    if isinstance(e, MetaRequestError):
        info = e.info
        parts = [
            "❌ <b>Duplicate test v16 error</b>",
            f"Source Adset: <code>{esc(source_id)}</code>",
            f"Stage: <b>{esc(e.stage or 'unknown')}</b>",
        ]
        if PARTIAL:
            parts.append("Partial: " + esc(json.dumps(PARTIAL, ensure_ascii=False)))
        parts.append(esc(str(e)))
        if info.get("fbtrace_id"):
            parts.append(f"fbtrace_id: {esc(info.get('fbtrace_id'))}")
        return "\n".join(parts)
    return (
        "❌ <b>Duplicate test v16 error</b>\n"
        f"Source Adset: <code>{esc(source_id)}</code>\n"
        f"Partial: {esc(json.dumps(PARTIAL, ensure_ascii=False))}\n"
        f"{esc(str(e))}"
    )


def summary_message(result):
    media = result.get("media_audit") or {}
    issues = result.get("ad_issues") or []
    failed_checks = result.get("failed_delivery_checks") or []
    feature_statuses = result.get("requested_feature_statuses") or {}
    feature_short = ", ".join(f"{k}={v or 'NOT_RETURNED'}" for k, v in feature_statuses.items())
    lines = [
        "✅ <b>Duplicate test v16 • SOURCE VIDEO IMAGE URL</b>",
        f"Account: <code>{esc(result.get('account_id'))}</code>",
        f"Source Adset: <code>{esc(result.get('source_adset_id'))}</code>",
        f"Copy Adset: <code>{esc(result.get('copied_adset_id'))}</code> • {esc((result.get('copied_adset') or {}).get('name'))}",
        f"Source Ad → Copy Ad: <code>{esc(result.get('source_ad_id'))}</code> → <code>{esc(result.get('copied_ad_id'))}</code>",
        f"Creative: <code>{esc(result.get('source_creative_id'))}</code> → <code>{esc(result.get('copied_creative_id'))}</code> {'✅ NEW' if result.get('creative_id_changed') else '❌ SAME'}",
        f"Pixel: {esc(result.get('pixel_source'))} → {esc(result.get('pixel_copy'))} {'✅' if result.get('pixel_match') else '❌'}",
        f"Multi-advertiser: {esc(result.get('multi_advertiser_status'))} {'✅' if result.get('multi_advertiser_status') == 'OPT_OUT' else '⚠️'}",
        f"Creative controls: {esc(feature_short or 'none returned')}",
        f"Media: {esc(media.get('mode'))} • thumbnail: {esc(media.get('preview_strategy'))}",
        f"Thumbnail UI Automatic: {'✅ YES' if result.get('thumbnail_ui_automatic') else 'ℹ️ NO — explicit generated frame'}",
        f"Thumbnail needs manual action: {'❌ YES' if not result.get('thumbnail_no_manual_intervention') else '✅ NO'}",
        f"Core diffs: adset={len(result.get('adset_diffs', []))}, ad={len(result.get('ad_diffs', []))}, creative={len(result.get('creative_diffs', []))}",
        f"Post-processing issues: {len(issues)} • failed checks: {len(failed_checks)} {'✅' if not issues and not failed_checks else '❌'}",
        f"Core OK: {'✅ YES' if result.get('core_ok') else '⚠️ NO'}",
        f"Scaler-ready: {'✅ YES' if result.get('scaler_ready') else '⚠️ NO'}",
    ]
    if issues:
        lines.append("issues_info: " + esc(json.dumps(issues, ensure_ascii=False)[:1200]))
    if failed_checks:
        lines.append("failed_delivery_checks: " + esc(json.dumps(failed_checks, ensure_ascii=False)[:1200]))
    return "\n".join(lines)


def main():
    if not ACCESS_TOKEN:
        print("FB_SCALER_ACCESS_TOKEN missing", flush=True)
        return 2

    source_id = (os.environ.get("TEST_ADSET_ID") or "").strip()
    if not source_id:
        print("TEST_ADSET_ID missing", flush=True)
        return 2

    diag = get_permissions_diag()
    send_access_diag(diag)
    report = {"api_access": diag, "source_adset_id": source_id, "result": None, "error": None}

    try:
        result = create_clean_clone(source_id)
        report["result"] = result
        send_telegram(summary_message(result))
        exit_code = 0
    except SkipSource as e:
        report["error"] = {"type": "skip", "message": str(e), "partial": deepcopy(PARTIAL)}
        send_telegram(
            "⏭ <b>Duplicate test v16 skipped</b>\n"
            f"Source Adset: <code>{esc(source_id)}</code>\n{esc(str(e))}"
        )
        exit_code = 1
    except Exception as e:
        report["error"] = {
            "type": type(e).__name__,
            "message": str(e),
            "stage": getattr(e, "stage", None),
            "partial": deepcopy(PARTIAL),
            "meta": getattr(e, "info", None),
        }
        send_telegram(format_error(e, source_id))
        exit_code = 1

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Report saved: {REPORT_FILE}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
