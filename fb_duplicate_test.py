import os
import sys
import json
import time
import html
import re
import urllib.request
import urllib.parse
import urllib.error
import uuid
from copy import deepcopy
from datetime import datetime

from fb_config import POLAND_TZ

API_VER = "v26.0"
TEST_VERSION = "v26.6"
BUILD_ID = "2026-08-12-native-ad-copies-r1"

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TEST_ADSET_ID = (os.environ.get("TEST_ADSET_ID") or "").strip()
TEST_VIDEO_FILE_URL = (os.environ.get("TEST_VIDEO_FILE_URL") or "").strip()
TEST_VIDEO_FILE_PATH = (os.environ.get("TEST_VIDEO_FILE_PATH") or "").strip()

REPORT_FILE = "duplicate_test_v26_6_native_ad_copies_report.json"

REQUIRED_PAGE_VIDEO_PERMISSIONS = {
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_show_list",
}

PARTIAL = {}
CLEANUP_CONTEXT = {"page_token": None}

ADSET_FIELDS = [
    "id", "name", "account_id", "campaign_id", "status", "effective_status",
    "bid_strategy", "bid_amount", "billing_event", "optimization_goal",
    "daily_budget", "lifetime_budget", "attribution_spec", "promoted_object",
    "destination_type", "pacing_type", "targeting", "is_dynamic_creative",
    "recurring_budget_semantics",
]

AD_FIELDS = [
    "id", "name", "adset_id", "status", "configured_status", "effective_status",
    "creative", "tracking_specs", "conversion_specs", "conversion_domain",
    "issues_info", "failed_delivery_checks", "updated_time",
]

CREATIVE_FIELDS = [
    "id", "name", "account_id", "status",
    "object_story_id", "effective_object_story_id",
    "object_story_spec", "url_tags", "thumbnail_url", "video_id",
    "contextual_multi_ads",
]

VIDEO_FIELDS = [
    "id", "created_time", "updated_time", "length",
    "picture", "post_id", "published", "source",
    "status", "permalink_url",
]

ADSET_FIDELITY_FIELDS = [
    "campaign_id", "bid_strategy", "bid_amount", "billing_event",
    "optimization_goal", "daily_budget", "lifetime_budget",
    "attribution_spec", "promoted_object", "destination_type",
    "pacing_type", "targeting", "is_dynamic_creative",
    "recurring_budget_semantics",
]

VIDEO_TEXT_FIDELITY_FIELDS = [
    "message", "title", "link_description", "call_to_action",
]


def esc(value):
    return html.escape(str(value), quote=False)


def telegram_chunks(message, max_chars=3500):
    chunks = []
    current = []

    for line in str(message).splitlines():
        candidate = "\n".join(current + [line])
        if current and len(candidate) > max_chars:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        chunks.append("\n".join(current))

    return chunks or [""]


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; skipped.", flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for i, chunk in enumerate(telegram_chunks(message), start=1):
        payload = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")

        try:
            urllib.request.urlopen(
                urllib.request.Request(url, data=payload),
                timeout=20,
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"Telegram HTTP {e.code}, chunk {i}: {body[:1000]}", flush=True)
        except Exception as e:
            print(f"Telegram error, chunk {i}: {e}", flush=True)


def decode_meta_error(body):
    try:
        data = json.loads(body)
        err = data.get("error", {})
        return {
            "message": err.get("message"),
            "code": err.get("code"),
            "subcode": err.get("error_subcode"),
            "is_transient": bool(err.get("is_transient")),
            "user_title": err.get("error_user_title"),
            "user_msg": err.get("error_user_msg"),
            "fbtrace_id": err.get("fbtrace_id"),
            "raw": data,
        }
    except Exception:
        return {"message": body, "raw": body}


class MetaError(RuntimeError):
    def __init__(self, stage, http_status, info):
        self.stage = stage
        self.http_status = http_status
        self.info = info
        msg = (
            f"Meta HTTP {http_status}: {info.get('message')} "
            f"(code={info.get('code')}, subcode={info.get('subcode')})"
        )
        if info.get("user_title"):
            msg += f" | {info.get('user_title')}"
        if info.get("user_msg"):
            msg += f" | {info.get('user_msg')}"
        super().__init__(msg)


class DiagnosticError(RuntimeError):
    def __init__(self, stage, message):
        self.stage = stage
        super().__init__(message)


def serialize(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def graph_request(method, path, params=None, token=None, stage="graph"):
    params = dict(params or {})
    params["access_token"] = token or ACCESS_TOKEN

    url = f"https://graph.facebook.com/{API_VER}/{str(path).lstrip('/')}"

    encoded = {
        k: serialize(v)
        for k, v in params.items()
        if v is not None
    }

    try:
        if method == "GET":
            req = urllib.request.Request(
                url + "?" + urllib.parse.urlencode(encoded),
                method="GET",
            )
        elif method == "POST":
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(encoded).encode("utf-8"),
                method="POST",
            )
        elif method == "DELETE":
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(encoded).encode("utf-8"),
                method="DELETE",
            )
        else:
            raise ValueError(f"Unsupported Graph method: {method}")

        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise MetaError(stage, e.code, decode_meta_error(body))


