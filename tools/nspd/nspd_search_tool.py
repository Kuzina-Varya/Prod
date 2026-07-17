"""
Search tool for the National Spatial Data System portal (nspd.gov.ru).

The public entry point is `search_documents_for_construction()`. It keeps the
same batch-oriented contract as the other tools in this repository, but returns
spatial objects/features instead of legal documents. The tool does not rank
semantic usefulness; an LLM should inspect `documents` and request the next
batch when needed.
"""

import json
import re
import ssl
import sys
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3
from bs4 import BeautifulSoup
from requests import exceptions as request_errors


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HTTP = requests.Session()
HTTP.trust_env = False

BASE_URL = "https://nspd.gov.ru"
START_PAGE_URL = f"{BASE_URL}/"
MAP_PAGE_URL = f"{BASE_URL}/map?thematic=PKK"
SEARCH_ENDPOINT = f"{BASE_URL}/api/geoportal/v2/search/geoportal"

DEFAULT_TIMEOUT = 25

THEMATIC_SEARCH_NAMES = {
    1: "Земельные участки, ОКС, помещения",
    2: "Единицы кадастрового деления",
    5: "ЗОУИТ, природные и иные территории",
    7: "Территориальные зоны",
}

IMPORTANT_OPTION_KEYS = (
    "cad_num",
    "obj_label",
    "obj_kind_value",
    "readable_address",
    "land_record_type",
    "land_record_category_type",
    "permitted_use_established_by_document",
    "ownership_type",
    "right_type",
    "specified_area",
    "declared_area",
    "area",
    "cost_value",
    "quarter_cad_number",
    "status",
    "date_cr",
    "is_actual",
)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def safe_int(value, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value


def is_cadastral_like(query: str) -> bool:
    return bool(re.search(r"\b\d{2}:\d{2}:\d{6,7}(?::\d+)?\b", str(query or "")))


def cadastral_depth(query: str) -> int:
    match = re.search(r"\b\d{2}:\d{2}:\d{6,7}(?::\d+)?\b", str(query or ""))
    if not match:
        return 0
    return match.group(0).count(":") + 1


def extract_cadastral_numbers(text: str) -> list[str]:
    return sorted(set(re.findall(r"\b\d{2}:\d{2}:\d{6,7}(?::\d+)?\b", str(text or ""))))


def collect_query_matches(query: str, *parts: str) -> list[str]:
    query_words = [
        word.lower()
        for word in re.findall(r"[а-яa-z0-9:.-]+", str(query or ""), flags=re.I)
        if len(word) >= 3 or ":" in word
    ]
    haystack = " ".join(str(part or "").lower() for part in parts)
    matches = []
    for word in query_words:
        if word in haystack and word not in matches:
            matches.append(word)
    return matches[:20]


def build_thematic_ids(query: str, thematic_search_ids=None, max_pages: int = 4) -> list[int]:
    if thematic_search_ids is not None:
        raw_ids = thematic_search_ids
    elif cadastral_depth(query) >= 4:
        raw_ids = [1, 2, 5, 7]
    elif is_cadastral_like(query):
        raw_ids = [2, 1, 5, 7]
    else:
        raw_ids = [1, 2, 5, 7]

    ids = []
    for value in raw_ids:
        number = safe_int(value, 0)
        if number > 0 and number not in ids:
            ids.append(number)
    return ids[:max(1, max_pages)]


def build_search_url(query: str, thematic_search_id: int) -> str:
    # NSPD WAF can reject the same request if the query parameter is placed
    # before thematicSearchId, so keep this exact parameter order.
    encoded_query = quote(str(query or ""), safe=":")
    return f"{SEARCH_ENDPOINT}?thematicSearchId={thematic_search_id}&query={encoded_query}"


def request_headers() -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": MAP_PAGE_URL,
        "Origin": BASE_URL,
    }


def fetch_url(url: str, verify_tls: bool = False) -> requests.Response:
    return HTTP.get(
        url,
        timeout=DEFAULT_TIMEOUT,
        verify=verify_tls,
        headers=request_headers(),
    )


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, request_errors.SSLError):
        return "tls_certificate_error"
    if isinstance(exc, request_errors.ProxyError):
        return "proxy_error"
    if isinstance(exc, request_errors.Timeout):
        return "timeout"
    if isinstance(exc, request_errors.ConnectionError):
        return "connection_error"
    return type(exc).__name__


