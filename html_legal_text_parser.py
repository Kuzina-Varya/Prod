import argparse
import re
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


DROP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "iframe",
    "nav", "header", "footer", "aside", "form", "button",
}

DROP_ATTR_WORDS = (
    "menu", "nav", "navbar", "header", "footer", "breadcrumb", "breadcrumbs",
    "sidebar", "aside", "search", "share", "social", "cookie", "captcha",
    "feedback", "subscribe", "pagination", "toolbar", "popup", "modal",
    "blind", "visually", "cabinet", "login", "lk",
)

CONTENT_ATTR_WORDS = (
    "content", "main", "article", "document", "doc", "text", "body",
    "news-detail", "publication", "page-content", "detail", "material",
)

LEGAL_MARKERS = (
    "статья", "глава", "раздел", "пункт", "часть", "абзац",
    "постановление", "приказ", "федеральный закон", "закон",
    "кодекс", "гост", "свод правил", "технический регламент",
    "правила", "положение", "утвердить", "внести изменения",
    "российской федерации", "министерство", "верховный суд",
)

BOILERPLATE_EXACT = {
    "найти", "меню", "поиск", "контакты", "новости", "деятельность",
    "документы", "правовая информация", "обращения граждан",
    "главная", "главная страница", "карта сайта", "обратная связь",
    "поделиться", "расширенный поиск", "все разделы сайта",
    "перейти к контенту", "загрузка...", "прервать",
    "рус", "eng", "ru", "en", "русский", "english",
    "версия для слабовидящих", "version for visually impaired",
    "личный кабинет", "вход", "пресс-центр", "официальный сайт",
    "наверх", "подача обращений", "электронная справочная",
}

BOILERPLATE_RE = (
    re.compile(r"^открыть панель\b", re.I),
    re.compile(r"^поделиться\b", re.I),
    re.compile(r"^версия для слабовидящих\b", re.I),
    re.compile(r"^version for visually impaired\b", re.I),
    re.compile(r"^личный кабинет\b", re.I),
    re.compile(r"^выполняется запрос\b", re.I),
    re.compile(r"^помним и гордимся победой!?$", re.I),
    re.compile(r"^\d{1,2}\s*:\s*\d{1,2}(?:\s*:\s*\d{1,2})?$"),
    re.compile(r"^99\s*:\s*99"),
)


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _attr_text(tag) -> str:
    if tag is None:
        return ""
    if getattr(tag, "attrs", None) is None:
        return ""
    values = []
    for name in ("id", "class", "role", "aria-label", "data-block", "data-area"):
        value = tag.get(name)
        if isinstance(value, (list, tuple)):
            values.extend(str(v) for v in value)
        elif value:
            values.append(str(value))
    return " ".join(values).lower()


def _remove_noise(soup: BeautifulSoup) -> None:
    for tag in soup(list(DROP_TAGS)):
        tag.decompose()

    for tag in list(soup.find_all(True)):
        if getattr(tag, "attrs", None) is None:
            continue
        attrs = _attr_text(tag)
        if attrs and any(word in attrs for word in DROP_ATTR_WORDS):
            tag.decompose()


def _line_is_boilerplate(line: str) -> bool:
    low = line.lower().strip(" .:;|")
    if not low:
        return True
    if low in BOILERPLATE_EXACT:
        return True
    if len(low) <= 2 and not low.isdigit():
        return True
    if len(low) <= 35 and any(pattern.search(low) for pattern in BOILERPLATE_RE):
        return True
    return False


def normalize_text(text: str) -> str:
    lines = []
    previous = ""
    short_nav_run = 0
    for raw in (text or "").splitlines():
        line = _norm_space(raw)
        if not line or _line_is_boilerplate(line):
            continue
        if line == previous:
            continue

        has_legal = any(marker in line.lower() for marker in LEGAL_MARKERS)
        if len(line) < 35 and not has_legal and not re.search(r"\d", line):
            short_nav_run += 1
            if short_nav_run >= 4:
                continue
        else:
            short_nav_run = 0

        lines.append(line)
        previous = line
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _text_from_tag(tag) -> str:
    return normalize_text(tag.get_text("\n", strip=True))


