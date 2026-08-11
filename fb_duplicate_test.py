import os
import sys
import json
import html
import urllib.request
import urllib.parse
import urllib.error

API_VER = "v26.0"

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TEST_ADSET_ID = (os.environ.get("TEST_ADSET_ID") or "").strip()

REPORT_FILE = "duplicate_test_v18_asset_feed_diag_report.json"

ADSET_FIELDS = [
    "id", "name", "account_id", "campaign_id", "status", "effective_status",
    "targeting", "promoted_object", "is_dynamic_creative",
]

AD_FIELDS = [
    "id", "name", "status", "effective_status", "adset_id", "campaign_id", "creative",
]

CREATIVE_FIELDS = [
    "id", "name", "account_id",
    "object_story_id", "effective_object_story_id",
    "object_story_spec",
    "asset_feed_spec",
    "platform_customizations",
    "degrees_of_freedom_spec",
    "format_transformation_spec",
    "creative_sourcing_spec",
    "generative_asset_spec",
    "destination_spec",
    "contextual_multi_ads",
    "url_tags",
    "image_hash", "image_url", "thumbnail_url", "video_id",
]


def esc(v):
    return html.escape(str(v), quote=False)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; skip.", flush=True)
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
            "user_title": err.get("error_user_title"),
            "user_msg": err.get("error_user_msg"),
            "fbtrace_id": err.get("fbtrace_id"),
            "raw": parsed,
        }
    except Exception:
        return {"message": body, "raw": body}


class MetaError(RuntimeError):
    def __init__(self, http_status, info, stage):
        self.http_status = http_status
        self.info = info
        self.stage = stage
        msg = f"{stage}: Meta HTTP {http_status}: {info.get('message')}"
        if info.get("code") is not None:
            msg += f" (code={info.get('code')}, subcode={info.get('subcode')})"
        super().__init__(msg)


def serialize(v):
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


def graph_get(path, params=None, stage="GET"):
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    q = urllib.parse.urlencode({k: serialize(v) for k, v in params.items() if v is not None})
    url = f"https://graph.facebook.com/{API_VER}/{path.lstrip('/')}?{q}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        info = decode_meta_error(e.read().decode("utf-8", errors="replace"))
        raise MetaError(e.code, info, stage)


def graph_get_all(path, params=None, stage="GET_ALL"):
    params = dict(params or {})
    params["access_token"] = ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VER}/{path.lstrip('/')}?" + urllib.parse.urlencode(
        {k: serialize(v) for k, v in params.items() if v is not None}
    )
    out = []
    while url:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            info = decode_meta_error(e.read().decode("utf-8", errors="replace"))
            raise MetaError(e.code, info, stage)
        out.extend(payload.get("data", []))
        url = payload.get("paging", {}).get("next")
    return out


def get_node(node_id, fields, stage):
    return graph_get(str(node_id), {"fields": ",".join(fields)}, stage)


def labels_of(item):
    labels = []
    for x in (item or {}).get("adlabels") or []:
        if isinstance(x, dict) and x.get("name"):
            labels.append(str(x["name"]))
    return labels


def summarize_asset_feed(asset_feed):
    if not isinstance(asset_feed, dict) or not asset_feed:
        return {
            "present": False,
            "counts": {},
            "videos": [],
            "images": [],
            "rules": [],
            "raw": asset_feed,
        }

    videos = []
    for i, item in enumerate(asset_feed.get("videos") or []):
        if isinstance(item, dict):
            videos.append({
                "index": i,
                "video_id": item.get("video_id"),
                "thumbnail_url": item.get("thumbnail_url"),
                "labels": labels_of(item),
                "raw": item,
            })

    images = []
    for i, item in enumerate(asset_feed.get("images") or []):
        if isinstance(item, dict):
            images.append({
                "index": i,
                "hash": item.get("hash"),
                "url": item.get("url"),
                "url_tags": item.get("url_tags"),
                "labels": labels_of(item),
                "raw": item,
            })

    rules = []
    for i, rule in enumerate(asset_feed.get("asset_customization_rules") or []):
        if not isinstance(rule, dict):
            continue
        rules.append({
            "index": i,
            "customization_spec": rule.get("customization_spec"),
            "video_label": rule.get("video_label"),
            "image_label": rule.get("image_label"),
            "body_label": rule.get("body_label"),
            "title_label": rule.get("title_label"),
            "description_label": rule.get("description_label"),
            "link_url_label": rule.get("link_url_label"),
            "raw": rule,
        })

    count_keys = [
        "videos", "images", "bodies", "titles", "descriptions", "link_urls",
        "call_to_action_types", "asset_customization_rules",
    ]
    counts = {}
    for k in count_keys:
        v = asset_feed.get(k)
        if isinstance(v, list):
            counts[k] = len(v)

    return {
        "present": True,
        "optimization_type": asset_feed.get("optimization_type"),
        "ad_formats": asset_feed.get("ad_formats"),
        "counts": counts,
        "videos": videos,
        "images": images,
        "rules": rules,
        "raw": asset_feed,
    }


