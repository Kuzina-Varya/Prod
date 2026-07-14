import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin, urlparse, urlunparse, unquote

import requests
from bs4 import BeautifulSoup


# ============================================================
# ПОДКЛЮЧЕНИЕ КОРНЯ ПРОЕКТА
# ============================================================

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


# ============================================================
# НАСТРОЙКИ
# ============================================================

BASE_URL = "http://sniprf.ru"

ALLOWED_HOSTS = {
    "sniprf.ru",
    "www.sniprf.ru",
}

BINARY_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf",
    ".xls", ".xlsx", ".zip", ".rar",
}

BAD_URL_PARTS = (
    "/user",
    "/login",
    "/register",
    "/taxonomy",
    "/news",
    "/contact",
    "/contacts",
    "/about",
    "/rss",
    "/feed",
    "/print",
)

BAD_TEXT_WORDS = (
    "поиск",
    "главная",
    "контакты",
    "карта сайта",
    "новости",
    "вход",
    "регистрация",
    "читать далее",
    "подробнее",
    "добавить комментарий",
)

DOCUMENT_MARKERS = (
    "сп ",
    "сп-",
    "гост",
    "свод правил",
    "строительные нормы",
    "основания",
    "здания",
    "сооружения",
    "конструкции",
    "бетонные",
    "железобетонные",
    "проектирование",
    "строительство",
    "фундамент",
    "фундаменты",
    "нагрузки",
    "воздействия",
    "безопасность",
    "свайные",
)

SP_YEARS = [
    2025, 2024, 2023, 2022, 2021, 2020,
    2019, 2018, 2017, 2016, 2015,
    2014, 2013, 2012, 2011,
]


# ============================================================
# УТИЛИТЫ
# ============================================================

def force_http(url: str) -> str:
    url = str(url or "").strip()
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return url


def get_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_allowed_host(url: str) -> bool:
    return get_host(url) in ALLOWED_HOSTS


def strip_fragment(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def get_url_suffix(url: str) -> str:
    path = unquote(urlparse(url).path)
    return Path(path).suffix.lower()


def is_binary_url(url: str) -> bool:
    return get_url_suffix(url) in BINARY_EXTENSIONS


def normalize_query_words(query: str) -> list[str]:
    text = str(query or "").lower().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9.\s-]+", " ", text, flags=re.I)

    stop_words = {
        "какие", "какой", "какая", "какое",
        "документы", "документ",
        "нужны", "нужен", "нужно",
        "надо", "требуется", "требуются",
        "для", "при", "по", "на", "и", "в", "во",
        "с", "со", "к", "ко", "от", "до",
        "строительства", "строительство",
    }

    words = []

    for word in text.split():
        word = word.strip(" .,-_")
        if not word:
            continue

        if word in {"сп", "гост"}:
            words.append(word)
            continue

        if len(word) < 3:
            continue

        if word in stop_words:
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


def extract_sp_numbers(query: str) -> list[str]:
    """
    Извлекает номера СП:
    СП 22.13330 -> 22-13330
    СП 63.13330 -> 63-13330
    """
    text = str(query or "").lower().replace(",", ".")
    result = []

    patterns = [
        r"\bсп\s*([0-9]+(?:\.[0-9]+)*)",
        r"\bsp\s*([0-9]+(?:\.[0-9]+)*)",
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, text, flags=re.I):
            num = m.group(1).strip(". ")
            if num:
                result.append(num.replace(".", "-"))

    return list(dict.fromkeys(result))


def looks_like_sp_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return bool(re.search(r"/sp\d", path))


def looks_like_snip_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/razdel-" in path or bool(re.search(r"/\d+-\d+-\d+", path))


# ============================================================
# ЗАГРУЗКА HTML
# ============================================================

def fetch_html(url: str) -> str:
    url = force_http(url)

    if not is_allowed_host(url):
        raise ValueError(f"Запрещённый домен: {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "close",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=(20, 60),
        allow_redirects=True,
    )

    response.raise_for_status()

    final_url = force_http(response.url)

    if not is_allowed_host(final_url):
        raise ValueError(f"Сайт перенаправил на чужой домен: {final_url}")

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    return response.text


# ============================================================
# URL ДЛЯ ПОИСКА
# ============================================================