def graph_multipart(
    path,
    fields,
    file_field,
    filename,
    file_bytes,
    content_type,
    token=None,
    stage="graph_multipart",
    host="graph.facebook.com",
):
    """POST one binary file to a public Graph API edge."""
    boundary = f"----MetaDuplicateTest{uuid.uuid4().hex}"
    boundary_bytes = boundary.encode("ascii")
    body = bytearray()

    multipart_fields = dict(fields or {})
    multipart_fields["access_token"] = token or ACCESS_TOKEN

    for key, value in multipart_fields.items():
        if value is None:
            continue
        body.extend(b"--" + boundary_bytes + b"\r\n")
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(str(serialize(value)).encode("utf-8"))
        body.extend(b"\r\n")

    body.extend(b"--" + boundary_bytes + b"\r\n")
    body.extend(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
    body.extend(file_bytes)
    body.extend(b"\r\n--" + boundary_bytes + b"--\r\n")

    url = f"https://{host}/{API_VER}/{str(path).lstrip('/')}"
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise MetaError(stage, e.code, decode_meta_error(raw))


def download_binary(url, stage, max_bytes=25 * 1024 * 1024):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "MetaDuplicateDiagnostic/26.4"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise DiagnosticError(
                    stage,
                    f"Downloaded asset exceeds the {max_bytes}-byte diagnostic limit",
                )
            content_type = resp.headers.get_content_type() or "application/octet-stream"
            return data, content_type
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise DiagnosticError(stage, f"HTTP {e.code}: {raw[:800]}")
    except urllib.error.URLError as e:
        raise DiagnosticError(stage, f"URL error: {e.reason}")


def graph_get_all(path, params=None, token=None, stage="graph_list"):
    params = dict(params or {})
    params["access_token"] = token or ACCESS_TOKEN

    url = (
        f"https://graph.facebook.com/{API_VER}/{str(path).lstrip('/')}?"
        + urllib.parse.urlencode({
            k: serialize(v)
            for k, v in params.items()
            if v is not None
        })
    )

    rows = []

    while url:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url),
                timeout=120,
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise MetaError(stage, e.code, decode_meta_error(body))

        rows.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")

    return rows


def get_node(node_id, fields, token=None, stage="get_node"):
    return graph_request(
        "GET",
        node_id,
        {"fields": ",".join(fields)},
        token=token,
        stage=stage,
    )


def clean_copy_suffixes(name):
    text = str(name or "").strip()
    patterns = [
        r"\s*[-—–]\s*(копия|копія|copy)\s*$",
        r"\s*\((копия|копія|copy)\)\s*$",
    ]

    previous = None
    while text and text != previous:
        previous = text
        for pattern in patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

    return text


def api_identity():
    me = graph_request("GET", "me", {"fields": "id,name"}, stage="diag_me")
    perms = graph_request("GET", "me/permissions", {}, stage="diag_permissions").get("data", [])
    granted = sorted(
        x.get("permission")
        for x in perms
        if x.get("status") == "granted" and x.get("permission")
    )
    return {"identity": me, "permissions": perms, "granted": granted}


def send_identity(diag):
    send_telegram(
        "🔐 <b>Meta API access diagnostics</b>\n"
        f"API identity: {esc(diag['identity'].get('name'))} • "
        f"<code>{esc(diag['identity'].get('id'))}</code>\n"
        f"Granted: {esc(', '.join(diag.get('granted') or []))}"
    )


def resolve_page_token(page_id):
    """
    Try to resolve a Page access token using the current scaler token.
    If Meta does not return one, fall back to the scaler token and let the
    Page-video endpoint tell us whether that token is sufficient.
    """
    try:
        page = graph_request(
            "GET",
            page_id,
            {"fields": "id,name,access_token,tasks"},
            stage="page_token_direct",
        )
        if page.get("access_token"):
            return {
                "token": page["access_token"],
                "source": "Page.access_token",
                "tasks": page.get("tasks"),
            }
    except MetaError:
        pass

    try:
        pages = graph_get_all(
            "me/accounts",
            {"fields": "id,name,access_token,tasks", "limit": 100},
            stage="page_token_me_accounts",
        )
        for page in pages:
            if str(page.get("id")) == str(page_id) and page.get("access_token"):
                return {
                    "token": page["access_token"],
                    "source": "me/accounts",
                    "tasks": page.get("tasks"),
                }
    except MetaError:
        pass

    return {
        "token": ACCESS_TOKEN,
        "source": "FB_SCALER_ACCESS_TOKEN fallback",
        "tasks": None,
    }


