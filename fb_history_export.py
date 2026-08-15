import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fb_config import ACCOUNTS, ATTRIBUTION_WINDOW, OFFERS, POLAND_TZ, currency_rate

API_VER = os.environ.get("FB_API_VER", "v25.0")
ACCESS_TOKEN = os.environ.get("FB_ACCESS_TOKEN") or os.environ.get("FB_SCALER_ACCESS_TOKEN")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "365"))
CHUNK_DAYS = int(os.environ.get("HISTORY_CHUNK_DAYS", "30"))
OUT_DIR = Path(os.environ.get("HISTORY_OUT_DIR", "history_export"))

CATEGORY_TOKENS = set(OFFERS.keys())
CATALOG_MARKERS = {"ктг", "каталог"}


def graph_get_all(path, params, stage="graph_get_all"):
    if not ACCESS_TOKEN:
        raise RuntimeError("FB_ACCESS_TOKEN / FB_SCALER_ACCESS_TOKEN is missing")

    params = dict(params)
    params["access_token"] = ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VER}/{path}?{urllib.parse.urlencode(params)}"
    rows = []

    while url:
        time.sleep(0.12)
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=90) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503, 504) or '"code":17' in body:
                print(f"[{stage}] transient Meta error {exc.code}; sleep 20s", flush=True)
                time.sleep(20)
                continue
            raise RuntimeError(f"[{stage}] Meta HTTP {exc.code}: {body}") from exc

        rows.extend(payload.get("data", []))
        url = payload.get("paging", {}).get("next")

    return rows


def normalized_parts(name):
    value = str(name or "").replace("—", "-").replace("–", "-").replace("−", "-")
    return [part.strip().lower() for part in value.split("-") if part.strip()]


def parse_offer_id(name):
    parts = normalized_parts(name)
    return next((part for part in parts if part.isdigit()), None)


def parse_category(name):
    parts = normalized_parts(name)
    hits = list(dict.fromkeys(part for part in parts if part in CATEGORY_TOKENS))
    return hits[0] if len(hits) == 1 else None


def is_catalog(name):
    return any(part in CATALOG_MARKERS for part in normalized_parts(name))


def get_leads(actions):
    values = {}
    for action in actions or []:
        action_type = action.get("action_type")
        if action_type in ("offsite_conversion.fb_pixel_lead", "lead"):
            values[action_type] = int(float(action.get("value", 0) or 0))
    for action_type in ("offsite_conversion.fb_pixel_lead", "lead"):
        if action_type in values:
            return values[action_type]
    return 0


def date_chunks(since_date, until_date, chunk_days):
    cursor = since_date
    while cursor <= until_date:
        chunk_until = min(until_date, cursor + timedelta(days=chunk_days - 1))
        yield cursor, chunk_until
        cursor = chunk_until + timedelta(days=1)


def load_campaigns(account_id):
    return graph_get_all(
        f"act_{account_id}/campaigns",
        {
            "fields": "id,name,status,effective_status,created_time,updated_time",
            "limit": 500,
        },
        stage=f"campaigns:{account_id}",
    )


def build_current_offer_category_map(all_campaigns):
    """
    Source of truth for old campaign category inference:
    current campaign names that explicitly contain BOTH offer_id and category.
    If one offer_id is currently seen with >1 category, do not infer it.
    """
    seen = defaultdict(set)
    sources = defaultdict(list)

    for account_id, campaigns in all_campaigns.items():
        for campaign in campaigns:
            name = campaign.get("name", "")
            if is_catalog(name):
                continue
            offer_id = parse_offer_id(name)
            category = parse_category(name)
            if not offer_id or not category:
                continue
            seen[offer_id].add(category)
            sources[offer_id].append({
                "account_id": account_id,
                "campaign_id": str(campaign.get("id", "")),
                "campaign_name": name,
                "category": category,
                "effective_status": campaign.get("effective_status", ""),
            })

    mapping = {}
    conflicts = {}
    for offer_id, categories in seen.items():
        if len(categories) == 1:
            mapping[offer_id] = next(iter(categories))
        else:
            conflicts[offer_id] = sorted(categories)

    return mapping, conflicts, sources


def load_adsets(account_id):
    # Metadata is the CURRENT object state. Meta Insights does not provide historical bid_amount/budget.
    return graph_get_all(
        f"act_{account_id}/adsets",
        {
            "fields": (
                "id,name,status,effective_status,created_time,updated_time,"
                "bid_strategy,bid_amount,daily_budget,lifetime_budget,"
                "campaign{id,name,status,effective_status}"
            ),
            "limit": 500,
        },
        stage=f"adsets:{account_id}",
    )