def video_story_summary(creative):
    oss = (creative or {}).get("object_story_spec") or {}
    vd = oss.get("video_data") or {}
    if not vd:
        return None
    return {
        "page_id": oss.get("page_id"),
        "video_id": vd.get("video_id") or creative.get("video_id"),
        "image_hash": vd.get("image_hash") or creative.get("image_hash"),
        "image_url": vd.get("image_url") or creative.get("image_url"),
        "thumbnail_url": creative.get("thumbnail_url"),
        "message_present": bool(vd.get("message")),
        "title_present": bool(vd.get("title")),
        "link_description_present": bool(vd.get("link_description")),
        "call_to_action": vd.get("call_to_action"),
    }


def telegram_summary(report):
    af = report["asset_feed_summary"]
    story = report.get("video_story_summary") or {}
    lines = [
        "🔎 <b>Creative structure diagnostic v18 — READ ONLY</b>",
        f"Adset: {esc(report['adset'].get('name'))}",
        f"Adset ID: <code>{esc(report['adset'].get('id'))}</code>",
        f"Ad ID: <code>{esc(report['ad'].get('id'))}</code>",
        f"Creative ID: <code>{esc(report['creative'].get('id'))}</code>",
        "",
        f"<b>asset_feed_spec:</b> {'✅ PRESENT' if af.get('present') else '❌ NONE'}",
    ]

    if af.get("present"):
        c = af.get("counts") or {}
        lines.append(
            "Counts: "
            f"videos={c.get('videos',0)}, images={c.get('images',0)}, "
            f"rules={c.get('asset_customization_rules',0)}"
        )
        if af.get("rules"):
            lines.append("<b>Placement customization rules:</b>")
            for r in af["rules"][:12]:
                spec = r.get("customization_spec") or {}
                lines.append(
                    f"#{r['index']}: video_label={esc(r.get('video_label'))} "
                    f"image_label={esc(r.get('image_label'))} "
                    f"placements={esc(spec)}"
                )
        else:
            lines.append("asset_customization_rules: none")

    lines += [
        "",
        "<b>Source video thumbnail refs:</b>",
        f"video_id: <code>{esc(story.get('video_id'))}</code>",
        f"image_hash: <code>{esc(story.get('image_hash'))}</code>",
        f"image_url: {esc(story.get('image_url'))}",
        f"creative.thumbnail_url: {esc(story.get('thumbnail_url'))}",
        "",
        f"platform_customizations: {'✅ PRESENT' if report['creative'].get('platform_customizations') else '❌ NONE'}",
        f"destination_spec: {'✅ PRESENT' if report['creative'].get('destination_spec') else '❌ NONE'}",
        f"degrees_of_freedom_spec: {'✅ PRESENT' if report['creative'].get('degrees_of_freedom_spec') else '❌ NONE'}",
        "",
        "🧪 Жодних змін у Meta не внесено.",
        "Повна структура збережена в JSON artifact.",
    ]
    return "\n".join(lines)


def main():
    if not ACCESS_TOKEN:
        raise SystemExit("FB_SCALER_ACCESS_TOKEN missing")
    if not TEST_ADSET_ID:
        raise SystemExit("TEST_ADSET_ID missing")

    report = {
        "version": "v18",
        "api": API_VER,
        "mode": "READ_ONLY_ASSET_FEED_DIAGNOSTIC",
        "adset": None,
        "ad": None,
        "creative": None,
        "asset_feed_summary": None,
        "video_story_summary": None,
        "notes": [
            "No POST/write calls are made.",
            "This run only inspects whether placement-specific customization is stored in source asset_feed_spec/platform_customizations.",
        ],
    }

    try:
        adset = get_node(TEST_ADSET_ID, ADSET_FIELDS, "source_adset")
        ads = graph_get_all(
            f"{TEST_ADSET_ID}/ads",
            {"fields": ",".join(AD_FIELDS), "limit": 10},
            "source_ads",
        )
        if not ads:
            raise RuntimeError("No source ads found in adset.")
        if len(ads) != 1:
            raise RuntimeError(f"Expected 1 ad in source adset, got {len(ads)}.")

        ad = ads[0]
        creative_id = (ad.get("creative") or {}).get("id")
        if not creative_id:
            raise RuntimeError("Source ad has no creative id.")

        creative = get_node(creative_id, CREATIVE_FIELDS, "source_creative")

        report["adset"] = adset
        report["ad"] = ad
        report["creative"] = creative
        report["asset_feed_summary"] = summarize_asset_feed(creative.get("asset_feed_spec"))
        report["video_story_summary"] = video_story_summary(creative)

        PathLike = __import__("pathlib").Path
        PathLike(REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        send_telegram(telegram_summary(report))
        return 0

    except Exception as e:
        report["error"] = str(e)
        try:
            PathLike = __import__("pathlib").Path
            PathLike(REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        send_telegram(
            "❌ <b>Creative structure diagnostic v18 error</b>\n"
            f"Adset: <code>{esc(TEST_ADSET_ID)}</code>\n"
            f"{esc(e)}"
        )
        print(f"ERROR: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
