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
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
from dateutil import parser as date_parser


DATA_FILE = Path("data/senns_data.json")
TMP_DATA_FILE = Path("data/senns_data.tmp.json")

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"
GOOGLE_SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
GOOGLE_SERVICE_ACCOUNT_SECRET = "GOOGLE_SERVICE_ACCOUNT_JSON"
GOOGLE_SALES_SHEET_ID = "1yv9EmGQbYjfup141LvEv8R3i9NHf-2Kn4HMkyWKoEU8"
MAILERLITE_API_BASE = "https://connect.mailerlite.com/api"
NBP_API_BASE = "https://api.nbp.pl/api/exchangerates/rates/A"

REQUEST_TIMEOUT = 25
MAX_RETRIES = 2
MAX_PAGES = 8
TARGET_CPL = 25.0
STALE_DATA_HOURS = 36.0

META_SOURCE = "meta_ads"
INSTAGRAM_SOURCE = "instagram_organic"
SALES_SOURCE = "sales"
MAILERLITE_SOURCE = "mailerlite"
# Backward compatibility for reading pre-migration dashboard snapshots only.
LEGACY_SALES_SOURCE = "lemonsqueezy"
ALL_SOURCES = [META_SOURCE, INSTAGRAM_SOURCE, SALES_SOURCE, MAILERLITE_SOURCE]
SOURCE_LABELS = {
    META_SOURCE: "Meta Ads",
    INSTAGRAM_SOURCE: "Instagram",
    SALES_SOURCE: "Sales",
    MAILERLITE_SOURCE: "MailerLite",
}

SALES_SHEET_COLUMNS = [
    "Date",
    "Email",
    "Product",
    "Product Key",
    "Order ID",
    "Currency",
    "Price",
    "Payment Type",
    "Country",
    "Coupon Code",
    "Event Type",
    "License Key",
]

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


def default_sales() -> dict[str, Any]:
    return {
        "currency": "PLN",
        "source_currency": "USD",
        "fx_rate_usd_pln": 0,
        "fx_rate_date": None,
        "total_revenue_usd": 0,
        "total_revenue_pln": 0,
        "orders_this_month": 0,
        "last_30_days_orders": 0,
        "last_order_date": None,
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
        SALES_SOURCE: "skipped",
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
        SALES_SOURCE: default_sales(),
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


def merge_section(section: Any, factory: Any) -> dict[str, Any]:
    merged = factory()
    if isinstance(section, dict):
        merged.update(deep_copy_dict(section))
    return merged


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
        sources_status = deep_copy_dict(raw_data["sources_status"])
        if SALES_SOURCE not in sources_status and LEGACY_SALES_SOURCE in sources_status:
            sources_status[SALES_SOURCE] = sources_status.get(LEGACY_SALES_SOURCE)
        sources_status.pop(LEGACY_SALES_SOURCE, None)
        previous["sources_status"].update(sources_status)
    previous[META_SOURCE] = ensure_section(raw_data, META_SOURCE, default_meta_ads)
    previous[INSTAGRAM_SOURCE] = ensure_section(raw_data, INSTAGRAM_SOURCE, default_instagram_organic)
    sales_section = raw_data.get(SALES_SOURCE)
    if not isinstance(sales_section, dict):
        sales_section = raw_data.get(LEGACY_SALES_SOURCE)
    previous[SALES_SOURCE] = merge_section(sales_section, default_sales)
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


def sanitize_output_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = deep_copy_dict(payload)
    sanitized.pop(LEGACY_SALES_SOURCE, None)

    sources_status = sanitized.get("sources_status")
    if isinstance(sources_status, dict):
        sources_status.pop(LEGACY_SALES_SOURCE, None)
        if SALES_SOURCE not in sources_status:
            sources_status[SALES_SOURCE] = "skipped"

    sales_section = sanitized.get(SALES_SOURCE)
    if not isinstance(sales_section, dict):
        sanitized[SALES_SOURCE] = default_sales()

    analysis = sanitized.get("analysis")
    if isinstance(analysis, dict):
        summary = analysis.get("claude_context_summary")
        if isinstance(summary, str) and summary:
            analysis["claude_context_summary"] = summary.replace("LemonSqueezy revenue", "Sales revenue").replace(
                "LemonSqueezy", "Sales"
            )

    return sanitized


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


def get_google_sheets_access_token(service_account_info: dict[str, Any]) -> tuple[str | None, str | None]:
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except ImportError as exc:
        return None, f"Google Sheets dependency missing: {exc}"

    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=[GOOGLE_SHEETS_SCOPE],
        )
        credentials.refresh(GoogleAuthRequest(session=requests.Session()))
    except Exception as exc:  # pragma: no cover - network/auth path
        return None, f"Google Sheets authorization failed: {mask_sensitive_text(str(exc))}"

    if not credentials.token:
        return None, "Google Sheets authorization failed: access token missing."
    return str(credentials.token), None


