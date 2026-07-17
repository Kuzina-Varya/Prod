"""
Search tool for FGIS TP materials on economy.gov.ru.

The public entry point is `search_documents_for_construction()`. It accepts a
plain text query and returns a JSON string with documents in batches. The tool
does not score or rank semantic usefulness; an LLM should inspect the returned
documents and request the next batch when needed.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


HTTP = requests.Session()
HTTP.trust_env = False


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


BASE_URL = "https://www.economy.gov.ru"
START_PAGE_URL = f"{BASE_URL}/material/directions/regionalnoe_razvitie/fgis_tp/"

ALLOWED_HOSTS = {
    "www.economy.gov.ru",
    "economy.gov.ru",
    "fgistp.economy.gov.ru",
    "pravo.gov.ru",
    "publication.pravo.gov.ru",
}

BINARY_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf", ".xls", ".xlsx", ".zip", ".rar",
}

BAD_URL_PARTS = (
    "/contacts",
    "/about/",
    "/login",
    "/register",
    "/privacy",
    "/sitemap",
    "/search/",
    "/subscribe",
    "/utils/",
    "/static/",
    "/assets/",
)

BAD_TEXT_WORDS = {
    "рус",
    "eng",
    "中文",
    "подробнее",
    "контакты",
    "карта сайта",
    "министерство",
    "деятельность",
    "документы",
    "расширенный поиск",
    "хронологии",
    "релевантности",
}

STOP_WORDS = {
    "какие", "какой", "какая", "какое", "документы", "документ", "нужны",
    "нужен", "нужно", "надо", "для", "при", "по", "на", "и", "в", "во",
    "с", "со", "к", "ко", "от", "до", "об", "о",
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
        if not word or word in STOP_WORDS:
            continue
        if len(word) < 3 and word not in {"тп"}:
            continue
        words.append(word)
    return list(dict.fromkeys(words))


def expand_query_words(words: list[str]) -> list[str]:
    synonyms = {
        "фгис": ["федеральная", "государственная", "информационная", "система"],
        "тп": ["территориального", "планирования"],
        "территориальное": ["территориального", "терпланирование"],
        "планирование": ["планирования", "терпланирование"],
        "градостроительный": ["градостроительного", "градостроительный"],
    }
    result = []
    for word in words:
        result.append(word)
        result.extend(synonyms.get(word, []))
    return list(dict.fromkeys(result))


def collect_query_matches(title: str, url: str, query: str, snippet: str = "") -> list[str]:
    blob = f"{title} {snippet} {url}".lower().replace("ё", "е")
    words = expand_query_words(normalize_query_words(query))
    matched = []
    for word in words:
        if word.lower().replace("ё", "е") in blob:
            matched.append(word.lower().replace("ё", "е"))
    return list(dict.fromkeys(matched))


def extract_document_numbers(query: str) -> list[str]:
    text = str(query or "").lower().replace(",", ".")
    patterns = [
        r"\b(?:фз|федеральный\s+закон)\s*(?:от\s*\d{1,2}\s+\S+\s+\d{4}\s*г\.?\s*)?(?:n|№)?\s*([0-9]+-?фз|[0-9]+)",
        r"\b(?:постановление|приказ)\b[^0-9]{0,80}(?:n|№)\s*([0-9]+[а-яa-z/-]*)",
        r"\b(?:n|№)\s*([0-9]+[а-яa-z/-]*)",
    ]
    result = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            value = match.group(1).strip(" .")
            if value:
                result.append(value)
    return list(dict.fromkeys(result))


def exact_identifier_matches(query: str, title: str, url: str, snippet: str = "") -> list[str]:
    blob = f"{title} {url} {snippet}".lower().replace("ё", "е")
    matches = []
    for number in extract_document_numbers(query):
        if number.lower() in blob:
            matches.append(number)
    return list(dict.fromkeys(matches))


def request_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "close",
    }


def fetch_html(url: str) -> str:
    if not is_allowed_host(url):
        raise ValueError(f"Forbidden host: {url}")
    response = HTTP.get(url, headers=request_headers(), timeout=(20, 60), allow_redirects=True)
    response.raise_for_status()
    final_url = response.url
    if not is_allowed_host(final_url):
        raise ValueError(f"Redirected to forbidden host: {final_url}")
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and not response.text.lstrip().startswith("<"):
        raise ValueError(f"Non-HTML response: {content_type}")
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def build_source_urls(query: str, max_pages: int = 3) -> list[tuple[str, str, str]]:
    urls = []
    pages = max(1, int(max_pages or 1))
    for page in range(1, pages + 1):
        url = f"{BASE_URL}/search/?q={quote_plus(query)}"
        if page > 1:
            url = f"{url}&PAGEN_1={page}"
        urls.append(("site_search", url, query))
    return urls


def extract_page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ('meta[property="og:title"]', 'h1', 'title'):
        node = soup.select_one(selector)
        if not node:
            continue
        title = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
        title = normalize_space(title)
        title = re.sub(r"\s*\|\s*Министерство.*$", "", title).strip()
        if title:
            return title
    return ""


def infer_document_type(title: str, url: str, snippet: str = "") -> str:
    text = f"{title} {url} {snippet}".lower()
    suffix = get_url_suffix(url)
    if suffix == ".pdf":
        return "PDF"
    if suffix in {".doc", ".docx", ".rtf"}:
        return "WORD"
    if strip_fragment(url) == START_PAGE_URL:
        return "Страница ФГИС ТП"
    if "градостроительный кодекс" in text:
        return "Кодекс"
    if "федеральный закон" in text or re.search(r"\b[0-9]+-фз\b", text):
        return "Федеральный закон"
    if "постановление" in text:
        return "Постановление"
    if "приказ" in text:
        return "Приказ"
    if "новост" in urlparse(url).path.lower():
        return "Новость"
    return "HTML"


def is_bad_link(url: str, title: str) -> bool:
    title_lower = normalize_space(title).lower()
    url_lower = url.lower()
    if not title_lower or len(title_lower) < 3:
        return True
    if title_lower in BAD_TEXT_WORDS:
        return True
    if any(part in url_lower for part in BAD_URL_PARTS):
        return True
    if not is_allowed_host(url):
        return True
    return False


def make_page_candidate(html: str, url: str, query: str, source_mode: str, source_query: str) -> dict | None:
    title = extract_page_title(html)
    if not title or is_bad_link(url, title):
        return None
    text = normalize_space(extract_main_text_from_html(html)[:700])
    return {
        "title": title,
        "url": strip_fragment(url),
        "document_type": infer_document_type(title, url, text),
        "matched_keywords": collect_query_matches(title, url, query, text),
        "exact_identifier_matches": exact_identifier_matches(query, title, url, text),
        "snippet": text[:800],
        "source_mode": source_mode,
        "source_search_url": url,
        "source_query_variant": source_query,
        "match_query_used": query,
    }


def extract_candidates_from_html(html: str, page_url: str, query: str, source_mode: str, source_query: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()

    scopes = soup.select(".e-material")
    if not scopes:
        scopes = soup.select("main")
    if not scopes:
        scopes = [soup]

    candidates = []
    seen_urls = set()
    for scope in scopes:
        for link in scope.find_all("a", href=True):
            href = str(link.get("href") or "").strip()
            full_url = strip_fragment(urljoin(page_url, href))
            if full_url in seen_urls or not is_allowed_host(full_url):
                continue
            title = normalize_space(link.get_text(" ", strip=True))
            parent = link.find_parent(["li", "p", "div", "article", "section"])
            snippet = normalize_space(parent.get_text(" ", strip=True) if parent else title)
            if not title or len(title) < 6:
                title = snippet[:180]
            if is_bad_link(full_url, title):
                continue
            candidates.append({
                "title": title,
                "url": full_url,
                "document_type": infer_document_type(title, full_url, snippet),
                "matched_keywords": collect_query_matches(title, full_url, query, snippet),
                "exact_identifier_matches": exact_identifier_matches(query, title, full_url, snippet),
                "snippet": snippet[:800],
                "source_mode": "linked_document" if source_mode != "site_search" else "site_search",
                "source_search_url": page_url,
                "source_query_variant": source_query,
                "match_query_used": query,
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
            "diagnostic": "Source page was not found. The search continues.",
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
        max_pages = max(0, int(max_pages))
    except Exception:
        max_pages = 3
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 5

    all_candidates = []
    seen_urls = set()
    page_reports = []

    def add_candidate(candidate: dict | None) -> int:
        if not candidate:
            return 0
        url_key = candidate["url"]
        if url_key in seen_urls:
            return 0
        seen_urls.add(url_key)
        all_candidates.append(candidate)
        return 1

    for mode, url, source_query in build_source_urls(search_query, max_pages=max_pages):
        report = {
            "mode": mode,
            "query_variant": source_query,
            "url": url,
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
            count = 0
            for candidate in extract_candidates_from_html(html, url, search_query, mode, source_query):
                count += add_candidate(candidate)
            report["status"] = "ok"
            report["candidates_found"] = count
            report["diagnostic"] = "Parsed economy.gov.ru site search page for the original query."
        except Exception as exc:
            report.update(classify_page_exception(exc))
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
        "source_site": START_PAGE_URL,
        "allowed_hosts": sorted(ALLOWED_HOSTS),
        "search_strategy": "submit_original_query_to_economy_site_search_then_parse_results",
        "exact_document_numbers": extract_document_numbers(search_query),
        "query_variants": [search_query],
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


def search_economy_fgis_tp_documents(*args, **kwargs) -> str:
    return search_documents_for_construction(*args, **kwargs)


if __name__ == "__main__":
    tests = [
        {
            "name": "Позитивный 1: ФГИС ТП",
            "query": "ФГИС ТП нормативное обеспечение",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 2: территориальное планирование",
            "query": "документы территориального планирования",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 3: постановление 289",
            "query": "Постановление Правительства 289 ФГИС ТП",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 4: пустой запрос",
            "query": "   ",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 5: отрицательная итерация",
            "query": "ФГИС ТП",
            "iteration_count": -5,
        },
        {
            "name": "Граничный 6: строка вместо номера итерации",
            "query": "ФГИС ТП",
            "iteration_count": "два",
        },
        {
            "name": "Граничный 7: огромная итерация",
            "query": "ФГИС ТП",
            "iteration_count": 9999,
        },
        {
            "name": "Граничный 8: спецсимволы",
            "query": "ФГИС ТП%/?\"&",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 9: маленький limit",
            "query": "ФГИС ТП нормативное обеспечение",
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

    out_path = Path(__file__).resolve().parent / "economy_fgis_tp_last_result.json"
    out_path.write_text(
        search_documents_for_construction(
            search_query="ФГИС ТП нормативное обеспечение",
            max_pages=2,
            limit=5,
            include_text=True,
        ),
        encoding="utf-8",
    )
    print(f"Saved last result to: {out_path}")
