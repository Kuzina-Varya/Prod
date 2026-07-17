"""
Поисковая тулза для строительных нормативных документов на sniprf.ru.

Модуль рассчитан на будущий вызов из LLM/tool-слоя. Главная точка входа -
`search_documents_for_construction()`: функция принимает обычный текстовый
запрос, ищет только по sniprf.ru/www.sniprf.ru, возвращает найденные документы пачками и
при необходимости добавляет очищенный HTML-текст через отдельный модуль
`html_legal_text_parser.extract_main_text_from_html()`.

Этот файл отвечает за поиск, фильтрацию URL и диагностику. Он не
должен заменять HTML-парсер и не должен искать по внешним сайтам.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin, urlparse, urlunparse, unquote

import requests
from bs4 import BeautifulSoup


HTTP = requests.Session()
HTTP.trust_env = False


# ============================================================
# ПОДКЛЮЧЕНИЕ КОРНЯ ПРОЕКТА
# ============================================================

def find_project_root() -> Path:
    """Находит корень проекта по файлу html_legal_text_parser.py."""
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

# ============================================================
# УТИЛИТЫ
# ============================================================

def force_http(url: str) -> str:
    """Приводит URL sniprf к HTTP, потому что сайт стабильнее работает так."""
    url = str(url or "").strip()
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return url


def get_host(url: str) -> str:
    """Возвращает домен URL в нижнем регистре."""
    return (urlparse(url).hostname or "").lower()


def is_allowed_host(url: str) -> bool:
    """Разрешает запросы только к sniprf.ru и www.sniprf.ru."""
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
    """Преобразует обычный запрос в значимые поисковые слова."""
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
    """Добавляет небольшой словарь строительных синонимов для диагностических совпадений."""
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
    Извлекает номера СП из запроса.

    Примеры:
    - "СП 22.13330" -> ["22-13330"]
    - "SP 63.13330" -> ["63-13330"]
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


def is_catalog_listing_url(url: str) -> bool:
    """Определяет страницы-каталоги sniprf, которые не являются документами."""
    path = urlparse(force_http(url)).path.lower().rstrip("/")
    return path in {"", "/sp"} or bool(re.fullmatch(r"/sp\d{3}x|/sp\d{4}", path))


def is_http_404(error: Exception) -> bool:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 404


def classify_page_exception(url: str, error: Exception) -> dict:
    """Преобразует ошибки загрузки в понятную диагностику для LLM."""
    if is_http_404(error) and is_catalog_listing_url(url):
        return {
            "status": "skipped",
            "severity": "info",
            "ignored": True,
            "error_kind": "expected_missing_catalog_page",
            "error": f"{type(error).__name__}: {error}",
            "diagnostic": (
                "На sniprf.ru нет этой годовой страницы каталога. "
                "Это нормально: скрипт пробует возможный раздел и идёт дальше."
            ),
        }

    if is_http_404(error):
        return {
            "status": "skipped",
            "severity": "warning",
            "ignored": True,
            "error_kind": "missing_page",
            "error": f"{type(error).__name__}: {error}",
            "diagnostic": "Страница не найдена на sniprf.ru. Документ пропущен, поиск продолжается.",
        }

    return {
        "status": "error",
        "severity": "error",
        "ignored": False,
        "error_kind": type(error).__name__,
        "error": f"{type(error).__name__}: {error}",
        "diagnostic": "Неожиданная ошибка загрузки или парсинга страницы.",
    }


def normalize_identifier(value: str) -> str:
    return re.sub(r"[^0-9]+", "-", str(value or "")).strip("-")


def exact_sp_number_matches(query: str, title: str, url: str) -> list[str]:
    """Возвращает номера СП из запроса, точно совпавшие с заголовком или URL."""
    text = f"{title} {url}".lower().replace(",", ".")
    matches = []

    for sp_num in extract_sp_numbers(query):
        dotted = sp_num.replace("-", ".")
        compact_patterns = (
            rf"\bсп\s*{re.escape(dotted)}\b",
            rf"\bsp\s*{re.escape(dotted)}\b",
            rf"/sp{re.escape(sp_num)}(?:-|/|$)",
        )

        if any(re.search(pattern, text, flags=re.I) for pattern in compact_patterns):
            matches.append(sp_num)

    return list(dict.fromkeys(matches))


# ============================================================
# ЗАГРУЗКА HTML
# ============================================================

def fetch_html(url: str) -> str:
    """
    Загружает HTML-страницу с sniprf.ru.

    Функция запрещает чужие домены до запроса и дополнительно проверяет, что
    сайт не перенаправил ответ на внешний домен.
    """
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

    response = HTTP.get(
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

def build_search_urls(query: str, max_pages: int = 3) -> list[str]:
    """
    Формирует URL внутреннего поиска sniprf.ru по исходному запросу.

    Эти URL также ограничены BASE_URL и не обращаются к внешним поисковикам.
    """
    query = str(query or "").strip()

    encoded_path = quote(query)
    encoded_plus = quote_plus(query)

    urls = []

    try:
        max_pages = max(1, int(max_pages))
    except Exception:
        max_pages = 3

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
    """Отбрасывает навигацию, кабинет, контакты, поиск и старые СНиП-ссылки."""
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
    """Определяет примерный тип документа по заголовку и URL."""
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


def collect_query_matches(title: str, url: str, query: str, snippet: str = "") -> list[str]:
    """Возвращает найденные слова запроса только как диагностику, без оценки документа."""
    title = normalize_space(title)
    snippet = normalize_space(snippet)

    title_lower = title.lower().replace("ё", "е")
    snippet_lower = snippet.lower().replace("ё", "е")
    url_lower = force_http(url).lower().replace("ё", "е")

    blob = f"{title_lower} {snippet_lower} {url_lower}"

    base_query_words = normalize_query_words(query)
    expanded_query_words = expand_query_words(base_query_words)
    matched = []

    for word in expanded_query_words:
        word = word.lower().replace("ё", "е")
        if word and word in blob:
            matched.append(word)

    return list(dict.fromkeys(matched))


def extract_page_title(html: str) -> str:
    """Достаёт заголовок страницы сначала из h1, затем из HTML title."""
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


def extract_candidates_from_html(html: str, page_url: str, query: str) -> list[dict]:
    """
    Парсит ссылки из HTML-страницы и возвращает кандидаты документов.

    Страницы-каталоги можно использовать как источники ссылок, но сами
    каталоги фильтруются и не возвращаются как итоговые документы.
    """
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

        if is_catalog_listing_url(full_url):
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

        matched = collect_query_matches(
            title=title,
            url=full_url,
            query=query,
            snippet=snippet,
        )

        candidates.append({
            "title": title,
            "url": full_url,
            "document_type": infer_document_type(title, full_url),
            "matched_keywords": matched,
            "exact_identifier_matches": exact_sp_number_matches(query, title, full_url),
            "snippet": snippet,
            "source_mode": "site_search",
            "source_search_url": page_url,
        })

        seen_urls.add(full_url)

    return candidates


# ============================================================
# ОБОГАЩЕНИЕ ТЕКСТОМ
# ============================================================

def enrich_candidate_with_text(candidate: dict, include_full_text: bool = False) -> dict:
    """
    Добавляет text_preview/text к HTML-кандидату через html_legal_text_parser.

    Бинарные файлы здесь не парсятся. Так поиск и извлечение текста остаются
    разными зонами ответственности.
    """
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
    """
    Ищет на sniprf.ru строительные документы по обычному текстовому запросу.

    Параметры:
    - search_query: текст запроса от пользователя или LLM.
    - iteration_count: страница результатов, начиная с 1.
    - max_pages: сколько страниц внутреннего поиска sniprf проверить.
    - limit: сколько документов вернуть за один вызов.
    - min_score: устаревший параметр совместимости, больше не используется.
    - include_text: добавлять `text_preview` через html_legal_text_parser.
    - include_full_text: добавлять полный HTML-текст, а не только превью.

    Возвращает:
    JSON-строку с диагностикой (`page_reports`, `page_errors`, `page_skipped`)
    и очередной пачкой `documents`. Скрипт отправляет исходный запрос во
    внутренний поиск сайта и не оценивает смысловую релевантность документов.
    """
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

    search_urls = build_search_urls(search_query, max_pages=max_pages)

    for url in search_urls:
        report = {
            "mode": "site_search",
            "url": url,
            "status": "",
            "severity": "",
            "ignored": False,
            "error_kind": "",
            "candidates_found": 0,
            "error": "",
            "diagnostic": "Parsed sniprf.ru internal search page for the original query.",
            "search_input_submitted": True,
        }

        try:
            html = fetch_html(url)
            candidates = extract_candidates_from_html(
                html=html,
                page_url=url,
                query=search_query,
            )

            count = 0
            for candidate in candidates:
                count += add_candidate(candidate)

            report["status"] = "ok"
            report["candidates_found"] = count

        except Exception as e:
            report.update(classify_page_exception(url, e))

        page_reports.append(report)

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

    error_count = sum(1 for report in page_reports if report["status"] == "error")
    skipped_count = sum(1 for report in page_reports if report["status"] == "skipped")

    result = {
        "status": "success",
        "query": search_query,
        "current_iteration": iteration_count,
        "max_pages": max_pages,
        "limit": limit,
        "source_site": BASE_URL,
        "search_strategy": "submit_original_query_to_sniprf_site_search_then_parse_results",
        "exact_document_numbers": extract_sp_numbers(search_query),
        "query_variants": [search_query],
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


# ============================================================
# ТЕСТЫ
# ============================================================

if __name__ == "__main__":
    print("=========================================")
    print("   ЗАПУСК БЫСТРОГО ТЕСТИРОВАНИЯ ТУЛЗЫ")
    print("=========================================")
    print(f"PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"HTML parser:  {PROJECT_ROOT / 'html_legal_text_parser.py'}")

    tests = [
        {
            "name": "Позитивный 1: точный СП",
            "query": "СП 22.13330 основания зданий",
            "iteration_count": 1,
            "limit": 5,
        },
        {
            "name": "Граничный 2: пустой запрос",
            "query": "   ",
            "iteration_count": 1,
            "limit": 5,
        },
        {
            "name": "Граничный 3: маленький limit",
            "query": "СП 22.13330 основания зданий",
            "iteration_count": 1,
            "limit": 1,
        },
        {
            "name": "Граничный 4: огромная итерация",
            "query": "СП 22.13330 основания зданий",
            "iteration_count": 9999,
        },
    ]

    for test in tests:
        print("\n-----------------------------------------")
        print(test["name"])
        print("-----------------------------------------")

        result = search_documents_for_construction(
            search_query=test["query"],
            iteration_count=test["iteration_count"],
            max_pages=0,
            limit=test.get("limit", 5),
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
        max_pages=0,
        limit=1,
        include_text=True,
        include_full_text=False,
    )

    print(result_with_text)

    out_path = Path(__file__).resolve().parent / "sniprf_last_result.json"
    out_path.write_text(result_with_text, encoding="utf-8")
    print(f"\nПоследний JSON сохранён для проверки: {out_path}")