def get_source_objects(adset_id):
    adset = get_node(adset_id, ADSET_FIELDS, stage="source_adset")

    ads = graph_get_all(
        f"{adset_id}/ads",
        {"fields": ",".join(AD_FIELDS), "limit": 10},
        stage="source_ads",
    )

    if len(ads) != 1:
        raise RuntimeError(f"Expected exactly 1 ad in source adset; got {len(ads)}")

    ad = ads[0]
    creative_id = str((ad.get("creative") or {}).get("id") or "")
    if not creative_id:
        raise RuntimeError("Source ad has no creative id")

    creative = get_node(
        creative_id,
        CREATIVE_FIELDS,
        stage="source_creative",
    )

    oss = creative.get("object_story_spec") or {}
    vd = oss.get("video_data") or {}

    if not vd:
        raise RuntimeError("v26 test supports ordinary VIDEO ad only")

    page_id = str(oss.get("page_id") or "")
    story_video_id = str(vd.get("video_id") or "")
    root_video_id = str(creative.get("video_id") or "")

    if not page_id:
        raise RuntimeError("Source creative has no page_id")
    if not story_video_id and not root_video_id:
        raise RuntimeError("Source creative has no video id")

    return {
        "adset": adset,
        "ad": ad,
        "creative": creative,
        "page_id": page_id,
        "story_video_id": story_video_id or None,
        "root_video_id": root_video_id or None,
        "video_data": vd,
    }


def resolve_source_video(source, page_access):
    """
    v26.5.2: inspect BOTH source video ids with BOTH available token contexts.

    Priority:
    1) explicit local MP4 path (developer console/local run);
    2) download explicit TEST_VIDEO_FILE_URL and upload its bytes;
    3) download a `source` URL returned by either token and upload its bytes.

    `file_url` and crossposting are intentionally not used in this run. The
    previous tests already reached permission/reuse failures on those branches.
    """
    attempts = []
    source_candidates = []

    token_contexts = []
    for token_source, token in (
        (f"page:{page_access['source']}", page_access["token"]),
        ("scaler_system_user", ACCESS_TOKEN),
    ):
        if not token:
            continue
        if any(existing[1] == token for existing in token_contexts):
            attempts.append({
                "token_source": token_source,
                "skipped": "same token value already tested",
            })
            continue
        token_contexts.append((token_source, token))

    # Story first: it is more likely to be Page-associated than root AdVideo.
    for label, video_id in (
        ("story_video_id", source.get("story_video_id")),
        ("root_creative_video_id", source.get("root_video_id")),
    ):
        if not video_id:
            continue

        for token_source, token in token_contexts:
            try:
                node = get_node(
                    video_id,
                    VIDEO_FIELDS,
                    token=token,
                    stage=f"read_source_video:{label}:{token_source}",
                )
                attempts.append({
                    "label": label,
                    "video_id": video_id,
                    "token_source": token_source,
                    "node": node,
                })
                if node.get("source"):
                    source_candidates.append({
                        "label": label,
                        "video_id": video_id,
                        "token_source": token_source,
                        "node": node,
                        "source_url": node["source"],
                    })
            except MetaError as e:
                attempts.append({
                    "label": label,
                    "video_id": video_id,
                    "token_source": token_source,
                    "error": str(e),
                    "meta": e.info,
                })

    if TEST_VIDEO_FILE_PATH:
        if not os.path.isfile(TEST_VIDEO_FILE_PATH):
            raise RuntimeError(
                f"TEST_VIDEO_FILE_PATH does not exist: {TEST_VIDEO_FILE_PATH}"
            )
        return {
            "mode": "local_binary_upload",
            "local_path": TEST_VIDEO_FILE_PATH,
            "attempts": attempts,
        }

    if TEST_VIDEO_FILE_URL:
        return {
            "mode": "explicit_url_binary_upload",
            "source_url": TEST_VIDEO_FILE_URL,
            "attempts": attempts,
        }

    if source_candidates:
        chosen = source_candidates[0]
        return {
            "mode": "api_source_binary_upload",
            **chosen,
            "attempts": attempts,
        }

    raise RuntimeError(
        "No downloadable video source was returned by either token. Provide "
        "TEST_VIDEO_FILE_URL or TEST_VIDEO_FILE_PATH."
    )


def redacted_source_video_resolution(value):
    result = deepcopy(value)
    if result.get("source_url"):
        result["source_url"] = "<REDACTED_SOURCE_URL>"
    if result.get("local_path"):
        result["local_path"] = os.path.basename(result["local_path"])
    for attempt in result.get("attempts") or []:
        node = attempt.get("node")
        if isinstance(node, dict) and node.get("source"):
            node["source"] = "<REDACTED_SOURCE_URL>"
        meta = attempt.get("meta")
        if isinstance(meta, dict):
            meta.pop("raw", None)
    return result


