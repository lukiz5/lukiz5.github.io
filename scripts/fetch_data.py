#!/usr/bin/env python3
"""Fetch and persist SENNS dashboard data with defensive fallbacks."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dateutil import parser as date_parser


DATA_FILE = Path("data/senns_data.json")
TMP_DATA_FILE = Path("data/senns_data.tmp.json")

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"
LEMONSQUEEZY_API_BASE = "https://api.lemonsqueezy.com/v1"
MAILERLITE_API_BASE = "https://connect.mailerlite.com/api"

REQUEST_TIMEOUT = 25
MAX_RETRIES = 2
MAX_PAGES = 8
TARGET_CPL = 25.0

META_SOURCE = "meta_ads"
INSTAGRAM_SOURCE = "instagram_organic"
LEMONSQUEEZY_SOURCE = "lemonsqueezy"
MAILERLITE_SOURCE = "mailerlite"
ALL_SOURCES = [META_SOURCE, INSTAGRAM_SOURCE, LEMONSQUEEZY_SOURCE, MAILERLITE_SOURCE]

LEAD_ACTION_TYPES = {
    "lead",
    "onsite_conversion.lead_grouped",
    "offsite_conversion.fb_pixel_lead",
    "onsite_web_lead",
}
PURCHASE_ACTION_TYPES = {
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "web_in_store_purchase",
}


def log(level: str, message: str) -> None:
    print(f"[{level}] {message}")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def parse_currency_string(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return safe_float(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = "".join(char for char in text if char.isdigit() or char in {".", "-"})
    if not cleaned or cleaned in {".", "-", "-."}:
        return None
    return safe_float(cleaned)


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def calculate_freshness_hours(last_updated: str) -> float | None:
    last_dt = parse_datetime(last_updated)
    if last_dt is None:
        return None
    delta_hours = (now_utc() - last_dt).total_seconds() / 3600
    return safe_round(max(delta_hours, 0.0), 2)


def deep_copy_dict(data: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(data)


def safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def nested_dict_value(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def normalize_lemonsqueezy_amount(
    raw_value: Any,
    *,
    formatted_value: Any = None,
    assume_cents: bool = False,
) -> float:
    formatted_amount = parse_currency_string(formatted_value)
    raw_text = str(raw_value).strip() if raw_value not in (None, "") else ""
    raw_amount = parse_currency_string(raw_value)

    if formatted_amount is not None and raw_amount is None:
        return safe_round(formatted_amount)
    if raw_amount is None:
        return 0.0

    raw_looks_decimal = "." in raw_text
    direct_amount = safe_round(raw_amount)
    cents_amount = safe_round(raw_amount / 100.0)

    if formatted_amount is not None:
        direct_diff = abs(direct_amount - formatted_amount)
        cents_diff = abs(cents_amount - formatted_amount)
        if cents_diff < direct_diff:
            return cents_amount
        if direct_diff < cents_diff:
            return direct_amount

    if assume_cents and not raw_looks_decimal:
        return cents_amount
    return direct_amount


def build_lemonsqueezy_amount_warning(
    *,
    context: str,
    normalized_amount: float,
    formatted_amount: float | None,
    unit_price: float,
    quantity: int,
) -> str | None:
    if formatted_amount is not None and formatted_amount > 0:
        ratio = normalized_amount / formatted_amount if formatted_amount else 1
        if ratio >= 50:
            return (
                f"LemonSqueezy {context} looks suspiciously high after normalization "
                f"(${safe_round(normalized_amount)} vs formatted ${safe_round(formatted_amount)})."
            )

    expected_total = unit_price * max(quantity, 1)
    if expected_total > 0 and normalized_amount > expected_total * 20:
        return (
            f"LemonSqueezy {context} looks unusually high versus line price "
            f"(${safe_round(normalized_amount)} vs expected about ${safe_round(expected_total)})."
        )

    return None


SENSITIVE_QUERY_KEYS = {
    "access_token",
    "client_secret",
    "client_id",
    "api_key",
    "authorization",
}


def mask_sensitive_value(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def mask_sensitive_text(text: str) -> str:
    masked = text
    for key in SENSITIVE_QUERY_KEYS:
        masked = re.sub(
            rf"({re.escape(key)}=)([^&\\s]+)",
            lambda match: f"{match.group(1)}{mask_sensitive_value(match.group(2))}",
            masked,
            flags=re.IGNORECASE,
        )
    masked = re.sub(
        r"(Bearer\s+)([A-Za-z0-9._-]+)",
        lambda match: f"{match.group(1)}{mask_sensitive_value(match.group(2))}",
        masked,
        flags=re.IGNORECASE,
    )
    return masked


def mask_sensitive_url(url: str) -> str:
    try:
        split = urlsplit(url)
        query_items = []
        for key, value in parse_qsl(split.query, keep_blank_values=True):
            query_items.append((key, mask_sensitive_value(value) if key.lower() in SENSITIVE_QUERY_KEYS else value))
        masked_query = urlencode(query_items)
        return urlunsplit((split.scheme, split.netloc, split.path, masked_query, split.fragment))
    except ValueError:
        return mask_sensitive_text(url)


def extract_error_details(payload: Any) -> str | None:
    if isinstance(payload, dict):
        error_payload = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        message = str(error_payload.get("message", "")).strip()
        error_type = str(error_payload.get("type", "")).strip()
        error_code = error_payload.get("code")
        error_subcode = error_payload.get("error_subcode")

        detail_parts = []
        if error_type:
            detail_parts.append(error_type)
        if error_code not in (None, ""):
            detail_parts.append(f"code {error_code}")
        if error_subcode not in (None, ""):
            detail_parts.append(f"subcode {error_subcode}")
        if message:
            detail_parts.append(message)

        if detail_parts:
            return " - ".join(str(part) for part in detail_parts)

        compact_payload = mask_sensitive_text(json.dumps(payload, ensure_ascii=False))
        return compact_payload[:400]

    if isinstance(payload, list):
        compact_payload = mask_sensitive_text(json.dumps(payload, ensure_ascii=False))
        return compact_payload[:400]

    if payload not in (None, ""):
        return mask_sensitive_text(str(payload))[:400]

    return None


def default_meta_ads() -> dict[str, Any]:
    return {
        "campaigns": [],
        "ad_sets": [],
        "total_spend": 0,
        "total_leads": 0,
        "total_purchases": 0,
        "avg_cpl": 0,
        "avg_cpa": 0,
        "blended_roas": 0,
    }


def default_instagram_organic() -> dict[str, Any]:
    return {
        "followers": 0,
        "posts_this_week": 0,
        "avg_reach": 0,
        "avg_engagement_rate": 0,
        "top_posts": [],
    }


def default_lemonsqueezy() -> dict[str, Any]:
    return {
        "total_revenue_usd": 0,
        "orders_this_month": 0,
        "orders_last_30_days": [],
        "products": [],
    }


def default_mailerlite() -> dict[str, Any]:
    return {
        "total_subscribers": 0,
        "new_subscribers_7d": 0,
        "avg_open_rate": 0,
        "avg_click_rate": 0,
        "sequences": [],
    }


def default_funnel() -> dict[str, Any]:
    return {
        "impressions": 0,
        "clicks": 0,
        "lp_visits": 0,
        "opt_ins": 0,
        "purchases_l1": 0,
        "consultations_l2": 0,
        "ctr": 0,
        "lp_cvr": 0,
        "email_cvr": 0,
    }


def default_analysis() -> dict[str, Any]:
    return {
        "break_even_roas": 0,
        "revenue_minus_spend": 0,
        "top_problem_area": "",
        "top_opportunity_area": "",
        "claude_context_summary": "",
    }


def default_sources_status() -> dict[str, str]:
    return {
        META_SOURCE: "skipped",
        INSTAGRAM_SOURCE: "skipped",
        LEMONSQUEEZY_SOURCE: "skipped",
        MAILERLITE_SOURCE: "skipped",
        "overall": "skipped",
    }


def default_dashboard_data() -> dict[str, Any]:
    return {
        "last_updated": "never",
        "data_freshness_hours": None,
        "errors": [],
        "warnings": [],
        "sources_status": default_sources_status(),
        META_SOURCE: default_meta_ads(),
        INSTAGRAM_SOURCE: default_instagram_organic(),
        LEMONSQUEEZY_SOURCE: default_lemonsqueezy(),
        MAILERLITE_SOURCE: default_mailerlite(),
        "funnel": default_funnel(),
        "analysis": default_analysis(),
    }


def ensure_section(previous_data: dict[str, Any], section_name: str, factory: Any) -> dict[str, Any]:
    section = previous_data.get(section_name)
    if isinstance(section, dict):
        merged = factory()
        merged.update(deep_copy_dict(section))
        return merged
    return factory()


def load_previous_data() -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not DATA_FILE.exists():
        return default_dashboard_data(), warnings

    try:
        with DATA_FILE.open("r", encoding="utf-8") as handle:
            raw_data = json.load(handle)
    except (OSError, ValueError) as exc:
        warnings.append(f"Could not read previous dashboard data: {exc}")
        return default_dashboard_data(), warnings

    if not isinstance(raw_data, dict):
        warnings.append("Previous dashboard data is not a JSON object; using defaults.")
        return default_dashboard_data(), warnings

    previous = default_dashboard_data()
    previous["last_updated"] = raw_data.get("last_updated", "never")
    previous["data_freshness_hours"] = raw_data.get("data_freshness_hours")
    previous["errors"] = raw_data.get("errors", []) if isinstance(raw_data.get("errors"), list) else []
    previous["warnings"] = raw_data.get("warnings", []) if isinstance(raw_data.get("warnings"), list) else []
    previous["sources_status"] = default_sources_status()
    if isinstance(raw_data.get("sources_status"), dict):
        previous["sources_status"].update(raw_data["sources_status"])
    previous[META_SOURCE] = ensure_section(raw_data, META_SOURCE, default_meta_ads)
    previous[INSTAGRAM_SOURCE] = ensure_section(raw_data, INSTAGRAM_SOURCE, default_instagram_organic)
    previous[LEMONSQUEEZY_SOURCE] = ensure_section(raw_data, LEMONSQUEEZY_SOURCE, default_lemonsqueezy)
    previous[MAILERLITE_SOURCE] = ensure_section(raw_data, MAILERLITE_SOURCE, default_mailerlite)
    previous["funnel"] = ensure_section(raw_data, "funnel", default_funnel)
    previous["analysis"] = ensure_section(raw_data, "analysis", default_analysis)
    return previous, warnings


def atomic_write_json(target_path: Path, tmp_path: Path, payload: dict[str, Any]) -> None:
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, target_path)


class HttpClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "senns-dashboard-fetcher/1.0"})

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> tuple[Any | None, str | None]:
        last_error: str | None = None
        masked_url = mask_sensitive_url(url)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, headers=headers, params=params, timeout=timeout)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    last_error = f"HTTP {response.status_code} from {masked_url}"
                    log("WARN", f"{last_error}; retry {attempt}/{MAX_RETRIES}")
                    time.sleep(1.0 * attempt)
                    continue
                response.raise_for_status()
                if not response.text.strip():
                    return {}, None
                return response.json(), None
            except requests.Timeout:
                last_error = f"Timeout while calling {masked_url}"
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code:
                    error_detail: str | None = None
                    if response is not None:
                        try:
                            response_payload = response.json()
                            error_detail = extract_error_details(response_payload)
                        except ValueError:
                            error_detail = extract_error_details(response.text)

                    last_error = f"HTTP {status_code} while calling {masked_url}"
                    if error_detail:
                        last_error = f"{last_error} - {error_detail}"
                else:
                    last_error = f"Request failed for {masked_url}: {mask_sensitive_text(str(exc))}"
            except ValueError:
                last_error = f"Invalid JSON returned by {masked_url}"

            if attempt < MAX_RETRIES:
                log("WARN", f"{last_error}; retry {attempt}/{MAX_RETRIES}")
                time.sleep(1.0 * attempt)

        return None, last_error or f"Unknown HTTP error for {masked_url}"

    def get_paginated(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        item_key: str = "data",
        max_pages: int = MAX_PAGES,
    ) -> tuple[list[Any], str | None]:
        items: list[Any] = []
        next_url = url
        next_params = params
        pages = 0

        while next_url and pages < max_pages:
            payload, error = self.request_json(next_url, headers=headers, params=next_params)
            if error:
                return items, error

            if isinstance(payload, dict):
                chunk = payload.get(item_key, [])
                if isinstance(chunk, list):
                    items.extend(chunk)
                elif chunk:
                    items.append(chunk)
                links = payload.get("paging") or payload.get("links") or {}
                next_link = links.get("next") if isinstance(links, dict) else None
            elif isinstance(payload, list):
                items.extend(payload)
                next_link = None
            else:
                next_link = None

            next_url = next_link
            next_params = None
            pages += 1

        return items, None


def normalize_ad_account_id(account_id: str) -> str:
    if account_id.startswith("act_"):
        return account_id
    return f"act_{account_id}"


def extract_action_value(actions: Any, action_types: set[str]) -> int:
    if not isinstance(actions, list):
        return 0
    total = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        if action.get("action_type") in action_types:
            total += safe_int(action.get("value"))
    return total


def extract_roas_value(roas_payload: Any) -> float:
    if isinstance(roas_payload, list):
        for item in roas_payload:
            if isinstance(item, dict) and item.get("value") not in (None, ""):
                return safe_float(item.get("value"))
    if isinstance(roas_payload, dict):
        return safe_float(roas_payload.get("value"))
    return safe_float(roas_payload)


def has_any_delivery_metrics(ad_set: dict[str, Any]) -> bool:
    return any(
        safe_float(ad_set.get(metric)) > 0
        for metric in ("spend", "impressions", "clicks", "leads", "purchases")
    )


def decide_ad_set(ad_set: dict[str, Any], target_cpl: float = TARGET_CPL) -> str:
    status = str(ad_set.get("status", "")).upper()
    impressions = safe_int(ad_set.get("impressions"))
    frequency = safe_float(ad_set.get("frequency"))
    leads = safe_int(ad_set.get("leads"))
    cpl = safe_float(ad_set.get("cpl"))

    if status != "ACTIVE":
        return "PAUSED"
    if not has_any_delivery_metrics(ad_set):
        return "NO_DATA"
    if impressions < 500:
        return "LEARNING"
    if frequency > 3.0:
        return "PAUSE"
    if cpl > 2 * target_cpl and impressions > 500:
        return "PAUSE"
    if cpl < target_cpl and frequency < 2.0 and leads >= 10:
        return "SCALE"
    if 2.0 <= frequency <= 3.0:
        return "NEW_CREATIVE"
    if cpl > target_cpl and cpl <= 2 * target_cpl:
        return "OPTIMIZE"
    return "MONITOR"


def fetch_meta_ads(
    client: HttpClient,
    previous_data: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str], list[str], dict[str, Any], bool]:
    previous_section = ensure_section(previous_data, META_SOURCE, default_meta_ads)
    section = deep_copy_dict(previous_section)
    warnings: list[str] = []
    errors: list[str] = []
    status = "ok"
    refreshed = False

    access_token = os.getenv("META_ACCESS_TOKEN", "").strip()
    ad_account_id = os.getenv("META_AD_ACCOUNT_ID", "").strip()
    if not access_token or not ad_account_id:
        warning = "Meta Ads credentials missing; keeping previous or empty data."
        log("WARN", warning)
        warnings.append(warning)
        return section, "skipped", errors, warnings, {}, False

    normalized_account_id = normalize_ad_account_id(ad_account_id)

    campaigns_url = f"{GRAPH_API_BASE}/{normalized_account_id}/campaigns"
    campaigns_payload, campaigns_error = client.request_json(
        campaigns_url,
        params={
            "access_token": access_token,
            "fields": "id,name,status,objective",
            "limit": 200,
        },
    )

    if campaigns_error:
        errors.append(f"Meta Ads campaigns fetch failed: {campaigns_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        campaign_rows = campaigns_payload.get("data", []) if isinstance(campaigns_payload, dict) else []
        section["campaigns"] = [
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "status": item.get("status", ""),
                "objective": item.get("objective", ""),
            }
            for item in campaign_rows
            if isinstance(item, dict)
        ]
        refreshed = True

    ad_sets_url = f"{GRAPH_API_BASE}/{normalized_account_id}/adsets"
    ad_sets_payload, ad_sets_error = client.request_json(
        ad_sets_url,
        params={
            "access_token": access_token,
            "fields": (
                "id,name,status,daily_budget,"
                "insights.limit(1){spend,impressions,clicks,cpm,ctr,cpc,frequency,actions}"
            ),
            "limit": 200,
        },
    )

    meta_context = {
        "impressions": 0,
        "clicks": 0,
        "leads": 0,
        "purchases": 0,
        "spend": 0.0,
    }

    if ad_sets_error:
        errors.append(f"Meta Ads ad sets fetch failed: {ad_sets_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        ad_set_rows = ad_sets_payload.get("data", []) if isinstance(ad_sets_payload, dict) else []
        parsed_ad_sets: list[dict[str, Any]] = []

        for item in ad_set_rows:
            if not isinstance(item, dict):
                continue
            insights_wrapper = item.get("insights", {})
            insight_rows = insights_wrapper.get("data", []) if isinstance(insights_wrapper, dict) else []
            insight = insight_rows[0] if insight_rows and isinstance(insight_rows[0], dict) else {}
            actions = insight.get("actions", [])

            spend = safe_round(safe_float(insight.get("spend")))
            impressions = safe_int(insight.get("impressions"))
            clicks = safe_int(insight.get("clicks"))
            cpm = safe_round(safe_float(insight.get("cpm")))
            ctr = safe_round(safe_float(insight.get("ctr")), 4)
            cpc = safe_round(safe_float(insight.get("cpc")))
            frequency = safe_round(safe_float(insight.get("frequency")), 2)
            leads = extract_action_value(actions, LEAD_ACTION_TYPES)
            purchases = extract_action_value(actions, PURCHASE_ACTION_TYPES)
            cpl = safe_round(spend / leads) if leads else 0
            cpa = safe_round(spend / purchases) if purchases else 0

            parsed_ad_set = {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "status": item.get("status", ""),
                "daily_budget": safe_round(safe_float(item.get("daily_budget")) / 100.0),
                "spend": spend,
                "impressions": impressions,
                "clicks": clicks,
                "cpm": cpm,
                "ctr": ctr,
                "cpc": cpc,
                "frequency": frequency,
                "leads": leads,
                "purchases": purchases,
                "cpl": cpl,
                "cpa": cpa,
            }
            parsed_ad_set["decision"] = decide_ad_set(parsed_ad_set)
            parsed_ad_sets.append(parsed_ad_set)

            meta_context["impressions"] += impressions
            meta_context["clicks"] += clicks
            meta_context["leads"] += leads
            meta_context["purchases"] += purchases
            meta_context["spend"] += spend

        section["ad_sets"] = parsed_ad_sets
        section["total_spend"] = safe_round(meta_context["spend"])
        section["total_leads"] = meta_context["leads"]
        section["total_purchases"] = meta_context["purchases"]
        section["avg_cpl"] = safe_round(meta_context["spend"] / meta_context["leads"]) if meta_context["leads"] else 0
        section["avg_cpa"] = safe_round(meta_context["spend"] / meta_context["purchases"]) if meta_context["purchases"] else 0
        refreshed = True

    insights_url = f"{GRAPH_API_BASE}/{normalized_account_id}/insights"
    insights_payload, insights_error = client.request_json(
        insights_url,
        params={
            "access_token": access_token,
            "fields": "spend,impressions,clicks,actions,purchase_roas",
            "date_preset": "last_30d",
            "level": "account",
            "limit": 1,
        },
    )

    if insights_error:
        errors.append(f"Meta Ads account insights fetch failed: {insights_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        insight_rows = insights_payload.get("data", []) if isinstance(insights_payload, dict) else []
        account_row = insight_rows[0] if insight_rows and isinstance(insight_rows[0], dict) else {}
        if account_row:
            total_spend = safe_round(safe_float(account_row.get("spend")), 2)
            total_impressions = safe_int(account_row.get("impressions"))
            total_clicks = safe_int(account_row.get("clicks"))
            actions = account_row.get("actions", [])
            total_leads = extract_action_value(actions, LEAD_ACTION_TYPES)
            total_purchases = extract_action_value(actions, PURCHASE_ACTION_TYPES)
            blended_roas = safe_round(extract_roas_value(account_row.get("purchase_roas")), 4)

            if total_spend > 0 or meta_context["spend"] == 0:
                section["total_spend"] = total_spend
                meta_context["spend"] = total_spend
            if total_leads > 0 or meta_context["leads"] == 0:
                section["total_leads"] = total_leads
                meta_context["leads"] = total_leads
            if total_purchases > 0 or meta_context["purchases"] == 0:
                section["total_purchases"] = total_purchases
                meta_context["purchases"] = total_purchases

            meta_context["impressions"] = total_impressions or meta_context["impressions"]
            meta_context["clicks"] = total_clicks or meta_context["clicks"]
            section["avg_cpl"] = safe_round(section["total_spend"] / section["total_leads"]) if section["total_leads"] else 0
            section["avg_cpa"] = safe_round(section["total_spend"] / section["total_purchases"]) if section["total_purchases"] else 0
            section["blended_roas"] = blended_roas
            refreshed = True

    if not refreshed:
        failure = "Meta Ads integration failed completely; preserving previous data."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, {}, False

    if status == "partial":
        warning = "Meta Ads data is partially refreshed; some values may come from previous runs."
        warnings.append(warning)
        log("WARN", warning)

    return section, status, errors, warnings, meta_context, True


def fetch_instagram_organic(
    client: HttpClient,
    previous_data: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str], list[str], bool]:
    previous_section = ensure_section(previous_data, INSTAGRAM_SOURCE, default_instagram_organic)
    section = deep_copy_dict(previous_section)
    warnings: list[str] = []
    errors: list[str] = []
    status = "ok"
    refreshed = False

    access_token = os.getenv("META_ACCESS_TOKEN", "").strip()
    instagram_id = os.getenv("META_INSTAGRAM_ID", "").strip()
    if not access_token or not instagram_id:
        warning = "Instagram credentials missing; keeping previous or empty data."
        log("WARN", warning)
        warnings.append(warning)
        return section, "skipped", errors, warnings, False

    profile_url = f"{GRAPH_API_BASE}/{instagram_id}"
    profile_payload, profile_error = client.request_json(
        profile_url,
        params={
            "access_token": access_token,
            "fields": "followers_count,media_count",
        },
    )

    followers = safe_int(section.get("followers"))
    if profile_error:
        errors.append(f"Instagram profile fetch failed: {profile_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        section["followers"] = safe_int(profile_payload.get("followers_count")) if isinstance(profile_payload, dict) else 0
        followers = safe_int(section.get("followers"))
        refreshed = True

    media_url = f"{GRAPH_API_BASE}/{instagram_id}/media"
    media_payload, media_error = client.request_json(
        media_url,
        params={
            "access_token": access_token,
            "fields": "id,caption,media_type,permalink,timestamp,like_count,comments_count",
            "limit": 12,
        },
    )

    if media_error:
        errors.append(f"Instagram media fetch failed: {media_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        media_rows = media_payload.get("data", []) if isinstance(media_payload, dict) else []
        week_ago = now_utc() - timedelta(days=7)
        top_posts: list[dict[str, Any]] = []
        reach_values: list[float] = []
        engagement_rates: list[float] = []
        posts_this_week = 0

        for item in media_rows:
            if not isinstance(item, dict):
                continue
            timestamp = parse_datetime(item.get("timestamp"))
            if timestamp and timestamp >= week_ago:
                posts_this_week += 1

            likes = safe_int(item.get("like_count"))
            comments = safe_int(item.get("comments_count"))
            reach = 0

            insights_url = f"{GRAPH_API_BASE}/{item.get('id', '')}/insights"
            insights_payload, insights_error = client.request_json(
                insights_url,
                params={
                    "access_token": access_token,
                    "metric": "reach",
                },
            )
            if insights_error:
                status = "partial"
                warnings.append(f"Instagram insights unavailable for media {item.get('id', '')}.")
                log("WARN", warnings[-1])
            else:
                insight_rows = insights_payload.get("data", []) if isinstance(insights_payload, dict) else []
                for insight_row in insight_rows:
                    if isinstance(insight_row, dict) and insight_row.get("name") == "reach":
                        values = insight_row.get("values", [])
                        first_value = values[0] if values and isinstance(values[0], dict) else {}
                        reach = safe_int(first_value.get("value"))
                        break

            engagement_count = likes + comments
            engagement_rate = safe_round((engagement_count / followers) * 100, 2) if followers > 0 else 0
            if reach > 0:
                reach_values.append(float(reach))
            engagement_rates.append(float(engagement_rate))
            top_posts.append(
                {
                    "id": item.get("id", ""),
                    "caption": (item.get("caption") or "")[:240],
                    "permalink": item.get("permalink", ""),
                    "timestamp": item.get("timestamp", ""),
                    "reach": reach,
                    "engagement_rate": engagement_rate,
                    "likes": likes,
                    "comments": comments,
                }
            )

        top_posts.sort(key=lambda post: (safe_float(post.get("engagement_rate")), safe_int(post.get("reach"))), reverse=True)
        section["posts_this_week"] = posts_this_week
        section["avg_reach"] = safe_round(sum(reach_values) / len(reach_values)) if reach_values else 0
        section["avg_engagement_rate"] = (
            safe_round(sum(engagement_rates) / len(engagement_rates), 2) if engagement_rates else 0
        )
        section["top_posts"] = top_posts[:5]
        refreshed = True

    if not refreshed:
        failure = "Instagram integration failed completely; preserving previous data."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False

    if status == "partial":
        warnings.append("Instagram data is partially refreshed; some media insight fields may be stale.")

    return section, status, errors, warnings, True


def parse_lemonsqueezy_order(order: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    attributes = order.get("attributes", {}) if isinstance(order, dict) else {}
    first_item = attributes.get("first_order_item", {}) if isinstance(attributes.get("first_order_item"), dict) else {}
    warnings: list[str] = []

    unit_price = normalize_lemonsqueezy_amount(
        first_item.get("price"),
        formatted_value=first_item.get("price_formatted"),
        assume_cents=True,
    )
    quantity = safe_int(first_item.get("quantity"), 1) or 1
    revenue = normalize_lemonsqueezy_amount(
        attributes.get("total_usd") or attributes.get("subtotal_usd") or first_item.get("price") or 0,
        formatted_value=attributes.get("total_formatted") or attributes.get("subtotal_formatted") or first_item.get("price_formatted"),
        assume_cents=True,
    )
    formatted_total = parse_currency_string(attributes.get("total_formatted") or attributes.get("subtotal_formatted"))
    sanity_warning = build_lemonsqueezy_amount_warning(
        context=f"order {order.get('id', '') or attributes.get('identifier', '') or 'unknown'}",
        normalized_amount=revenue,
        formatted_amount=formatted_total,
        unit_price=unit_price,
        quantity=quantity,
    )
    if sanity_warning:
        warnings.append(sanity_warning)

    created_at = attributes.get("created_at") or attributes.get("createdAt") or ""
    return (
        {
            "id": order.get("id", ""),
            "identifier": attributes.get("identifier", ""),
            "status": attributes.get("status", ""),
            "created_at": created_at,
            "revenue_usd": safe_round(revenue),
            "product_name": first_item.get("product_name") or attributes.get("product_name") or "",
            "customer_name": attributes.get("user_name") or "",
            "quantity": quantity,
            "unit_price_usd": safe_round(unit_price),
        },
        warnings,
    )


def fetch_lemonsqueezy(
    client: HttpClient,
    previous_data: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str], list[str], bool]:
    previous_section = ensure_section(previous_data, LEMONSQUEEZY_SOURCE, default_lemonsqueezy)
    section = deep_copy_dict(previous_section)
    warnings: list[str] = []
    errors: list[str] = []
    status = "ok"
    refreshed = False

    api_key = os.getenv("LEMONSQUEEZY_API_KEY", "").strip()
    if not api_key:
        warning = "LemonSqueezy credentials missing; keeping previous or empty data."
        log("WARN", warning)
        warnings.append(warning)
        return section, "skipped", errors, warnings, False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/vnd.api+json",
    }

    orders_url = f"{LEMONSQUEEZY_API_BASE}/orders"
    orders, orders_error = client.get_paginated(
        orders_url,
        headers=headers,
        params={"page[size]": 100},
        item_key="data",
    )

    parsed_orders: list[dict[str, Any]] = []
    revenue_total = 0.0
    month_count = 0
    recent_orders: list[dict[str, Any]] = []
    current_time = now_utc()
    month_start = current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = current_time - timedelta(days=30)

    if orders_error:
        errors.append(f"LemonSqueezy orders fetch failed: {orders_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        for order in orders:
            if not isinstance(order, dict):
                continue
            parsed, order_warnings = parse_lemonsqueezy_order(order)
            parsed_orders.append(parsed)
            warnings.extend(order_warnings)
            revenue_total += safe_float(parsed.get("revenue_usd"))
            created_at = parse_datetime(parsed.get("created_at"))
            if created_at and created_at >= month_start:
                month_count += 1
            if created_at and created_at >= thirty_days_ago:
                recent_orders.append(parsed)

        recent_orders.sort(key=lambda order: order.get("created_at", ""), reverse=True)
        section["total_revenue_usd"] = safe_round(revenue_total)
        section["orders_this_month"] = month_count
        section["orders_last_30_days"] = recent_orders[:50]
        refreshed = True

    products_url = f"{LEMONSQUEEZY_API_BASE}/products"
    products, products_error = client.get_paginated(
        products_url,
        headers=headers,
        params={"page[size]": 100},
        item_key="data",
    )

    if products_error:
        errors.append(f"LemonSqueezy products fetch failed: {products_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        revenue_by_product: dict[str, float] = {}
        orders_by_product: dict[str, int] = {}
        for order in parsed_orders:
            product_name = order.get("product_name") or "Unknown"
            revenue_by_product[product_name] = revenue_by_product.get(product_name, 0.0) + safe_float(order.get("revenue_usd"))
            orders_by_product[product_name] = orders_by_product.get(product_name, 0) + 1

        parsed_products: list[dict[str, Any]] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            attributes = product.get("attributes", {})
            name = attributes.get("name", "")
            normalized_price = normalize_lemonsqueezy_amount(
                attributes.get("price"),
                formatted_value=attributes.get("price_formatted"),
                assume_cents=True,
            )
            parsed_products.append(
                {
                    "id": product.get("id", ""),
                    "name": name,
                    "status": attributes.get("status", ""),
                    "price_usd": safe_round(normalized_price),
                    "orders": orders_by_product.get(name, 0),
                    "revenue_usd": safe_round(revenue_by_product.get(name, 0.0)),
                }
            )
        section["products"] = parsed_products
        refreshed = True

        for product in parsed_products:
            price_usd = safe_float(product.get("price_usd"))
            orders_count = safe_int(product.get("orders"))
            revenue_usd = safe_float(product.get("revenue_usd"))
            if price_usd > 0 and orders_count > 0 and revenue_usd > price_usd * orders_count * 20:
                warnings.append(
                    f"LemonSqueezy revenue for product '{product.get('name', 'Unknown')}' looks unusually high "
                    f"(${safe_round(revenue_usd)} for {orders_count} orders at ${safe_round(price_usd)})."
                )

    if not section.get("products") and parsed_orders:
        fallback_products: dict[str, dict[str, Any]] = {}
        for order in parsed_orders:
            product_name = order.get("product_name") or "Unknown"
            entry = fallback_products.setdefault(
                product_name,
                {"id": product_name, "name": product_name, "status": "unknown", "price_usd": 0, "orders": 0, "revenue_usd": 0},
            )
            entry["orders"] += 1
            entry["revenue_usd"] = safe_round(entry["revenue_usd"] + safe_float(order.get("revenue_usd")))
        section["products"] = list(fallback_products.values())

    if not refreshed:
        failure = "LemonSqueezy integration failed completely; preserving previous data."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False

    if status == "partial":
        warnings.append("LemonSqueezy data is partially refreshed; missing endpoints used previous or fallback values.")

    return section, status, errors, warnings, True


def fetch_mailerlite_collection(
    client: HttpClient,
    endpoint: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any], str | None]:
    items: list[Any] = []
    next_url = f"{MAILERLITE_API_BASE}{endpoint}"
    next_params = params
    pages = 0
    first_payload: dict[str, Any] = {}

    while next_url and pages < MAX_PAGES:
        payload, error = client.request_json(next_url, headers=headers, params=next_params)
        if error:
            return items, first_payload, error
        if isinstance(payload, dict):
            if not first_payload:
                first_payload = payload
            chunk = payload.get("data", [])
            if isinstance(chunk, list):
                items.extend(chunk)
            elif chunk:
                items.append(chunk)
            links = payload.get("links", {})
            next_url = links.get("next") if isinstance(links, dict) else None
        else:
            next_url = None
        next_params = None
        pages += 1

    return items, first_payload, None


def fetch_mailerlite(
    client: HttpClient,
    previous_data: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str], list[str], bool]:
    previous_section = ensure_section(previous_data, MAILERLITE_SOURCE, default_mailerlite)
    section = deep_copy_dict(previous_section)
    warnings: list[str] = []
    errors: list[str] = []
    status = "ok"
    refreshed = False

    api_key = os.getenv("MAILERLITE_API_KEY", "").strip()
    if not api_key:
        warning = "MailerLite credentials missing; keeping previous or empty data."
        log("WARN", warning)
        warnings.append(warning)
        return section, "skipped", errors, warnings, False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    subscribers, subscribers_payload, subscribers_error = fetch_mailerlite_collection(
        client,
        "/subscribers",
        headers,
        params={"limit": 100},
    )

    if subscribers_error:
        errors.append(f"MailerLite subscribers fetch failed: {subscribers_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        meta = subscribers_payload.get("meta", {}) if isinstance(subscribers_payload, dict) else {}
        total_subscribers = safe_int(meta.get("total") or meta.get("total_results") or len(subscribers))
        week_ago = now_utc() - timedelta(days=7)
        new_subscribers = 0
        for subscriber in subscribers:
            if not isinstance(subscriber, dict):
                continue
            created_at = parse_datetime(
                subscriber.get("created_at")
                or subscriber.get("subscribed_at")
                or subscriber.get("date_created")
            )
            if created_at and created_at >= week_ago:
                new_subscribers += 1

        section["total_subscribers"] = total_subscribers
        section["new_subscribers_7d"] = new_subscribers
        refreshed = True

    campaigns, _, campaigns_error = fetch_mailerlite_collection(
        client,
        "/campaigns",
        headers,
        params={"limit": 50},
    )

    if campaigns_error:
        errors.append(f"MailerLite campaigns fetch failed: {campaigns_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        sent_campaigns: list[dict[str, Any]] = []
        for campaign in campaigns:
            if not isinstance(campaign, dict):
                continue
            statistics = safe_dict(campaign.get("statistics"))
            stats = safe_dict(campaign.get("stats"))
            open_rate = safe_float(
                campaign.get("open_rate")
                or statistics.get("open_rate")
                or stats.get("open_rate")
            )
            click_rate = safe_float(
                campaign.get("click_rate")
                or statistics.get("click_rate")
                or stats.get("click_rate")
            )
            status_value = str(campaign.get("status", "")).lower()
            if status_value in {"sent", "completed"} or open_rate > 0 or click_rate > 0:
                sent_campaigns.append({"open_rate": open_rate, "click_rate": click_rate})

        if sent_campaigns:
            section["avg_open_rate"] = safe_round(
                sum(item["open_rate"] for item in sent_campaigns) / len(sent_campaigns),
                2,
            )
            section["avg_click_rate"] = safe_round(
                sum(item["click_rate"] for item in sent_campaigns) / len(sent_campaigns),
                2,
            )
        else:
            section["avg_open_rate"] = 0
            section["avg_click_rate"] = 0
        refreshed = True

    automations, _, automations_error = fetch_mailerlite_collection(
        client,
        "/automations",
        headers,
        params={"limit": 50},
    )

    if automations_error:
        errors.append(f"MailerLite automations fetch failed: {automations_error}")
        log("ERROR", errors[-1])
        status = "partial"
    else:
        section["sequences"] = [
            {
                "id": automation.get("id", ""),
                "name": automation.get("name") or automation.get("title") or "",
                "status": automation.get("status", ""),
                "subscribers": safe_int(
                    automation.get("subscribers_count")
                    or nested_dict_value(automation, "stats", "subscribers")
                    or nested_dict_value(automation, "statistics", "subscribers")
                ),
                "emails": safe_int(
                    automation.get("emails_count")
                    or automation.get("workflow_emails_count")
                    or automation.get("steps_count")
                ),
            }
            for automation in automations
            if isinstance(automation, dict)
        ]
        refreshed = True

    if not refreshed:
        failure = "MailerLite integration failed completely; preserving previous data."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False

    if status == "partial":
        warnings.append("MailerLite data is partially refreshed; some lifecycle stats may be stale.")

    return section, status, errors, warnings, True


def calculate_funnel(
    meta_ads: dict[str, Any],
    lemonsqueezy: dict[str, Any],
    previous_funnel: dict[str, Any],
    meta_status: str,
    lemonsqueezy_status: str,
) -> dict[str, Any]:
    funnel = deep_copy_dict(previous_funnel)

    if meta_status not in {"failed", "skipped"}:
        impressions = sum(safe_int(ad_set.get("impressions")) for ad_set in meta_ads.get("ad_sets", []))
        clicks = sum(safe_int(ad_set.get("clicks")) for ad_set in meta_ads.get("ad_sets", []))
        opt_ins = safe_int(meta_ads.get("total_leads"))
        funnel["impressions"] = impressions
        funnel["clicks"] = clicks
        funnel["lp_visits"] = clicks
        funnel["opt_ins"] = opt_ins
        funnel["ctr"] = safe_round((clicks / impressions) * 100, 2) if impressions > 0 else 0
        funnel["lp_cvr"] = safe_round((opt_ins / clicks) * 100, 2) if clicks > 0 else 0

    paid_purchases = safe_int(meta_ads.get("total_purchases")) if meta_status not in {"failed", "skipped"} else 0
    revenue_purchases = 0
    if lemonsqueezy_status not in {"failed", "skipped"}:
        recent_orders = lemonsqueezy.get("orders_last_30_days", [])
        revenue_purchases = len(recent_orders) if isinstance(recent_orders, list) and recent_orders else safe_int(
            lemonsqueezy.get("orders_this_month")
        )

    if paid_purchases > 0 or revenue_purchases > 0:
        funnel["purchases_l1"] = max(paid_purchases, revenue_purchases)
    elif meta_status in {"failed", "skipped"} and lemonsqueezy_status in {"failed", "skipped"}:
        funnel["purchases_l1"] = safe_int(previous_funnel.get("purchases_l1"))
    else:
        funnel["purchases_l1"] = 0

    opt_ins = safe_int(funnel.get("opt_ins"))
    purchases = safe_int(funnel.get("purchases_l1"))
    consultations = safe_int(previous_funnel.get("consultations_l2"))
    funnel["consultations_l2"] = consultations
    funnel["email_cvr"] = safe_round((purchases / opt_ins) * 100, 2) if opt_ins > 0 else 0
    return funnel


def calculate_analysis(
    meta_ads: dict[str, Any],
    lemonsqueezy: dict[str, Any],
    mailerlite: dict[str, Any],
    funnel: dict[str, Any],
) -> dict[str, Any]:
    revenue = safe_float(lemonsqueezy.get("total_revenue_usd"))
    spend = safe_float(meta_ads.get("total_spend"))
    leads = safe_int(meta_ads.get("total_leads"))
    avg_cpl = safe_float(meta_ads.get("avg_cpl"))
    level1_purchases = safe_int(funnel.get("purchases_l1"))

    revenue_minus_spend = safe_round(revenue - spend)
    break_even_roas = safe_round(spend / revenue, 4) if revenue > 0 else 0

    active_ad_sets = [
        ad_set
        for ad_set in meta_ads.get("ad_sets", [])
        if isinstance(ad_set, dict) and str(ad_set.get("status", "")).upper() == "ACTIVE"
    ]
    data_available = any(
        [
            spend > 0,
            revenue > 0,
            leads > 0,
            level1_purchases > 0,
            safe_int(mailerlite.get("total_subscribers")) > 0,
            safe_int(funnel.get("clicks")) > 0,
            safe_int(funnel.get("opt_ins")) > 0,
            bool(active_ad_sets),
        ]
    )

    if not data_available:
        return {
            "break_even_roas": break_even_roas,
            "revenue_minus_spend": revenue_minus_spend,
            "top_problem_area": "",
            "top_opportunity_area": "",
            "claude_context_summary": (
                "Not enough live data is available yet. The dashboard currently contains default or preserved values "
                "until the integrations return a successful refresh."
            ),
        }

    top_problem_area = ""
    if spend > 0 and leads == 0:
        top_problem_area = "No leads from paid traffic"
    elif any(
        safe_float(ad_set.get("cpl")) > TARGET_CPL and safe_int(ad_set.get("impressions")) > 500
        for ad_set in active_ad_sets
    ):
        top_problem_area = "High CPL on active ad sets"
    elif safe_int(funnel.get("lp_visits")) > 0 and safe_float(funnel.get("lp_cvr")) < 15:
        top_problem_area = "Low landing page conversion"
    elif safe_int(mailerlite.get("new_subscribers_7d")) <= 0:
        top_problem_area = "Mailing list not growing"

    top_opportunity_area = ""
    if safe_float(funnel.get("email_cvr")) >= 5 and level1_purchases > 0:
        top_opportunity_area = "Strong email conversion"
    elif revenue > spend and spend > 0:
        top_opportunity_area = "Low spend with positive revenue"
    elif avg_cpl > 0 and avg_cpl < TARGET_CPL and leads >= 10:
        top_opportunity_area = "Healthy CPL with room to scale"

    summary = (
        f"Paid media spend is ${safe_round(spend)} with {leads} leads, "
        f"{level1_purchases} level-1 purchases, "
        f"and blended ROAS {safe_round(safe_float(meta_ads.get('blended_roas')), 2)}. "
        f"LemonSqueezy revenue is ${safe_round(revenue)}, so revenue minus spend is ${revenue_minus_spend}. "
        f"MailerLite has {safe_int(mailerlite.get('total_subscribers'))} subscribers and "
        f"{safe_int(mailerlite.get('new_subscribers_7d'))} new subscribers in the last 7 days. "
        f"Funnel metrics show {safe_int(funnel.get('clicks'))} clicks, {safe_int(funnel.get('opt_ins'))} opt-ins, "
        f"LP CVR {safe_round(safe_float(funnel.get('lp_cvr')), 2)}%, and email CVR {safe_round(safe_float(funnel.get('email_cvr')), 2)}%. "
        f"Main problem area: {top_problem_area or 'No acute issue detected'}. "
        f"Main opportunity: {top_opportunity_area or 'Keep monitoring current data mix for stronger signals'}."
    )
    summary = summary[:1200].strip()

    return {
        "break_even_roas": break_even_roas,
        "revenue_minus_spend": revenue_minus_spend,
        "top_problem_area": top_problem_area,
        "top_opportunity_area": top_opportunity_area,
        "claude_context_summary": summary,
    }


def calculate_overall_status(sources_status: dict[str, str]) -> str:
    source_values = [sources_status.get(source, "skipped") for source in ALL_SOURCES]
    if all(value in {"failed", "skipped"} for value in source_values):
        return "failed"
    if any(value in {"failed", "partial"} for value in source_values):
        return "partial"
    if all(value in {"ok", "skipped"} for value in source_values):
        return "ok"
    return "partial"


def build_final_payload(previous_data: dict[str, Any]) -> dict[str, Any]:
    client = HttpClient()
    errors: list[str] = []
    warnings: list[str] = []
    refreshed_sources = 0

    meta_ads, meta_status, meta_errors, meta_warnings, _, meta_refreshed = fetch_meta_ads(client, previous_data)
    instagram_organic, instagram_status, instagram_errors, instagram_warnings, instagram_refreshed = fetch_instagram_organic(client, previous_data)
    lemonsqueezy, lemonsqueezy_status, lemonsqueezy_errors, lemonsqueezy_warnings, lemonsqueezy_refreshed = fetch_lemonsqueezy(client, previous_data)
    mailerlite, mailerlite_status, mailerlite_errors, mailerlite_warnings, mailerlite_refreshed = fetch_mailerlite(client, previous_data)

    errors.extend(meta_errors + instagram_errors + lemonsqueezy_errors + mailerlite_errors)
    warnings.extend(meta_warnings + instagram_warnings + lemonsqueezy_warnings + mailerlite_warnings)
    refreshed_sources += int(meta_refreshed)
    refreshed_sources += int(instagram_refreshed)
    refreshed_sources += int(lemonsqueezy_refreshed)
    refreshed_sources += int(mailerlite_refreshed)

    funnel = calculate_funnel(
        meta_ads,
        lemonsqueezy,
        ensure_section(previous_data, "funnel", default_funnel),
        meta_status,
        lemonsqueezy_status,
    )
    analysis = calculate_analysis(meta_ads, lemonsqueezy, mailerlite, funnel)

    sources_status = default_sources_status()
    sources_status[META_SOURCE] = meta_status
    sources_status[INSTAGRAM_SOURCE] = instagram_status
    sources_status[LEMONSQUEEZY_SOURCE] = lemonsqueezy_status
    sources_status[MAILERLITE_SOURCE] = mailerlite_status
    sources_status["overall"] = calculate_overall_status(sources_status)

    if refreshed_sources > 0:
        last_updated = isoformat(now_utc())
    else:
        last_updated = previous_data.get("last_updated", "never")

    payload = {
        "last_updated": last_updated,
        "data_freshness_hours": calculate_freshness_hours(last_updated),
        "errors": errors,
        "warnings": warnings,
        "sources_status": sources_status,
        META_SOURCE: meta_ads,
        INSTAGRAM_SOURCE: instagram_organic,
        LEMONSQUEEZY_SOURCE: lemonsqueezy,
        MAILERLITE_SOURCE: mailerlite,
        "funnel": funnel,
        "analysis": analysis,
    }
    return payload


def main() -> int:
    previous_data, bootstrap_warnings = load_previous_data()
    payload = default_dashboard_data()

    try:
        payload = build_final_payload(previous_data)
        payload["warnings"] = bootstrap_warnings + payload.get("warnings", [])
    except Exception as exc:  # pragma: no cover - defensive top-level guard
        message = f"Unexpected fetcher error: {exc}"
        log("ERROR", message)
        payload = deep_copy_dict(previous_data)
        payload["errors"] = [message]
        payload["warnings"] = bootstrap_warnings + ["Previous dashboard data was preserved after an unexpected failure."]
        payload["sources_status"] = default_sources_status()
        payload["sources_status"]["overall"] = "failed"
        payload["data_freshness_hours"] = calculate_freshness_hours(payload.get("last_updated", "never"))

    atomic_write_json(DATA_FILE, TMP_DATA_FILE, payload)
    log("INFO", f"Dashboard data written to {DATA_FILE}")
    log("INFO", f"Overall source status: {payload.get('sources_status', {}).get('overall', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
