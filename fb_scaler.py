import hashlib
import html
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta

from fb_config import (
    ACCOUNTS,
    ATTRIBUTION_WINDOW,
    OFFERS,
    POLAND_TZ,
    SCALER_ACCOUNT_CAP,
    SCALER_BID_DUPLICATE_MULTIPLIER,
    SCALER_BID_JITTER_MAX,
    SCALER_BID_JITTER_MIN,
    SCALER_CAMPAIGN_CAP,
    SCALER_COST_GOAL_DUPLICATE_MULTIPLIER,
    SCALER_OFFER_TIERS,
    SCALER_SOURCE_MATRIX,
    SCALER_START_HOUR,
    SCALER_START_MINUTE_FROM,
    SCALER_START_MINUTE_TO,
    currency_rate,
    parse_campaign_name,
)

# Reuse the exact v26.7-stable duplication implementation that has already
# passed the empirical Meta test. During the Scaler test phase this prevents a
# second, subtly different media/creative implementation from appearing.
import fb_duplicate_test as stable_duplicate


SCALER_VERSION = "v1.1-test"
BUILD_ID = "2026-08-13-scaler-account-labels-r3"
API_VER = stable_duplicate.API_VER

ACCESS_TOKEN = os.environ.get("FB_SCALER_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

MODE = (os.environ.get("SCALER_MODE") or "plan").strip().lower()
TEST_SOURCE_ADSET_ID = (
    os.environ.get("SCALER_TEST_SOURCE_ADSET_ID") or ""
).strip()
ACCOUNT_FILTER = (os.environ.get("SCALER_ACCOUNT_ID") or "").strip()
CREATE_CONFIRMATION = (
    os.environ.get("SCALER_CREATE_CONFIRMATION") or ""
).strip()

ALLOWED_MODES = {"plan", "test_one_paused"}
CREATE_CONFIRMATION_PHRASE = "CREATE_ONE_PAUSED_DUPLICATE"
SUPPORTED_STRATEGIES = {
    "COST_CAP": ("cost_goal", "кос", SCALER_COST_GOAL_DUPLICATE_MULTIPLIER),
    "LOWEST_COST_WITH_BID_CAP": (
        "bid_cap",
        "бід",
        SCALER_BID_DUPLICATE_MULTIPLIER,
    ),
}

PRODUCTION_MARKER_RE = re.compile(
    r"\[SCALER:(?P<date>\d{4}-\d{2}-\d{2}):"
    r"SRC-(?P<source>\d+):N-(?P<sequence>\d+)\]"
)
TEST_MARKER_RE = re.compile(
    r"\[SCALER-TEST:(?P<date>\d{4}-\d{2}-\d{2}):"
    r"SRC-(?P<source>\d+):N-(?P<sequence>\d+)\]"
)
NAME_STRATEGY_RE = re.compile(r"(?<![\w])(кос|бід)(?![\w])", re.IGNORECASE)

ADSET_DISCOVERY_FIELDS = [
    "id",
    "name",
    "account_id",
    "campaign_id",
    "status",
    "effective_status",
    "bid_strategy",
    "bid_amount",
    "daily_budget",
    "lifetime_budget",
    "campaign{id,name,status,effective_status}",
]


def esc(value):
    return html.escape(str(value), quote=False)


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; skipped.", flush=True)
        return

    chunks = []
    current = []
    for line in str(message).splitlines():
        candidate = "\n".join(current + [line])
        if current and len(candidate) > 3500:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chunk in chunks:
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
        except Exception as exc:
            print(f"Telegram error: {exc}", flush=True)


def get_leads(actions):
    values = {}
    for action in actions or []:
        action_type = action.get("action_type")
        if action_type in ("offsite_conversion.fb_pixel_lead", "lead"):
            values[action_type] = int(float(action.get("value", 0)))
    for action_type in ("offsite_conversion.fb_pixel_lead", "lead"):
        if action_type in values:
            return values[action_type]
    return 0


def strategy_info(bid_strategy):
    return SUPPORTED_STRATEGIES.get(str(bid_strategy or "").upper())


def source_duplicates(leads, cpl_ratio):
    if leads <= 0 or cpl_ratio is None:
        return 0
    for min_leads, max_leads, cpl_tiers in SCALER_SOURCE_MATRIX:
        if leads < min_leads:
            continue
        if max_leads is not None and leads > max_leads:
            continue
        for upper_bound, count in cpl_tiers:
            if cpl_ratio <= upper_bound + 1e-12:
                return count
        return 0
    return 0


def offer_scale(cpl_ratio):
    if cpl_ratio is None:
        return 0.0, False
    for upper_bound, multiplier, alert in SCALER_OFFER_TIERS:
        if cpl_ratio <= upper_bound + 1e-12:
            return multiplier, alert
    return 0.0, True


def calculate_requested_duplicates(
    source_leads,
    source_cpl_ratio,
    offer_cpl_ratio,
    bid_strategy,
):
    info = strategy_info(bid_strategy)
    if not info:
        return {
            "eligible": False,
            "reason": f"unsupported bid strategy: {bid_strategy or 'EMPTY'}",
            "base": 0,
            "lead_cap": None,
            "strategy_multiplier": 0.0,
            "offer_multiplier": 0.0,
            "offer_alert": False,
            "offer_blocked": False,
            "blocked_duplicates": 0,
            "requested": 0,
        }

    base = source_duplicates(source_leads, source_cpl_ratio)
    offer_multiplier, offer_alert = offer_scale(offer_cpl_ratio)
    strategy_multiplier = info[2]
    before_offer = math.floor(base * strategy_multiplier + 1e-12)

    # Floor is deliberate: multipliers and caps must never be exceeded by
    # rounding. This is a conservative technical rounding rule.
    requested = math.floor(
        base * strategy_multiplier * offer_multiplier + 1e-12
    )
    offer_blocked = bool(offer_alert and before_offer > 0)

    reasons = []
    if source_leads <= 0:
        reasons.append("source has no leads")
    elif base == 0:
        reasons.append("source failed the approved lead/CPL matrix")
    if offer_blocked:
        reasons.append("offer CPL is above 1.00×BE")
    elif before_offer > 0 and requested == 0:
        reasons.append("result rounded to zero after multipliers")

    return {
        "eligible": requested > 0,
        "reason": "; ".join(reasons),
        "base": base,
        "lead_cap": None,
        "strategy_multiplier": strategy_multiplier,
        "offer_multiplier": offer_multiplier,
        "offer_alert": offer_alert,
        "offer_blocked": offer_blocked,
        "blocked_duplicates": before_offer if offer_blocked else 0,
        "requested": requested,
    }

def allocate_caps(candidates):
    account_used = defaultdict(int)
    campaign_used = defaultdict(int)
    ordered = sorted(
        candidates,
        key=lambda row: (
            row["account_id"],
            row["offer_cpl_ratio"],
            -row["source_leads"],
            row["source_cpl_ratio"],
            row["source_adset_id"],
        ),
    )

    for row in ordered:
        account_key = row["account_id"]
        campaign_key = (row["account_id"], row["campaign_id"])
        account_remaining = max(0, SCALER_ACCOUNT_CAP - account_used[account_key])
        campaign_remaining = max(
            0,
            SCALER_CAMPAIGN_CAP - campaign_used[campaign_key],
        )
        allocated = min(
            row["requested"],
            account_remaining,
            campaign_remaining,
        )
        row["allocated"] = int(allocated)
        row["account_cap_limited"] = allocated < row["requested"] and (
            account_remaining <= campaign_remaining
        )
        row["campaign_cap_limited"] = allocated < row["requested"] and (
            campaign_remaining <= account_remaining
        )
        account_used[account_key] += allocated
        campaign_used[campaign_key] += allocated

    return ordered


def clean_scaler_markers(name):
    value = PRODUCTION_MARKER_RE.sub("", str(name or ""))
    value = TEST_MARKER_RE.sub("", value)
    return stable_duplicate.clean_copy_suffixes(value).strip(" -—–")


def name_strategy_marker(name):
    match = NAME_STRATEGY_RE.search(str(name or ""))
    return match.group(1).lower() if match else None


def corrected_source_name(name, bid_strategy):
    info = strategy_info(bid_strategy)
    if not info:
        raise ValueError(f"Unsupported bid strategy: {bid_strategy}")
    desired = info[1]
    base = clean_scaler_markers(name)
    if NAME_STRATEGY_RE.search(base):
        return NAME_STRATEGY_RE.sub(desired, base)
    if " / " in base:
        return f"{base} {desired}".strip()
    return f"{base} / {desired}".strip()


def scaler_marker(run_date, source_adset_id, sequence, test=False):
    prefix = "SCALER-TEST" if test else "SCALER"
    return (
        f"[{prefix}:{run_date}:SRC-{source_adset_id}:N-{int(sequence):02d}]"
    )


def deterministic_fraction(key):
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def deterministic_start_time(run_date, source_adset_id, sequence):
    next_day = datetime.strptime(run_date, "%Y-%m-%d").date() + timedelta(days=1)
    minute_count = SCALER_START_MINUTE_TO - SCALER_START_MINUTE_FROM + 1
    fraction = deterministic_fraction(
        f"start:{run_date}:{source_adset_id}:{sequence}"
    )
    minute = SCALER_START_MINUTE_FROM + min(
        minute_count - 1,
        int(fraction * minute_count),
    )
    return datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        SCALER_START_HOUR,
        minute,
        tzinfo=POLAND_TZ,
    )


