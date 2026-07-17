"""
Search tool for construction-related legal and technical documents on docs.cntd.ru.

The public entry point is `search_documents_for_construction()`. It accepts a
plain user/LLM query, searches only docs.cntd.ru and the public API used by that
site, and returns a JSON string compatible with the sniprf tool shape. Semantic
relevance is checked by the LLM, not by this script.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


def find_project_root() -> Path:
    current_file = Path(__file__).resolve()
    for parent in [current_file.parent, *current_file.parents]:
        if (parent / "html_legal_text_parser.py").exists():
            return parent
    return Path.cwd()


PROJECT_ROOT = find_project_root()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from html_legal_text_parser import extract_main_text_from_html


BASE_URL = "https://docs.cntd.ru"
API_BASE_URL = "https://api.docs.cntd.ru/v1"

# This key is exposed in docs.cntd.ru frontend config and is used by the public
# search UI. If the site rotates it, update the constant or parse it from /.
DOCS_API_KEY = "39dfb4bf-79ba-4916-aff1-849edc97a706"

HTTP = requests.Session()
HTTP.trust_env = False

ALLOWED_HOSTS = {"docs.cntd.ru"}
API_ALLOWED_HOSTS = {"api.docs.cntd.ru"}

BINARY_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf", ".xls", ".xlsx", ".zip", ".rar",
}

BAD_URL_PARTS = (
    "/contacts", "/about", "/login", "/register", "/search", "/favorites",
    "/purchased", "/hotdocs", "/policy", "/images/", "/resources/",
)

STOP_WORDS = {
    "какие", "какой", "какая", "какое", "документы", "документ", "нужны",
    "нужен", "нужно", "надо", "требуется", "требуются", "для", "при",
    "по", "на", "и", "в", "во", "с", "со", "к", "ко", "от", "до",
    "строительства", "строительство",
}

def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_allowed_host(url: str) -> bool:
    return get_host(url) in ALLOWED_HOSTS


def is_api_allowed_host(url: str) -> bool:
    return get_host(url) in API_ALLOWED_HOSTS


def get_url_suffix(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def is_binary_url(url: str) -> bool:
    return get_url_suffix(url) in BINARY_EXTENSIONS


def normalize_query_words(query: str) -> list[str]:
    text = str(query or "").lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9.\s-]+", " ", text, flags=re.I)
    words = []
    for word in text.split():
        word = word.strip(" .,-_")
        if not word:
            continue
        if word in {"сп", "гост", "снип"}:
            words.append(word)
            continue
        if len(word) < 3 or word in STOP_WORDS:
            continue
        words.append(word)
    return list(dict.fromkeys(words))


def expand_query_words(words: list[str]) -> list[str]:
    synonyms = {
        "фундамент": ["основания", "основание", "фундаменты", "свайные"],
        "фундамента": ["основания", "основание", "фундамент", "свайные"],
        "основания": ["основание", "фундамент", "фундаменты"],
        "бетон": ["бетонные", "железобетонные"],
        "бетонные": ["бетон", "железобетонные"],
        "железобетонные": ["бетонные", "бетон"],
        "дом": ["здание", "здания", "сооружения"],
        "дома": ["здание", "здания", "сооружения"],
    }
    result = []
    for word in words:
        result.append(word)
        result.extend(synonyms.get(word, []))
    return list(dict.fromkeys(result))


def extract_document_numbers(query: str) -> list[str]:
    text = str(query or "").lower().replace(",", ".")
    patterns = (
        r"\b(?:сп|sp)\s*([0-9]+(?:\.[0-9]+)+(?:[-.][0-9]{4})?)",
        r"\b(?:гост|gost)\s*([0-9]+(?:[.\-][0-9а-яa-z]+)*)",
        r"\b(?:снип|snip)\s*([0-9]+(?:[.\-][0-9а-яa-z*]+)*)",
    )
    result = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = match.group(1).strip(" .,-")
            if value:
                result.append(value)
    return list(dict.fromkeys(result))


def number_variants(number: str) -> set[str]:
    number = str(number or "").lower()
    return {
        number,
        number.replace(".", "-"),
        number.replace("-", "."),
        re.sub(r"[^0-9a-zа-я]+", "", number),
    }


def exact_identifier_matches(query: str, title: str, url: str, registrations: list[dict] | None = None) -> list[str]:
    blob_parts = [title, url]
    for reg in registrations or []:
        blob_parts.append(str(reg.get("number") or ""))
    blob = " ".join(blob_parts).lower().replace(",", ".")
    compact_blob = re.sub(r"[^0-9a-zа-я]+", "", blob)
    matches = []
    for number in extract_document_numbers(query):
        variants = number_variants(number)
        if any(variant and (variant in blob or variant in compact_blob) for variant in variants):
            matches.append(number)
    return list(dict.fromkeys(matches))


def infer_document_type(title: str, url: str, registrations: list[dict] | None = None) -> str:
    text_parts = [title, url]
    for reg in registrations or []:
        doctype = reg.get("doctype") or {}
        text_parts.extend([str(doctype.get("name") or ""), str(reg.get("number") or "")])
    text = " ".join(text_parts).lower()

    if re.search(r"^\s*сп\s*\d+", title, flags=re.I):
        return "СП"
    if re.search(r"^\s*гост\b", title, flags=re.I):
        return "ГОСТ"
    if re.search(r"^\s*снип\b", title, flags=re.I):
        return "СНиП"
    if re.search(r"^\s*(?:тсн|мгсн)\b", title, flags=re.I):
        return "ТСН/МГСН"
    if "приказ" in text:
        return "Приказ"
    if "снип" in text and "свод правил" not in text:
        return "СНиП"
    if re.search(r"\bсп\s*\d+|\bсвод правил\b", text, flags=re.I):
        return "СП"
    if re.search(r"\bгост\b|\bgost\b", text, flags=re.I):
        return "ГОСТ"
    if "технический регламент" in text:
        return "Технический регламент"
    if "приказ" in text:
        return "Приказ"

    suffix = get_url_suffix(url)
    if suffix == ".pdf":
        return "PDF"
    if suffix in {".doc", ".docx", ".rtf"}:
        return "WORD"
    if suffix in {".xls", ".xlsx"}:
        return "EXCEL"
    return "HTML"


def is_bad_link(url: str, title: str) -> bool:
    url_lower = url.lower()
    title_lower = normalize_space(title).lower()
    if not title_lower or len(title_lower) < 3:
        return True
    if any(part in url_lower for part in BAD_URL_PARTS):
        return True
    return False


def collect_query_matches(
    title: str,
    url: str,
    query: str,
    snippet: str = "",
    registrations: list[dict] | None = None,
    status_name: str = "",
) -> list[str]:
    """Returns query word matches as diagnostics only; it does not rank candidates."""
    title = normalize_space(title)
    snippet = normalize_space(snippet)
    title_lower = title.lower().replace("ё", "е")
    snippet_lower = snippet.lower().replace("ё", "е")
    url_lower = url.lower()

    reg_text_parts = []
    for reg in registrations or []:
        doctype = reg.get("doctype") or {}
        department = reg.get("department") or {}
        reg_text_parts.extend([
            str(reg.get("number") or ""),
            str(reg.get("date") or ""),
            str(doctype.get("name") or ""),
            str(department.get("name") or ""),
        ])
    reg_lower = " ".join(reg_text_parts).lower().replace("ё", "е")
    blob = f"{title_lower} {snippet_lower} {url_lower} {reg_lower}"

    base_words = normalize_query_words(query)
    expanded_words = expand_query_words(base_words)
    matched = []

    for word in expanded_words:
        needle = word.lower().replace("ё", "е")
        if needle and needle in blob:
            matched.append(needle)

    return list(dict.fromkeys(matched))


def request_headers(api: bool = False) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "close",
    }
    if api:
        headers["x-api-key"] = DOCS_API_KEY
        headers["Origin"] = BASE_URL
        headers["Referer"] = f"{BASE_URL}/"
    return headers


def fetch_html(url: str) -> str:
    if not is_allowed_host(url):
        raise ValueError(f"Forbidden host: {url}")

    response = HTTP.get(
        url,
        headers=request_headers(api=False),
        timeout=(20, 60),
        allow_redirects=True,
    )
    response.raise_for_status()

    if not is_allowed_host(response.url):
        raise ValueError(f"Site redirected to a foreign host: {response.url}")

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def fetch_api_json(url: str) -> dict:
    if not is_api_allowed_host(url):
        raise ValueError(f"Forbidden API host: {url}")

    response = HTTP.get(
        url,
        headers=request_headers(api=True),
        timeout=(20, 60),
        allow_redirects=True,
    )
    response.raise_for_status()

    if not is_api_allowed_host(response.url):
        raise ValueError(f"API redirected to a foreign host: {response.url}")

    return response.json()


def build_html_search_urls(query: str, max_pages: int = 3) -> list[str]:
    query = str(query or "").strip()
    encoded = quote_plus(query)
    urls = []
    for page in range(1, max(1, int(max_pages)) + 1):
        # docs.cntd.ru uses infinite loading, but keeping page in diagnostics is
        # useful and harmless; duplicates are removed later.
        suffix = "" if page == 1 else f"&page={page}"
        urls.append(f"{BASE_URL}/search?q={encoded}{suffix}")
    return urls


def build_api_search_url(query: str, cursor: str | None = None) -> str:
    encoded = quote_plus(str(query or "").strip())
    url = f"{API_BASE_URL}/search?query={encoded}"
    if cursor:
        url += f"&cursor={quote_plus(cursor)}"
    return url


def extract_candidates_from_search_html(
    html: str,
    page_url: str,
    query: str,
    match_query: str | None = None,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    candidates = []
    seen_urls = set()

    for item in soup.select("li.document-list_i"):
        link = item.select_one('a[href^="/document/"], a[href*="/document/"]')
        if not link:
            continue

        full_url = strip_fragment(urljoin(page_url, str(link.get("href") or "")))
        if not is_allowed_host(full_url) or full_url in seen_urls:
            continue

        title_node = item.select_one(".document-list_i_t")
        title = normalize_space(title_node.get_text(" ", strip=True) if title_node else link.get_text(" ", strip=True))
        snippet_nodes = [node.get_text(" ", strip=True) for node in item.select(".__color_grey")]
        snippet = normalize_space(" ".join(snippet_nodes))

        if is_bad_link(full_url, title):
            continue

        matched = collect_query_matches(title, full_url, match_query or query, snippet=snippet)

        candidates.append({
            "title": title,
            "url": full_url,
            "document_type": infer_document_type(title, full_url),
            "matched_keywords": matched,
            "exact_identifier_matches": exact_identifier_matches(query, title, full_url),
            "snippet": snippet[:500],
            "source_mode": "site_search_page",
            "source_search_url": page_url,
            "source_query_variant": query,
            "match_query_used": match_query or query,
        })
        seen_urls.add(full_url)

    return candidates


def first_registration_summary(registrations: list[dict] | None) -> str:
    if not registrations:
        return ""
    parts = []
    for reg in registrations[:2]:
        doctype = reg.get("doctype") or {}
        department = reg.get("department") or {}
        segment = " ".join(
            str(value)
            for value in (
                doctype.get("name"),
                reg.get("date"),
                reg.get("number"),
                department.get("name"),
            )
            if value
        )
        if segment:
            parts.append(segment)
    return normalize_space("; ".join(parts))


def candidate_from_api_item(
    item: dict,
    query: str,
    source_url: str,
    match_query: str | None = None,
) -> dict | None:
    doc_id = str(item.get("id") or "").strip()
    if not doc_id:
        return None

    names = item.get("names") or []
    title = normalize_space(str(names[0] if names else ""))
    url = f"{BASE_URL}/document/{doc_id}"
    registrations = item.get("registrations") or []
    snippet = first_registration_summary(registrations)
    status = item.get("status") or {}
    status_name = str(status.get("name") or "")

    if is_bad_link(url, title):
        return None

    matched = collect_query_matches(
        title=title,
        url=url,
        query=match_query or query,
        snippet=snippet,
        registrations=registrations,
        status_name=status_name,
    )

    candidate = {
        "title": title,
        "url": url,
        "document_type": infer_document_type(title, url, registrations),
        "matched_keywords": matched,
        "exact_identifier_matches": exact_identifier_matches(query, title, url, registrations),
        "snippet": snippet[:500],
        "source_mode": "api_search",
        "source_search_url": source_url,
        "source_query_variant": query,
        "match_query_used": match_query or query,
        "access": item.get("access"),
        "status_name": status_name,
        "in_product_updated": item.get("in_product_updated"),
    }
    return candidate


def enrich_candidate_with_text(candidate: dict, include_full_text: bool = False) -> dict:
    url = candidate["url"]
    if is_binary_url(url):
        candidate["content_status"] = "binary_document_not_parsed_here"
        candidate["text_preview"] = ""
        if include_full_text:
            candidate["text"] = ""
        return candidate

    try:
        html = fetch_html(url)
        text = extract_main_text_from_html(html) or ""
        candidate["content_status"] = "html_text_extracted" if text.strip() else "empty_text"
        candidate["text_preview"] = text[:1000]
        if include_full_text:
            candidate["text"] = text
    except Exception as exc:
        candidate["content_status"] = f"html_parse_error: {type(exc).__name__}: {exc}"
        candidate["text_preview"] = ""
        if include_full_text:
            candidate["text"] = ""

    return candidate


def classify_page_exception(error: Exception) -> dict:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 404:
        return {
            "status": "skipped",
            "severity": "warning",
            "ignored": True,
            "error_kind": "missing_page",
            "error": f"{type(error).__name__}: {error}",
            "diagnostic": "Страница не найдена на docs.cntd.ru. Поиск продолжается.",
        }
    return {
        "status": "error",
        "severity": "error",
        "ignored": False,
        "error_kind": type(error).__name__,
        "error": f"{type(error).__name__}: {error}",
        "diagnostic": "Неожиданная ошибка загрузки или парсинга страницы.",
    }


def search_documents_for_construction(
    search_query: str,
    iteration_count: int = 1,
    max_pages: int = 3,
    limit: int = 5,
    min_score: float = 0.15,
    include_text: bool = True,
    include_full_text: bool = False,
) -> str:
    """
    Searches docs.cntd.ru for construction documents by a plain text query.

    `max_pages` controls how many source pages/chunks to inspect. The first
    source is the SSR HTML search page; subsequent chunks are loaded through
    the same public API that the site uses for infinite scrolling.
    `min_score` is kept for backward compatibility and is ignored.
    """
    search_query = str(search_query or "").strip()
    if not search_query:
        return json.dumps({
            "status": "error",
            "message": "Поисковый запрос не может быть пустым.",
        }, ensure_ascii=False, indent=2)

    if not isinstance(iteration_count, int) or iteration_count < 1:
        iteration_count = 1
    try:
        max_pages = max(1, int(max_pages))
    except Exception:
        max_pages = 3
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 5

    all_candidates = []
    seen_urls = {}
    page_reports = []

    def add_candidate(candidate: dict | None) -> int:
        if not candidate:
            return 0
        url_key = candidate["url"]
        if url_key in seen_urls:
            return 0
        all_candidates.append(candidate)
        seen_urls[url_key] = len(all_candidates) - 1
        return 1

    query_variants = [search_query]

    for query_variant in query_variants:
        html_urls = build_html_search_urls(query_variant, max_pages=1)
        for url in html_urls:
            report = {
                "mode": "site_search_page",
                "query_variant": query_variant,
                "url": url,
                "search_input_submitted": True,
                "status": "",
                "severity": "",
                "ignored": False,
                "error_kind": "",
                "candidates_found": 0,
                "error": "",
                "diagnostic": "",
            }
            try:
                html = fetch_html(url)
                candidates = extract_candidates_from_search_html(
                    html,
                    url,
                    search_query,
                    match_query=search_query,
                )
                report["candidates_found"] = sum(add_candidate(candidate) for candidate in candidates)
                report["status"] = "ok"
                report["diagnostic"] = (
                    "Parsed docs.cntd.ru search results page. "
                    "The URL is the same route used by the site search field."
                )
            except Exception as exc:
                report.update(classify_page_exception(exc))
            page_reports.append(report)

        cursor = None
        for page_number in range(1, max_pages + 1):
            api_url = build_api_search_url(query_variant, cursor=cursor)
            report = {
                "mode": "api_search",
                "query_variant": query_variant,
                "url": api_url,
                "status": "",
                "severity": "",
                "ignored": False,
                "error_kind": "",
                "candidates_found": 0,
                "error": "",
                "diagnostic": "",
                "api_page_number": page_number,
            }
            try:
                payload = fetch_api_json(api_url)
                candidates = [
                    candidate_from_api_item(
                        item,
                        search_query,
                        api_url,
                        match_query=search_query,
                    )
                    for item in payload.get("data") or []
                ]
                report["candidates_found"] = sum(add_candidate(candidate) for candidate in candidates)
                pagination = payload.get("pagination") or {}
                cursor = pagination.get("cursor")
                report["status"] = "ok"
                report["diagnostic"] = (
                    "Parsed public docs.cntd.ru API search chunk. "
                    "Next cursor exists." if cursor else "Parsed final public docs.cntd.ru API search chunk."
                )
                report["api_total"] = pagination.get("total")
                report["api_per_page"] = pagination.get("per_page")
                report["api_next_cursor"] = cursor
                if not cursor:
                    page_reports.append(report)
                    break
            except Exception as exc:
                report.update(classify_page_exception(exc))
                cursor = None
            page_reports.append(report)
            if not cursor:
                break

    offset = (iteration_count - 1) * limit
    selected = all_candidates[offset: offset + limit]
    if include_text:
        selected = [
            enrich_candidate_with_text(dict(candidate), include_full_text=include_full_text)
            for candidate in selected
        ]

    error_count = sum(1 for report in page_reports if report["status"] == "error")
    skipped_count = sum(1 for report in page_reports if report["status"] == "skipped")

    result = {
        "status": "success",
        "query": search_query,
        "current_iteration": iteration_count,
        "max_pages": max_pages,
        "limit": limit,
        "source_site": BASE_URL,
        "search_strategy": "submit_query_to_docs_cntd_search_route_then_parse_results",
        "exact_document_numbers": extract_document_numbers(search_query),
        "query_variants": query_variants,
        "project_root": str(PROJECT_ROOT),
        "parser_path": str(PROJECT_ROOT / "html_legal_text_parser.py"),
        "text_parser": "html_legal_text_parser.extract_main_text_from_html",
        "pages_checked": len(page_reports),
        "page_errors": error_count,
        "page_skipped": skipped_count,
        "page_reports": page_reports,
        "total_candidates_found": len(all_candidates),
        "documents_returned": len(selected),
        "has_more_documents": offset + limit < len(all_candidates),
        "documents": selected,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def search_cntd_documents_for_construction(*args, **kwargs) -> str:
    return search_documents_for_construction(*args, **kwargs)


if __name__ == "__main__":
    tests = [
        {
            "name": "Позитивный 1: точный СП",
            "query": "СП 22.13330 основания зданий",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 2: естественный вопрос",
            "query": "Какие документы нужны для строительства фундамента жилого дома?",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 3: контекст вечномерзлых грунтов",
            "query": "Какие документы нужны для строительства фундамента на вечномерзлых грунтах?",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 4: пустой запрос",
            "query": "   ",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 5: отрицательная итерация",
            "query": "Основания зданий",
            "iteration_count": -5,
        },
        {
            "name": "Граничный 6: строка вместо номера итерации",
            "query": "Основания зданий",
            "iteration_count": "два",
        },
        {
            "name": "Граничный 7: огромная итерация",
            "query": "Основания зданий",
            "iteration_count": 9999,
        },
        {
            "name": "Граничный 8: спецсимволы",
            "query": "СП 22.13330%/?\"&",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 9: маленький limit",
            "query": "СП 22.13330 основания зданий",
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

    out_path = Path(__file__).resolve().parent / "cntd_last_result.json"
    out_path.write_text(
        search_documents_for_construction(
            search_query="СП 22.13330 основания зданий",
            max_pages=2,
            limit=5,
            include_text=True,
        ),
        encoding="utf-8",
    )
    print(f"Saved last result to: {out_path}")