def parse_forbidden_details(text: str) -> dict:
    details = {}
    request_id = re.search(r"Request ID:\s*([^<\r\n]+)", text or "")
    rule = re.search(r"Rule:\s*([^<\r\n]+)", text or "")
    client_ip = re.search(r"Client IP:\s*([^<\r\n]+)", text or "")
    if request_id:
        details["request_id"] = normalize_space(request_id.group(1))
    if rule:
        details["waf_rule"] = normalize_space(rule.group(1))
    if client_ip:
        details["client_ip"] = normalize_space(client_ip.group(1))
    return details


def summarize_options(options: dict) -> dict:
    if not isinstance(options, dict):
        return {}
    result = {}
    for key in IMPORTANT_OPTION_KEYS:
        value = options.get(key)
        if value not in (None, ""):
            result[key] = value
    return result


def iter_coordinates(geometry):
    if not isinstance(geometry, dict):
        return
    coords = geometry.get("coordinates")

    def walk(value):
        if not isinstance(value, list):
            return
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            yield float(value[0]), float(value[1])
            return
        for item in value:
            yield from walk(item)

    yield from walk(coords)


def geometry_summary(geometry: dict, include_full_geometry: bool = False) -> dict:
    if not isinstance(geometry, dict):
        return {}
    points = list(iter_coordinates(geometry))
    result = {
        "geometry_type": geometry.get("type"),
        "crs": ((geometry.get("crs") or {}).get("properties") or {}).get("name"),
        "points_count": len(points),
    }
    if points:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        result["bbox"] = [min(xs), min(ys), max(xs), max(ys)]
        result["coordinates_preview"] = points[:5]
    if include_full_geometry:
        result["geometry"] = geometry
    return result


def make_feature_document(feature: dict, query: str, thematic_search_id: int, source_url: str, include_full_geometry: bool) -> dict:
    properties = feature.get("properties") or {}
    options = properties.get("options") or {}
    category_name = properties.get("categoryName") or "Объект НСПД"
    label = properties.get("label") or properties.get("descr") or options.get("cad_num") or str(feature.get("id"))
    address = options.get("readable_address") or ""
    title = normalize_space(f"{category_name}: {label}")
    if address:
        snippet = normalize_space(f"{label}. {address}")
    else:
        option_summary = summarize_options(options)
        snippet = normalize_space("; ".join(f"{key}: {value}" for key, value in option_summary.items()))

    document = {
        "title": title,
        "url": source_url,
        "document_type": category_name,
        "content_status": "geojson_feature",
        "source_mode": "nspd_geoportal_api",
        "source_search_url": source_url,
        "source_query_variant": query,
        "match_query_used": query,
        "thematic_search_id": thematic_search_id,
        "thematic_search_name": THEMATIC_SEARCH_NAMES.get(thematic_search_id, "Неизвестный тематический поиск"),
        "object_id": feature.get("id"),
        "category_id": properties.get("category"),
        "category_name": category_name,
        "label": label,
        "external_key": properties.get("externalKey"),
        "cadastral_number": options.get("cad_num") or properties.get("externalKey"),
        "address": address,
        "snippet": snippet[:1000],
        "matched_keywords": collect_query_matches(query, title, label, address, snippet),
        "exact_identifier_matches": sorted(set(extract_cadastral_numbers(query)) & set(extract_cadastral_numbers(f"{label} {snippet}"))),
        "properties_preview": summarize_options(options),
        "geometry_summary": geometry_summary(feature.get("geometry"), include_full_geometry=include_full_geometry),
    }
    return document