def load_daily_insights(account_id, since_date, until_date):
    rows = []
    for chunk_since, chunk_until in date_chunks(since_date, until_date, CHUNK_DAYS):
        print(f"Account {account_id}: insights {chunk_since} -> {chunk_until}", flush=True)
        chunk_rows = graph_get_all(
            f"act_{account_id}/insights",
            {
                "level": "adset",
                "fields": (
                    "date_start,date_stop,account_id,account_name,campaign_id,campaign_name,"
                    "adset_id,adset_name,spend,impressions,reach,frequency,clicks,"
                    "inline_link_clicks,cpm,cpc,ctr,actions"
                ),
                "time_range": json.dumps({
                    "since": chunk_since.isoformat(),
                    "until": chunk_until.isoformat(),
                }),
                "time_increment": 1,
                "action_attribution_windows": json.dumps(ATTRIBUTION_WINDOW),
                "limit": 500,
            },
            stage=f"insights:{account_id}:{chunk_since}:{chunk_until}",
        )
        rows.extend(chunk_rows)
    return rows


def enrich_row(row, currency, adset_meta, category_map, conflicts):
    campaign_name = row.get("campaign_name", "")
    adset_id = str(row.get("adset_id", ""))
    meta = adset_meta.get(adset_id, {})

    offer_id = parse_offer_id(campaign_name)
    explicit_category = parse_category(campaign_name)
    inferred_category = None
    category_source = "missing"

    if explicit_category:
        category = explicit_category
        category_source = "campaign_name"
    elif offer_id and offer_id in category_map:
        category = category_map[offer_id]
        inferred_category = category
        category_source = "current_campaign_offer_map"
    elif offer_id and offer_id in conflicts:
        category = None
        category_source = "current_campaign_offer_map_conflict"
    else:
        category = None

    be_usd = OFFERS.get(category) if category else None
    rate = currency_rate(currency)
    be_account_currency = be_usd * rate if be_usd is not None else None

    spend = float(row.get("spend", 0) or 0)
    leads = get_leads(row.get("actions"))
    cpl = spend / leads if leads > 0 else None
    cpl_be = cpl / be_account_currency if cpl is not None and be_account_currency else None

    bid_amount_raw = meta.get("bid_amount")
    daily_budget_raw = meta.get("daily_budget")
    lifetime_budget_raw = meta.get("lifetime_budget")

    def money_minor_to_major(value):
        if value in (None, ""):
            return None
        try:
            return float(value) / 100.0
        except (TypeError, ValueError):
            return None

    return {
        "date": row.get("date_start", ""),
        "account_id": str(row.get("account_id", "")),
        "account_name": row.get("account_name", ""),
        "currency": currency,
        "campaign_id": str(row.get("campaign_id", "")),
        "campaign_name": campaign_name,
        "offer_id": offer_id or "",
        "category": category or "",
        "category_source": category_source,
        "category_inferred": inferred_category or "",
        "category_conflict": "|".join(conflicts.get(offer_id, [])) if offer_id else "",
        "is_catalog": int(is_catalog(campaign_name)),
        "be_usd": be_usd if be_usd is not None else "",
        "be_account_currency": be_account_currency if be_account_currency is not None else "",
        "adset_id": adset_id,
        "adset_name": row.get("adset_name", ""),
        "adset_status_current": meta.get("status", ""),
        "adset_effective_status_current": meta.get("effective_status", ""),
        "adset_created_time": meta.get("created_time", ""),
        "adset_updated_time": meta.get("updated_time", ""),
        "bid_strategy_current": meta.get("bid_strategy", ""),
        "bid_amount_current": money_minor_to_major(bid_amount_raw) or "",
        "daily_budget_current": money_minor_to_major(daily_budget_raw) or "",
        "lifetime_budget_current": money_minor_to_major(lifetime_budget_raw) or "",
        "spend": spend,
        "impressions": int(row.get("impressions", 0) or 0),
        "reach": int(row.get("reach", 0) or 0),
        "frequency": float(row.get("frequency", 0) or 0),
        "clicks": int(row.get("clicks", 0) or 0),
        "inline_link_clicks": int(row.get("inline_link_clicks", 0) or 0),
        "cpm": float(row.get("cpm", 0) or 0),
        "cpc": float(row.get("cpc", 0) or 0),
        "ctr": float(row.get("ctr", 0) or 0),
        "leads": leads,
        "cpl": cpl if cpl is not None else "",
        "cpl_be": cpl_be if cpl_be is not None else "",
        "metadata_note": "bid/budget/status are current object metadata, not guaranteed historical values",
    }