def _raw_text(tag) -> str:
    return tag.get_text("\n", strip=True) if tag is not None else ""


def _link_density(tag) -> float:
    text_len = len(tag.get_text(" ", strip=True))
    if text_len == 0:
        return 1.0
    link_len = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
    return link_len / text_len


def _score_text(text: str, tag=None) -> float:
    low = text.lower()
    length = len(text)
    lines = [line for line in text.splitlines() if line.strip()]
    legal_hits = sum(low.count(marker) for marker in LEGAL_MARKERS)
    numbered = len(re.findall(r"(?m)^\s*(?:\d+[\.)]|[а-я]\))\s+\S+", text, flags=re.I))
    articles = len(re.findall(r"(?im)^\s*статья\s+\d+", text))
    long_lines = sum(1 for line in lines if len(line) > 80)
    short_lines = sum(1 for line in lines if len(line) < 35)

    score = length + legal_hits * 1200 + numbered * 220 + articles * 700 + long_lines * 70
    score -= short_lines * 35

    if tag is not None:
        attrs = _attr_text(tag)
        if any(word in attrs for word in CONTENT_ATTR_WORDS):
            score *= 1.4
        density = _link_density(tag)
        if density > 0.45:
            score *= 0.08
        elif density > 0.30:
            score *= 0.20
        elif density > 0.18:
            score *= 0.55

    menu_hits = sum(low.count(word) for word in BOILERPLATE_EXACT)
    score -= menu_hits * 450
    return score


def _candidate_tags(soup: BeautifulSoup):
    selectors = [
        "main", "article", "[role=main]",
        ".content", ".main", ".article", ".document", ".doc", ".text",
        ".page-content", ".news-detail", ".detail", ".material",
        "#content", "#main", "#article", "#document", "#docContent",
    ]
    seen = set()
    for selector in selectors:
        for tag in soup.select(selector):
            marker = id(tag)
            if marker not in seen:
                seen.add(marker)
                yield tag
    for tag in soup.find_all(("div", "section", "article", "td", "body")):
        marker = id(tag)
        if marker not in seen:
            seen.add(marker)
            yield tag


def _trim_before_document_start(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    start_patterns = (
        r"^(российская федерация|конституция|кодекс)\b",
        r"^(федеральный конституционный закон|федеральный закон|закон)\b",
        r"^(постановление|приказ|распоряжение|определение|решение)\b",
        r"^об утверждении\b",
        r"^гражданский процессуальный кодекс\b",
    )

    for idx, line in enumerate(lines[:80]):
        low = line.lower()
        if any(re.search(pattern, low, flags=re.I) for pattern in start_patterns):
            return "\n".join(lines[idx:]).strip() + "\n"

    best_idx = 0
    for idx, line in enumerate(lines[:80]):
        low = line.lower()
        if any(marker in low for marker in LEGAL_MARKERS) and len(line) > 45:
            best_idx = idx
            break
    return "\n".join(lines[best_idx:]).strip() + "\n"


def extract_main_text_from_html(html: str) -> str:
    if not html:
        return ""

    if BeautifulSoup is None:
        text = re.sub(r"(?is)<(script|style|noscript|svg).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
        return _trim_before_document_start(normalize_text(text))

    soup = BeautifulSoup(html, "html.parser")
    _remove_noise(soup)

    body = soup.body or soup
    best_text = _text_from_tag(body)
    best_score = _score_text(best_text, body)

    for tag in _candidate_tags(soup):
        text = _text_from_tag(tag)
        if len(text) < 180:
            continue
        score = _score_text(text, tag)
        if score > best_score:
            best_text = text
            best_score = score

    return _trim_before_document_start(best_text)


def clean_html_file(input_path: Path, output_path: Path) -> None:
    html = input_path.read_text(encoding="utf-8-sig", errors="replace")
    text = extract_main_text_from_html(html)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8-sig")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean an HTML file into legal/document text.")
    parser.add_argument("input", help="HTML file path")
    parser.add_argument("output", nargs="?", help="Output TXT path. Defaults to input stem + .clean.txt")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".clean.txt")
    clean_html_file(input_path, output_path)
    print(output_path.resolve())


if __name__ == "__main__":
    main()