def create_new_page_video(source, source_video, page_access):
    vd = source["video_data"]

    payload = {
        "published": False,
        "unpublished_content_type": "ADS_POST",
    }

    mode = source_video.get("mode")

    if mode == "local_binary_upload":
        stage = "create_unpublished_page_video:local_binary"
    elif mode in ("explicit_url_binary_upload", "api_source_binary_upload"):
        stage = "create_unpublished_page_video:downloaded_binary"
    else:
        raise RuntimeError(f"Unknown source video mode: {mode}")

    # Page Video metadata is not the ad creative. The complete ad text, URL
    # and CTA are copied later through object_story_spec.video_data.
    if vd.get("message"):
        payload["description"] = vd.get("message")

    if vd.get("title"):
        payload["title"] = vd.get("title")

    PARTIAL["page_video_source_mode"] = mode
    PARTIAL["page_video_source_video_id"] = source_video.get("video_id")

    if mode == "local_binary_upload":
        with open(source_video["local_path"], "rb") as f:
            video_bytes = f.read()
        if not video_bytes:
            raise RuntimeError("TEST_VIDEO_FILE_PATH is empty")
        source_filename = os.path.basename(source_video["local_path"])
        source_content_type = "video/mp4"
    else:
        video_bytes, detected_content_type = download_binary(
            source_video["source_url"],
            stage="download_source_video",
            max_bytes=250 * 1024 * 1024,
        )
        if not video_bytes:
            raise DiagnosticError(
                "download_source_video",
                "Source video download returned an empty file",
            )
        source_filename = f"source-video-{source_video.get('video_id') or 'explicit'}.mp4"
        source_content_type = (
            detected_content_type
            if detected_content_type.startswith("video/")
            else "video/mp4"
        )

    PARTIAL["source_video_download"] = {
        "bytes": len(video_bytes),
        "content_type": source_content_type,
        "filename": source_filename,
    }

    response = graph_multipart(
        f"{source['page_id']}/videos",
        payload,
        "source",
        source_filename,
        video_bytes,
        source_content_type,
        token=page_access["token"],
        stage=stage,
        host="graph-video.facebook.com",
    )

    new_video_id = str(response.get("id") or "")
    if not new_video_id:
        raise RuntimeError(f"Page video create returned no id: {response}")

    PARTIAL["new_page_video_id"] = new_video_id
    PARTIAL["page_video_create_response"] = response

    return new_video_id, payload, response


def video_ready_state(node):
    status = node.get("status")
    candidates = []

    if isinstance(status, str):
        candidates.append(status)
    elif isinstance(status, dict):
        for key in ("video_status", "status"):
            if status.get(key):
                candidates.append(status[key])
        for phase_key in ("uploading_phase", "processing_phase", "publishing_phase"):
            phase = status.get(phase_key)
            if isinstance(phase, dict) and phase.get("status"):
                candidates.append(phase["status"])

    normalized = [str(x).strip().lower() for x in candidates]
    ready = any(x in ("ready", "complete", "completed", "published") for x in normalized)
    failed = any(x in ("error", "failed", "expired") for x in normalized)
    return {"ready": ready, "failed": failed, "states": normalized}


def wait_for_video_and_thumbnails(video_id, page_token):
    snapshots = []

    for delay in (5, 10, 15, 30, 45, 60, 60):
        time.sleep(delay)

        node = get_node(
            video_id,
            VIDEO_FIELDS,
            token=page_token,
            stage="poll_new_page_video",
        )

        try:
            thumbs = graph_get_all(
                f"{video_id}/thumbnails",
                {
                    "fields": "id,uri,is_preferred,width,height,scale",
                    "limit": 50,
                },
                token=page_token,
                stage="poll_new_page_video_thumbnails",
            )
        except MetaError as e:
            thumbs = [{"_error": str(e)}]

        readiness = video_ready_state(node)
        snapshots.append({
            "video": node,
            "readiness": readiness,
            "thumbnails": thumbs,
        })

        real_thumbs = [
            x for x in thumbs
            if isinstance(x, dict) and x.get("id") and x.get("uri")
        ]

        if readiness["failed"]:
            raise RuntimeError(
                "NEW Page video processing failed: "
                + json.dumps(snapshots[-1], ensure_ascii=False)[:1800]
            )

        # Some Page videos expose post_id before the nested status reaches a
        # stable `ready` spelling. Either signal plus real thumbnails is enough
        # to continue while leaving Meta's generated thumbnail selection on Auto.
        if real_thumbs and (readiness["ready"] or node.get("post_id")):
            return node, real_thumbs, snapshots

    raise RuntimeError(
        "Timed out waiting for NEW Page video readiness + thumbnails: "
        + json.dumps(snapshots[-1] if snapshots else {}, ensure_ascii=False)[:1800]
    )


def copy_adset(source_adset, suffix):
    response = graph_request(
        "POST",
        f"{source_adset['id']}/copies",
        {"deep_copy": False, "status_option": "PAUSED"},
        stage="copy_adset",
    )

    copied_id = str(
        response.get("copied_adset_id")
        or response.get("id")
        or ""
    )

    if not copied_id:
        raise RuntimeError(f"No copied adset id: {response}")

    PARTIAL["copied_adset_id"] = copied_id

    graph_request(
        "POST",
        copied_id,
        {
            "name": f"{clean_copy_suffixes(source_adset.get('name'))}{suffix}",
            "status": "PAUSED",
        },
        stage="rename_copied_adset",
    )

    return copied_id


