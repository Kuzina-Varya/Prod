"""
Search tool for the FAU FCC ruleset registry on faufcc.ru.

The public entry point is `search_documents_for_construction()`. It returns a
JSON string with ruleset registry entries in batches. The tool does not score
semantic usefulness; an LLM should inspect `documents` and request the next
batch when needed.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests import exceptions as request_errors


HTTP = requests.Session()
HTTP.trust_env = False

SITE_BASE_URL = "https://faufcc.ru"
API_BASE_URL = "https://api.faufcc.ru"
START_PAGE_PATH = "/deiatelnost/normirovanie-i-standartizatsiia/reestr-svodov-pravil"
START_PAGE_URL = f"{SITE_BASE_URL}{START_PAGE_PATH}"
PAGE_FIND_URL = f"{API_BASE_URL}/api/pages/find"
PAGE_URL = f"{API_BASE_URL}/api/pages"
CATEGORIES_URL = f"{API_BASE_URL}/api/registries/categories"
ENTRIES_URL = f"{API_BASE_URL}/api/registries/entries"

DEFAULT_TIMEOUT = 30
REGISTRY_INCLUDE = "sections.type,sections.groups.fields.value,registry"
ENTRY_INCLUDE = "asset,category"
KNOWN_PAGE_ID = "199ac460-6619-45ad-b44a-0b59b6f727a1"
KNOWN_REGISTRY_ID = "c5020d9a-a239-4806-a793-ac66cb36c71a"
KNOWN_REGISTRY_TYPE = "ruleset"
KNOWN_REGISTRY_TITLE = "Реестр сводов правил"

_DISCOVERY_CACHE = None
_CATEGORIES_CACHE = {}
_ENTRIES_CACHE = {}


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE_BASE_URL,
        "Referer": START_PAGE_URL,
    }


def get_json(url: str, params: dict | None = None) -> tuple[dict, str, int]:
    time.sleep(0.08)
    response = HTTP.get(url, params=params, headers=request_headers(), timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    return response.json(), response.url, response.status_code


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, request_errors.Timeout):
        return "timeout"
    if isinstance(exc, request_errors.ProxyError):
        return "proxy_error"
    if isinstance(exc, request_errors.SSLError):
        return "tls_certificate_error"
    if isinstance(exc, request_errors.ConnectionError):
        return "connection_error"
    if isinstance(exc, request_errors.HTTPError):
        return "http_error"
    return type(exc).__name__


def compact_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def extract_ruleset_numbers(text: str) -> list[str]:
    patterns = [
        r"\bСП\s*\d+(?:\.\d+){0,2}(?:-\d+)?(?:\.\d{4})?\b",
        r"\bСНиП\s*[IVXLC\d.\-]+(?:-\d+)?\b",
        r"\bГОСТ\s*[Рр]?\s*\d+(?:[.\-]\d+)*\b",
    ]
    found = []
    for pattern in patterns:
        for match in re.findall(pattern, str(text or ""), flags=re.I):
            value = normalize_space(match).upper().replace("  ", " ")
            if value not in found:
                found.append(value)
    return found


def normalize_query_words(query: str) -> list[str]:
    text = str(query or "").lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9.\s-]+", " ", text, flags=re.I)
    stop_words = {
        "какие", "какой", "какая", "какое", "документы", "документ", "нужны",
        "нужен", "нужно", "надо", "для", "при", "по", "на", "и", "в", "во",
        "с", "со", "к", "ко", "от", "до", "об", "о", "сп", "снип", "гост",
    }
    words = []
    for word in text.split():
        word = word.strip(" .,-_")
        if not word or word in stop_words:
            continue
        if len(word) < 3 and not any(char.isdigit() for char in word):
            continue
        if word not in words:
            words.append(word)
    return words[:20]


def collect_query_matches(query: str, *parts: str) -> list[str]:
    haystack = " ".join(str(part or "").lower().replace("ё", "е") for part in parts)
    matches = []
    for number in extract_ruleset_numbers(query):
        if number.lower() in haystack and number not in matches:
            matches.append(number)
    for word in normalize_query_words(query):
        if word in haystack and word not in matches:
            matches.append(word)
    return matches[:30]


def exact_identifier_matches(query: str, *parts: str) -> list[str]:
    query_numbers = set(extract_ruleset_numbers(query))
    text_numbers = set(extract_ruleset_numbers(" ".join(str(part or "") for part in parts)))
    return sorted(query_numbers & text_numbers)


def fetch_start_page_status() -> dict:
    try:
        response = HTTP.get(START_PAGE_URL, headers={"User-Agent": request_headers()["User-Agent"]}, timeout=DEFAULT_TIMEOUT)
        response.encoding = "utf-8"
        title = ""
        if "html" in (response.headers.get("content-type") or ""):
            soup = BeautifulSoup(response.text, "html.parser")
            title = normalize_space(soup.title.get_text(" ", strip=True) if soup.title else "")
        return {
            "mode": "start_page",
            "url": response.url,
            "status": "ok",
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "content_length": len(response.content),
            "title": title,
            "error_kind": "",
            "error": "",
        }
    except Exception as exc:
        return {
            "mode": "start_page",
            "url": START_PAGE_URL,
            "status": "error",
            "http_status": 0,
            "error_kind": classify_exception(exc),
            "error": str(exc),
        }


def discover_registry() -> tuple[dict | None, list[dict]]:
    global _DISCOVERY_CACHE
    if _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE, [{
            "mode": "registry_discovery_cache",
            "url": START_PAGE_URL,
            "status": "ok",
            "http_status": 0,
            "candidates_found": 1,
            "error_kind": "",
            "error": "",
            "diagnostic": {
                "page_id": (_DISCOVERY_CACHE.get("page") or {}).get("id"),
                "registry_id": (_DISCOVERY_CACHE.get("registry") or {}).get("id"),
            },
        }]

    reports = []
    try:
        page_payload, page_url, http_status = get_json(PAGE_FIND_URL, {
            "include": "parents,sections.type,sections.groups.fields.value,sidebars.groups.fields.value,sidebars.type,permissions",
            "url": START_PAGE_PATH,
        })
        page = page_payload.get("data") or {}
        reports.append({
            "mode": "page_find",
            "url": page_url,
            "status": "ok",
            "http_status": http_status,
            "candidates_found": 1 if page else 0,
            "error_kind": "",
            "error": "",
            "diagnostic": {
                "page_id": page.get("id"),
                "page_name": page.get("name"),
                "page_type": (page.get("type") or {}).get("name"),
            },
        })
        if not page.get("id"):
            return None, reports

        page_details, details_url, details_status = get_json(f"{PAGE_URL}/{page['id']}", {"include": REGISTRY_INCLUDE})
        details = page_details.get("data") or {}
        registry = ((details.get("registry") or {}).get("data")) or {}
        reports.append({
            "mode": "page_registry",
            "url": details_url,
            "status": "ok",
            "http_status": details_status,
            "candidates_found": 1 if registry else 0,
            "error_kind": "",
            "error": "",
            "diagnostic": {
                "registry_id": registry.get("id"),
                "registry_type": ((registry.get("type") or {}).get("name")),
                "registry_title": ((registry.get("type") or {}).get("title")),
            },
        })
        _DISCOVERY_CACHE = {
            "page": details,
            "registry": registry,
        }
        return _DISCOVERY_CACHE, reports
    except Exception as exc:
        reports.append({
            "mode": "registry_discovery",
            "url": PAGE_FIND_URL,
            "status": "error",
            "http_status": 0,
            "candidates_found": 0,
            "error_kind": classify_exception(exc),
            "error": str(exc),
            "diagnostic": "",
        })
        fallback = {
            "page": {
                "id": KNOWN_PAGE_ID,
                "slug": "reestr-svodov-pravil",
                "link": START_PAGE_PATH,
                "name": "Реестр сводов правил",
            },
            "registry": {
                "id": KNOWN_REGISTRY_ID,
                "type": {
                    "name": KNOWN_REGISTRY_TYPE,
                    "title": KNOWN_REGISTRY_TITLE,
                },
            },
        }
        reports.append({
            "mode": "registry_discovery_fallback",
            "url": START_PAGE_URL,
            "status": "ok",
            "http_status": 0,
            "candidates_found": 1,
            "error_kind": "",
            "error": "",
            "diagnostic": "Used known page_id and registry_id after API discovery failed.",
        })
        _DISCOVERY_CACHE = fallback
        return fallback, reports


def fetch_categories(registry_id: str) -> tuple[list[dict], list[dict]]:
    if registry_id in _CATEGORIES_CACHE:
        return _CATEGORIES_CACHE[registry_id], [{
            "mode": "registry_categories_cache",
            "url": CATEGORIES_URL,
            "status": "ok",
            "http_status": 0,
            "candidates_found": len(_CATEGORIES_CACHE[registry_id]),
            "error_kind": "",
            "error": "",
            "diagnostic": "Used in-process categories cache.",
        }]

    reports = []
    categories = []

    def walk(parent_id: str | None, parent_path: list[str]):
        params = {"registry": registry_id, "parent": parent_id if parent_id else "undefined"}
        try:
            payload, request_url, http_status = get_json(CATEGORIES_URL, params)
            children = payload.get("data") or []
            reports.append({
                "mode": "registry_categories",
                "url": request_url,
                "status": "ok",
                "http_status": http_status,
                "candidates_found": len(children),
                "error_kind": "",
                "error": "",
                "diagnostic": {
                    "parent_id": parent_id,
                    "parent_path": " / ".join(parent_path),
                },
            })
            for child in children:
                path = [*parent_path, child.get("name") or child.get("id")]
                item = dict(child)
                item["path"] = path
                item["parent_id"] = parent_id
                categories.append(item)
                walk(child.get("id"), path)
        except Exception as exc:
            reports.append({
                "mode": "registry_categories",
                "url": CATEGORIES_URL,
                "status": "error",
                "http_status": 0,
                "candidates_found": 0,
                "error_kind": classify_exception(exc),
                "error": str(exc),
                "diagnostic": {
                    "parent_id": parent_id,
                    "parent_path": " / ".join(parent_path),
                },
            })

    walk(None, [])
    _CATEGORIES_CACHE[registry_id] = categories
    return categories, reports


def entry_asset(entry: dict) -> dict:
    return ((entry.get("asset") or {}).get("data")) or {}


def make_document(entry: dict, category: dict, query: str, source_url: str) -> dict:
    asset = entry_asset(entry)
    links = asset.get("links") or {}
    api_category = ((entry.get("category") or {}).get("data")) or {}
    if api_category and not category:
        category = api_category
    category_name = category.get("name") or api_category.get("name") or ""
    category_path = category.get("path") or ([category_name] if category_name else [])
    number = normalize_space(entry.get("number") or "")
    name = normalize_space(entry.get("name") or "")
    title = normalize_space(f"{number} {name}") if number else name
    state = entry.get("state") or {}
    snippet_parts = [
        f"Категория: {' / '.join(category_path)}",
        f"Статус: {state.get('title')}" if state.get("title") else "",
        f"Действует с: {entry.get('activeSince')}" if entry.get("activeSince") else "",
        f"Действует до: {entry.get('activeTill')}" if entry.get("activeTill") else "",
    ]
    snippet = normalize_space(". ".join(part for part in snippet_parts if part))
    url = links.get("download") or links.get("open") or entry.get("link") or source_url
    return {
        "title": title,
        "url": url,
        "document_type": "Свод правил",
        "content_status": "binary_document_not_parsed_here" if asset else "registry_entry",
        "source_mode": "faufcc_registry_api",
        "source_search_url": source_url,
        "source_query_variant": query,
        "match_query_used": query,
        "entry_id": entry.get("id"),
        "number": number,
        "name": name,
        "state": state,
        "active_since": entry.get("activeSince"),
        "active_till": entry.get("activeTill"),
        "suspended_since": entry.get("suspendedSince"),
        "suspended_till": entry.get("suspendedTill"),
        "cancelled_at": entry.get("cancelledAt"),
        "category_id": category.get("id"),
        "category_name": category_name,
        "category_path": category_path,
        "asset": {
            "id": asset.get("id"),
            "mime": asset.get("mime"),
            "name": asset.get("name"),
            "filename": asset.get("filename"),
            "extension": asset.get("extension"),
            "open_url": links.get("open"),
            "download_url": links.get("download"),
        } if asset else {},
        "matched_keywords": collect_query_matches(query, number, name, snippet),
        "exact_identifier_matches": exact_identifier_matches(query, number, name),
        "snippet": snippet,
    }


def fetch_entries_for_category(registry_id: str, category: dict, query: str, max_pages_per_category: int) -> tuple[list[dict], list[dict]]:
    documents = []
    reports = []
    category_id = category.get("id")
    total_pages = 1
    page = 1
    while page <= total_pages and page <= max_pages_per_category:
        cache_key = (registry_id, category_id, page)
        try:
            from_cache = cache_key in _ENTRIES_CACHE
            if cache_key in _ENTRIES_CACHE:
                payload, request_url, http_status = _ENTRIES_CACHE[cache_key]
            else:
                filters = compact_json({"registry": registry_id, "category": category_id})
                params = {"page": page, "filters": filters, "include": ENTRY_INCLUDE}
                payload, request_url, http_status = get_json(ENTRIES_URL, params)
                _ENTRIES_CACHE[cache_key] = (payload, request_url, http_status)
            entries = payload.get("data") or []
            pagination = ((payload.get("meta") or {}).get("pagination")) or {}
            total_pages = max(1, safe_int(pagination.get("totalPages"), 1))
            reports.append({
                "mode": "registry_entries_cache" if from_cache else "registry_entries",
                "url": request_url,
                "status": "ok",
                "http_status": http_status,
                "candidates_found": len(entries),
                "error_kind": "",
                "error": "",
                "diagnostic": {
                    "category_id": category_id,
                    "category_name": category.get("name"),
                    "page": page,
                    "total": pagination.get("total"),
                    "total_pages": pagination.get("totalPages"),
                },
            })
            for entry in entries:
                documents.append(make_document(entry, category, query, request_url))
        except Exception as exc:
            reports.append({
                "mode": "registry_entries",
                "url": ENTRIES_URL,
                "status": "error",
                "http_status": 0,
                "candidates_found": 0,
                "error_kind": classify_exception(exc),
                "error": str(exc),
                "diagnostic": {
                    "category_id": category_id,
                    "category_name": category.get("name"),
                    "page": page,
                },
            })
            break
        page += 1
    return documents, reports


def fetch_entries_for_search(registry_id: str, query: str, required_count: int, max_pages: int) -> tuple[list[dict], list[dict]]:
    documents = []
    reports = []
    seen = set()
    total_pages = 1
    page = 1
    while page <= total_pages and page <= max(1, max_pages):
        filters = compact_json({"registry": registry_id, "search": query})
        params = {"page": page, "filters": filters, "include": ENTRY_INCLUDE}
        try:
            payload, request_url, http_status = get_json(ENTRIES_URL, params)
            entries = payload.get("data") or []
            pagination = ((payload.get("meta") or {}).get("pagination")) or {}
            total_pages = max(1, safe_int(pagination.get("totalPages"), 1))
            reports.append({
                "mode": "registry_entries_search",
                "url": request_url,
                "status": "ok",
                "http_status": http_status,
                "candidates_found": len(entries),
                "error_kind": "",
                "error": "",
                "diagnostic": {
                    "page": page,
                    "total": pagination.get("total"),
                    "total_pages": pagination.get("totalPages"),
                    "search": query,
                },
            })
            for entry in entries:
                key = entry.get("id") or (entry.get("number"), entry.get("name"))
                if key in seen:
                    continue
                seen.add(key)
                documents.append(make_document(entry, {}, query, request_url))
            if len(documents) >= required_count:
                break
        except Exception as exc:
            reports.append({
                "mode": "registry_entries_search",
                "url": ENTRIES_URL,
                "status": "error",
                "http_status": 0,
                "candidates_found": 0,
                "error_kind": classify_exception(exc),
                "error": str(exc),
                "diagnostic": {
                    "page": page,
                    "search": query,
                },
            })
            break
        page += 1
    return documents, reports


def collect_documents(registry_id: str, categories: list[dict], query: str, required_count: int, max_pages: int) -> tuple[list[dict], list[dict]]:
    all_documents = []
    reports = []
    seen = set()
    max_pages_per_category = max(1, max_pages)

    for category in categories:
        docs, entry_reports = fetch_entries_for_category(registry_id, category, query, max_pages_per_category)
        reports.extend(entry_reports)
        for document in docs:
            key = document.get("entry_id") or (document.get("number"), document.get("name"))
            if key in seen:
                continue
            seen.add(key)
            all_documents.append(document)
        if len(all_documents) >= required_count:
            break

    return all_documents, reports


def search_documents_for_construction(
    search_query: str,
    iteration_count=1,
    max_pages=3,
    limit=5,
    min_score=0.15,
    include_text=True,
    include_full_text=False,
) -> str:
    query = normalize_space(search_query)
    if not query:
        return json.dumps({
            "status": "error",
            "message": "Поисковый запрос не может быть пустым.",
            "documents": [],
        }, ensure_ascii=False, indent=2)

    iteration_count = max(1, safe_int(iteration_count, 1))
    max_pages = max(1, safe_int(max_pages, 3))
    limit = max(1, safe_int(limit, 5))
    required_count = iteration_count * limit

    page_reports = [fetch_start_page_status()]
    discovery, discovery_reports = discover_registry()
    page_reports.extend(discovery_reports)

    if not discovery or not (discovery.get("registry") or {}).get("id"):
        result = {
            "status": "error",
            "message": "Не удалось определить ID реестра сводов правил.",
            "query": query,
            "source_site": START_PAGE_URL,
            "page_reports": page_reports,
            "documents": [],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    registry = discovery["registry"]
    registry_id = registry["id"]
    candidate_pool, entry_reports = fetch_entries_for_search(
        registry_id=registry_id,
        query=query,
        required_count=required_count,
        max_pages=max_pages,
    )
    page_reports.extend(entry_reports)

    offset = (iteration_count - 1) * limit
    selected = candidate_pool[offset:offset + limit]
    result = {
        "status": "success",
        "query": query,
        "current_iteration": iteration_count,
        "max_pages": max_pages,
        "limit": limit,
        "source_site": START_PAGE_URL,
        "api_site": API_BASE_URL,
        "registry_id": registry_id,
        "registry_type": ((registry.get("type") or {}).get("name")),
        "registry_title": ((registry.get("type") or {}).get("title")),
        "search_strategy": "discover_registry_then_use_site_registry_search",
        "query_variants": [query],
        "min_score_ignored": min_score,
        "include_text_ignored": include_text,
        "include_full_text_ignored": include_full_text,
        "exact_document_numbers": extract_ruleset_numbers(query),
        "categories_found": None,
        "leaf_categories_checked": None,
        "pages_checked": len(page_reports),
        "page_errors": sum(1 for report in page_reports if report.get("status") == "error"),
        "page_skipped": 0,
        "page_reports": page_reports,
        "total_candidates_found": len(candidate_pool),
        "documents_returned": len(selected),
        "has_more_documents": offset + limit < len(candidate_pool),
        "documents": selected,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def search_faufcc_sp_documents(*args, **kwargs) -> str:
    return search_documents_for_construction(*args, **kwargs)


if __name__ == "__main__":
    tests = [
        {
            "name": "Позитивный 1: запрос с номером СП",
            "query": "СП 20.13330.2016",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 2: фундаменты",
            "query": "основания фундаменты",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 3: информационное моделирование",
            "query": "информационное моделирование",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 4: пустой запрос",
            "query": "   ",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 5: отрицательная итерация",
            "query": "СП 20.13330.2016",
            "iteration_count": -5,
        },
        {
            "name": "Граничный 6: строка вместо номера итерации",
            "query": "СП 20.13330.2016",
            "iteration_count": "два",
        },
        {
            "name": "Граничный 7: огромная итерация",
            "query": "СП 20.13330.2016",
            "iteration_count": 9999,
        },
        {
            "name": "Граничный 8: маленький limit",
            "query": "СП 20.13330.2016",
            "iteration_count": 1,
            "limit": 1,
        },
    ]

    for test in tests:
        print("=" * 80)
        print(test["name"])
        print(search_documents_for_construction(
            search_query=test["query"],
            iteration_count=test["iteration_count"],
            max_pages=2,
            limit=test.get("limit", 5),
            include_text=False,
        ))

    out_path = Path(__file__).resolve().parent / "faufcc_sp_last_result.json"
    out_path.write_text(
        search_documents_for_construction(
            search_query="СП 20.13330.2016",
            max_pages=2,
            limit=5,
            include_text=False,
        ),
        encoding="utf-8",
    )
    print(f"Saved last result to: {out_path}")