def build_direct_sp_urls(query: str) -> list[str]:
    """
    Если в запросе есть СП 22.13330, сразу пробуем прямые страницы:
    /sp22-13330-2016
    /sp22-13330-2011
    и т.д.
    """
    urls = []

    for sp_num in extract_sp_numbers(query):
        for year in SP_YEARS:
            urls.append(f"{BASE_URL}/sp{sp_num}-{year}")

    return list(dict.fromkeys(urls))


def build_catalog_urls() -> list[str]:
    """
    sniprf плохо ищется через /search/node.
    Поэтому берём каталоги СП и парсим ссылки оттуда.
    """
    urls = [
        f"{BASE_URL}/",
        f"{BASE_URL}/sp",
    ]

    for year in SP_YEARS:
        urls.append(f"{BASE_URL}/sp{year}")

    return list(dict.fromkeys(urls))


def build_search_urls(query: str, max_pages: int = 3) -> list[str]:
    """
    Оставляем старые поисковые URL только как fallback/диагностику.
    """
    query = str(query or "").strip()

    encoded_path = quote(query)
    encoded_plus = quote_plus(query)

    urls = []

    for page in range(max_pages):
        if page == 0:
            urls.extend([
                f"{BASE_URL}/search/node/{encoded_path}",
                f"{BASE_URL}/?do=search&subaction=search&story={encoded_plus}",
                f"{BASE_URL}/index.php?do=search&subaction=search&story={encoded_plus}",
            ])
        else:
            urls.extend([
                f"{BASE_URL}/search/node/{encoded_path}?page={page}",
                f"{BASE_URL}/?do=search&subaction=search&story={encoded_plus}&page={page}",
                f"{BASE_URL}/index.php?do=search&subaction=search&story={encoded_plus}&page={page}",
            ])

    return list(dict.fromkeys([force_http(url) for url in urls]))


# ============================================================
# ФИЛЬТРАЦИЯ И ОЦЕНКА
# ============================================================

def is_bad_link(url: str, title: str) -> bool:
    url_lower = force_http(url).lower()
    title_lower = normalize_space(title).lower()

    if not title_lower or len(title_lower) < 3:
        return True

    if any(part in url_lower for part in BAD_URL_PARTS):
        return True

    if title_lower in BAD_TEXT_WORDS:
        return True

    is_sp = looks_like_sp_url(url_lower) or bool(re.search(r"\bсп\s*\d+", title_lower))

    # ВАЖНО:
    # СП часто содержит фразу "актуализированная редакция СНиП".
    # Такой документ НЕ баним.
    if not is_sp:
        if (
            "снип" in title_lower
            or "snip" in title_lower
            or "снип" in url_lower
            or "snip" in url_lower
            or looks_like_snip_url(url_lower)
        ):
            return True

    return False


def infer_document_type(title: str, url: str) -> str:
    text = f"{title} {url}".lower()

    if re.search(r"\bсп\s*\d+|/sp\d", text, flags=re.I):
        return "СП"

    if re.search(r"\bгост\b|\bgost\b", text, flags=re.I):
        return "ГОСТ"

    if "свод правил" in text:
        return "Свод правил"

    suffix = get_url_suffix(url)

    if suffix == ".pdf":
        return "PDF"
    if suffix in {".doc", ".docx", ".rtf"}:
        return "WORD"
    if suffix in {".xls", ".xlsx"}:
        return "EXCEL"

    return "HTML"


def score_candidate(title: str, url: str, query: str, snippet: str = "") -> tuple[float, list[str]]:
    title = normalize_space(title)
    snippet = normalize_space(snippet)

    title_lower = title.lower().replace("ё", "е")
    snippet_lower = snippet.lower().replace("ё", "е")
    url_lower = force_http(url).lower().replace("ё", "е")

    blob = f"{title_lower} {snippet_lower} {url_lower}"

    query_words = expand_query_words(normalize_query_words(query))

    score = 0
    matched = []

    for word in query_words:
        word = word.lower().replace("ё", "е")
        if word and word in blob:
            score += 14
            matched.append(word)

    for marker in DOCUMENT_MARKERS:
        if marker in blob:
            score += 10

    if re.search(r"\bсп\s*\d+(?:\.\d+)*|/sp\d", blob, flags=re.I):
        score += 30

    if re.search(r"\bгост\b|\bgost\b", blob, flags=re.I):
        score += 25

    if "свод правил" in blob:
        score += 20

    suffix = get_url_suffix(url)

    if suffix in {".pdf", ".doc", ".docx", ".rtf"}:
        score += 18
    elif suffix == ".html":
        score += 10
    else:
        if re.search(r"/sp\d|/gost|/document|/docs?", url_lower):
            score += 16

    if len(title) >= 20:
        score += 8

    if "search" in url_lower:
        score -= 35

    if "контакты" in blob or "главная" in blob or "карта сайта" in blob:
        score -= 25

    if not matched and not any(marker in blob for marker in DOCUMENT_MARKERS):
        score -= 30

    normalized = max(0.0, min(1.0, score / 100))
    return normalized, list(dict.fromkeys(matched))