def deterministic_jittered_bid(bid_amount, run_date, source_adset_id, sequence):
    original = int(bid_amount)
    if original <= 0:
        raise ValueError("bid_amount must be a positive integer")

    magnitude_fraction = deterministic_fraction(
        f"jitter-magnitude:{run_date}:{source_adset_id}:{sequence}"
    )
    magnitude = (
        SCALER_BID_JITTER_MIN
        + magnitude_fraction * (SCALER_BID_JITTER_MAX - SCALER_BID_JITTER_MIN)
    )
    sign_fraction = deterministic_fraction(
        f"jitter-sign:{run_date}:{source_adset_id}:{sequence}"
    )
    signed = magnitude if sign_fraction >= 0.5 else -magnitude
    jittered = int(round(original * (1.0 + signed)))
    if jittered == original:
        jittered = original + (1 if signed > 0 else -1)
    return max(1, jittered), signed


def parse_marker_sequences(names, run_date, source_adset_id, test=False):
    regex = TEST_MARKER_RE if test else PRODUCTION_MARKER_RE
    sequences = set()
    for name in names:
        for match in regex.finditer(str(name or "")):
            if (
                match.group("date") == run_date
                and match.group("source") == str(source_adset_id)
            ):
                sequences.add(int(match.group("sequence")))
    return sequences