def quote_sheet_range(sheet_title: str, cell_range: str = "A:Z") -> str:
    escaped_title = sheet_title.replace("'", "''")
    return quote(f"'{escaped_title}'!{cell_range}", safe="")


def row_cell(row: list[Any], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def get_sales_column_indexes(header_row: list[Any] | None) -> tuple[dict[str, int], bool, list[str]]:
    default_indexes = {column.lower(): index for index, column in enumerate(SALES_SHEET_COLUMNS)}
    if not header_row:
        return default_indexes, False, []

    header_lookup = {
        str(value).strip().lower(): index
        for index, value in enumerate(header_row)
        if str(value).strip()
    }
    matched_columns = set(default_indexes).intersection(header_lookup)
    if len(matched_columns) < 4:
        return default_indexes, False, []

    indexes = dict(default_indexes)
    for column in default_indexes:
        if column in header_lookup:
            indexes[column] = header_lookup[column]

    missing_columns = [column for column in SALES_SHEET_COLUMNS if column.lower() not in header_lookup]
    warnings: list[str] = []
    if missing_columns:
        warnings.append(
            "Sales sheet is missing expected columns: " + ", ".join(missing_columns)
        )

    return indexes, True, warnings


def build_sales_order(row: list[Any], column_indexes: dict[str, int], row_number: int) -> tuple[dict[str, Any], datetime | None, list[str]]:
    warnings: list[str] = []
    raw_date = row_cell(row, column_indexes.get("date"))
    raw_price = row_cell(row, column_indexes.get("price"))
    parsed_date = parse_datetime(raw_date)
    if raw_date and parsed_date is None:
        warnings.append(f"Sales row {row_number} has an invalid Date value: {raw_date}")

    parsed_price = parse_currency_string(raw_price)
    if raw_price and parsed_price is None:
        warnings.append(f"Sales row {row_number} has an invalid Price value: {raw_price}")
    price_usd = safe_round(parsed_price) if parsed_price is not None and parsed_price > 0 else 0

    email = row_cell(row, column_indexes.get("email"))
    product = row_cell(row, column_indexes.get("product"))
    product_key = row_cell(row, column_indexes.get("product key"))
    order_id = row_cell(row, column_indexes.get("order id"))
    currency = row_cell(row, column_indexes.get("currency"))
    payment_type = row_cell(row, column_indexes.get("payment type"))
    country = row_cell(row, column_indexes.get("country"))
    coupon_code = row_cell(row, column_indexes.get("coupon code"))
    event_type = row_cell(row, column_indexes.get("event type"))
    license_key = row_cell(row, column_indexes.get("license key"))

    order = {
        "id": order_id or f"row-{row_number}",
        "identifier": license_key or product_key or order_id or f"row-{row_number}",
        "status": event_type or payment_type or "paid",
        "created_at": isoformat(parsed_date) if parsed_date else raw_date,
        "revenue_usd": price_usd,
        "product_name": product,
        "customer_name": email,
        "quantity": 1,
        "unit_price_usd": price_usd,
        "currency": currency,
        "country": country,
        "coupon_code": coupon_code,
        "payment_type": payment_type,
        "event_type": event_type,
        "product_key": product_key,
        "license_key": license_key,
    }
    return order, parsed_date, warnings


def summarize_sales_products(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    product_map: dict[str, dict[str, Any]] = {}
    for order in orders:
        product_name = str(order.get("product_name") or "Unknown")
        product_key = str(order.get("product_key") or product_name)
        entry = product_map.setdefault(
            product_key,
            {
                "id": product_key,
                "name": product_name,
                "status": "",
                "price_usd": 0,
                "price_pln": 0,
                "orders": 0,
                "revenue_usd": 0,
                "revenue_pln": 0,
            },
        )
        entry["orders"] += 1
        entry["revenue_usd"] = safe_round(entry["revenue_usd"] + safe_float(order.get("revenue_usd")))
        entry["revenue_pln"] = safe_round(entry["revenue_pln"] + safe_float(order.get("revenue_pln")))
        if safe_float(order.get("unit_price_usd")) > 0:
            entry["price_usd"] = safe_round(safe_float(order.get("unit_price_usd")))
        if safe_float(order.get("unit_price_pln")) > 0:
            entry["price_pln"] = safe_round(safe_float(order.get("unit_price_pln")))

    products = list(product_map.values())
    products.sort(key=lambda item: (safe_float(item.get("revenue_pln")), safe_int(item.get("orders"))), reverse=True)
    return products


def fetch_usd_pln_rate(client: HttpClient, previous_sales: dict[str, Any]) -> tuple[float | None, str | None, list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    rate_url = f"{NBP_API_BASE}/USD"
    payload, request_error = client.request_json(rate_url, params={"format": "json"})

    if request_error:
        fallback_rate = safe_float(previous_sales.get("fx_rate_usd_pln"))
        fallback_date = str(previous_sales.get("fx_rate_date") or "").strip() or None
        if fallback_rate > 0:
            warnings.append(
                f"NBP USD/PLN fetch failed; using previous FX rate {safe_round(fallback_rate, 4)}."
            )
            return fallback_rate, fallback_date, errors, warnings
        errors.append(f"NBP USD/PLN fetch failed: {request_error}")
        return None, None, errors, warnings

    rates = payload.get("rates", []) if isinstance(payload, dict) else []
    first_rate = rates[0] if rates and isinstance(rates[0], dict) else {}
    mid = safe_float(first_rate.get("mid"))
    effective_date = str(first_rate.get("effectiveDate") or "").strip() or None
    if mid <= 0:
        errors.append("NBP USD/PLN response did not include a valid mid rate.")
        return None, None, errors, warnings

    return safe_round(mid, 6), effective_date, errors, warnings


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
    reporting_now = now_utc()
    reporting_until = reporting_now.date()
    reporting_since = reporting_until - timedelta(days=30)
    reporting_time_range = {
        "since": reporting_since.isoformat(),
        "until": reporting_until.isoformat(),
    }
    reporting_time_range_json = json.dumps(reporting_time_range, separators=(",", ":"))

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
    ad_sets_insights_fragment = (
        f'insights.time_range({reporting_time_range_json}).limit(1)'
        "{spend,impressions,clicks,cpm,ctr,cpc,frequency,actions}"
    )
    ad_sets_payload, ad_sets_error = client.request_json(
        ad_sets_url,
        params={
            "access_token": access_token,
            "fields": (
                "id,name,status,daily_budget,"
                f"{ad_sets_insights_fragment}"
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
            "time_range": reporting_time_range_json,
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


def fetch_sales_data(
    client: HttpClient,
    previous_data: dict[str, Any],
) -> tuple[dict[str, Any], str, list[str], list[str], bool]:
    previous_section = ensure_section(previous_data, SALES_SOURCE, default_sales)
    section = deep_copy_dict(previous_section)
    warnings: list[str] = []
    errors: list[str] = []
    status = "ok"
    refreshed = False

    service_account_raw = os.getenv(GOOGLE_SERVICE_ACCOUNT_SECRET, "").strip()
    if not service_account_raw:
        warning = "Google Sheets credentials missing; keeping previous or empty data."
        log("WARN", warning)
        warnings.append(warning)
        return section, "skipped", errors, warnings, False

    try:
        service_account_info = json.loads(service_account_raw)
    except ValueError as exc:
        failure = f"Google Sheets credentials are invalid JSON: {exc}"
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False

    access_token, auth_error = get_google_sheets_access_token(service_account_info)
    if auth_error:
        errors.append(auth_error)
        log("ERROR", auth_error)
        return previous_section, "failed", errors, warnings, False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    spreadsheet_url = f"{GOOGLE_SHEETS_API_BASE}/{GOOGLE_SALES_SHEET_ID}"
    spreadsheet_payload, spreadsheet_error = client.request_json(
        spreadsheet_url,
        headers=headers,
        params={"fields": "sheets(properties(title,index))"},
    )
    if spreadsheet_error:
        errors.append(f"Sales sheet metadata fetch failed: {spreadsheet_error}")
        log("ERROR", errors[-1])
        return previous_section, "failed", errors, warnings, False

    sheet_entries = spreadsheet_payload.get("sheets", []) if isinstance(spreadsheet_payload, dict) else []
    first_sheet_title = ""
    if isinstance(sheet_entries, list):
        ordered_sheets = sorted(
            (
                entry.get("properties", {})
                for entry in sheet_entries
                if isinstance(entry, dict) and isinstance(entry.get("properties"), dict)
            ),
            key=lambda item: safe_int(item.get("index")),
        )
        if ordered_sheets:
            first_sheet_title = str(ordered_sheets[0].get("title", "")).strip()

    if not first_sheet_title:
        failure = "Sales sheet metadata fetch returned no sheets."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False

    values_url = f"{GOOGLE_SHEETS_API_BASE}/{GOOGLE_SALES_SHEET_ID}/values/{quote_sheet_range(first_sheet_title)}"
    values_payload, values_error = client.request_json(
        values_url,
        headers=headers,
        params={"majorDimension": "ROWS"},
    )
    if values_error:
        errors.append(f"Sales sheet values fetch failed: {values_error}")
        log("ERROR", errors[-1])
        return previous_section, "failed", errors, warnings, False

    rows = values_payload.get("values", []) if isinstance(values_payload, dict) else []
    row_list = rows if isinstance(rows, list) else []
    header_row = row_list[0] if row_list and isinstance(row_list[0], list) else None
    column_indexes, has_header_row, header_warnings = get_sales_column_indexes(header_row)
    warnings.extend(header_warnings)
    if header_warnings:
        status = "partial"
    data_rows = row_list[1:] if has_header_row else row_list

    parsed_orders: list[dict[str, Any]] = []
    revenue_total_usd = 0.0
    revenue_total_pln = 0.0
    month_count = 0
    recent_orders: list[dict[str, Any]] = []
    recent_order_dates: list[tuple[datetime, dict[str, Any]]] = []
    current_time = now_utc()
    month_start = current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = current_time - timedelta(days=30)
    last_order_date: datetime | None = None
    usd_pln_rate, usd_pln_date, rate_errors, rate_warnings = fetch_usd_pln_rate(client, previous_section)
    errors.extend(rate_errors)
    warnings.extend(rate_warnings)
    if usd_pln_rate is None:
        failure = "Sales conversion requires USD/PLN rate but NBP rate is unavailable."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False
    if rate_warnings:
        status = "partial"

    for row_index, row in enumerate(data_rows, start=2 if has_header_row else 1):
        if not isinstance(row, list):
            continue
        if not any(str(value).strip() for value in row):
            continue
        parsed_order, order_date, order_warnings = build_sales_order(row, column_indexes, row_index)
        parsed_orders.append(parsed_order)
        warnings.extend(order_warnings)
        if order_warnings:
            status = "partial"

        order_revenue = safe_float(parsed_order.get("revenue_usd"))
        order_revenue_pln = safe_round(order_revenue * usd_pln_rate)
        parsed_order["revenue_pln"] = order_revenue_pln
        parsed_order["unit_price_pln"] = safe_round(safe_float(parsed_order.get("unit_price_usd")) * usd_pln_rate)
        if order_revenue > 0:
            revenue_total_usd += order_revenue
            revenue_total_pln += order_revenue_pln
        if order_date and order_date >= month_start:
            month_count += 1
        if order_date and order_date >= thirty_days_ago:
            recent_order_dates.append((order_date, parsed_order))
        if order_date and (last_order_date is None or order_date > last_order_date):
            last_order_date = order_date

    recent_orders = [
        order
        for _, order in sorted(recent_order_dates, key=lambda item: item[0], reverse=True)[:50]
    ]
    section["currency"] = "PLN"
    section["source_currency"] = "USD"
    section["fx_rate_usd_pln"] = safe_round(usd_pln_rate, 6)
    section["fx_rate_date"] = usd_pln_date
    section["total_revenue_usd"] = safe_round(revenue_total_usd)
    section["total_revenue_pln"] = safe_round(revenue_total_pln)
    section["orders_this_month"] = month_count
    section["last_30_days_orders"] = len(recent_orders)
    section["last_order_date"] = isoformat(last_order_date) if last_order_date else None
    section["orders_last_30_days"] = recent_orders
    section["products"] = summarize_sales_products(parsed_orders)
    refreshed = True

    if not refreshed:
        failure = "Sales integration failed completely; preserving previous data."
        errors.append(failure)
        log("ERROR", failure)
        return previous_section, "failed", errors, warnings, False

    if status == "partial":
        warnings.append("Sales data is partially refreshed; some rows could not be fully parsed.")

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
    sales: dict[str, Any],
    previous_funnel: dict[str, Any],
    meta_status: str,
    sales_status: str,
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
    if sales_status not in {"failed", "skipped"}:
        recent_orders = sales.get("orders_last_30_days", [])
        revenue_purchases = safe_int(sales.get("last_30_days_orders"))
        if revenue_purchases <= 0 and isinstance(recent_orders, list) and recent_orders:
            revenue_purchases = len(recent_orders)
        if revenue_purchases <= 0:
            revenue_purchases = safe_int(sales.get("orders_this_month"))

    if paid_purchases > 0 or revenue_purchases > 0:
        funnel["purchases_l1"] = max(paid_purchases, revenue_purchases)
    elif meta_status in {"failed", "skipped"} and sales_status in {"failed", "skipped"}:
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
    sales: dict[str, Any],
    mailerlite: dict[str, Any],
    funnel: dict[str, Any],
    sources_status: dict[str, str],
    data_freshness_hours: float | None,
) -> dict[str, Any]:
    revenue = safe_float(sales.get("total_revenue_pln"))
    spend = safe_float(meta_ads.get("total_spend"))
    leads = safe_int(meta_ads.get("total_leads"))
    avg_cpl = safe_float(meta_ads.get("avg_cpl"))
    level1_purchases = safe_int(funnel.get("purchases_l1"))
    clicks = safe_int(funnel.get("clicks"))
    impressions = safe_int(funnel.get("impressions"))
    lp_visits = safe_int(funnel.get("lp_visits"))
    opt_ins = safe_int(funnel.get("opt_ins"))
    total_subscribers = safe_int(mailerlite.get("total_subscribers"))
    new_subscribers = safe_int(mailerlite.get("new_subscribers_7d"))
    fx_rate = safe_float(sales.get("fx_rate_usd_pln"))
    fx_rate_date = sales.get("fx_rate_date")

    revenue_minus_spend = safe_round(revenue - spend)
    break_even_roas = safe_round(spend / revenue, 4) if revenue > 0 else 0

    active_ad_sets = [
        ad_set
        for ad_set in meta_ads.get("ad_sets", [])
        if isinstance(ad_set, dict) and str(ad_set.get("status", "")).upper() == "ACTIVE"
    ]
    failed_sources = [SOURCE_LABELS[source] for source in ALL_SOURCES if sources_status.get(source) == "failed"]
    partial_sources = [SOURCE_LABELS[source] for source in ALL_SOURCES if sources_status.get(source) == "partial"]
    overall_status = str(sources_status.get("overall", "skipped")).lower()
    meta_healthy = sources_status.get(META_SOURCE) == "ok"
    instagram_healthy = sources_status.get(INSTAGRAM_SOURCE) == "ok"
    measurable_paid_activity = any([spend > 0, impressions > 0, clicks > 0, bool(active_ad_sets)])
    measurable_funnel_data = any([impressions > 0, clicks > 0, lp_visits > 0, opt_ins > 0])
    sparse_data = any([revenue > 0, leads > 0, level1_purchases > 0, clicks > 0, opt_ins > 0]) and all(
        [
            clicks < 100,
            leads < 5,
            opt_ins < 10,
            level1_purchases <= 1,
        ]
    )
    is_stale = data_freshness_hours is not None and safe_float(data_freshness_hours) > STALE_DATA_HOURS

    if failed_sources:
        top_problem_area = f"Source failure is limiting conclusions ({', '.join(failed_sources)})"
    elif partial_sources:
        top_problem_area = f"Some source data is partial ({', '.join(partial_sources)})"
    elif is_stale:
        top_problem_area = "Dashboard data is stale"
    elif not measurable_paid_activity and meta_healthy:
        top_problem_area = "No paid activity detected yet"
    elif not measurable_funnel_data:
        top_problem_area = "Funnel has no measurable acquisition data yet"
    elif sparse_data:
        top_problem_area = "Data is still too sparse for strong performance conclusions"
    elif spend > 0 and leads == 0:
        top_problem_area = "No leads from paid traffic"
    elif any(
        safe_float(ad_set.get("cpl")) > TARGET_CPL and safe_int(ad_set.get("impressions")) > 500
        for ad_set in active_ad_sets
    ):
        top_problem_area = "High CPL on active ad sets"
    elif lp_visits > 0 and safe_float(funnel.get("lp_cvr")) < 15:
        top_problem_area = "Low landing page conversion"
    elif total_subscribers > 0 and new_subscribers <= 0:
        top_problem_area = "Mailing list not growing"
    elif overall_status == "ok" and not measurable_paid_activity and revenue <= 0:
        top_problem_area = "Healthy integrations but no campaign activity yet"
    else:
        top_problem_area = "No major operating issue detected right now"

    if level1_purchases == 1:
        top_opportunity_area = "First Level 1 sale recorded"
    elif safe_float(funnel.get("email_cvr")) >= 5 and level1_purchases > 0:
        top_opportunity_area = "Strong email conversion"
    elif revenue > spend and spend > 0:
        top_opportunity_area = "Low spend with positive revenue"
    elif avg_cpl > 0 and avg_cpl < TARGET_CPL and leads >= 10:
        top_opportunity_area = "Healthy CPL with room to scale"
    elif revenue > 0 and spend == 0:
        top_opportunity_area = "Sales are appearing before measured paid attribution"
    elif new_subscribers > 0 and total_subscribers < 100:
        top_opportunity_area = "Email list is growing but still too small for conversion conclusions"
    elif meta_healthy and instagram_healthy:
        top_opportunity_area = "Meta and Instagram integrations are healthy"
    else:
        top_opportunity_area = "Keep collecting fresh signal before making bigger optimizations"

    technical_state = "All integrations healthy"
    if failed_sources:
        technical_state = f"Failed sources: {', '.join(failed_sources)}"
    elif partial_sources:
        technical_state = f"Partial sources: {', '.join(partial_sources)}"
    elif is_stale:
        technical_state = f"Data is stale ({safe_round(safe_float(data_freshness_hours), 1)}h old)"

    business_state = top_problem_area
    if top_opportunity_area and top_opportunity_area != top_problem_area:
        business_state = f"{top_problem_area}. {top_opportunity_area}."

    summary = (
        f"Technical state: {technical_state}. "
        f"Business state: {business_state} "
        f"Paid spend is {safe_round(spend)} PLN with {leads} leads, {level1_purchases} level-1 purchases, "
        f"and blended ROAS {safe_round(safe_float(meta_ads.get('blended_roas')), 2)}. "
        f"Sales revenue is {safe_round(revenue)} PLN, so revenue minus spend is {revenue_minus_spend} PLN. "
        f"USD to PLN rate is {safe_round(fx_rate, 4)}"
        f"{f' ({fx_rate_date})' if fx_rate_date else ''}. "
        f"MailerLite has {total_subscribers} subscribers and {new_subscribers} new subscribers in the last 7 days. "
        f"Funnel metrics show {clicks} clicks, {opt_ins} opt-ins, "
        f"LP CVR {safe_round(safe_float(funnel.get('lp_cvr')), 2)}%, and email CVR {safe_round(safe_float(funnel.get('email_cvr')), 2)}%."
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
    sales, sales_status, sales_errors, sales_warnings, sales_refreshed = fetch_sales_data(client, previous_data)
    mailerlite, mailerlite_status, mailerlite_errors, mailerlite_warnings, mailerlite_refreshed = fetch_mailerlite(client, previous_data)

    errors.extend(meta_errors + instagram_errors + sales_errors + mailerlite_errors)
    warnings.extend(meta_warnings + instagram_warnings + sales_warnings + mailerlite_warnings)
    refreshed_sources += int(meta_refreshed)
    refreshed_sources += int(instagram_refreshed)
    refreshed_sources += int(sales_refreshed)
    refreshed_sources += int(mailerlite_refreshed)

    sources_status = default_sources_status()
    sources_status[META_SOURCE] = meta_status
    sources_status[INSTAGRAM_SOURCE] = instagram_status
    sources_status[SALES_SOURCE] = sales_status
    sources_status[MAILERLITE_SOURCE] = mailerlite_status
    sources_status["overall"] = calculate_overall_status(sources_status)

    if refreshed_sources > 0:
        last_updated = isoformat(now_utc())
    else:
        last_updated = previous_data.get("last_updated", "never")
    data_freshness_hours = calculate_freshness_hours(last_updated)

    funnel = calculate_funnel(
        meta_ads,
        sales,
        ensure_section(previous_data, "funnel", default_funnel),
        meta_status,
        sales_status,
    )
    analysis = calculate_analysis(meta_ads, sales, mailerlite, funnel, sources_status, data_freshness_hours)

    payload = {
        "last_updated": last_updated,
        "data_freshness_hours": data_freshness_hours,
        "errors": errors,
        "warnings": warnings,
        "sources_status": sources_status,
        META_SOURCE: meta_ads,
        INSTAGRAM_SOURCE: instagram_organic,
        SALES_SOURCE: sales,
        MAILERLITE_SOURCE: mailerlite,
        "funnel": funnel,
        "analysis": analysis,
    }
    return sanitize_output_payload(payload)


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

    payload = sanitize_output_payload(payload)
    atomic_write_json(DATA_FILE, TMP_DATA_FILE, payload)
    log("INFO", f"Dashboard data written to {DATA_FILE}")
    log("INFO", f"Overall source status: {payload.get('sources_status', {}).get('overall', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