def copy_ad_natively(source_ad, copied_adset_id, suffix):
    """Use Meta's native Ad Copies API without supplying creative inputs."""
    request_payload = {
        "adset_id": copied_adset_id,
        "status_option": "PAUSED",
    }
    response = graph_request(
        "POST",
        f"{source_ad['id']}/copies",
        request_payload,
        stage="copy_ad_via_native_copies_edge",
    )

    copied_ad_id = str(response.get("copied_ad_id") or response.get("id") or "")
    if not copied_ad_id:
        raise RuntimeError(f"No copied ad id: {response}")

    PARTIAL["new_ad_id"] = copied_ad_id

    graph_request(
        "POST",
        copied_ad_id,
        {
            "name": f"{clean_copy_suffixes(source_ad.get('name'))}{suffix}",
            "status": "PAUSED",
        },
        stage="rename_native_copied_ad",
    )

    return copied_ad_id, request_payload, response


def select_meta_generated_thumbnail(thumbnails):
    """Choose one frame generated by Meta; never upload or mark a custom frame."""
    real = [
        item for item in thumbnails
        if isinstance(item, dict) and item.get("id") and item.get("uri")
    ]
    if not real:
        raise RuntimeError("Meta generated no usable thumbnail candidates")

    preferred = [item for item in real if item.get("is_preferred") is True]
    selected = preferred[0] if preferred else real[0]
    return deepcopy(selected)


def build_new_object_story_spec(source, new_video_id, thumbnail_url):
    """
    Build a fresh unpublished video-ad story.

    Crucially, the payload contains object_story_spec and never
    object_story_id. That is the API equivalent of creating a new ad rather
    than selecting an existing Page publication.
    """
    source_spec = source["creative"].get("object_story_spec") or {}
    source_video_data = source_spec.get("video_data") or {}

    spec = {"page_id": source["page_id"]}
    for key in ("instagram_actor_id",):
        if source_spec.get(key):
            spec[key] = deepcopy(source_spec[key])

    video_data = {}
    for key in VIDEO_TEXT_FIDELITY_FIELDS:
        if source_video_data.get(key) is not None:
            video_data[key] = deepcopy(source_video_data[key])

    # These optional writable fields occur on some ordinary video ads. Copy
    # them only when present; do not copy the old video/image identifiers.
    for key in ("caption_ids", "page_welcome_message"):
        if source_video_data.get(key) is not None:
            video_data[key] = deepcopy(source_video_data[key])

    video_data["video_id"] = str(new_video_id)
    # Meta rejects a fresh video creative without image_url/image_hash
    # (code 100, subcode 1443226). Use a frame generated by Meta for this NEW
    # Page Video. We do not upload a custom thumbnail and do not write
    # is_preferred. Ads Manager's Auto/Manual label remains an empirical UI
    # check for this test revision.
    video_data["image_url"] = str(thumbnail_url)

    spec["video_data"] = video_data
    return spec


def create_creative_from_new_story_spec(
    source,
    new_video_id,
    thumbnail_url,
    suffix,
):
    account_id = str(source["adset"]["account_id"])

    object_story_spec = build_new_object_story_spec(
        source,
        new_video_id,
        thumbnail_url,
    )

    payload = {
        "name": f"{source['creative'].get('name') or 'creative'}{suffix}",
        "object_story_spec": object_story_spec,
        "contextual_multi_ads": deepcopy(
            source["creative"].get("contextual_multi_ads")
            or {"enroll_status": "OPT_OUT"}
        ),
    }

    if source["creative"].get("url_tags"):
        payload["url_tags"] = source["creative"]["url_tags"]

    response = graph_request(
        "POST",
        f"act_{account_id}/adcreatives",
        payload,
        stage="create_creative_via_object_story_spec",
    )

    creative_id = str(response.get("id") or "")
    if not creative_id:
        raise RuntimeError(f"Creative create returned no id: {response}")

    PARTIAL["new_creative_id"] = creative_id

    return creative_id, payload


def compare_fields(source, copied, fields):
    differences = {}
    for field in fields:
        source_value = source.get(field)
        copied_value = copied.get(field)
        if source_value != copied_value:
            differences[field] = {
                "source": source_value,
                "copy": copied_value,
            }
    return differences