def load_account_snapshot(account_id, currency, run_date):
    try:
        account = stable_duplicate.get_node(
            f"act_{account_id}",
            ["id", "name", "account_id", "currency"],
            stage=f"scaler_account:{account_id}",
        )
    except Exception as exc:
        print(
            f"Account name lookup failed for {account_id}: {exc}",
            flush=True,
        )
        account = {}

    adsets = stable_duplicate.graph_get_all(
        f"act_{account_id}/adsets",
        {
            "fields": ",".join(ADSET_DISCOVERY_FIELDS),
            "limit": 500,
        },
        stage=f"scaler_adsets:{account_id}",
    )

    since = (
        datetime.strptime(run_date, "%Y-%m-%d").date() - timedelta(days=1)
    ).isoformat()
    insights = stable_duplicate.graph_get_all(
        f"act_{account_id}/insights",
        {
            "level": "adset",
            "fields": "adset_id,spend,actions,impressions",
            "time_range": {"since": since, "until": run_date},
            "action_attribution_windows": ATTRIBUTION_WINDOW,
            "limit": 500,
        },
        stage=f"scaler_insights:{account_id}",
    )
    stats = {
        str(row.get("adset_id")): {
            "spend": float(row.get("spend", 0) or 0),
            "leads": get_leads(row.get("actions")),
            "impressions": int(row.get("impressions", 0) or 0),
        }
        for row in insights
        if row.get("adset_id")
    }

    return {
        "account_id": str(account_id),
        "account_name": account.get("name") or f"Account {account_id}",
        "currency": currency,
        "rate": currency_rate(currency),
        "adsets": adsets,
        "stats": stats,
    }


