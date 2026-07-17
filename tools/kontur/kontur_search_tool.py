"""
Search tool for construction-related documents on normativ.kontur.ru.

Public entry point: `search_documents_for_construction()`.
It submits a query to the same search route used by the site's search UI,
parses HTML result pages, and returns a JSON string. Semantic relevance is
checked by the LLM, not by this script.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse, urlunparse

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


BASE_URL = "https://normativ.kontur.ru"
SEARCH_RESULTS_PATH = "/search-results"

HTTP = requests.Session()
HTTP.trust_env = False

ALLOWED_HOSTS = {"normativ.kontur.ru"}

BINARY_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf", ".xls", ".xlsx", ".zip", ".rar",
}

BAD_URL_PARTS = (
    "/login", "/register", "/support", "/help", "/tariff", "/oferta",
    "/theme/", "/ajax/", "/api/", "/webapi/", "/client-analytics",
)

STOP_WORDS = {
    "какие", "какой", "какая", "какое", "документы", "документ", "нужны",
    "нужен", "нужно", "надо", "требуется", "требуются", "нормативные",
    "для", "при", "по", "на", "и", "в", "во", "с", "со", "к", "ко",
    "от", "до", "строительства", "строительство",
}

def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def strip_volatile_query(url: str) -> str:
    parsed = urlparse(url)
    keep = {}
    for key, values in parse_qs(parsed.query).items():
        if key.lower() in {"moduleid", "documentid"} and values:
            keep[key] = values[-1]
    query = urlencode(keep)
    return urlunparse(parsed._replace(query=query, fragment=""))


def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_allowed_host(url: str) -> bool:
    return get_host(url) in ALLOWED_HOSTS


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
        "фундамента": ["основания", "основание", "фундамент", "фундаменты", "свайные"],
        "основания": ["основание", "фундамент", "фундаменты"],
        "дом": ["здание", "здания", "сооружения"],
        "дома": ["здание", "здания", "сооружения"],
        "бетон": ["бетонные", "железобетонные"],
        "железобетон": ["бетонные", "железобетонные"],
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


def exact_identifier_matches(query: str, title: str, url: str, snippet: str = "") -> list[str]:
    # Search snippets may contain the submitted query as highlighted text even
    # when the document title is unrelated, so exact IDs are checked only
    # against stable document fields.
    blob = f"{title} {url}".lower().replace(",", ".")
    matches = []
    for number in extract_document_numbers(query):
        variants = [variant for variant in number_variants(number) if variant and re.search(r"[.\-]", variant)]
        if any(re.search(r"(?<![0-9a-zа-я])" + re.escape(variant) + r"(?![0-9a-zа-я])", blob, flags=re.I) for variant in variants):
            matches.append(number)
    return list(dict.fromkeys(matches))


def infer_document_type(title: str, url: str, snippet: str = "") -> str:
    text = f"{title} {url} {snippet}".lower()
    if re.search(r"^\s*сп\s*\d+", title, flags=re.I):
        return "СП"
    if re.search(r"^\s*гост\b", title, flags=re.I):
        return "ГОСТ"
    if re.search(r"^\s*снип\b", title, flags=re.I):
        return "СНиП"
    if re.search(r"^\s*(?:тсн|мгсн)\b", title, flags=re.I):
        return "ТСН/МГСН"
    if "свод правил" in text or re.search(r"\bсп\s*\d+", text, flags=re.I):
        return "СП"
    if "гост" in text:
        return "ГОСТ"
    if "снип" in text:
        return "СНиП"
    if "приказ" in text:
        return "Приказ"
    return "HTML"


def is_bad_link(url: str, title: str) -> bool:
    url_lower = url.lower()
    title_lower = normalize_space(title).lower()
    if not title_lower or len(title_lower) < 3:
        return True
    if any(part in url_lower for part in BAD_URL_PARTS):
        return True
    if "/document" not in urlparse(url).path.lower():
        return True
    return False


def collect_query_matches(title: str, url: str, query: str, snippet: str = "") -> list[str]:
    """Returns query word matches as diagnostics only; it does not rank candidates."""
    title = normalize_space(title)
    snippet = normalize_space(snippet)
    title_lower = title.lower().replace("ё", "е")
    snippet_lower = snippet.lower().replace("ё", "е")
    blob = f"{title_lower} {snippet_lower} {url.lower()}"

    base_words = normalize_query_words(query)
    expanded_words = expand_query_words(base_words)
    matched = []

    for word in expanded_words:
        needle = word.lower().replace("ё", "е")
        if needle and needle in blob:
            matched.append(needle)

    return list(dict.fromkeys(matched))


def request_headers(referer: str | None = None) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def fetch_html(url: str) -> str:
    if not is_allowed_host(url):
        raise ValueError(f"Forbidden host: {url}")

    response = HTTP.get(
        url,
        headers=request_headers(),
        timeout=(20, 60),
        allow_redirects=True,
    )
    response.raise_for_status()
    if not is_allowed_host(response.url):
        raise ValueError(f"Site redirected to a foreign host: {response.url}")
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def build_search_query_string(query: str, page: int = 1) -> str:
    params = {
        "searching": "true",
        "query": str(query or "").strip(),
        "sortby": "1",
    }
    if page > 1:
        params["page"] = str(page)
    return urlencode(params)


def build_search_page_url(query: str, page: int = 1) -> str:
    return f"{BASE_URL}/?{build_search_query_string(query, page=page)}"


def build_search_results_url(query: str, page: int = 1) -> str:
    return f"{BASE_URL}{SEARCH_RESULTS_PATH}?{build_search_query_string(query, page=page)}"


def fetch_search_results_html(query: str, page: int = 1) -> tuple[str, str]:
    url = build_search_results_url(query, page=page)
    referer = build_search_page_url(query, page=page)
    response = HTTP.post(
        url,
        data={},
        headers={
            **request_headers(referer=referer),
            "Accept": "text/html, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        },
        timeout=(20, 60),
        allow_redirects=True,
    )
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text, url


def extract_pages_total(html: str) -> int | None:
    match = re.search(r"pagesTotal:\s*(\d+)", html)
    if match:
        return int(match.group(1))
    return None


def extract_candidates_from_html(
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

    for item in soup.select("li.js-found_doc"):
        link = item.select_one("a.js-found_doc-link[href]")
        if not link:
            continue

        full_url = strip_volatile_query(strip_fragment(urljoin(page_url, str(link.get("href") or ""))))
        if not is_allowed_host(full_url) or full_url in seen_urls:
            continue

        title = normalize_space(link.get_text(" ", strip=True))
        snippet_node = item.select_one(".snippet")
        snippet = normalize_space(snippet_node.get_text(" ", strip=True) if snippet_node else "")
        meta = normalize_space(" ".join(p.get_text(" ", strip=True) for p in item.select("p.f_15") if p is not snippet_node))
        full_snippet = normalize_space(f"{meta} {snippet}")

        if is_bad_link(full_url, title):
            continue

        matched = collect_query_matches(title, full_url, match_query or query, snippet=full_snippet)

        candidates.append({
            "title": title,
            "url": full_url,
            "document_type": infer_document_type(title, full_url, full_snippet),
            "matched_keywords": matched,
            "exact_identifier_matches": exact_identifier_matches(query, title, full_url, full_snippet),
            "snippet": full_snippet[:800],
            "source_mode": "site_search_post_html",
            "source_search_url": page_url,
            "source_query_variant": query,
            "match_query_used": match_query or query,
            "module_id": item.get("data-moduleid"),
            "document_id": item.get("data-documentid"),
            "source_position": item.get("data-position"),
        })
        seen_urls.add(full_url)

    return candidates


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
            "diagnostic": "Search results page was not found on normativ.kontur.ru.",
        }
    return {
        "status": "error",
        "severity": "error",
        "ignored": False,
        "error_kind": type(error).__name__,
        "error": f"{type(error).__name__}: {error}",
        "diagnostic": "Unexpected page loading or parsing error.",
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
    query_variants = [search_query]

    def add_candidate(candidate: dict | None) -> int:
        if not candidate:
            return 0
        url_key = candidate["url"]
        if url_key in seen_urls:
            return 0
        seen_urls[url_key] = len(all_candidates)
        all_candidates.append(candidate)
        return 1

    for query_variant in query_variants:
        pages_total = None
        for page_number in range(1, max_pages + 1):
            report = {
                "mode": "site_search_post_html",
                "query_variant": query_variant,
                "url": build_search_results_url(query_variant, page=page_number),
                "status": "",
                "severity": "",
                "ignored": False,
                "error_kind": "",
                "candidates_found": 0,
                "error": "",
                "diagnostic": "",
                "search_input_submitted": True,
                "page_number": page_number,
            }
            try:
                html, source_url = fetch_search_results_html(query_variant, page=page_number)
                candidates = extract_candidates_from_html(
                    html,
                    source_url,
                    search_query,
                    match_query=search_query,
                )
                report["candidates_found"] = sum(add_candidate(candidate) for candidate in candidates)
                pages_total = pages_total or extract_pages_total(html)
                report["pages_total_detected"] = pages_total
                report["status"] = "ok"
                report["diagnostic"] = (
                    "Parsed HTML returned by normativ.kontur.ru search POST endpoint. "
                    "This is the same search route used by the site UI."
                )
                if pages_total is not None and page_number >= pages_total:
                    page_reports.append(report)
                    break
            except Exception as exc:
                report.update(classify_page_exception(exc))
                page_reports.append(report)
                break
            page_reports.append(report)

    offset = (iteration_count - 1) * limit
    selected = all_candidates[offset: offset + limit]
    if include_text:
        selected = [
            enrich_candidate_with_text(dict(candidate), include_full_text=include_full_text)
            for candidate in selected
        ]

    result = {
        "status": "success",
        "query": search_query,
        "current_iteration": iteration_count,
        "max_pages": max_pages,
        "limit": limit,
        "source_site": BASE_URL,
        "search_strategy": "post_query_to_normativ_kontur_search_results_then_parse_html",
        "exact_document_numbers": extract_document_numbers(search_query),
        "query_variants": query_variants,
        "project_root": str(PROJECT_ROOT),
        "parser_path": str(PROJECT_ROOT / "html_legal_text_parser.py"),
        "text_parser": "html_legal_text_parser.extract_main_text_from_html",
        "pages_checked": len(page_reports),
        "page_errors": sum(1 for report in page_reports if report["status"] == "error"),
        "page_skipped": sum(1 for report in page_reports if report["status"] == "skipped"),
        "page_reports": page_reports,
        "total_candidates_found": len(all_candidates),
        "documents_returned": len(selected),
        "has_more_documents": offset + limit < len(all_candidates),
        "documents": selected,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def search_kontur_documents_for_construction(*args, **kwargs) -> str:
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
            "query": "Какие документы нужны для строительства фундамента?",
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

    out_path = Path(__file__).resolve().parent / "kontur_last_result.json"
    out_path.write_text(
        search_documents_for_construction(
            search_query="Какие документы нужны для строительства фундамента?",
            max_pages=2,
            limit=10,
            include_text=True,
        ),
        encoding="utf-8",
    )
    print(f"Saved last result to: {out_path}")