def compare_adset_fidelity(source_adset, copied_adset):
    """
    Keep the raw API difference visible, but treat the one UI-verified
    /copies readback normalization seen in v26.4/v26.5.2 as non-blocking:
    source targeting_automation.individual_setting is omitted on the copy
    while every other targeting value remains identical.
    """
    raw = compare_fields(source_adset, copied_adset, ADSET_FIDELITY_FIELDS)
    effective = deepcopy(raw)
    warnings = []

    targeting_difference = raw.get("targeting")
    if targeting_difference:
        source_targeting = deepcopy(targeting_difference.get("source") or {})
        copy_targeting = deepcopy(targeting_difference.get("copy") or {})

        source_automation = source_targeting.get("targeting_automation") or {}
        copy_automation = copy_targeting.get("targeting_automation") or {}
        source_individual = source_automation.pop("individual_setting", None)
        copy_individual = copy_automation.pop("individual_setting", None)

        if source_automation:
            source_targeting["targeting_automation"] = source_automation
        else:
            source_targeting.pop("targeting_automation", None)
        if copy_automation:
            copy_targeting["targeting_automation"] = copy_automation
        else:
            copy_targeting.pop("targeting_automation", None)

        if (
            source_targeting == copy_targeting
            and source_individual == {"age": 1, "gender": 1}
            and copy_individual is None
        ):
            effective.pop("targeting", None)
            warnings.append({
                "field": "targeting.targeting_automation.individual_setting",
                "source": source_individual,
                "copy": copy_individual,
                "classification": "UI_VERIFIED_READBACK_NORMALIZATION",
            })

    return raw, effective, warnings


def cleanup_partial_test_objects():
    """Best-effort cleanup of objects created by this failed test run only."""
    cleanup = []
    targets = [
        ("new_ad_id", ACCESS_TOKEN),
        ("new_creative_id", ACCESS_TOKEN),
        ("copied_adset_id", ACCESS_TOKEN),
        ("new_page_video_id", CLEANUP_CONTEXT.get("page_token")),
    ]

    for key, token in targets:
        object_id = str(PARTIAL.get(key) or "")
        if not object_id:
            continue
        try:
            response = graph_request(
                "DELETE",
                object_id,
                token=token or ACCESS_TOKEN,
                stage=f"cleanup_{key}",
            )
            cleanup.append({"object": key, "id": object_id, "deleted": True, "response": response})
        except Exception as cleanup_exc:
            cleanup.append({"object": key, "id": object_id, "deleted": False, "error": str(cleanup_exc)})

    PARTIAL["cleanup"] = cleanup
    return cleanup


def audit_creative_fidelity(source, new_creative, new_video_id):
    source_spec = source["creative"].get("object_story_spec") or {}
    source_vd = source_spec.get("video_data") or {}
    new_spec = new_creative.get("object_story_spec") or {}
    new_vd = new_spec.get("video_data") or {}

    text_differences = compare_fields(
        source_vd,
        new_vd,
        VIDEO_TEXT_FIDELITY_FIELDS,
    )

    source_instagram = source_spec.get("instagram_actor_id")
    new_instagram = new_spec.get("instagram_actor_id")

    return {
        "creation_request_used_object_story_spec": True,
        "creation_request_used_object_story_id": False,
        "new_object_story_spec_present": bool(new_spec),
        "new_video_data_present": bool(new_vd),
        "page_id_source": source_spec.get("page_id"),
        "page_id_copy": new_spec.get("page_id"),
        "instagram_actor_id_source": source_instagram,
        "instagram_actor_id_copy": new_instagram,
        "video_id_source": str(source_vd.get("video_id") or ""),
        "video_id_expected": str(new_video_id),
        "video_id_copy": str(new_vd.get("video_id") or new_creative.get("video_id") or ""),
        "text_cta_differences": text_differences,
        "text_cta_match": not text_differences,
        "page_id_match": str(source_spec.get("page_id") or "") == str(new_spec.get("page_id") or ""),
        "instagram_actor_id_match": source_instagram == new_instagram,
        "new_video_id_match": str(new_vd.get("video_id") or new_creative.get("video_id") or "") == str(new_video_id),
    }


def create_ad(source, copied_adset_id, creative_id, suffix):
    account_id = str(source["adset"]["account_id"])

    payload = {
        "name": f"{source['ad'].get('name') or 'ad'}{suffix}",
        "adset_id": copied_adset_id,
        "creative": {"creative_id": creative_id},
        "status": "PAUSED",
    }

    if source["ad"].get("conversion_domain"):
        payload["conversion_domain"] = deepcopy(source["ad"]["conversion_domain"])

    response = graph_request(
        "POST",
        f"act_{account_id}/ads",
        payload,
        stage="create_new_ad",
    )

    ad_id = str(response.get("id") or "")
    if not ad_id:
        raise RuntimeError(f"Ad create returned no id: {response}")

    PARTIAL["new_ad_id"] = ad_id

    return ad_id, payload


def poll_ad(ad_id):
    snapshots = []

    for delay in (15, 45, 90):
        time.sleep(delay)

        node = get_node(
            ad_id,
            AD_FIELDS,
            stage="poll_new_ad",
        )

        snapshots.append(node)

        if node.get("issues_info") or node.get("failed_delivery_checks"):
            break

    return snapshots[-1], snapshots