def collect_snapshots(run_date):
    snapshots = []
    for account_id, currency in ACCOUNTS.items():
        if ACCOUNT_FILTER and str(account_id) != ACCOUNT_FILTER:
            continue
        snapshots.append(load_account_snapshot(str(account_id), currency, run_date))
    if not snapshots:
        raise RuntimeError("No configured accounts matched SCALER_ACCOUNT_ID")
    return snapshots


def build_offer_totals(snapshots):
    totals = {}
    conflicts = defaultdict(set)

    for snapshot in snapshots:
        rate = snapshot["rate"]
        for adset in snapshot["adsets"]:
            campaign = adset.get("campaign") or {}
            parsed = parse_campaign_name(campaign.get("name", ""))
            if not parsed["valid"] or parsed["is_catalog"]:
                continue
            stats = snapshot["stats"].get(
                str(adset.get("id")),
                {"spend": 0.0, "leads": 0, "impressions": 0},
            )
            offer_id = parsed["offer_id"]
            category = parsed["category"]
            conflicts[offer_id].add(category)
            item = totals.setdefault(offer_id, {
                "offer_id": offer_id,
                "category": category,
                "spend_usd": 0.0,
                "leads": 0,
                "category_conflict": False,
            })
            item["spend_usd"] += stats["spend"] / rate
            item["leads"] += stats["leads"]

    for offer_id, item in totals.items():
        item["category_conflict"] = len(conflicts[offer_id]) > 1
        item["categories_seen"] = sorted(conflicts[offer_id])
        item["be_usd"] = OFFERS[item["category"]]
        item["cpl_usd"] = (
            item["spend_usd"] / item["leads"]
            if item["leads"] > 0
            else None
        )
        item["cpl_ratio"] = (
            item["cpl_usd"] / item["be_usd"]
            if item["cpl_usd"] is not None
            else None
        )

    return totals


