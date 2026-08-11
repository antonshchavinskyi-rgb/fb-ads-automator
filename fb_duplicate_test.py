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

SOURCE_AD_ID = (os.environ.get("SOURCE_AD_ID") or "").strip()
FIXED_AD_ID = (os.environ.get("FIXED_AD_ID") or "").strip()
BROKEN_AD_ID = (os.environ.get("BROKEN_AD_ID") or "").strip()

REPORT_FILE = "duplicate_test_v19_compare_report.json"

AD_FIELDS = [
    "id","name","account_id","adset_id","campaign_id","status","effective_status",
    "creative","tracking_specs","conversion_specs","conversion_domain",
    "issues_info","failed_delivery_checks","ad_review_feedback",
]

CREATIVE_FIELDS = [
    "id","name","account_id","actor_id","status",
    "object_story_id","effective_object_story_id","source_facebook_post_id",
    "object_story_spec",
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
    "image_hash","image_url","thumbnail_id","thumbnail_url","video_id",
    "link_url","object_url","url_tags",
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

def graph_get(node_id, fields, stage):
    params = {
        "fields": ",".join(fields),
        "access_token": ACCESS_TOKEN,
    }
    url = f"https://graph.facebook.com/{API_VER}/{node_id}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{stage}: HTTP {e.code}: {body}")

def fetch_ad_and_creative(ad_id, label):
    ad = graph_get(ad_id, AD_FIELDS, f"{label}_ad")
    creative_id = (ad.get("creative") or {}).get("id")
    if not creative_id:
        raise RuntimeError(f"{label}: ad has no creative id")
    creative = graph_get(creative_id, CREATIVE_FIELDS, f"{label}_creative")
    return {"ad": ad, "creative": creative}

IGNORE_PATH_PARTS = {
    "id", "name", "account_id", "adset_id", "campaign_id",
    "status", "effective_status", "effective_object_story_id",
    "object_story_id", "source_facebook_post_id",
}

def deep_diff(a, b, path=""):
    diffs = []
    if type(a) != type(b):
        diffs.append({"path": path or "$", "a": a, "b": b, "kind": "type/value"})
        return diffs
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k in IGNORE_PATH_PARTS:
                continue
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

def focus(diffs):
    needles = (
        "thumbnail", "image_hash", "image_url", "video_id",
        "asset_feed_spec", "platform_customizations", "portrait_customizations",
        "degrees_of_freedom_spec", "destination_spec",
        "media_sourcing_spec", "creative_sourcing_spec",
        "format_transformation_spec", "generative_asset_spec",
        "contextual_multi_ads", "object_story_spec",
        "tracking_specs", "conversion_specs", "conversion_domain",
    )
    return [d for d in diffs if any(n in d["path"] for n in needles)]

def short(v, limit=180):
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= limit else s[:limit-1] + "…"

def telegram_pair(title, pair):
    focused = pair["focused_diffs"]
    lines = [
        f"🔬 <b>{esc(title)}</b>",
        f"All structural diffs: <b>{len(pair['all_diffs'])}</b>",
        f"Focused creative/tracking diffs: <b>{len(focused)}</b>",
    ]
    if focused:
        lines.append("")
        for d in focused[:18]:
            lines.append(
                f"• <code>{esc(d['path'])}</code>\n"
                f"  A: {esc(short(d['a']))}\n"
                f"  B: {esc(short(d['b']))}"
            )
        if len(focused) > 18:
            lines.append(f"…ще {len(focused)-18} — дивись JSON artifact.")
    else:
        lines.append("✅ У ключових полях відмінностей не знайдено.")
    return "\n".join(lines)

def compare(left, right):
    all_diffs = (
        deep_diff(left["ad"], right["ad"], "ad") +
        deep_diff(left["creative"], right["creative"], "creative")
    )
    return {"all_diffs": all_diffs, "focused_diffs": focus(all_diffs)}

def main():
    if not ACCESS_TOKEN:
        raise SystemExit("FB_SCALER_ACCESS_TOKEN missing")
    if not SOURCE_AD_ID or not FIXED_AD_ID:
        raise SystemExit("SOURCE_AD_ID and FIXED_AD_ID are required")

    report = {
        "version": "v19",
        "api": API_VER,
        "mode": "READ_ONLY_CREATIVE_COMPARE",
        "source_ad_id": SOURCE_AD_ID,
        "fixed_ad_id": FIXED_AD_ID,
        "broken_ad_id": BROKEN_AD_ID or None,
        "notes": [
            "READ ONLY: no POST/write calls.",
            "Goal: identify exact API-visible fields changed by manual thumbnail/save/publish action.",
        ],
    }

    try:
        report["source"] = fetch_ad_and_creative(SOURCE_AD_ID, "source")
        report["fixed"] = fetch_ad_and_creative(FIXED_AD_ID, "fixed")
        report["source_vs_fixed"] = compare(report["source"], report["fixed"])

        if BROKEN_AD_ID:
            report["broken"] = fetch_ad_and_creative(BROKEN_AD_ID, "broken")
            report["broken_vs_fixed"] = compare(report["broken"], report["fixed"])

        Path(REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        parts = [
            "🔎 <b>Creative compare v19 — READ ONLY</b>",
            f"Source Ad: <code>{esc(SOURCE_AD_ID)}</code>",
            f"Fixed Ad: <code>{esc(FIXED_AD_ID)}</code>",
            "",
            telegram_pair("SOURCE → FIXED", report["source_vs_fixed"]),
        ]
        if report.get("broken_vs_fixed"):
            parts += ["", telegram_pair("BROKEN → FIXED (найважливіше)", report["broken_vs_fixed"])]
        parts += ["", "🧪 Жодних змін у Meta не внесено.", "Повний diff — у JSON artifact."]
        send_telegram("\n".join(parts))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0
    except Exception as e:
        report["error"] = str(e)
        Path(REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        send_telegram("❌ <b>Creative compare v19 error</b>\n" + esc(e))
        print(f"ERROR: {e}", flush=True)
        return 1

if __name__ == "__main__":
    sys.exit(main())