def run():
    if not ACCESS_TOKEN:
        raise RuntimeError("FB_SCALER_ACCESS_TOKEN missing")
    if not TEST_ADSET_ID:
        raise RuntimeError("TEST_ADSET_ID missing")

    diag = api_identity()
    send_identity(diag)
    source = get_source_objects(TEST_ADSET_ID)

    # 1. Duplicate the AdSet in the same campaign.
    suffix = f" [PYTEST-V26.6 {datetime.now(POLAND_TZ).strftime('%Y%m%d-%H%M%S')}]"
    copied_adset_id = copy_adset(source["adset"], suffix)

    # 2. Duplicate the Ad through Meta's native /{ad_id}/copies endpoint.
    # No creative parameters, object_story_id, image_url or image_hash are
    # supplied. This is the API equivalent of Ads Manager's Duplicate action.
    new_ad_id, native_copy_payload, native_copy_response = copy_ad_natively(
        source["ad"],
        copied_adset_id,
        suffix,
    )
    final_ad, ad_poll = poll_ad(new_ad_id)

    copied_adset = get_node(
        copied_adset_id,
        ADSET_FIELDS,
        stage="audit_copied_adset",
    )

    copied_creative_id = str((final_ad.get("creative") or {}).get("id") or "")
    if not copied_creative_id:
        raise RuntimeError("Native copied Ad has no creative id")
    copied_creative = get_node(
        copied_creative_id,
        CREATIVE_FIELDS,
        stage="audit_native_copied_creative",
    )

    source_pixel = (source["adset"].get("promoted_object") or {}).get("pixel_id")
    copy_pixel = (copied_adset.get("promoted_object") or {}).get("pixel_id")

    adset_raw_differences, adset_effective_differences, adset_warnings = compare_adset_fidelity(
        source["adset"],
        copied_adset,
    )
    creative_differences = compare_fields(
        source["creative"],
        copied_creative,
        ["object_story_spec", "url_tags", "video_id", "contextual_multi_ads"],
    )
    ad_delivery_differences = compare_fields(
        source["ad"],
        final_ad,
        ["tracking_specs", "conversion_specs", "conversion_domain"],
    )
    ad_structure = {
        "new_ad_id_differs_from_source": str(new_ad_id) != str(source["ad"]["id"]),
        "new_adset_id_differs_from_source": str(copied_adset_id) != str(source["adset"]["id"]),
        "new_ad_inside_new_adset": str(final_ad.get("adset_id") or "") == str(copied_adset_id),
        "copied_ad_has_creative": bool(copied_creative_id),
    }

    issues = final_ad.get("issues_info") or []
    failed = final_ad.get("failed_delivery_checks") or []

    result = {
        "version": TEST_VERSION,
        "build_id": BUILD_ID,
        "mode": "NATIVE_AD_COPIES_API",
        "source_adset_id": TEST_ADSET_ID,
        "account_id": source["adset"]["account_id"],
        "page_id": source["page_id"],

        "source_ad_id": source["ad"]["id"],
        "source_creative_id": source["creative"]["id"],
        "source_root_video_id": source["root_video_id"],
        "source_story_video_id": source["story_video_id"],

        "copied_adset_id": copied_adset_id,
        "new_ad_id": new_ad_id,
        "copied_creative_id": copied_creative_id,
        "creative_reused_by_meta": copied_creative_id == str(source["creative"]["id"]),
        "copied_creative": copied_creative,
        "final_ad": final_ad,
        "ad_poll": ad_poll,

        "native_ad_copy_payload": native_copy_payload,
        "native_ad_copy_response": native_copy_response,
        "creative_parameters_passed": False,
        "object_story_id_passed": False,
        "thumbnail_fields_passed": False,
        "ads_manager_thumbnail_label_expected": "VERIFY_AUTO_ON_SCREEN",

        "adset_fidelity_fields": ADSET_FIDELITY_FIELDS,
        "adset_raw_differences": adset_raw_differences,
        "adset_effective_differences": adset_effective_differences,
        "adset_normalization_warnings": adset_warnings,
        "adset_fidelity_match": not adset_effective_differences,
        "creative_differences": creative_differences,
        "creative_fidelity_match": not creative_differences,
        "ad_delivery_differences": ad_delivery_differences,
        "ad_delivery_fidelity_match": not ad_delivery_differences,
        "ad_structure": ad_structure,

        "pixel_source": source_pixel,
        "pixel_copy": copy_pixel,
        "pixel_match": source_pixel == copy_pixel,

        "issues": issues,
        "failed_delivery_checks": failed,
        "cta_url_preservation_tested": True,
        "true_duplicate_ok": (
            not adset_effective_differences
            and all(ad_structure.values())
            and not creative_differences
            and not ad_delivery_differences
            and source_pixel == copy_pixel
        ),
        "publish_probe_ok": (
            not issues
            and not failed
            and source_pixel == copy_pixel
            and not adset_effective_differences
            and all(ad_structure.values())
            and not creative_differences
            and not ad_delivery_differences
        ),
    }

    return diag, result