def build_plan(snapshots, run_date):
    offer_totals = build_offer_totals(snapshots)
    candidates = []
    blocked_sources = []
    skipped = []
    mismatches = []

    for snapshot in snapshots:
        rate = snapshot["rate"]
        all_names = [row.get("name", "") for row in snapshot["adsets"]]
        for adset in snapshot["adsets"]:
            source_id = str(adset.get("id") or "")
            campaign = adset.get("campaign") or {}
            campaign_id = str(adset.get("campaign_id") or campaign.get("id") or "")
            campaign_name = campaign.get("name", "")
            parsed = parse_campaign_name(campaign_name)
            stats = snapshot["stats"].get(
                source_id,
                {"spend": 0.0, "leads": 0, "impressions": 0},
            )

            base = {
                "account_id": snapshot["account_id"],
                "account_name": (
                    snapshot.get("account_name")
                    or f"Account {snapshot['account_id']}"
                ),
                "currency": snapshot["currency"],
                "source_adset_id": source_id,
                "source_adset_name": adset.get("name", ""),
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
            }

            if not parsed["valid"]:
                skipped.append({**base, "reason": parsed["reason"]})
                continue
            if parsed["is_catalog"]:
                skipped.append({**base, "reason": "catalog campaign excluded"})
                continue
            if str(campaign.get("status") or "").upper() != "ACTIVE":
                skipped.append({**base, "reason": "campaign is not ACTIVE"})
                continue
            if str(adset.get("status") or "").upper() not in ("ACTIVE", "PAUSED"):
                skipped.append({**base, "reason": "source adset is not ACTIVE/PAUSED"})
                continue

            info = strategy_info(adset.get("bid_strategy"))
            if not info:
                skipped.append({
                    **base,
                    "reason": (
                        "unsupported bid strategy: "
                        + str(adset.get("bid_strategy") or "EMPTY")
                    ),
                })
                continue

            offer = offer_totals.get(parsed["offer_id"])
            if not offer or offer["category_conflict"]:
                skipped.append({
                    **base,
                    "reason": "offer category conflict across accounts",
                })
                continue

            be_account = OFFERS[parsed["category"]] * rate
            source_cpl = (
                stats["spend"] / stats["leads"]
                if stats["leads"] > 0
                else None
            )
            source_ratio = (
                source_cpl / be_account if source_cpl is not None else None
            )
            calc = calculate_requested_duplicates(
                stats["leads"],
                source_ratio,
                offer["cpl_ratio"],
                adset.get("bid_strategy"),
            )
            if not calc["eligible"]:
                if calc["offer_blocked"]:
                    blocked_sources.append({
                        **base,
                        "offer_id": parsed["offer_id"],
                        "category": parsed["category"],
                        "be_usd": offer["be_usd"],
                        "source_leads": stats["leads"],
                        "source_cpl_ratio": source_ratio,
                        "offer_cpl_usd": offer["cpl_usd"],
                        "offer_cpl_ratio": offer["cpl_ratio"],
                        "bid_strategy": adset.get("bid_strategy"),
                        "blocked_duplicates": calc["blocked_duplicates"],
                    })
                skipped.append({**base, "reason": calc["reason"] or "not eligible"})
                continue

            actual_marker = info[1]
            name_marker = name_strategy_marker(adset.get("name"))
            if name_marker != actual_marker:
                mismatches.append({
                    **base,
                    "name_marker": name_marker,
                    "api_marker": actual_marker,
                    "bid_strategy": adset.get("bid_strategy"),
                })

            existing_sequences = sorted(
                parse_marker_sequences(all_names, run_date, source_id, test=False)
            )
            candidates.append({
                **base,
                "offer_id": parsed["offer_id"],
                "category": parsed["category"],
                "be_account_currency": be_account,
                "source_spend": stats["spend"],
                "source_leads": stats["leads"],
                "source_cpl": source_cpl,
                "source_cpl_ratio": source_ratio,
                "offer_spend_usd": offer["spend_usd"],
                "offer_leads": offer["leads"],
                "offer_cpl_usd": offer["cpl_usd"],
                "offer_cpl_ratio": offer["cpl_ratio"],
                "bid_strategy": adset.get("bid_strategy"),
                "bid_amount": adset.get("bid_amount"),
                "daily_budget": adset.get("daily_budget"),
                "base_duplicates": calc["base"],
                "lead_cap": calc["lead_cap"],
                "strategy_multiplier": calc["strategy_multiplier"],
                "offer_multiplier": calc["offer_multiplier"],
                "offer_alert": calc["offer_alert"],
                "requested": calc["requested"],
                "existing_production_sequences": existing_sequences,
            })

    blocked_by_offer = {}
    for row in blocked_sources:
        item = blocked_by_offer.setdefault(row["offer_id"], {
            "offer_id": row["offer_id"],
            "offer_cpl_usd": row["offer_cpl_usd"],
            "offer_cpl_ratio": row["offer_cpl_ratio"],
            "be_usd": row["be_usd"],
            "source_candidates": 0,
            "blocked_duplicates": 0,
            "accounts": [],
        })
        item["source_candidates"] += 1
        item["blocked_duplicates"] += row["blocked_duplicates"]
        account_ref = {
            "account_id": row["account_id"],
            "account_name": row["account_name"],
        }
        if account_ref not in item["accounts"]:
            item["accounts"].append(account_ref)

    blocked_offers = sorted(
        blocked_by_offer.values(),
        key=lambda row: (row["offer_cpl_ratio"], row["offer_id"]),
    )

    allocated = allocate_caps(candidates)
    for row in allocated:
        existing = set(row["existing_production_sequences"])
        row["missing_sequences"] = [
            sequence
            for sequence in range(1, row["allocated"] + 1)
            if sequence not in existing
        ]
        row["create_count"] = len(row["missing_sequences"])

    return {
        "run_date": run_date,
        "scanned_accounts": [
            {
                "account_id": snapshot["account_id"],
                "account_name": (
                    snapshot.get("account_name")
                    or f"Account {snapshot['account_id']}"
                ),
                "currency": snapshot["currency"],
            }
            for snapshot in snapshots
        ],
        "offer_totals": offer_totals,
        "candidates": allocated,
        "blocked_offers": blocked_offers,
        "skipped": skipped,
        "bid_name_mismatches": mismatches,
        "planned_new_duplicates": sum(x["create_count"] for x in allocated),
        "campaign_cap_triggered": any(x["campaign_cap_limited"] for x in allocated),
        "account_cap_triggered": any(x["account_cap_limited"] for x in allocated),
    }