def parse_search_response(response: requests.Response, query: str, thematic_search_id: int, include_full_geometry: bool) -> tuple[list[dict], dict]:
    report = {
        "mode": "nspd_geoportal_api",
        "query_variant": query,
        "thematic_search_id": thematic_search_id,
        "thematic_search_name": THEMATIC_SEARCH_NAMES.get(thematic_search_id, "Неизвестный тематический поиск"),
        "url": response.url,
        "status": "ok",
        "http_status": response.status_code,
        "severity": "",
        "ignored": False,
        "error_kind": "",
        "candidates_found": 0,
        "error": "",
        "diagnostic": "",
    }

    if response.status_code == 403:
        report.update({
            "status": "error",
            "severity": "warning",
            "error_kind": "nspd_waf_forbidden",
            "error": "NSPD WAF returned 403 Forbidden.",
            "diagnostic": parse_forbidden_details(response.text),
        })
        return [], report

    try:
        payload = response.json()
    except ValueError:
        report.update({
            "status": "error",
            "severity": "error",
            "error_kind": "non_json_response",
            "error": normalize_space(response.text[:500]),
        })
        return [], report

    if response.status_code == 404 and payload.get("code") == 204:
        report.update({
            "status": "ok",
            "severity": "",
            "error_kind": "no_objects_found",
            "error": payload.get("message", ""),
            "diagnostic": "NSPD returned no objects for this thematic search.",
        })
        return [], report

    if response.status_code >= 400:
        report.update({
            "status": "error",
            "severity": "warning",
            "error_kind": f"http_{response.status_code}",
            "error": payload.get("message", normalize_space(response.text[:500])),
            "diagnostic": payload,
        })
        return [], report

    features = (((payload.get("data") or {}).get("features")) or [])
    documents = [
        make_feature_document(feature, query, thematic_search_id, response.url, include_full_geometry)
        for feature in features
        if isinstance(feature, dict)
    ]
    report["candidates_found"] = len(documents)
    report["diagnostic"] = payload.get("meta", [])
    return documents, report


def diagnose_nspd_connection() -> str:
    checks = []

    for name, trust_env, verify_tls in [
        ("system_proxy_and_default_tls", True, True),
        ("direct_default_tls", False, True),
        ("direct_insecure_tls", False, False),
    ]:
        session = requests.Session()
        session.trust_env = trust_env
        item = {
            "name": name,
            "trust_env": trust_env,
            "verify_tls": verify_tls,
            "url": START_PAGE_URL,
            "status": "unknown",
        }
        try:
            response = session.get(
                START_PAGE_URL,
                timeout=DEFAULT_TIMEOUT,
                verify=verify_tls,
                headers={"User-Agent": request_headers()["User-Agent"]},
            )
            response.encoding = "utf-8"
            title = ""
            if "html" in (response.headers.get("content-type") or ""):
                soup = BeautifulSoup(response.text, "html.parser")
                title = normalize_space(soup.title.get_text(" ", strip=True) if soup.title else "")
            item.update({
                "status": "ok",
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "content_length": len(response.content),
                "title": title,
            })
        except Exception as exc:
            item.update({
                "status": "error",
                "error_kind": classify_exception(exc),
                "error": str(exc),
            })
        checks.append(item)

    return json.dumps({
        "status": "success",
        "site": START_PAGE_URL,
        "diagnostic": checks,
        "note": (
            "nspd.gov.ru uses a Russian trusted certificate chain. Standard "
            "Python/Chrome installations can reject it unless the Russian root "
            "certificates are trusted. This tool uses direct connection with "
            "verify_tls=False by default."
        ),
    }, ensure_ascii=False, indent=2)


