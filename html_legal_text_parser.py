"""Offline extraction of the main legal text from an already loaded HTML string."""

import argparse
import re
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:
    PlaywrightError = Exception
    sync_playwright = None


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

PRINT_CONTROL_RE = re.compile(
    r"^(?:распечатать|печать|напечатать|верси[яю]\s+для\s+печати|print|print\s+version|printer)\b",
    re.I,
)


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _attr_text(tag) -> str:
    if tag is None:
        return ""
    if isinstance(tag, dict):
        return str(tag.get("attrs") or "").lower()
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


def _line_is_boilerplate(line: str) -> bool:
    low = line.lower().strip(" .:;|")
    if not low:
        return True
    if PRINT_CONTROL_RE.search(low):
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
    if isinstance(tag, dict):
        return normalize_text(str(tag.get("text") or ""))
    return normalize_text(tag.get_text("\n", strip=True))


def _raw_text(tag) -> str:
    if isinstance(tag, dict):
        return str(tag.get("text") or "")
    return tag.get_text("\n", strip=True) if tag is not None else ""


def _link_density(tag) -> float:
    if isinstance(tag, dict):
        text_len = int(tag.get("text_len") or 0)
        if text_len == 0:
            return 1.0
        return int(tag.get("link_len") or 0) / text_len
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


def _playwright_dom_snapshot(page) -> dict:
    return page.evaluate(
        """({ dropTags, dropAttrWords }) => {
            const printPatterns = [
                /распечат/i,
                /\\bпечать\\b/i,
                /напечат/i,
                /верси[яю]\\s+для\\s+печати/i,
                /\\bprint\\b/i,
                /print\\s+version/i,
                /printer/i,
            ];

            const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const visibleText = (el) => norm(el.innerText || el.textContent || el.value || '');
            const attrText = (el) => {
                const names = ['id', 'class', 'role', 'aria-label', 'data-block', 'data-area', 'title', 'value', 'alt'];
                return names.map((name) => el.getAttribute(name) || '').join(' ').toLowerCase();
            };
            const textWithBreaks = (el) => (el.innerText || el.textContent || '').trim();
            const linkLength = (el) => Array.from(el.querySelectorAll('a'))
                .reduce((total, link) => total + visibleText(link).length, 0);
            const snapshot = (el) => {
                const text = textWithBreaks(el);
                return {
                    text,
                    attrs: attrText(el),
                    text_len: norm(text).length,
                    link_len: linkLength(el),
                };
            };

            document.querySelectorAll(dropTags.join(',')).forEach((el) => el.remove());

            const controlSelector = 'a,input,[role="button"],[onclick],[class*="print" i],[id*="print" i]';
            document.querySelectorAll(controlSelector).forEach((el) => {
                const combined = norm(`${visibleText(el)} ${attrText(el)}`);
                if (combined.length <= 140 && printPatterns.some((pattern) => pattern.test(combined))) {
                    el.remove();
                }
            });

            Array.from(document.querySelectorAll('*')).forEach((el) => {
                const attrs = attrText(el);
                if (attrs && dropAttrWords.some((word) => attrs.includes(word))) {
                    el.remove();
                }
            });

            const selectors = [
                'main', 'article', '[role=main]',
                '.content', '.main', '.article', '.document', '.doc', '.text',
                '.page-content', '.news-detail', '.detail', '.material',
                '#content', '#main', '#article', '#document', '#docContent',
            ];
            const seen = new Set();
            const candidates = [];
            const add = (el) => {
                if (!el || seen.has(el)) {
                    return;
                }
                seen.add(el);
                candidates.push(snapshot(el));
            };

            selectors.forEach((selector) => document.querySelectorAll(selector).forEach(add));
            document.querySelectorAll('div,section,article,td,body').forEach(add);

            return {
                body: snapshot(document.body || document.documentElement),
                candidates,
            };
        }""",
        {
            "dropTags": sorted(DROP_TAGS),
            "dropAttrWords": list(DROP_ATTR_WORDS),
        },
    )


def _block_external_request(route) -> None:
    url = route.request.url.lower()
    if url.startswith(("http://", "https://")):
        route.abort()
        return
    route.continue_()


def _extract_with_playwright(html: str) -> str:
    if sync_playwright is None:
        raise RuntimeError("playwright is not installed")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except PlaywrightError:
            browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(java_script_enabled=False)
            page.route("**/*", _block_external_request)
            page.set_content(html, wait_until="domcontentloaded", timeout=15000)
            snapshot = _playwright_dom_snapshot(page)
        finally:
            browser.close()

    body = snapshot.get("body") or {"text": ""}
    best_text = _text_from_tag(body)
    best_score = _score_text(best_text, body)

    for tag in snapshot.get("candidates") or []:
        text = _text_from_tag(tag)
        if len(text) < 180:
            continue
        score = _score_text(text, tag)
        if score > best_score:
            best_text = text
            best_score = score

    return _trim_before_document_start(best_text)


def _extract_with_regex_fallback(html: str) -> str:
    text = html
    for tag in sorted(DROP_TAGS, key=len, reverse=True):
        text = re.sub(rf"(?is)<{tag}\b.*?>.*?</{tag}>", " ", text)
    text = re.sub(
        r"(?is)<(?:a|input)\b[^>]*(?:распечат|печать|print|printer)[^>]*(?:>.*?</a>|/?>)",
        " ",
        text,
    )
    text = re.sub(
        r"(?is)<a\b[^>]*>[^<]*(?:распечат|печать|print|printer)[^<]*</a>",
        " ",
        text,
    )
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    return _trim_before_document_start(normalize_text(text))


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
    """Return cleaned document text from an HTML string without making network requests."""
    if not html:
        return ""

    try:
        return _extract_with_playwright(html)
    except (RuntimeError, PlaywrightError):
        return _extract_with_regex_fallback(html)


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