def extract_page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    if h1:
        title = normalize_space(h1.get_text(" ", strip=True))
        if title:
            return title

    title_tag = soup.find("title")
    if title_tag:
        title = normalize_space(title_tag.get_text(" ", strip=True))
        title = re.sub(r"\s*\|\s*.*$", "", title).strip()
        if title:
            return title

    return ""


def make_candidate_from_url(url: str, query: str, source_mode: str) -> dict | None:
    """
    Для прямых URL типа /sp22-13330-2016.
    """
    try:
        html = fetch_html(url)
        title = extract_page_title(html)

        if not title:
            return None

        if is_bad_link(url, title):
            return None

        text_for_snippet = extract_main_text_from_html(html)
        snippet = normalize_space(text_for_snippet[:500])

        score, matched = score_candidate(
            title=title,
            url=url,
            query=query,
            snippet=snippet,
        )

        if score < 0.10:
            return None

        return {
            "title": title,
            "url": force_http(url),
            "document_type": infer_document_type(title, url),
            "score": round(score, 3),
            "matched_keywords": matched,
            "snippet": snippet,
            "source_mode": source_mode,
            "source_search_url": url,
        }

    except Exception:
        return None


def extract_candidates_from_html(html: str, page_url: str, query: str, min_score: float) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    candidates = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()

        if not href:
            continue

        full_url = force_http(urljoin(page_url, href))
        full_url = strip_fragment(full_url)

        if not is_allowed_host(full_url):
            continue

        title = normalize_space(a.get_text(" ", strip=True))

        parent = a.find_parent(["li", "div", "article", "section", "p", "td"])
        parent_text = normalize_space(parent.get_text(" ", strip=True)) if parent else title

        if not title or len(title) < 8:
            title = parent_text[:180]

        snippet = parent_text[:500]

        if is_bad_link(full_url, title):
            continue

        if full_url in seen_urls:
            continue

        score, matched = score_candidate(
            title=title,
            url=full_url,
            query=query,
            snippet=snippet,
        )

        if score < min_score:
            continue

        candidates.append({
            "title": title,
            "url": full_url,
            "document_type": infer_document_type(title, full_url),
            "score": round(score, 3),
            "matched_keywords": matched,
            "snippet": snippet,
            "source_mode": "catalog_or_search",
            "source_search_url": page_url,
        })

        seen_urls.add(full_url)

    return candidates


