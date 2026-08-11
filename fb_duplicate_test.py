import os
import sys
import json
import html
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

API_VER = "v26.0"

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SOURCE_ADSET_ID = (os.environ.get("SOURCE_ADSET_ID") or "").strip()
FIXED_ADSET_ID = (os.environ.get("FIXED_ADSET_ID") or "").strip()
BROKEN_ADSET_ID = (os.environ.get("BROKEN_ADSET_ID") or "").strip()

REPORT_FILE = "duplicate_test_v21_compare_by_adset_report.json"

AD_FIELDS = [
    "id",
    "name",
    "status",
    "effective_status",
    "creative",
    "tracking_specs",
    "conversion_specs",
    "conversion_domain",
    "issues_info",
    "failed_delivery_checks",
    "ad_review_feedback",
]

CREATIVE_REQUIRED = ["id", "name", "object_story_spec"]
CREATIVE_OPTIONAL = [
    "account_id",
    "actor_id",
    "object_story_id",
    "effective_object_story_id",
    "source_facebook_post_id",
    "asset_feed_spec",
    "platform_customizations",
    "portrait_customizations",
    "degrees_of_freedom_spec",
    "destination_spec",
    "media_sourcing_spec",
    "creative_sourcing_spec",
    "format_transformation_spec",
    "generative_asset_spec",
    "interactive_components_spec",
    "contextual_multi_ads",
    "categorization_criteria",
    "category_media_source",
    "image_hash",
    "image_url",
    "thumbnail_id",
    "thumbnail_url",
    "video_id",
    "link_url",
    "object_url",
    "url_tags",
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

def parse_meta_error(body):
    try:
        payload = json.loads(body)
        err = payload.get("error", {})
        return {
            "message": err.get("message", body),
            "code": err.get("code"),
            "subcode": err.get("error_subcode"),
            "raw": payload,
        }
    except Exception:
        return {"message": body, "raw": body}

class GraphError(RuntimeError):
    def __init__(self, status, info, stage):
        self.status = status
        self.info = info
        self.stage = stage
        super().__init__(f"{stage}: HTTP {status}: {info.get('message')}")

def graph_get_url(url, stage):
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise GraphError(e.code, parse_meta_error(body), stage)

def graph_get(node_id, fields, stage):
    params = {
        "fields": ",".join(fields),
        "access_token": ACCESS_TOKEN,
    }
    url = f"https://graph.facebook.com/{API_VER}/{node_id}?" + urllib.parse.urlencode(params)
    return graph_get_url(url, stage)

def graph_get_edge(node_id, edge, fields, stage, limit=10):
    params = {
        "fields": ",".join(fields),
        "limit": limit,
        "access_token": ACCESS_TOKEN,
    }
    url = f"https://graph.facebook.com/{API_VER}/{node_id}/{edge}?" + urllib.parse.urlencode(params)
    payload = graph_get_url(url, stage)
    return payload.get("data", [])

def is_missing_field_error(err):
    if not isinstance(err, GraphError):
        return False
    msg = (err.info.get("message") or "").lower()
    return err.info.get("code") == 100 and (
        "nonexisting field" in msg
        or "non-existing field" in msg
        or "tried accessing" in msg
    )

def fetch_optional_tolerant(node_id, fields, stage_prefix, out, unsupported):
    if not fields:
        return
    try:
        data = graph_get(node_id, fields, stage_prefix)
        for f in fields:
            if f in data:
                out[f] = data[f]
        return
    except GraphError as e:
        if not is_missing_field_error(e):
            raise
        if len(fields) == 1:
            unsupported[fields[0]] = e.info.get("message")
            return
        mid = len(fields) // 2
        fetch_optional_tolerant(node_id, fields[:mid], stage_prefix + "_a", out, unsupported)
        fetch_optional_tolerant(node_id, fields[mid:], stage_prefix + "_b", out, unsupported)

def get_one_ad_from_adset(adset_id, label):
    ads = graph_get_edge(
        adset_id,
        "ads",
        AD_FIELDS,
        f"{label}_adset_ads",
        limit=10,
    )
    if not ads:
        raise RuntimeError(f"{label}: no ads found under adset {adset_id}")
    if len(ads) != 1:
        raise RuntimeError(
            f"{label}: expected exactly 1 ad under adset {adset_id}, got {len(ads)}"
        )

    ad = ads[0]
    creative_id = (ad.get("creative") or {}).get("id")
    if not creative_id:
        raise RuntimeError(
            f"{label}: ad {ad.get('id')} returned via adset edge has no creative id"
        )

    creative = graph_get(
        creative_id,
        CREATIVE_REQUIRED,
        f"{label}_creative_required",
    )
    unsupported = {}
    fetch_optional_tolerant(
        creative_id,
        CREATIVE_OPTIONAL,
        f"{label}_creative_optional",
        creative,
        unsupported,
    )

    return {
        "adset_id": adset_id,
        "ad": ad,
        "creative": creative,
        "unsupported_creative_fields": unsupported,
    }

IGNORE_KEYS = {
    "id",
    "name",
    "status",
    "effective_status",
    "account_id",
    "object_story_id",
    "effective_object_story_id",
    "source_facebook_post_id",
}

def deep_diff(a, b, path=""):
    diffs = []
    if type(a) != type(b):
        diffs.append({"path": path or "$", "a": a, "b": b, "kind": "type/value"})
        return diffs

    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k in IGNORE_KEYS:
                continue
            p = f"{path}.{k}" if path else k
            if k not in a:
                diffs.append({"path": p, "a": "<MISSING>", "b": b[k], "kind": "added"})
            elif k not in b:
                diffs.append({"path": p, "a": a[k], "b": "<MISSING>", "kind": "removed"})
            else:
                diffs.extend(deep_diff(a[k], b[k], p))
    elif isinstance(a, list):
        n = max(len(a), len(b))
        for i in range(n):
            p = f"{path}[{i}]"
            if i >= len(a):
                diffs.append({"path": p, "a": "<MISSING>", "b": b[i], "kind": "added"})
            elif i >= len(b):
                diffs.append({"path": p, "a": a[i], "b": "<MISSING>", "kind": "removed"})
            else:
                diffs.extend(deep_diff(a[i], b[i], p))
    else:
        if a != b:
            diffs.append({"path": path or "$", "a": a, "b": b, "kind": "value"})
    return diffs

FOCUS_NEEDLES = (
    "thumbnail",
    "image_hash",
    "image_url",
    "video_id",
    "asset_feed_spec",
    "platform_customizations",
    "portrait_customizations",
    "degrees_of_freedom_spec",
    "destination_spec",
    "media_sourcing_spec",
    "creative_sourcing_spec",
    "format_transformation_spec",
    "generative_asset_spec",
    "contextual_multi_ads",
    "object_story_spec",
    "tracking_specs",
    "conversion_specs",
    "conversion_domain",
    "issues_info",
    "failed_delivery_checks",
    "ad_review_feedback",
)

def focus(diffs):
    return [d for d in diffs if any(n in d["path"] for n in FOCUS_NEEDLES)]

def compare(left, right):
    all_diffs = (
        deep_diff(left["ad"], right["ad"], "ad")
        + deep_diff(left["creative"], right["creative"], "creative")
    )
    return {
        "all_diffs": all_diffs,
        "focused_diffs": focus(all_diffs),
    }

def short(v, limit=170):
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= limit else s[:limit - 1] + "…"

def pair_text(title, pair):
    focused = pair["focused_diffs"]
    lines = [
        f"🔬 <b>{esc(title)}</b>",
        f"All diffs: <b>{len(pair['all_diffs'])}</b>",
        f"Focused diffs: <b>{len(focused)}</b>",
    ]
    if focused:
        for d in focused[:16]:
            lines.append(
                f"• <code>{esc(d['path'])}</code>\n"
                f"  A: {esc(short(d['a']))}\n"
                f"  B: {esc(short(d['b']))}"
            )
        if len(focused) > 16:
            lines.append(f"…ще {len(focused)-16} у JSON artifact.")
    else:
        lines.append("✅ Ключових API-visible відмінностей не знайдено.")
    return "\n".join(lines)

def item_header(label, item):
    return (
        f"{label}: Adset <code>{esc(item['adset_id'])}</code> → "
        f"Ad <code>{esc(item['ad'].get('id'))}</code> → "
        f"Creative <code>{esc(item['creative'].get('id'))}</code>"
    )

def main():
    if not ACCESS_TOKEN:
        raise SystemExit("FB_SCALER_ACCESS_TOKEN missing")
    if not SOURCE_ADSET_ID or not FIXED_ADSET_ID:
        raise SystemExit("SOURCE_ADSET_ID and FIXED_ADSET_ID are required")

    report = {
        "version": "v21",
        "api": API_VER,
        "mode": "READ_ONLY_COMPARE_BY_ADSET_EDGE",
        "source_adset_id": SOURCE_ADSET_ID,
        "fixed_adset_id": FIXED_ADSET_ID,
        "broken_adset_id": BROKEN_ADSET_ID or None,
        "notes": [
            "READ ONLY: no POST/write calls.",
            "Ads are resolved via /{adset_id}/ads because that exact route already worked in v18.",
            "This avoids direct /{ad_id}?fields=creative ambiguity and removes manual Ad-vs-Adset ID confusion.",
        ],
    }

    try:
        report["source"] = get_one_ad_from_adset(SOURCE_ADSET_ID, "source")
        report["fixed"] = get_one_ad_from_adset(FIXED_ADSET_ID, "fixed")
        report["source_vs_fixed"] = compare(report["source"], report["fixed"])

        if BROKEN_ADSET_ID:
            report["broken"] = get_one_ad_from_adset(BROKEN_ADSET_ID, "broken")
            report["broken_vs_fixed"] = compare(report["broken"], report["fixed"])

        Path(REPORT_FILE).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        parts = [
            "🔎 <b>Creative compare v21 — READ ONLY / BY ADSET</b>",
            item_header("SOURCE", report["source"]),
            item_header("FIXED", report["fixed"]),
            "",
            pair_text("SOURCE → FIXED", report["source_vs_fixed"]),
        ]
        if report.get("broken_vs_fixed"):
            parts += [
                "",
                item_header("BROKEN", report["broken"]),
                pair_text("BROKEN → FIXED (найважливіше)", report["broken_vs_fixed"]),
            ]
        parts += [
            "",
            "🧪 Жодних змін у Meta не внесено.",
            "Повний diff — у JSON artifact.",
        ]
        send_telegram("\n".join(parts))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0

    except Exception as e:
        report["error"] = str(e)
        Path(REPORT_FILE).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        send_telegram("❌ <b>Creative compare v21 error</b>\n" + esc(e))
        print(f"ERROR: {e}", flush=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