def iso_to_datetime(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def duplicate_one_paused(candidate, run_date):
    source_id = candidate["source_adset_id"]
    sequence = 1
    marker = scaler_marker(run_date, source_id, sequence, test=True)
    start_time = deterministic_start_time(run_date, source_id, sequence)
    final_name = (
        f"{corrected_source_name(candidate['source_adset_name'], candidate['bid_strategy'])} "
        f"{marker}"
    )

    stable_duplicate.ACCESS_TOKEN = ACCESS_TOKEN
    stable_duplicate.PARTIAL.clear()
    stable_duplicate.CLEANUP_CONTEXT["page_token"] = None

    try:
        diag = stable_duplicate.api_identity()
        granted = set(diag.get("granted") or [])
        missing = sorted(stable_duplicate.REQUIRED_PAGE_VIDEO_PERMISSIONS - granted)
        if missing:
            raise stable_duplicate.DiagnosticError(
                "preflight_page_video_permissions",
                "Missing permissions: " + ", ".join(missing),
            )

        source = stable_duplicate.get_source_objects(source_id)
        page_access = stable_duplicate.resolve_page_token(source["page_id"])
        stable_duplicate.CLEANUP_CONTEXT["page_token"] = page_access["token"]
        source_video = stable_duplicate.resolve_source_video(source, page_access)
        new_video_id, _, _ = stable_duplicate.create_new_page_video(
            source,
            source_video,
            page_access,
        )
        _, thumbnails, _ = stable_duplicate.wait_for_video_and_thumbnails(
            new_video_id,
            page_access["token"],
        )
        selected_thumbnail = stable_duplicate.select_meta_generated_thumbnail(
            thumbnails
        )

        suffix = f" {marker}"
        copied_adset_id = stable_duplicate.copy_adset(source["adset"], suffix)

        if not source["adset"].get("bid_amount"):
            raise RuntimeError("Source adset has no numeric bid_amount for jitter")
        jittered_bid, jitter_fraction = deterministic_jittered_bid(
            source["adset"]["bid_amount"],
            run_date,
            source_id,
            sequence,
        )
        stable_duplicate.graph_request(
            "POST",
            copied_adset_id,
            {
                "name": final_name,
                "status": "PAUSED",
                "start_time": start_time.isoformat(),
                "bid_amount": jittered_bid,
            },
            stage="configure_scaler_test_adset",
        )

        new_creative_id, _ = (
            stable_duplicate.create_creative_from_new_story_spec(
                source,
                new_video_id,
                selected_thumbnail["uri"],
                suffix,
            )
        )
        new_ad_id, _ = stable_duplicate.create_ad(
            source,
            copied_adset_id,
            new_creative_id,
            suffix,
        )
        final_ad, _ = stable_duplicate.poll_ad(new_ad_id)
        copied_adset = stable_duplicate.get_node(
            copied_adset_id,
            stable_duplicate.ADSET_FIELDS + ["start_time"],
            stage="audit_scaler_test_adset",
        )
        copied_creative = stable_duplicate.get_node(
            new_creative_id,
            stable_duplicate.CREATIVE_FIELDS,
            stage="audit_scaler_test_creative",
        )
        creative_fidelity = stable_duplicate.audit_creative_fidelity(
            source,
            copied_creative,
            new_video_id,
        )

        expected_start = start_time
        actual_start = iso_to_datetime(copied_adset.get("start_time"))
        start_time_match = bool(
            actual_start
            and abs(
                actual_start.timestamp() - expected_start.timestamp()
            ) < 60
        )
        result = {
            "marker": marker,
            "source_adset_id": source_id,
            "copied_adset_id": copied_adset_id,
            "new_page_video_id": new_video_id,
            "new_creative_id": new_creative_id,
            "new_ad_id": new_ad_id,
            "expected_start_time": start_time.isoformat(),
            "readback_start_time": copied_adset.get("start_time"),
            "start_time_match": start_time_match,
            "source_bid_amount": source["adset"].get("bid_amount"),
            "jittered_bid_amount": jittered_bid,
            "bid_amount_copy": copied_adset.get("bid_amount"),
            "jitter_fraction": jitter_fraction,
            "daily_budget_source": source["adset"].get("daily_budget"),
            "daily_budget_copy": copied_adset.get("daily_budget"),
            "bid_strategy_source": source["adset"].get("bid_strategy"),
            "bid_strategy_copy": copied_adset.get("bid_strategy"),
            "adset_status": copied_adset.get("status"),
            "adset_effective_status": copied_adset.get("effective_status"),
            "adset_name": copied_adset.get("name"),
            "source_pixel": (
                source["adset"].get("promoted_object") or {}
            ).get("pixel_id"),
            "copy_pixel": (
                copied_adset.get("promoted_object") or {}
            ).get("pixel_id"),
            "new_ad_inside_new_adset": (
                str(final_ad.get("adset_id") or "") == str(copied_adset_id)
            ),
            "new_ad_uses_new_creative": (
                str((final_ad.get("creative") or {}).get("id") or "")
                == str(new_creative_id)
            ),
            "ad_issues": final_ad.get("issues_info") or [],
            "failed_delivery_checks": (
                final_ad.get("failed_delivery_checks") or []
            ),
            "creative_fidelity": creative_fidelity,
        }
        result["success"] = (
            result["start_time_match"]
            and str(result["adset_status"]).upper() == "PAUSED"
            and result["daily_budget_source"] == result["daily_budget_copy"]
            and result["bid_strategy_source"] == result["bid_strategy_copy"]
            and str(result["jittered_bid_amount"])
            == str(result["bid_amount_copy"])
            and marker in str(result["adset_name"] or "")
            and result["source_pixel"] == result["copy_pixel"]
            and result["new_ad_inside_new_adset"]
            and result["new_ad_uses_new_creative"]
            and not result["ad_issues"]
            and not result["failed_delivery_checks"]
            and creative_fidelity["text_cta_match"]
            and creative_fidelity["new_video_id_match"]
        )
        return result
    except Exception as original_exc:
        cleanup = stable_duplicate.cleanup_partial_test_objects()
        raise RuntimeError(
            f"Scaler test duplicate failed: {original_exc}; cleanup="
            + json.dumps(cleanup, ensure_ascii=False)
        ) from original_exc


def plan_summary(plan, mode, execution=None):
    lines = [
        f"📈 <b>FB Scaler {esc(SCALER_VERSION)}</b>",
        f"Build: <code>{esc(BUILD_ID)}</code>",
        f"Mode: <b>{esc(mode)}</b>",
        f"Date: <code>{esc(plan['run_date'])}</code>",
        f"Accounts scanned: <b>{len(plan['scanned_accounts'])}</b>",
        f"Eligible source adsets: <b>{len(plan['candidates'])}</b>",
        f"Planned new duplicates: <b>{plan['planned_new_duplicates']}</b>",
        f"Blocked offers: <b>{len(plan['blocked_offers'])}</b>",
        f"Campaign cap: {'⚠️ HIT' if plan['campaign_cap_triggered'] else 'not hit'}",
        f"Account cap: {'⚠️ HIT' if plan['account_cap_triggered'] else 'not hit'}",
        f"Bid/name mismatches: <b>{len(plan['bid_name_mismatches'])}</b>",
    ]
    for account in plan["scanned_accounts"]:
        lines.append(
            f"• {esc(account['account_name'])} • "
            f"<code>{esc(account['account_id'])}</code> • "
            f"{esc(account['currency'])}"
        )
    for row in plan["candidates"][:20]:
        lines.append(
            "\n"
            f"<b>{esc(row['offer_id'])}</b> • "
            f"AID <code>{esc(row['source_adset_id'])}</code>\n"
            f"Account: {esc(row['account_name'])} • "
            f"<code>{esc(row['account_id'])}</code>\n"
            f"Leads {row['source_leads']} • "
            f"source {row['source_cpl_ratio']:.3f}×BE • "
            f"offer {row['offer_cpl_ratio']:.3f}×BE • "
            f"{esc(row['bid_strategy'])}\n"
            f"requested {row['requested']} → allocated {row['allocated']} → "
            f"missing {row['create_count']}"
        )
    for item in plan["blocked_offers"]:
        account_labels = ", ".join(
            f"{account['account_name']} ({account['account_id']})"
            for account in item["accounts"]
        )
        lines.append(
            "\n"
            f"🚫 <b>{esc(item['offer_id'])}</b> • offer "
            f"{item['offer_cpl_ratio']:.3f}×BE\n"
            f"CPL ${item['offer_cpl_usd']:.2f} • BE ${item['be_usd']:.2f}\n"
            f"Accounts: {esc(account_labels)}\n"
            f"sources {item['source_candidates']} • "
            f"blocked duplicates {item['blocked_duplicates']}\n"
            "Офер загалом вище BE — дублювання заблоковано"
        )
    if execution:
        lines.extend([
            "",
            f"Test duplicate: {'✅ PASS' if execution.get('success') else '❌ FAIL'}",
            f"New AdSet: <code>{esc(execution.get('copied_adset_id'))}</code>",
            f"New Ad: <code>{esc(execution.get('new_ad_id'))}</code>",
            f"Scheduled readback: <code>{esc(execution.get('readback_start_time'))}</code>",
            "Status: <b>PAUSED</b> (automatic activation is not enabled in this test)",
        ])
    return "\n".join(lines)


def run():
    if not ACCESS_TOKEN:
        raise RuntimeError("FB_SCALER_ACCESS_TOKEN missing")
    if MODE not in ALLOWED_MODES:
        raise RuntimeError(f"Unsupported SCALER_MODE: {MODE}")
    if MODE == "test_one_paused":
        if not TEST_SOURCE_ADSET_ID:
            raise RuntimeError("SCALER_TEST_SOURCE_ADSET_ID is required")
        if CREATE_CONFIRMATION != CREATE_CONFIRMATION_PHRASE:
            raise RuntimeError(
                "Creation blocked: confirmation phrase does not match"
            )

    stable_duplicate.ACCESS_TOKEN = ACCESS_TOKEN
    now = datetime.now(POLAND_TZ)
    run_date = now.strftime("%Y-%m-%d")
    snapshots = collect_snapshots(run_date)
    plan = build_plan(snapshots, run_date)
    execution = None

    if MODE == "test_one_paused":
        candidate = next(
            (
                row for row in plan["candidates"]
                if row["source_adset_id"] == TEST_SOURCE_ADSET_ID
                and row["allocated"] > 0
            ),
            None,
        )
        if not candidate:
            raise RuntimeError(
                "Selected source AdSet is not eligible in the current Scaler plan"
            )
        all_names = [
            adset.get("name", "")
            for snapshot in snapshots
            for adset in snapshot["adsets"]
            if snapshot["account_id"] == candidate["account_id"]
        ]
        if parse_marker_sequences(
            all_names,
            run_date,
            TEST_SOURCE_ADSET_ID,
            test=True,
        ):
            raise RuntimeError(
                "Idempotency guard: today's SCALER-TEST duplicate already exists"
            )
        execution = duplicate_one_paused(candidate, run_date)

    report = {
        "version": SCALER_VERSION,
        "build_id": BUILD_ID,
        "mode": MODE,
        "generated_at": now.isoformat(),
        "plan": plan,
        "execution": execution,
    }
    return report


def main():
    now = datetime.now(POLAND_TZ)
    report_file = (
        f"scaler_{MODE}_{now.strftime('%Y_%m_%d_%H%M%S')}_report.json"
    )
    try:
        report = run()
        send_telegram(plan_summary(
            report["plan"],
            report["mode"],
            report.get("execution"),
        ))
        code = 0
    except Exception as exc:
        report = {
            "version": SCALER_VERSION,
            "build_id": BUILD_ID,
            "mode": MODE,
            "generated_at": now.isoformat(),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        send_telegram(
            "❌ <b>FB Scaler test error</b>\n"
            f"Build: <code>{esc(BUILD_ID)}</code>\n"
            f"Mode: <b>{esc(MODE)}</b>\n"
            f"{esc(exc)}"
        )
        code = 1

    with open(report_file, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"Report saved: {report_file}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