def search_documents_for_construction(
    search_query: str,
    iteration_count=1,
    max_pages=4,
    limit=5,
    min_score=0.15,
    include_text=True,
    include_full_text=False,
    thematic_search_ids=None,
    include_full_geometry=False,
    verify_tls=False,
) -> str:
    query = normalize_space(search_query)
    if not query:
        return json.dumps({
            "status": "error",
            "message": "Поисковый запрос не может быть пустым.",
            "documents": [],
        }, ensure_ascii=False, indent=2)

    iteration_count = max(1, safe_int(iteration_count, 1))
    max_pages = max(1, safe_int(max_pages, 4))
    limit = max(1, safe_int(limit, 5))
    thematic_ids = build_thematic_ids(query, thematic_search_ids=thematic_search_ids, max_pages=max_pages)

    all_documents = []
    page_reports = []
    seen_keys = set()

    for thematic_id in thematic_ids:
        url = build_search_url(query, thematic_id)
        try:
            response = fetch_url(url, verify_tls=verify_tls)
            documents, report = parse_search_response(response, query, thematic_id, include_full_geometry)
        except Exception as exc:
            documents = []
            report = {
                "mode": "nspd_geoportal_api",
                "query_variant": query,
                "thematic_search_id": thematic_id,
                "thematic_search_name": THEMATIC_SEARCH_NAMES.get(thematic_id, "Неизвестный тематический поиск"),
                "url": url,
                "status": "error",
                "http_status": 0,
                "severity": "error",
                "ignored": False,
                "error_kind": classify_exception(exc),
                "candidates_found": 0,
                "error": str(exc),
                "diagnostic": "",
            }

        page_reports.append(report)
        for document in documents:
            key = (document.get("object_id"), document.get("category_id"), document.get("cadastral_number"))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_documents.append(document)

    offset = (iteration_count - 1) * limit
    selected = all_documents[offset:offset + limit]

    result = {
        "status": "success",
        "query": query,
        "current_iteration": iteration_count,
        "max_pages": max_pages,
        "limit": limit,
        "source_site": START_PAGE_URL,
        "search_endpoint": SEARCH_ENDPOINT,
        "search_strategy": "nspd_geoportal_v2_search_by_thematic_ids",
        "query_variants": [query],
        "tls_note": "По умолчанию verify_tls=False из-за российской цепочки сертификата NSPD.",
        "proxy_note": "HTTP.trust_env=False: системный proxy не используется.",
        "min_score_ignored": min_score,
        "include_text_ignored": include_text,
        "include_full_text_ignored": include_full_text,
        "thematic_search_ids": thematic_ids,
        "exact_document_numbers": extract_cadastral_numbers(query),
        "pages_checked": len(page_reports),
        "page_errors": sum(1 for report in page_reports if report["status"] == "error"),
        "page_skipped": 0,
        "page_reports": page_reports,
        "total_candidates_found": len(all_documents),
        "documents_returned": len(selected),
        "has_more_documents": offset + limit < len(all_documents),
        "documents": selected,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def search_nspd_objects(*args, **kwargs) -> str:
    return search_documents_for_construction(*args, **kwargs)


if __name__ == "__main__":
    print("=" * 80)
    print("Диагностика подключения")
    print(diagnose_nspd_connection())

    tests = [
        {
            "name": "Позитивный 1: земельный участок",
            "query": "03:01:320101:8",
            "iteration_count": 1,
            "thematic_search_ids": [1, 2, 5, 7],
        },
        {
            "name": "Позитивный 2: кадастровый квартал",
            "query": "77:02:0011003",
            "iteration_count": 1,
            "thematic_search_ids": [2, 1, 5, 7],
        },
        {
            "name": "Позитивный 3: здание",
            "query": "52:51:0010002:1139",
            "iteration_count": 1,
            "thematic_search_ids": [1, 2, 5, 7],
        },
        {
            "name": "Граничный 4: пустой запрос",
            "query": "   ",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 5: отрицательная итерация",
            "query": "03:01:320101:8",
            "iteration_count": -5,
            "thematic_search_ids": [1],
        },
        {
            "name": "Граничный 6: строка вместо номера итерации",
            "query": "03:01:320101:8",
            "iteration_count": "два",
            "thematic_search_ids": [1],
        },
        {
            "name": "Граничный 7: огромная итерация",
            "query": "03:01:320101:8",
            "iteration_count": 9999,
            "thematic_search_ids": [1],
        },
        {
            "name": "Граничный 8: маленький limit",
            "query": "77:02:0011003",
            "iteration_count": 1,
            "limit": 1,
            "thematic_search_ids": [2, 5],
        },
    ]

    for test in tests:
        print("=" * 80)
        print(test["name"])
        print(search_documents_for_construction(
            search_query=test["query"],
            iteration_count=test["iteration_count"],
            max_pages=4,
            limit=test.get("limit", 5),
            thematic_search_ids=test.get("thematic_search_ids"),
            include_full_geometry=False,
            verify_tls=False,
        ))

    out_path = Path(__file__).resolve().parent / "nspd_last_result.json"
    out_path.write_text(
        search_documents_for_construction(
            search_query="03:01:320101:8",
            max_pages=4,
            limit=5,
            thematic_search_ids=[1, 2, 5, 7],
            include_full_geometry=False,
            verify_tls=False,
        ),
        encoding="utf-8",
    )
    print(f"Saved last result to: {out_path}")
