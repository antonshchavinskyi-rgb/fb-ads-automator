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

from fb_config import POLAND_TZ

API_VER = "v26.0"

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TEST_ADSET_ID = (os.environ.get("TEST_ADSET_ID") or "").strip()

REPORT_FILE = "duplicate_test_v26_page_video_object_story_report.json"

PARTIAL = {}

ADSET_FIELDS = [
    "id", "name", "account_id", "campaign_id", "status", "effective_status",
    "bid_strategy", "bid_amount", "billing_event", "optimization_goal",
    "daily_budget", "lifetime_budget", "attribution_spec", "promoted_object",
    "destination_type", "pacing_type", "targeting", "is_dynamic_creative",
    "recurring_budget_semantics",
]

AD_FIELDS = [
    "id", "name", "status", "configured_status", "effective_status",
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
        else:
            req = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(encoded).encode("utf-8"),
                method="POST",
            )

        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise MetaError(stage, e.code, decode_meta_error(body))


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


def read_source_video_with_url(source):
    """
    Prefer root creative.video_id, then story video_id.
    We need a real source URL so the Page Videos endpoint can create a NEW video.
    """
    attempts = []

    for label, video_id in (
        ("root_creative_video_id", source.get("root_video_id")),
        ("story_video_id", source.get("story_video_id")),
    ):
        if not video_id:
            continue

        try:
            node = get_node(
                video_id,
                VIDEO_FIELDS,
                stage=f"read_source_video:{label}",
            )
            attempts.append({"label": label, "video_id": video_id, "node": node})

            if node.get("source"):
                return {
                    "label": label,
                    "video_id": video_id,
                    "node": node,
                    "source_url": node["source"],
                    "attempts": attempts,
                }
        except MetaError as e:
            attempts.append({
                "label": label,
                "video_id": video_id,
                "error": str(e),
            })

    raise RuntimeError(
        "Could not read video source URL from root or story video. "
        + json.dumps(attempts, ensure_ascii=False)[:1800]
    )


def create_new_page_video(source, source_video, page_access):
    vd = source["video_data"]

    payload = {
        "file_url": source_video["source_url"],
        "published": False,
        "unpublished_content_type": "ADS_POST",
    }

    # Keep only text fields here. CTA is intentionally NOT sent in the first
    # diagnostic run, to isolate Page-video + thumbnail + object_story_id.
    if vd.get("message"):
        payload["description"] = vd.get("message")

    if vd.get("title"):
        payload["title"] = vd.get("title")

    response = graph_request(
        "POST",
        f"{source['page_id']}/videos",
        payload,
        token=page_access["token"],
        stage="create_unpublished_page_video",
    )

    new_video_id = str(response.get("id") or "")
    if not new_video_id:
        raise RuntimeError(f"Page video create returned no id: {response}")

    PARTIAL["new_page_video_id"] = new_video_id
    PARTIAL["page_video_create_response"] = response

    return new_video_id, payload, response


def wait_for_video_and_thumbnails(video_id, page_token):
    snapshots = []

    for delay in (10, 15, 30, 45, 60, 60):
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

        snapshots.append({"video": node, "thumbnails": thumbs})

        real_thumbs = [
            x for x in thumbs
            if isinstance(x, dict) and x.get("id") and x.get("uri")
        ]

        if real_thumbs:
            return node, real_thumbs, snapshots

    raise RuntimeError("Timed out waiting for NEW Page video thumbnails")


def choose_thumbnail(thumbs):
    preferred = next((x for x in thumbs if x.get("is_preferred")), None)
    selected = preferred or thumbs[0]

    return {
        "id": str(selected.get("id")),
        "uri": selected.get("uri"),
        "is_preferred_before": bool(selected.get("is_preferred")),
        "width": selected.get("width"),
        "height": selected.get("height"),
        "selection_reason": "existing_preferred" if preferred else "first_generated",
    }


def set_preferred_thumbnail(video_id, thumb, page_token):
    response = graph_request(
        "POST",
        video_id,
        {"preferred_thumbnail_id": thumb["id"]},
        token=page_token,
        stage="set_preferred_thumbnail",
    )

    time.sleep(5)

    thumbs_after = graph_get_all(
        f"{video_id}/thumbnails",
        {
            "fields": "id,uri,is_preferred,width,height,scale",
            "limit": 50,
        },
        token=page_token,
        stage="verify_preferred_thumbnail",
    )

    selected_after = next(
        (x for x in thumbs_after if str(x.get("id")) == str(thumb["id"])),
        None,
    )

    return {
        "post_response": response,
        "selected_after": selected_after,
        "preferred_after": bool(selected_after and selected_after.get("is_preferred")),
        "all_after": thumbs_after,
    }