def summary(result):
    lines = [
        "🧪 <b>Duplicate test v26.6 • NATIVE AD COPIES API</b>",
        f"Build: <code>{esc(BUILD_ID)}</code>",
        f"Account: <code>{esc(result['account_id'])}</code>",
        f"Page: <code>{esc(result['page_id'])}</code>",
        "",
        f"Source Adset: <code>{esc(result['source_adset_id'])}</code>",
        f"Source Ad: <code>{esc(result['source_ad_id'])}</code>",
        f"Source Creative: <code>{esc(result['source_creative_id'])}</code>",
        f"Source root video: <code>{esc(result['source_root_video_id'])}</code>",
        f"Source story video: <code>{esc(result['source_story_video_id'])}</code>",
        "",
        f"Copy Adset: <code>{esc(result['copied_adset_id'])}</code>",
        f"NEW Ad: <code>{esc(result['new_ad_id'])}</code> (PAUSED)",
        f"Copied Creative: <code>{esc(result['copied_creative_id'])}</code>",
        "Creative relationship: "
        f"<b>{'REUSED BY META' if result['creative_reused_by_meta'] else 'NEW CREATIVE FROM NATIVE COPY'}</b>",
        f"Ad inside NEW Adset: {'✅' if result['ad_structure']['new_ad_inside_new_adset'] else '❌'}",
        f"New Ad ID: {'✅' if result['ad_structure']['new_ad_id_differs_from_source'] else '❌'}",
        "Ad copy endpoint: <b>POST /{source_ad_id}/copies</b>",
        "Creative parameters passed: <b>NO</b>",
        "object_story_id passed: <b>NO</b>",
        "image_url/image_hash passed: <b>NO</b>",
        "Ads Manager thumbnail label: <b>VERIFY AUTO ON SCREEN</b>",
        "",
        f"Adset settings fidelity: {'✅' if result['adset_fidelity_match'] else '❌'}",
        f"Creative fidelity: {'✅' if result['creative_fidelity_match'] else '❌'}",
        f"Tracking + conversion fidelity: {'✅' if result['ad_delivery_fidelity_match'] else '❌'}",
        f"Pixel: {esc(result['pixel_source'])} → {esc(result['pixel_copy'])} "
        f"{'✅' if result['pixel_match'] else '❌'}",
        f"Post-processing issues: {len(result['issues'])}",
        f"Failed delivery checks: {len(result['failed_delivery_checks'])}",
        "",
        f"Native duplicate v26.6: {'✅ PASS' if result['true_duplicate_ok'] else '❌ FAIL'}",
        f"Publish probe v26.6: {'✅ PASS' if result['publish_probe_ok'] else '❌ FAIL'}",
    ]

    if result["issues"]:
        lines.append(
            "issues_info: "
            + esc(json.dumps(result["issues"], ensure_ascii=False)[:1200])
        )

    if result["adset_raw_differences"]:
        lines.append(
            "Adset raw differences: "
            + esc(json.dumps(result["adset_raw_differences"], ensure_ascii=False)[:1200])
        )

    if result["adset_normalization_warnings"]:
        lines.append(
            "Adset normalization warning: "
            + esc(json.dumps(result["adset_normalization_warnings"], ensure_ascii=False)[:1200])
        )

    if result["creative_differences"]:
        lines.append(
            "Creative differences: "
            + esc(json.dumps(result["creative_differences"], ensure_ascii=False)[:1200])
        )

    if result["ad_delivery_differences"]:
        lines.append(
            "Ad delivery differences: "
            + esc(json.dumps(result["ad_delivery_differences"], ensure_ascii=False)[:1200])
        )

    return "\n".join(lines)


def error_message(exc):
    stage = getattr(exc, "stage", None)
    return (
        "❌ <b>Duplicate test v26.6 error</b>\n"
        f"Build: <code>{esc(BUILD_ID)}</code>\n"
        f"Source Adset: <code>{esc(TEST_ADSET_ID)}</code>\n"
        f"Stage: <b>{esc(stage or 'python')}</b>\n"
        f"Partial: {esc(json.dumps(PARTIAL, ensure_ascii=False)[:1800])}\n"
        f"{esc(str(exc))}"
    )


def main():
    report = {
        "version": TEST_VERSION,
        "build_id": BUILD_ID,
        "mode": "NATIVE_AD_COPIES_API",
        "source_adset_id": TEST_ADSET_ID,
        "result": None,
        "error": None,
    }

    try:
        diag, result = run()
        report["api_access"] = diag
        report["result"] = result
        send_telegram(summary(result))
        code = 0
    except Exception as exc:
        cleanup = cleanup_partial_test_objects()
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "stage": getattr(exc, "stage", None),
            "partial": deepcopy(PARTIAL),
            "meta": getattr(exc, "info", None),
            "cleanup": cleanup,
        }
        send_telegram(error_message(exc))
        code = 1

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Report saved: {REPORT_FILE}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