def aggregate_adsets(daily_rows):
    grouped = {}
    for row in daily_rows:
        key = (row["account_id"], row["adset_id"])
        item = grouped.setdefault(key, {
            "account_id": row["account_id"],
            "account_name": row["account_name"],
            "currency": row["currency"],
            "campaign_id": row["campaign_id"],
            "campaign_name": row["campaign_name"],
            "offer_id": row["offer_id"],
            "category": row["category"],
            "category_source": row["category_source"],
            "be_account_currency": row["be_account_currency"],
            "adset_id": row["adset_id"],
            "adset_name": row["adset_name"],
            "bid_strategy_current": row["bid_strategy_current"],
            "bid_amount_current": row["bid_amount_current"],
            "daily_budget_current": row["daily_budget_current"],
            "first_day_with_delivery": row["date"],
            "last_day_with_delivery": row["date"],
            "days_with_delivery": 0,
            "spend": 0.0,
            "impressions": 0,
            "clicks": 0,
            "inline_link_clicks": 0,
            "leads": 0,
        })
        item["first_day_with_delivery"] = min(item["first_day_with_delivery"], row["date"])
        item["last_day_with_delivery"] = max(item["last_day_with_delivery"], row["date"])
        item["days_with_delivery"] += 1
        item["spend"] += float(row["spend"] or 0)
        item["impressions"] += int(row["impressions"] or 0)
        item["clicks"] += int(row["clicks"] or 0)
        item["inline_link_clicks"] += int(row["inline_link_clicks"] or 0)
        item["leads"] += int(row["leads"] or 0)

    result = []
    for item in grouped.values():
        leads = item["leads"]
        spend = item["spend"]
        item["cpl"] = spend / leads if leads else ""
        be = float(item["be_account_currency"]) if item["be_account_currency"] not in ("", None) else None
        item["cpl_be"] = (item["cpl"] / be) if item["cpl"] != "" and be else ""
        item["ctr_calc"] = (item["clicks"] / item["impressions"] * 100) if item["impressions"] else ""
        item["cpm_calc"] = (spend / item["impressions"] * 1000) if item["impressions"] else ""
        item["metadata_note"] = "bid/budget are current object metadata, not guaranteed historical values"
        result.append(item)

    return sorted(result, key=lambda x: (x["account_id"], x["campaign_id"], x["adset_id"]))


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    if HISTORY_DAYS <= 0:
        raise ValueError("HISTORY_DAYS must be > 0")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(POLAND_TZ)
    until_date = now.date()
    since_date = until_date - timedelta(days=HISTORY_DAYS - 1)

    all_campaigns = {str(account_id): load_campaigns(str(account_id)) for account_id in ACCOUNTS}
    category_map, conflicts, sources = build_current_offer_category_map(all_campaigns)

    print(f"Offer/category map: {len(category_map)} offers; conflicts: {len(conflicts)}", flush=True)

    daily_rows = []
    metadata_coverage = {}

    for account_id, currency in ACCOUNTS.items():
        account_id = str(account_id)
        adsets = load_adsets(account_id)
        adset_meta = {str(row.get("id")): row for row in adsets if row.get("id")}
        insights = load_daily_insights(account_id, since_date, until_date)
        enriched = [
            enrich_row(row, currency, adset_meta, category_map, conflicts)
            for row in insights
        ]
        daily_rows.extend(enriched)
        ids_in_insights = {str(row.get("adset_id", "")) for row in insights}
        ids_with_meta = ids_in_insights.intersection(adset_meta)
        metadata_coverage[account_id] = {
            "insight_adsets": len(ids_in_insights),
            "adsets_with_current_metadata": len(ids_with_meta),
            "coverage_pct": round(100 * len(ids_with_meta) / len(ids_in_insights), 2) if ids_in_insights else 100.0,
        }

    adset_rows = aggregate_adsets(daily_rows)

    write_csv(OUT_DIR / "fb_history_daily.csv", daily_rows)
    write_csv(OUT_DIR / "fb_history_adsets.csv", adset_rows)

    manifest = {
        "generated_at": now.isoformat(),
        "api_version": API_VER,
        "history_days": HISTORY_DAYS,
        "since": since_date.isoformat(),
        "until": until_date.isoformat(),
        "accounts": ACCOUNTS,
        "attribution_window": ATTRIBUTION_WINDOW,
        "offer_category_map": category_map,
        "offer_category_conflicts": conflicts,
        "offer_category_sources": sources,
        "metadata_coverage": metadata_coverage,
        "notes": [
            "Old campaign category is inferred from CURRENT campaign names by offer_id only when the current mapping is unique.",
            "If one offer_id maps to multiple current categories, historical category is left unresolved and flagged as conflict.",
            "Meta Insights supplies historical delivery/performance, but bid_amount, bid_strategy and budgets in this export are CURRENT adset metadata and may not equal the value that was active on each historical day.",
            "No Meta objects are modified by this script; it is read-only.",
        ],
    }
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Wrote {len(daily_rows)} daily rows and {len(adset_rows)} adset rows to {OUT_DIR}", flush=True)


if __name__ == "__main__":
    main()