def wait_for_post_id(video_id, page_token):
    snapshots = []

    for delay in (0, 5, 10, 20, 30):
        if delay:
            time.sleep(delay)

        node = get_node(
            video_id,
            VIDEO_FIELDS,
            token=page_token,
            stage="wait_for_post_id",
        )

        snapshots.append(node)

        if node.get("post_id"):
            return node, snapshots

    raise RuntimeError("NEW Page video never returned post_id")


def normalize_object_story_id(page_id, post_id):
    value = str(post_id)
    return value if "_" in value else f"{page_id}_{value}"


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


def create_creative_from_object_story(source, object_story_id, suffix):
    account_id = str(source["adset"]["account_id"])

    payload = {
        "name": f"{source['creative'].get('name') or 'creative'}{suffix}",
        "object_story_id": object_story_id,
        "contextual_multi_ads": {"enroll_status": "OPT_OUT"},
    }

    if source["creative"].get("url_tags"):
        payload["url_tags"] = source["creative"]["url_tags"]

    response = graph_request(
        "POST",
        f"act_{account_id}/adcreatives",
        payload,
        stage="create_creative_via_object_story_id",
    )

    creative_id = str(response.get("id") or "")
    if not creative_id:
        raise RuntimeError(f"Creative create returned no id: {response}")

    PARTIAL["new_creative_id"] = creative_id

    return creative_id, payload


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

    page_access = resolve_page_token(source["page_id"])

    source_video = read_source_video_with_url(source)

    # 1. NEW unpublished Page video.
    new_page_video_id, page_video_payload, page_video_response = create_new_page_video(
        source,
        source_video,
        page_access,
    )

    # 2. Wait for generated thumbnails.
    new_video_node, thumbs, video_poll = wait_for_video_and_thumbnails(
        new_page_video_id,
        page_access["token"],
    )

    # 3. Explicitly select preferred thumbnail on the NEW Page video.
    chosen_thumb = choose_thumbnail(thumbs)
    thumb_update = set_preferred_thumbnail(
        new_page_video_id,
        chosen_thumb,
        page_access["token"],
    )

    if not thumb_update["preferred_after"]:
        raise RuntimeError(
            "preferred_thumbnail_id was sent, but verification says selected thumbnail is not preferred"
        )

    # 4. Wait for real Page post id.
    video_with_post, post_poll = wait_for_post_id(
        new_page_video_id,
        page_access["token"],
    )

    object_story_id = normalize_object_story_id(
        source["page_id"],
        video_with_post["post_id"],
    )
    PARTIAL["object_story_id"] = object_story_id

    # 5. Only now duplicate the AdSet.
    suffix = f" [PYTEST-V26 {datetime.now(POLAND_TZ).strftime('%Y%m%d-%H%M%S')}]"
    copied_adset_id = copy_adset(source["adset"], suffix)

    # 6. NEW Creative from already-prepared Page post.
    new_creative_id, creative_payload = create_creative_from_object_story(
        source,
        object_story_id,
        suffix,
    )

    new_creative = get_node(
        new_creative_id,
        CREATIVE_FIELDS,
        stage="audit_new_creative",
    )

    # 7. NEW PAUSED Ad.
    new_ad_id, ad_payload = create_ad(
        source,
        copied_adset_id,
        new_creative_id,
        suffix,
    )

    final_ad, ad_poll = poll_ad(new_ad_id)

    copied_adset = get_node(
        copied_adset_id,
        ADSET_FIELDS,
        stage="audit_copied_adset",
    )

    source_pixel = (source["adset"].get("promoted_object") or {}).get("pixel_id")
    copy_pixel = (copied_adset.get("promoted_object") or {}).get("pixel_id")

    issues = final_ad.get("issues_info") or []
    failed = final_ad.get("failed_delivery_checks") or []

    result = {
        "version": "v26",
        "mode": "PAGE_VIDEO_TO_OBJECT_STORY_ID",
        "source_adset_id": TEST_ADSET_ID,
        "account_id": source["adset"]["account_id"],
        "page_id": source["page_id"],
        "page_token_source": page_access["source"],
        "page_tasks": page_access.get("tasks"),

        "source_ad_id": source["ad"]["id"],
        "source_creative_id": source["creative"]["id"],
        "source_root_video_id": source["root_video_id"],
        "source_story_video_id": source["story_video_id"],
        "source_video_resolution": source_video,

        "new_page_video_id": new_page_video_id,
        "page_video_create_payload": {
            **page_video_payload,
            "file_url": "<REDACTED_SOURCE_URL>",
        },
        "page_video_create_response": page_video_response,
        "video_poll": video_poll,

        "chosen_thumbnail": chosen_thumb,
        "thumbnail_update": thumb_update,
        "video_with_post": video_with_post,
        "post_poll": post_poll,

        "object_story_id": object_story_id,

        "copied_adset_id": copied_adset_id,
        "new_creative_id": new_creative_id,
        "new_creative": new_creative,
        "new_ad_id": new_ad_id,
        "final_ad": final_ad,
        "ad_poll": ad_poll,

        "creative_payload": creative_payload,
        "ad_payload": ad_payload,

        "pixel_source": source_pixel,
        "pixel_copy": copy_pixel,
        "pixel_match": source_pixel == copy_pixel,

        "issues": issues,
        "failed_delivery_checks": failed,
        "publish_probe_ok": (
            not issues
            and not failed
            and source_pixel == copy_pixel
        ),
    }

    return diag, result