# ============================================================
# ОБОГАЩЕНИЕ ТЕКСТОМ
# ============================================================

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

    except Exception as e:
        candidate["content_status"] = f"html_parse_error: {type(e).__name__}: {e}"
        candidate["text_preview"] = ""

        if include_full_text:
            candidate["text"] = ""

    return candidate


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

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

    all_candidates = []
    seen_urls = set()
    page_reports = []

    def add_candidate(candidate: dict | None) -> int:
        if not candidate:
            return 0

        url_key = candidate["url"]

        if url_key in seen_urls:
            return 0

        all_candidates.append(candidate)
        seen_urls.add(url_key)
        return 1

    # 1. Прямые СП URL: /sp22-13330-2016
    direct_urls = build_direct_sp_urls(search_query)

    for url in direct_urls:
        report = {
            "mode": "direct_sp_url",
            "url": url,
            "status": "",
            "candidates_found": 0,
            "error": "",
        }

        try:
            candidate = make_candidate_from_url(
                url=url,
                query=search_query,
                source_mode="direct_sp_url",
            )
            report["candidates_found"] = add_candidate(candidate)
            report["status"] = "ok"
        except Exception as e:
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}"

        page_reports.append(report)

    # 2. Каталоги СП: /sp, /sp2016, /sp2018...
    catalog_urls = build_catalog_urls()

    for url in catalog_urls:
        report = {
            "mode": "catalog",
            "url": url,
            "status": "",
            "candidates_found": 0,
            "error": "",
        }

        try:
            html = fetch_html(url)
            candidates = extract_candidates_from_html(
                html=html,
                page_url=url,
                query=search_query,
                min_score=min_score,
            )

            count = 0
            for candidate in candidates:
                count += add_candidate(candidate)

            report["status"] = "ok"
            report["candidates_found"] = count

        except Exception as e:
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}"

        page_reports.append(report)

    # 3. Старый внутренний поиск — только fallback/диагностика
    search_urls = build_search_urls(search_query, max_pages=max_pages)

    for url in search_urls:
        report = {
            "mode": "site_search",
            "url": url,
            "status": "",
            "candidates_found": 0,
            "error": "",
        }

        try:
            html = fetch_html(url)
            candidates = extract_candidates_from_html(
                html=html,
                page_url=url,
                query=search_query,
                min_score=min_score,
            )

            count = 0
            for candidate in candidates:
                count += add_candidate(candidate)

            report["status"] = "ok"
            report["candidates_found"] = count

        except Exception as e:
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}"

        page_reports.append(report)

    all_candidates.sort(key=lambda item: item["score"], reverse=True)

    offset = (iteration_count - 1) * limit
    selected = all_candidates[offset: offset + limit]

    if include_text:
        selected = [
            enrich_candidate_with_text(
                candidate=dict(candidate),
                include_full_text=include_full_text,
            )
            for candidate in selected
        ]

    result = {
        "status": "success",
        "query": search_query,
        "current_iteration": iteration_count,
        "max_pages": max_pages,
        "limit": limit,
        "min_score": min_score,
        "project_root": str(PROJECT_ROOT),
        "parser_path": str(PROJECT_ROOT / "html_legal_text_parser.py"),
        "pages_checked": len(page_reports),
        "page_reports": page_reports,
        "total_candidates_found": len(all_candidates),
        "documents_returned": len(selected),
        "has_more_documents": offset + limit < len(all_candidates),
        "documents": selected,
    }

    return json.dumps(result, ensure_ascii=False, indent=2)


# ============================================================
# ТЕСТЫ
# ============================================================

if __name__ == "__main__":
    print("=========================================")
    print("   ЗАПУСК ПОЛНОГО ТЕСТИРОВАНИЯ ТУЛЗЫ")
    print("=========================================")
    print(f"PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"HTML parser:  {PROJECT_ROOT / 'html_legal_text_parser.py'}")

    tests = [
        {
            "name": "Позитивный 1: бетонные конструкции",
            "query": "Бетонные и железобетонные конструкции",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 2: СП 22",
            "query": "СП 22.13330 основания зданий",
            "iteration_count": 1,
        },
        {
            "name": "Позитивный 3: фундамент",
            "query": "Какие документы нужны для строительства фундамента жилого дома?",
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
            "name": "Граничный 6: строка вместо числа",
            "query": "Основания зданий",
            "iteration_count": "два",
        },
        {
            "name": "Граничный 7: СНиП должен фильтроваться",
            "query": "СНиП 3.02.01-87",
            "iteration_count": 1,
        },
        {
            "name": "Граничный 8: огромная итерация",
            "query": "Основания зданий",
            "iteration_count": 9999,
        },
        {
            "name": "Граничный 9: спецсимволы",
            "query": "СП 22.13330%/?\"&",
            "iteration_count": 1,
        },
    ]

    for test in tests:
        print("\n-----------------------------------------")
        print(test["name"])
        print("-----------------------------------------")

        result = search_documents_for_construction(
            search_query=test["query"],
            iteration_count=test["iteration_count"],
            max_pages=3,
            limit=5,
            min_score=0.15,
            include_text=False,
            include_full_text=False,
        )

        print(result)

    print("\n=========================================")
    print("   ДОП. ТЕСТ: ИЗВЛЕЧЕНИЕ HTML-ТЕКСТА")
    print("=========================================")

    result_with_text = search_documents_for_construction(
        search_query="СП 22.13330 основания зданий",
        iteration_count=1,
        max_pages=3,
        limit=3,
        min_score=0.15,
        include_text=True,
        include_full_text=False,
    )

    print(result_with_text)

    out_path = Path(__file__).resolve().parent / "sniprf_last_result.json"
    out_path.write_text(result_with_text, encoding="utf-8")
    print(f"\nПоследний JSON сохранён для проверки: {out_path}")