def summary(result):
    lines = [
        "🧪 <b>Duplicate test v26 • PAGE VIDEO → OBJECT_STORY_ID</b>",
        f"Account: <code>{esc(result['account_id'])}</code>",
        f"Page: <code>{esc(result['page_id'])}</code>",
        f"Page token source: {esc(result['page_token_source'])}",
        "",
        f"Source Adset: <code>{esc(result['source_adset_id'])}</code>",
        f"Source Ad: <code>{esc(result['source_ad_id'])}</code>",
        f"Source Creative: <code>{esc(result['source_creative_id'])}</code>",
        f"Source root video: <code>{esc(result['source_root_video_id'])}</code>",
        f"Source story video: <code>{esc(result['source_story_video_id'])}</code>",
        "",
        f"NEW Page Video: <code>{esc(result['new_page_video_id'])}</code> ✅",
        f"Preferred thumbnail: <code>{esc(result['chosen_thumbnail']['id'])}</code>",
        f"Preferred verified: {'✅ YES' if result['thumbnail_update']['preferred_after'] else '❌ NO'}",
        f"Object Story ID: <code>{esc(result['object_story_id'])}</code>",
        "",
        f"Copy Adset: <code>{esc(result['copied_adset_id'])}</code>",
        f"NEW Creative: <code>{esc(result['new_creative_id'])}</code>",
        f"Creative root video_id: <code>{esc(result['new_creative'].get('video_id'))}</code>",
        f"NEW Ad: <code>{esc(result['new_ad_id'])}</code> (PAUSED)",
        "",
        f"Pixel: {esc(result['pixel_source'])} → {esc(result['pixel_copy'])} "
        f"{'✅' if result['pixel_match'] else '❌'}",
        f"Post-processing issues: {len(result['issues'])}",
        f"Failed delivery checks: {len(result['failed_delivery_checks'])}",
        "",
        f"Publish probe v26: {'✅ PASS' if result['publish_probe_ok'] else '❌ FAIL'}",
    ]

    if result["issues"]:
        lines.append(
            "issues_info: "
            + esc(json.dumps(result["issues"], ensure_ascii=False)[:1200])
        )

    return "\n".join(lines)


def error_message(exc):
    stage = getattr(exc, "stage", None)
    return (
        "❌ <b>Duplicate test v26 error</b>\n"
        f"Source Adset: <code>{esc(TEST_ADSET_ID)}</code>\n"
        f"Stage: <b>{esc(stage or 'python')}</b>\n"
        f"Partial: {esc(json.dumps(PARTIAL, ensure_ascii=False)[:1800])}\n"
        f"{esc(str(exc))}"
    )


def main():
    report = {
        "version": "v26",
        "mode": "PAGE_VIDEO_TO_OBJECT_STORY_ID",
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
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "stage": getattr(exc, "stage", None),
            "partial": deepcopy(PARTIAL),
            "meta": getattr(exc, "info", None),
        }
        send_telegram(error_message(exc))
        code = 1

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Report saved: {REPORT_FILE}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
