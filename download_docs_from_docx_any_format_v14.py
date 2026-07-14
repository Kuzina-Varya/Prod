import argparse
import csv
import logging
import mimetypes
import os
import re
import sys
import time
import zipfile
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, urlencode, unquote, urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests
from docx import Document
from docx.oxml.ns import qn

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from charset_normalizer import from_bytes as charset_from_bytes
except ImportError:
    charset_from_bytes = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from striprtf.striprtf import rtf_to_text as striprtf_to_text
except ImportError:
    striprtf_to_text = None

try:
    from html_legal_text_parser import extract_main_text_from_html
except ImportError:
    extract_main_text_from_html = None

CHUNK_SIZE = 1024 * 256
INVALID_CHARS = '<>:"/\\|?*'
DEFAULT_MAX_PATH = 200

logging.getLogger("pypdf").setLevel(logging.ERROR)


def safe_name(text: str, max_len: int = 120) -> str:
    """РћС‡РёС‰Р°РµС‚ РёРјСЏ С„Р°Р№Р»Р°/РїР°РїРєРё РѕС‚ Р·Р°РїСЂРµС‰С‘РЅРЅС‹С… Windows-СЃРёРјРІРѕР»РѕРІ."""
    text = str(text or "").strip()
    text = re.sub(r"\s+", " ", text)
    for ch in INVALID_CHARS:
        text = text.replace(ch, "_")
    text = text.strip(" ._")
    return (text[:max_len].strip(" ._") or "document")


def make_subpath(folder_text: str) -> Path:
    """РџСЂРµРѕР±СЂР°Р·СѓРµС‚ СЃС‚СЂРѕРєСѓ РІРёРґР° 05_...\\01_... РІ Path СЃ РІР»РѕР¶РµРЅРЅС‹РјРё РїР°РїРєР°РјРё."""
    folder_text = str(folder_text or "").strip().strip("\\/")
    parts = re.split(r"[\\/]+", folder_text)
    return Path(*[safe_name(p, 80) for p in parts if p.strip()])


def get_cell_hyperlinks(cell, rels) -> list[str]:
    """Р”РѕСЃС‚Р°С‘С‚ URL РёР· РіРёРїРµСЂСЃСЃС‹Р»РѕРє Word РІРЅСѓС‚СЂРё СЏС‡РµР№РєРё + URL, РІСЃС‚Р°РІР»РµРЅРЅС‹Рµ РїСЂРѕСЃС‚С‹Рј С‚РµРєСЃС‚РѕРј."""
    urls = []
    for hyperlink in cell._tc.xpath(".//w:hyperlink"):
        rid = hyperlink.get(qn("r:id"))
        if rid and rid in rels:
            urls.append(rels[rid].target_ref)

    text_urls = re.findall(r"https?://\S+", cell.text or "")
    urls.extend(text_urls)

    result = []
    seen = set()
    for url in urls:
        url = url.strip().rstrip(").,;]")
        if url and url not in seen:
            result.append(url)
            seen.add(url)
    return result


def force_scheme(url: str, scheme: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        return urlunparse(parsed._replace(scheme=scheme))
    return url


def normalized_host(parsed) -> str:
    return (parsed.hostname or parsed.netloc or "").lower().removeprefix("www.")


def publication_pravo_eonumber(url: str) -> str:
    parsed = urlparse(url)
    host = normalized_host(parsed)
    if host != "publication.pravo.gov.ru":
        return ""

    qs = parse_qs(parsed.query)
    for key in ("eoNumber", "eoNumber".lower()):
        if key in qs and qs[key]:
            return safe_name(qs[key][0], 40)

    match = re.search(r"/(?:document|Document/View)/([0-9]{10,})", parsed.path, flags=re.I)
    if match:
        return match.group(1)
    return ""


def publication_pravo_pdf_url(eonumber: str, scheme: str = "http") -> str:
    return f"{scheme}://publication.pravo.gov.ru/file/pdf?eoNumber={eonumber}"


def candidate_urls(original_url: str) -> list[str]:
    """РЎРЅР°С‡Р°Р»Р° СЃС‚СЂРѕРіРѕ HTTP, РїРѕС‚РѕРј HTTPS. Р”СѓР±Р»Рё СѓР±РёСЂР°СЋС‚СЃСЏ."""
    candidates = []
    eonumber = publication_pravo_eonumber(original_url)
    if eonumber:
        # publication.pravo.gov.ru С‡Р°СЃС‚Рѕ РѕС‚РґР°С‘С‚ 500 РЅР° HTML-СЃС‚СЂР°РЅРёС†Р°С… РґРѕРєСѓРјРµРЅС‚Р°,
        # РЅРѕ РїСЂСЏРјРѕР№ PDF РїРѕ HTTP СЃС‚Р°Р±РёР»СЊРЅРѕ РґРѕСЃС‚СѓРїРµРЅ.
        publication_http = [
            publication_pravo_pdf_url(eonumber, "http"),
            f"http://publication.pravo.gov.ru/document/{eonumber}",
            f"http://publication.pravo.gov.ru/Document/View/{eonumber}",
        ]
        publication_https = [
            publication_pravo_pdf_url(eonumber, "https"),
            f"https://publication.pravo.gov.ru/document/{eonumber}",
            f"https://publication.pravo.gov.ru/Document/View/{eonumber}",
        ]
        for cand in publication_http + publication_https:
            if cand not in candidates:
                candidates.append(cand)

    for scheme in ("http", "https"):
        cand = force_scheme(original_url, scheme)
        if cand not in candidates:
            candidates.append(cand)
    return candidates


def is_pravo_ips_url(url: str) -> bool:
    """РџСЂРѕРІРµСЂСЏРµС‚, С‡С‚Рѕ СЌС‚Рѕ СЃС‚Р°СЂР°СЏ IPS-СЃС‚СЂР°РЅРёС†Р° pravo.gov.ru/proxy/ips/."""
    parsed = urlparse(url)
    host = normalized_host(parsed)
    return host.endswith("pravo.gov.ru") and "/proxy/ips" in parsed.path.lower()


def pravo_doc_itself_url(url: str) -> str:
    """
    Р”Р»СЏ СЃС‚Р°СЂРѕРіРѕ pravo.gov.ru СЃСЃС‹Р»РєР° РІРёРґР° ?docbody=&nd=... РѕС‚РєСЂС‹РІР°РµС‚ РѕР±РѕР»РѕС‡РєСѓ:
    С‚СѓР»Р±Р°СЂ, СЃРїРёСЃРѕРє СЂРµРґР°РєС†РёР№, РїРѕРёСЃРє Рё СЃРѕРѕР±С‰РµРЅРёРµ В«Р—Р°РіСЂСѓР·РєР° РґРѕРєСѓРјРµРЅС‚Р°В».

    Р РµР°Р»СЊРЅС‹Р№ С‚РµРєСЃС‚ РґРѕРєСѓРјРµРЅС‚Р° Р»РµР¶РёС‚ РїРѕ С‚РѕРјСѓ Р¶Рµ nd, РЅРѕ СЃ РїР°СЂР°РјРµС‚СЂРѕРј doc_itself=.
    РџРѕСЌС‚РѕРјСѓ РґР»СЏ СЃРєР°С‡РёРІР°РЅРёСЏ С‚РµРєСЃС‚Р° Р·Р°РјРµРЅСЏРµРј docbody/doc-body РЅР° doc_itself.
    """
    if not is_pravo_ips_url(url):
        return url

    parsed = urlparse(url)
    pairs = parse_qs(parsed.query, keep_blank_values=True)
    keys_lower = {k.lower() for k in pairs.keys()}

    if "doc_itself" in keys_lower:
        return url

    if "docbody" not in keys_lower and "doc-body" not in keys_lower:
        return url

    ordered = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        k = key.lower()
        # Р­С‚Рё РїР°СЂР°РјРµС‚СЂС‹ РѕС‚РЅРѕСЃСЏС‚СЃСЏ Рє РѕР±РѕР»РѕС‡РєРµ/СЃРїРёСЃРєСѓ РІС‹РґР°С‡Рё, Р° РЅРµ Рє С‚РµР»Сѓ РґРѕРєСѓРјРµРЅС‚Р°.
        if k in {"docbody", "doc-body", "firstdoc", "lastdoc"}:
            continue
        for value in values:
            ordered.append((key, value))

    ordered.insert(0, ("doc_itself", ""))
    new_query = urlencode(ordered, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def preferred_content_url(original_url: str) -> str:
    """URL, СЃ РєРѕС‚РѕСЂРѕРіРѕ РЅР°РґРѕ Р±СЂР°С‚СЊ РёРјРµРЅРЅРѕ СЃРѕРґРµСЂР¶РёРјРѕРµ, Р° РЅРµ РѕР±РѕР»РѕС‡РєСѓ СЃР°Р№С‚Р°."""
    eonumber = publication_pravo_eonumber(original_url)
    if eonumber:
        return publication_pravo_pdf_url(eonumber, "http")
    return pravo_doc_itself_url(original_url)


def extract_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("eoNumber", "nd"):
        if key in qs and qs[key]:
            return safe_name(qs[key][0], 60)
    last = unquote(parsed.path.rstrip("/").split("/")[-1])
    if last and "." in last:
        return safe_name(Path(last).stem, 60)
    if last and re.fullmatch(r"[0-9A-Za-z_-]{6,}", last):
        return safe_name(last, 60)
    return ""


def filename_from_content_disposition(header: str) -> str:
    if not header:
        return ""
    msg = Message()
    msg["content-disposition"] = header
    filename = msg.get_param("filename", header="content-disposition")
    if filename:
        return safe_name(unquote(filename), 100)
    # РРЅРѕРіРґР° filename*=UTF-8''...
    m = re.search(r"filename\*=([^']*)''([^;]+)", header, flags=re.I)
    if m:
        return safe_name(unquote(m.group(2)), 100)
    return ""


def is_probably_pdf(first_chunk: bytes, content_type: str) -> bool:
    return first_chunk.startswith(b"%PDF") or "pdf" in (content_type or "").lower()


def is_probably_zip_office(first_chunk: bytes) -> bool:
    return first_chunk.startswith(b"PK\x03\x04")


def classify_response(url: str, content_type: str, first_chunk: bytes, content_disposition: str = "") -> tuple[str, str]:
    """Р’РѕР·РІСЂР°С‰Р°РµС‚ (С‚РёРї, СЂР°СЃС€РёСЂРµРЅРёРµ). HTML СЃРѕС…СЂР°РЅСЏРµРј РєР°Рє .txt, Р° РЅРµ .html."""
    ct = (content_type or "").split(";")[0].strip().lower()
    cd_name = filename_from_content_disposition(content_disposition)
    cd_suffix = Path(cd_name).suffix.lower() if cd_name else ""
    url_suffix = Path(unquote(urlparse(url).path)).suffix.lower()

    if is_probably_pdf(first_chunk, content_type):
        return "PDF", ".pdf"

    if ct in ("text/html", "application/xhtml+xml") or b"<html" in first_chunk[:500].lower() or b"<!doctype html" in first_chunk[:500].lower():
        return "HTML_TEXT", ".txt"

    if "rtf" in ct or first_chunk.startswith(b"{\\rtf") or url_suffix == ".rtf" or cd_suffix == ".rtf":
        return "WORD_RTF", ".rtf"

    word_exts = {".doc", ".docx"}
    excel_exts = {".xls", ".xlsx", ".csv"}
    if url_suffix in word_exts or cd_suffix in word_exts or "word" in ct or "msword" in ct:
        return "WORD_DOC", cd_suffix or url_suffix or ".docx"
    if url_suffix in excel_exts or cd_suffix in excel_exts or "excel" in ct or "spreadsheet" in ct:
        return "EXCEL_TABLE", cd_suffix or url_suffix or ".xlsx"

    if is_probably_zip_office(first_chunk):
        # Р•СЃР»Рё СЌС‚Рѕ Office Open XML, РЅРѕ С‚РёРї РЅРµ РїРѕРґСЃРєР°Р·Р°Р», СЃРѕС…СЂР°РЅСЏРµРј СЃ СЂР°СЃС€РёСЂРµРЅРёРµРј РёР· URL, РёРЅР°С‡Рµ .zip.
        if url_suffix in word_exts | excel_exts:
            doc_type = "WORD_DOC" if url_suffix in word_exts else "EXCEL_TABLE"
            return doc_type, url_suffix
        return "ZIP_OR_OFFICE", cd_suffix or url_suffix or ".zip"

    if ct.startswith("text/"):
        return "TEXT", ".txt"

    ext = cd_suffix or url_suffix
    if not ext or len(ext) > 10:
        ext = mimetypes.guess_extension(ct) or ".bin"
    return "BINARY_FILE", ext.lower()


def make_filename(num: str, title: str, original_url: str, ext: str) -> str:
    num = safe_name(num, 20) if num else ""
    doc_id = extract_id_from_url(original_url)
    title_part = safe_name(title, 110)

    if num and title_part and doc_id:
        base = f"{num}_{title_part}_{doc_id}"
    elif num and title_part:
        base = f"{num}_{title_part}"
    elif title_part and doc_id:
        base = f"{title_part}_{doc_id}"
    elif num and doc_id:
        base = f"{num}_{doc_id}"
    elif title_part:
        base = title_part
    elif num:
        base = num
    elif doc_id:
        base = doc_id
    else:
        base = "document"

    if doc_id and len(base) > 130:
        keep = max(40, 130 - len(doc_id) - 1)
        base = f"{safe_name(base[:keep], keep)}_{doc_id}"

    return safe_name(base, 140) + ext


def fit_path_length(target_dir: Path, filename: str, max_path: int = DEFAULT_MAX_PATH) -> Path:
    """РџРѕРґСЂРµР·Р°РµС‚ РёРјСЏ С„Р°Р№Р»Р°, РµСЃР»Рё РїРѕР»РЅС‹Р№ РїСѓС‚СЊ РїРѕР»СѓС‡Р°РµС‚СЃСЏ СЃР»РёС€РєРѕРј РґР»РёРЅРЅС‹Рј РґР»СЏ Windows."""
    target_dir_abs = target_dir.resolve()
    path = target_dir_abs / filename
    if len(str(path)) <= max_path:
        return path

    suffix = Path(filename).suffix
    stem = Path(filename).stem
    allowed = max_path - len(str(target_dir_abs)) - 1 - len(suffix)
    allowed = max(30, allowed)
    if len(stem) <= allowed:
        new_stem = safe_name(stem, allowed)
    else:
        tail_len = min(36, max(10, allowed // 3))
        head_len = max(10, allowed - tail_len - 1)
        new_stem = safe_name(stem[:head_len], head_len) + "_" + safe_name(stem[-tail_len:], tail_len)
    new_name = safe_name(new_stem, allowed) + suffix
    return target_dir_abs / new_name


def header_map(row) -> dict[str, int]:
    headers = [re.sub(r"\s+", " ", c.text.strip()).lower() for c in row.cells]
    result = {}
    for i, h in enumerate(headers):
        if h in {"№", "no", "n"}:
            result["num"] = i
        elif "документ" in h:
            result["document"] = i
        elif "папка" in h:
            result["folder"] = i
        elif "ссылка" in h or "источник" in h:
            result["link"] = i
    return result


def iter_registry_rows(docx_path: Path):
    doc = Document(docx_path)
    rels = doc.part.rels
    fallback_counter = 1

    for table_no, table in enumerate(doc.tables, start=1):
        if not table.rows:
            continue
        hm = header_map(table.rows[0])
        if not {"document", "folder", "link"}.issubset(hm):
            continue

        for row_no, row in enumerate(table.rows[1:], start=2):
            cells = row.cells
            title = cells[hm["document"]].text.strip()
            folder = cells[hm["folder"]].text.strip()
            num = cells[hm["num"]].text.strip() if "num" in hm else str(fallback_counter)
            link_cell = cells[hm["link"]]
            urls = get_cell_hyperlinks(link_cell, rels)

            if not title or not folder or not urls:
                continue

            for url in urls:
                yield {
                    "table_no": table_no,
                    "row_no": row_no,
                    "num": num,
                    "title": title,
                    "folder": folder,
                    "original_url": url,
                    "download_url": force_scheme(url, "http"),
                }
            fallback_counter += 1


def charset_from_content_type(content_type: str) -> str:
    """Р”РѕСЃС‚Р°С‘С‚ charset РёР· HTTP-Р·Р°РіРѕР»РѕРІРєР° Content-Type, РµСЃР»Рё РѕРЅ С‚Р°Рј РµСЃС‚СЊ."""
    if not content_type:
        return ""
    m = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, flags=re.I)
    return (m.group(1).strip() if m else "")


def looks_like_mojibake(text: str) -> bool:
    if not text:
        return False
    sample = text[:200000]
    replacement = sample.count("�") + sample.count("пїЅ")
    controls = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", sample))
    mojibake_tokens = len(re.findall(r"(?:Р.|С.|Ð.|Ñ.|Г.|Â.|вЂ.|В«|В»)", sample))
    cyrillic = len(re.findall(r"[\u0400-\u04FF]", sample))
    return replacement > 5 or controls > 3 or (mojibake_tokens > 20 and cyrillic < mojibake_tokens * 2)


def looks_like_pravo_shell_text(text: str) -> bool:
    if not text:
        return False
    sample = text[:20000]
    shell_markers = 0
    for marker in (
        "Текст документа:",
        "Загрузка документа",
        "Сравнение редакций",
        "Выберите предпочитаемый стиль",
        "Проверка ЭЦП",
        "Вниз по тексту",
        "Вверх по тексту",
    ):
        if marker in sample:
            shell_markers += 1
    has_articles = bool(re.search(r"\bСтатья\s+\d+\b|\bГлава\s+[IVXLC0-9]+\b", sample, flags=re.I))
    has_numbered_points = len(re.findall(r"(?m)^\s*\d+[.)]\s+\S+", sample)) >= 5
    return shell_markers >= 2 and not (has_articles or has_numbered_points)


def decode_html(content: bytes, content_type: str = "", http_encoding: str = "") -> tuple[str, str]:
    def normalize_encoding(enc: str) -> str:
        return (enc or "").strip().lower().replace("_", "-")

    def meta_charsets(data: bytes) -> list[str]:
        head = data[:50000]
        found = []
        patterns = (
            br"<meta[^>]+charset\s*=\s*['\"]?\s*([A-Za-z0-9_\-]+)",
            br"charset\s*=\s*['\"]?\s*([A-Za-z0-9_\-]+)",
            br"<\?xml[^>]+encoding\s*=\s*['\"]([^'\"]+)",
        )
        for pattern in patterns:
            for m in re.finditer(pattern, head, flags=re.I):
                enc = m.group(1).decode("ascii", errors="ignore").strip()
                if enc:
                    found.append(enc)
        return found

    def text_score(text: str, enc: str) -> int:
        sample = text[:200000]
        cyrillic = len(re.findall(r"[\u0400-\u04FF]", sample))
        controls = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", sample))
        replacement = sample.count("�") + sample.count("пїЅ")
        mojibake_tokens = len(re.findall(r"(?:Р.|С.|Ð.|Ñ.|Г.|Â.|вЂ.|В«|В»)", sample))
        good_words = len(re.findall(
            r"\b(Российской|Российская|Россия|Федерации|Федерация|Конституция|документ|документы|постановление|приказ|закон|кодекс|суд|суда|судебн\w*|эксперт\w*|экспертиз\w*|Министерство|Минюст|юстиции|правительство|текст|редакция|официальн\w*|государственн\w*)\b",
            sample,
            flags=re.I,
        ))
        service_words = len(re.findall(r"\b(и|в|во|на|по|от|до|для|или|не|с|со|к|ко|о|об|из|за|при|как)\b", sample, flags=re.I))
        enc_norm = normalize_encoding(enc)
        cp1251_bonus = 2500 if enc_norm in ("windows-1251", "cp1251") and controls == 0 and replacement == 0 else 0
        utf8_bonus = 1500 if enc_norm in ("utf-8", "utf-8-sig") and controls == 0 and replacement == 0 else 0
        return (
            good_words * 10000
            + service_words * 120
            + cyrillic * 2
            + cp1251_bonus
            + utf8_bonus
            - controls * 5000
            - replacement * 4000
            - mojibake_tokens * 800
        )

    try:
        utf8_text = content.decode("utf-8", errors="strict")
        if not looks_like_mojibake(utf8_text):
            return utf8_text, "utf-8"
    except UnicodeDecodeError:
        pass

    candidates = []
    candidates.extend(meta_charsets(content))
    header_charset = charset_from_content_type(content_type)
    if header_charset:
        candidates.append(header_charset)
    if http_encoding:
        candidates.append(http_encoding)
    if charset_from_bytes is not None:
        try:
            best = charset_from_bytes(content).best()
            if best and getattr(best, "encoding", None):
                candidates.append(best.encoding)
        except Exception:
            pass
    candidates.extend(["windows-1251", "cp1251", "utf-8", "utf-8-sig", "koi8-r", "iso-8859-5", "iso-8859-1", "latin-1"])

    unique = []
    seen = set()
    for enc in candidates:
        enc = (enc or "").strip()
        key = normalize_encoding(enc)
        if enc and key not in seen:
            unique.append(enc)
            seen.add(key)

    best_text = None
    best_enc = ""
    best_score = -10**18
    for enc in unique:
        try:
            if normalize_encoding(enc) in ("utf-8", "utf-8-sig"):
                text = content.decode(enc, errors="strict")
            else:
                text = content.decode(enc, errors="replace")
        except Exception:
            continue
        score = text_score(text, enc)
        if score > best_score:
            best_text = text
            best_enc = enc
            best_score = score

    if best_text is not None:
        return best_text, best_enc
    return content.decode("utf-8", errors="replace"), "utf-8-replace"
def html_to_readable_text(content: bytes, content_type: str = "", http_encoding: str = "") -> tuple[str, str]:
    html, encoding = decode_html(content, content_type=content_type, http_encoding=http_encoding)

    if extract_main_text_from_html is not None:
        try:
            text = extract_main_text_from_html(html)
            if text.strip():
                return text, encoding
        except Exception:
            pass

    if BeautifulSoup is None:
        # Р‘РµР· bs4 С…РѕС‚СЏ Р±С‹ СѓР±РµСЂС‘Рј С‚РµРіРё РіСЂСѓР±Рѕ.
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    else:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")

    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip() + "\n", encoding


def normalize_link_text(text: str, max_len: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:max_len].strip()


def links_summary_text(links: list[dict]) -> str:
    lines = []
    for link in links:
        title = normalize_link_text(link.get("title", ""), 180)
        url = link.get("url", "")
        if title and url:
            lines.append(f"- {title}: {url}")
        elif url:
            lines.append(f"- {url}")
    return "\n".join(lines)


def pravo_nd(url: str) -> str:
    parsed = urlparse(url)
    host = normalized_host(parsed)
    if host != "pravo.gov.ru" or "/proxy/ips" not in parsed.path.lower():
        return ""
    return parse_qs(parsed.query).get("nd", [""])[0]


def canonical_link_key(url: str) -> str:
    nd = pravo_nd(url)
    if nd:
        return "pravo-nd:" + nd
    parsed = urlparse(url)
    host = normalized_host(parsed)
    path = parsed.path or "/"
    return urlunparse(("http", host, path, "", parsed.query, ""))


def is_probably_direct_document_link(url: str) -> bool:
    parsed = urlparse(url)
    host = normalized_host(parsed)
    path = parsed.path.lower()
    suffix = Path(unquote(path)).suffix.lower()
    if suffix in {".pdf", ".rtf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"}:
        return True
    if "/uploaded/files/" in path:
        return True
    if re.search(r"/(?:ru/)?documents/\d+/?$", path):
        return True
    if host == "vsrf.ru" and re.search(r"/documents/(?:own|practice|thematics|reviews|arbitration)/\d+/?$", path):
        return True
    if host == "publication.pravo.gov.ru" and (
        re.search(r"/(?:document|document/view)/\d{10,}", path) or path == "/file/pdf"
    ):
        return True
    return False


def chain_link_score(link: dict) -> tuple[int, int, str]:
    url = link.get("url", "")
    title = link.get("title", "")
    if is_probably_direct_document_link(url):
        return (0, len(title), url)
    if is_generic_source_url(url):
        return (2, len(title), url)
    return (1, len(title), url)


def is_followable_page_link(url: str, text: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        return False
    if normalized_host(parsed) != normalized_host(base):
        return False

    nd = pravo_nd(url)
    if nd and nd != pravo_nd(base_url):
        return True

    path = parsed.path.lower()
    base_path = base.path.lower()
    suffix = Path(unquote(path)).suffix.lower()
    if suffix in {".pdf", ".rtf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"}:
        return True
    if "/uploaded/files/" in path:
        return True
    if re.search(r"/(?:ru/)?documents/\d+/?$", path):
        return True
    host = normalized_host(parsed)
    if host == "vsrf.ru" and re.search(r"/documents/(?:own|practice|thematics|reviews|arbitration)/\d+/?$", path):
        return True
    if host == "vsrf.ru" and base_path.startswith("/documents"):
        if re.fullmatch(r"/documents/(?:own|presidium-resolutions|reviews|statistics|international_practice|newsletters|arbitration)/?", path):
            return True
        if path == "/documents/own" and parsed.query:
            return True
    if host == "publication.pravo.gov.ru" and re.search(r"/(?:document|document/view)/\d{10,}", path):
        return True
    if host == "publication.pravo.gov.ru" and is_generic_source_url(base_url):
        if re.search(r"/(?:document|document/view)/\d{10,}", path) or path == "/file/pdf":
            return True

    legal_words = (
        "приказ", "постановление", "федеральный закон", "закон", "перечень",
        "положение", "устав", "регламент", "экспертиз", "эксперт",
    )
    return len(text) >= 40 and any(word in text.lower() for word in legal_words)


def extract_followable_links_from_html(content: bytes, base_url: str, content_type: str = "", http_encoding: str = "") -> list[dict]:
    html, _ = decode_html(content, content_type=content_type, http_encoding=http_encoding)
    links = []
    seen = set()

    if BeautifulSoup is None:
        for m in re.finditer(r"(?is)<a\b[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", html):
            href = m.group(1).strip()
            text = normalize_link_text(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))
            url = urljoin(base_url, href)
            key = canonical_link_key(url)
            if key not in seen and is_followable_page_link(url, text, base_url):
                links.append({"url": url, "title": text or extract_id_from_url(url) or "linked_document"})
                seen.add(key)
        return links

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        link_text = normalize_link_text(a.get_text(" ", strip=True))
        parent_text = link_text
        if not pravo_nd(url):
            parent = a.find_parent(["p", "li", "div"])
            if parent is not None:
                candidate_raw = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip()
                if link_text and link_text in candidate_raw and len(candidate_raw) <= 260:
                    parent_text = normalize_link_text(candidate_raw)
        title = parent_text or link_text or extract_id_from_url(url) or "linked_document"
        key = canonical_link_key(url)
        if key not in seen and is_followable_page_link(url, title, base_url):
            links.append({"url": url, "title": title})
            seen.add(key)

    return links


def extract_links_from_html_fragments(fragments: list[str], base_url: str) -> list[dict]:
    links = []
    seen = set()

    for fragment in fragments or []:
        if BeautifulSoup is not None:
            soup = BeautifulSoup(str(fragment), "html.parser")
            anchors = soup.find_all("a", href=True)
            for a in anchors:
                href = a.get("href", "").strip()
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                    continue
                url = urljoin(base_url, href)
                title = normalize_link_text(a.get_text(" ", strip=True)) or extract_id_from_url(url) or "linked_document"
                key = canonical_link_key(url)
                if key not in seen and is_followable_page_link(url, title, base_url):
                    links.append({"url": url, "title": title})
                    seen.add(key)
        else:
            for m in re.finditer(r"(?is)<a\b[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", str(fragment)):
                href = m.group(1).strip()
                url = urljoin(base_url, href)
                title = normalize_link_text(re.sub(r"(?s)<[^>]+>", " ", m.group(2))) or extract_id_from_url(url) or "linked_document"
                key = canonical_link_key(url)
                if key not in seen and is_followable_page_link(url, title, base_url):
                    links.append({"url": url, "title": title})
                    seen.add(key)

    return links


def vsrf_documents_ajax_url(url: str) -> str:
    parsed = urlparse(url)
    if normalized_host(parsed) != "vsrf.ru":
        return ""

    path = parsed.path.rstrip("/").lower()
    if not re.fullmatch(
        r"/documents/(?:own|presidium-resolutions|reviews|statistics|international_practice|newsletters|arbitration)",
        path,
    ):
        return ""

    params = parse_qs(parsed.query, keep_blank_values=True)
    params["init"] = ["y"]
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def extract_vsrf_ajax_document_links(
    session: requests.Session,
    base_url: str,
    args,
) -> list[dict]:
    ajax_url = vsrf_documents_ajax_url(base_url)
    if not ajax_url:
        return []

    try:
        response = session.get(
            ajax_url,
            timeout=(args.connect_timeout, args.read_timeout),
            headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return []

    pages = data.get("pages") if isinstance(data, dict) else {}
    before_pages = 0
    if isinstance(pages, dict):
        try:
            before_pages = int(pages.get("before") or 0)
        except (TypeError, ValueError):
            before_pages = 0

    if before_pages > 0:
        try:
            parsed = urlparse(ajax_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params["before"] = [str(before_pages)]
            full_ajax_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))
            full_response = session.get(
                full_ajax_url,
                timeout=(args.connect_timeout, args.read_timeout),
                headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
            )
            full_response.raise_for_status()
            full_data = full_response.json()
            if isinstance(full_data, dict) and len(full_data.get("list") or []) >= len(data.get("list") or []):
                data = full_data
        except Exception:
            pass

    if not isinstance(data, dict):
        return []

    return extract_links_from_html_fragments(data.get("list") or [], base_url)


def is_generic_source_url(url: str) -> bool:
    """True РґР»СЏ СЃСЃС‹Р»РѕРє РЅР° РѕР±С‰РёР№ РїРѕСЂС‚Р°Р»/РєР°С‚Р°Р»РѕРі, РіРґРµ РЅРµС‚ РєРѕРЅРєСЂРµС‚РЅРѕРіРѕ РґРѕРєСѓРјРµРЅС‚Р°."""
    parsed = urlparse(url)
    host = normalized_host(parsed)
    path = parsed.path.rstrip("/").lower()
    query = parsed.query.strip()

    if host == "protect.gost.ru" and path in {"/search", "/search.aspx"}:
        qs = parse_qs(parsed.query)
        if "search" in {key.lower() for key in qs}:
            return True
    if host == "publication.pravo.gov.ru" and path in {"/search", "/search/"}:
        return True
    if host == "vsrf.ru" and re.fullmatch(
        r"/documents/(?:own|presidium-resolutions|reviews|statistics|international_practice|newsletters|arbitration)/?",
        path,
    ):
        return True

    if query:
        return False

    generic_paths = {
        "pravo.gov.ru": {"", "/", "/"},
        "publication.pravo.gov.ru": {"", "/", "/"},
        "protect.gost.ru": {"", "/", "/", "/v.aspx"},
        "minstroyrf.gov.ru": {"", "/", "/docs"},
        "vsrf.ru": {"", "/", "/documents"},
        "sudexpert.ru": {"", "/", "/"},
    }
    return host in generic_paths and (path or "/") in generic_paths[host]


def is_allowed_generic_source_item(item: dict) -> bool:
    text = " ".join(
        str(item.get(key, ""))
        for key in ("folder", "title")
    ).lower()
    markers = (
        "контрольные_источники",
        "контрольные источники",
        "правила_актуализации",
        "правила актуализации",
        "классификаторы",
        "официальный раздел",
        "официальный интернет-портал",
        "официальные_материалы",
        "официальные материалы",
    )
    return any(marker in text for marker in markers)


def item_requires_link_target(item: dict, args) -> bool:
    if item.get("generic_source_requires_links") == "1":
        return True
    return (
        is_generic_source_url(item.get("original_url", ""))
        and not getattr(args, "allow_generic_pages", False)
        and not is_allowed_generic_source_item(item)
    )


def text_sidecar_path(path: Path, max_path: int = DEFAULT_MAX_PATH) -> Path:
    return fit_path_length(path.parent, path.stem + ".txt", max_path)


def normalize_extracted_text(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip() + ("\n" if lines else "")


def repair_wrapped_words(text: str) -> str:
    lines = [line.rstrip() for line in (text or "").splitlines()]
    repaired = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if repaired:
            prev = repaired[-1]
            prev_word = prev.rsplit(" ", 1)[-1]
            if (
                re.search(r"[A-Za-z\u0400-\u04FF]$", prev)
                and re.match(r"^[A-Za-z\u0400-\u04FF]", stripped)
                and len(prev_word) <= 4
            ):
                repaired[-1] = prev + stripped
                continue
        repaired.append(stripped)
    return "\n".join(repaired)


def extract_docx_text(path: Path) -> str:
    doc = Document(path)
    parts = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            cells = [normalize_extracted_text(cell.text).strip() for cell in row.cells]
            cells = [cell for cell in cells if cell]
            if cells:
                parts.append(" | ".join(cells))
    return normalize_extracted_text("\n".join(parts))


def extract_rtf_text(path: Path) -> str:
    raw = path.read_bytes()
    if striprtf_to_text is not None:
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                converted = striprtf_to_text(raw.decode(enc, errors="ignore"))
                converted = normalize_extracted_text(repair_wrapped_words(converted))
                if len(converted) > 200:
                    return converted
            except Exception:
                pass

    data = raw.decode("latin-1", errors="ignore")
    ignore_destinations = {
        "fonttbl", "colortbl", "stylesheet", "info", "generator", "themedata",
        "datastore", "listtable", "listoverridetable", "rsidtbl", "xmlnstbl",
        "latentstyles", "filetbl", "revtbl", "pnseclvl", "shppict", "pict",
        "object", "footer", "header", "footnote", "annotation", "fldinst",
    }
    special_chars = {
        "par": "\n",
        "line": "\n",
        "page": "\n",
        "tab": "\t",
        "emdash": "вЂ”",
        "endash": "вЂ“",
        "bullet": "вЂў",
        "lquote": "В«",
        "rquote": "В»",
        "ldblquote": "В«",
        "rdblquote": "В»",
    }

    out = []
    stack = []
    skip_depth = 0
    uc_skip = 1
    i = 0

    def read_control(pos: int) -> tuple[str, str, int, bool]:
        if pos >= len(data):
            return "", "", pos, False
        if not data[pos].isalpha():
            return data[pos], "", pos + 1, False
        start = pos
        while pos < len(data) and data[pos].isalpha():
            pos += 1
        word = data[start:pos]
        sign = ""
        if pos < len(data) and data[pos] in "+-":
            sign = data[pos]
            pos += 1
        num_start = pos
        while pos < len(data) and data[pos].isdigit():
            pos += 1
        arg = sign + data[num_start:pos]
        had_space = pos < len(data) and data[pos] == " "
        if had_space:
            pos += 1
        return word, arg, pos, had_space

    while i < len(data):
        ch = data[i]

        if ch == "{":
            j = i + 1
            destination = ""
            if j < len(data) and data[j] == "\\":
                j += 1
                if j < len(data) and data[j] == "*":
                    j += 1
                    if j < len(data) and data[j] == "\\":
                        j += 1
                destination, _, _, _ = read_control(j)
            stack.append(skip_depth)
            if skip_depth or destination in ignore_destinations:
                skip_depth += 1
            i += 1
            continue

        if ch == "}":
            if skip_depth:
                skip_depth -= 1
            if stack:
                stack.pop()
            i += 1
            continue

        if skip_depth:
            i += 1
            continue

        if ch == "\\":
            word, arg, new_i, _ = read_control(i + 1)
            if word == "'":
                hex_value = data[new_i:new_i + 2]
                if re.fullmatch(r"[0-9a-fA-F]{2}", hex_value):
                    out.append(bytes([int(hex_value, 16)]).decode("cp1251", errors="replace"))
                    i = new_i + 2
                else:
                    i = new_i
                continue
            if word == "u" and arg:
                code = int(arg)
                if code < 0:
                    code += 65536
                out.append(chr(code))
                i = new_i + uc_skip
                continue
            if word == "uc" and arg:
                uc_skip = max(0, int(arg))
                i = new_i
                continue
            if word in special_chars:
                out.append(special_chars[word])
            elif word in ("{", "}", "\\"):
                out.append(word)
            i = new_i
            continue

        out.append(ch)
        i += 1

    return normalize_extracted_text(repair_wrapped_words("".join(out)))


def xml_text(node) -> str:
    return "".join(node.itertext())


def extract_xlsx_text(path: Path) -> str:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows_out = []
    with zipfile.ZipFile(path) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall(".//a:si", ns):
                shared.append(xml_text(item))

        sheet_names = [name for name in zf.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
        for sheet_name in sorted(sheet_names):
            rows_out.append(f"[{Path(sheet_name).stem}]")
            root = ET.fromstring(zf.read(sheet_name))
            for row in root.findall(".//a:row", ns):
                values = []
                for cell in row.findall("a:c", ns):
                    value_node = cell.find("a:v", ns)
                    if value_node is None:
                        inline_node = cell.find("a:is", ns)
                        value = xml_text(inline_node) if inline_node is not None else ""
                    else:
                        value = value_node.text or ""
                        if cell.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                    if value.strip():
                        values.append(value.strip())
                if values:
                    rows_out.append(" | ".join(values))
    return normalize_extracted_text("\n".join(rows_out))


def extract_csv_text(path: Path) -> str:
    text, _ = decode_html(path.read_bytes(), content_type="text/csv")
    return normalize_extracted_text(text)


def extract_excel_with_pandas(path: Path) -> str:
    if pd is None:
        return ""
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
    parts = []
    for sheet_name, frame in sheets.items():
        parts.append(f"[{sheet_name}]")
        frame = frame.fillna("")
        for _, row in frame.iterrows():
            values = [str(value).strip() for value in row.tolist() if str(value).strip()]
            if values:
                parts.append(" | ".join(values))
    return normalize_extracted_text("\n".join(parts))


def extract_pdf_text(path: Path) -> str:
    reader_cls = None
    if pypdf is not None:
        reader_cls = pypdf.PdfReader
    elif PyPDF2 is not None:
        reader_cls = PyPDF2.PdfReader
    if reader_cls is None:
        return ""

    reader = reader_cls(str(path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as e:
            page_text = f"[Не удалось извлечь текст страницы {i}: {type(e).__name__}: {e}]"
        if page_text.strip():
            pages.append(f"[Страница {i}]\n{page_text}")
    return normalize_extracted_text("\n\n".join(pages))


def extract_text_for_saved_file(path: Path, file_type: str) -> tuple[str, str]:
    suffix = path.suffix.lower()
    try:
        if file_type == "WORD_DOC" and suffix == ".docx":
            return extract_docx_text(path), ""
        if file_type == "WORD_RTF" or suffix == ".rtf":
            return extract_rtf_text(path), ""
        if file_type == "EXCEL_TABLE" and suffix == ".xlsx":
            pandas_text = extract_excel_with_pandas(path)
            if pandas_text:
                return pandas_text, ""
            return extract_xlsx_text(path), ""
        if file_type == "EXCEL_TABLE" and suffix == ".xls":
            pandas_text = extract_excel_with_pandas(path)
            if pandas_text:
                return pandas_text, ""
            return "", "XLS СЃРѕС…СЂР°РЅС‘РЅ РєР°Рє РѕСЂРёРіРёРЅР°Р»; РґР»СЏ СЃС‚Р°СЂРѕРіРѕ .xls РЅСѓР¶РµРЅ xlrd/LibreOffice РёР»Рё РєРѕРЅРІРµСЂС‚Р°С†РёСЏ РІ XLSX."
        if file_type == "EXCEL_TABLE" and suffix == ".csv":
            return extract_csv_text(path), ""
        if file_type == "PDF":
            text = extract_pdf_text(path)
            if text:
                return text, ""
            return "", ""
        if file_type == "WORD_DOC" and suffix == ".doc":
            return "", "DOC СЃРѕС…СЂР°РЅС‘РЅ РєР°Рє РѕСЂРёРіРёРЅР°Р»; РґР»СЏ СЃС‚Р°СЂРѕРіРѕ .doc РЅСѓР¶РµРЅ LibreOffice/antiword РёР»Рё РєРѕРЅРІРµСЂС‚Р°С†РёСЏ РІ DOCX."
    except Exception as e:
        return "", f"РќРµ СѓРґР°Р»РѕСЃСЊ РёР·РІР»РµС‡СЊ С‚РµРєСЃС‚: {type(e).__name__}: {e}"
    return "", ""


def append_result_error(result: dict, message: str) -> None:
    if not message:
        return
    result["error"] = (result.get("error", "") + " | " if result.get("error") else "") + message


def maybe_write_text_sidecar(target_path: Path, file_type: str, args, result: dict) -> bool:
    text_path = text_sidecar_path(target_path, args.max_path)
    result["text_saved_to"] = ""
    if text_path.exists() and not args.overwrite_text:
        result["text_saved_to"] = str(text_path)
        return True

    extracted_text, extract_error = extract_text_for_saved_file(target_path, file_type)
    if extracted_text:
        write_text_atomic(text_path, extracted_text, encoding="utf-8-sig")
        result["text_saved_to"] = str(text_path)
    if extract_error:
        append_result_error(result, extract_error)
    return bool(extracted_text)


def replace_saved_file_with_text(target_path: Path, file_type: str, args, result: dict) -> bool:
    text_path = text_sidecar_path(target_path, args.max_path)
    result["text_saved_to"] = ""

    if text_path.exists() and not args.overwrite_text:
        result["saved_to"] = str(text_path)
        return True

    extracted_text, extract_error = extract_text_for_saved_file(target_path, file_type)
    if extracted_text:
        write_text_atomic(text_path, extracted_text, encoding="utf-8-sig")
        if target_path.exists() and target_path != text_path:
            try:
                target_path.unlink()
            except Exception as e:
                append_result_error(result, f"РќРµ СѓРґР°Р»РѕСЃСЊ СѓРґР°Р»РёС‚СЊ СЃС‹СЂРѕР№ С„Р°Р№Р» РїРѕСЃР»Рµ СЃРѕР·РґР°РЅРёСЏ TXT: {type(e).__name__}: {e}")
        result["saved_to"] = str(text_path)
        return True

    if extract_error:
        append_result_error(result, extract_error)
    return False


def linked_documents_folder_text(parent_folder: str, parent_path: Path, parent_num: str = "") -> str:
    num = safe_name(parent_num, 20) if parent_num else extract_id_from_url(parent_path.stem)
    folder_name = safe_name((num + "_вложения") if num else (parent_path.stem[:40] + "_вложения"), 80)
    return str(make_subpath(parent_folder) / folder_name)


def download_linked_documents(
    session: requests.Session,
    parent_item: dict,
    parent_path: Path,
    html_content: bytes,
    base_url: str,
    content_type: str,
    http_encoding: str,
    root_dir: Path,
    args,
) -> tuple[int, int, list[str]]:
    if not args.follow_page_links:
        return 0, 0, []

    links = extract_followable_links_from_html(
        html_content,
        base_url=base_url,
        content_type=content_type,
        http_encoding=http_encoding,
    )
    ajax_document_links = extract_vsrf_ajax_document_links(session, base_url, args)
    if ajax_document_links:
        links = ajax_document_links
    saved = 0
    errors = 0
    messages = []
    child_folder = linked_documents_folder_text(parent_item["folder"], parent_path, parent_item.get("num", ""))
    parent_depth = int(parent_item.get("link_depth", 0) or 0)
    max_link_depth = getattr(args, "max_link_depth", 2)
    chain_to_first_document = item_requires_link_target(parent_item, args)
    navigation_source = (
        chain_to_first_document
        or is_generic_source_url(parent_item.get("original_url", ""))
        or is_generic_source_url(base_url)
    )
    visited_keys = set(filter(None, str(parent_item.get("visited_url_keys", "")).split("\n")))
    for visited_url in (parent_item.get("original_url", ""), base_url):
        if visited_url:
            visited_keys.add(canonical_link_key(visited_url))
    if navigation_source:
        if not ajax_document_links:
            links = sorted(links, key=chain_link_score)
        max_chain_candidates = getattr(args, "max_chain_candidates", 0)
        if max_chain_candidates > 0:
            links = links[:max_chain_candidates]
    elif args.max_page_links > 0:
        links = [link for link in links if is_probably_direct_document_link(link["url"])]
        links = links[:args.max_page_links]
    else:
        links = [link for link in links if is_probably_direct_document_link(link["url"])]

    for idx, link in enumerate(links, start=1):
        link_key = canonical_link_key(link["url"])
        if navigation_source and link_key in visited_keys:
            continue
        child_depth = parent_depth + 1
        child_item = {
            "registry": parent_item.get("registry", ""),
            "table_no": parent_item.get("table_no", ""),
            "row_no": parent_item.get("row_no", ""),
            "num": f"{parent_item.get('num', '')}.{idx:02d}".strip("."),
            "title": link["title"],
            "folder": child_folder,
            "original_url": link["url"],
            "download_url": force_scheme(link["url"], "http"),
            "parent_url": parent_item.get("original_url", ""),
            "parent_saved_to": str(parent_path),
            "link_depth": str(child_depth),
            "no_follow_page_links": child_depth >= max_link_depth,
            "visited_url_keys": "\n".join(sorted(visited_keys | {link_key})),
        }
        child_result = download_one(session, child_item, root_dir, args)
        status = child_result.get("status", "")
        if status.startswith("OK") or status.startswith("SKIPPED_EXISTS"):
            child_linked_saved = int(child_result.get("linked_saved") or 0)
            saved += max(1, child_linked_saved)
        else:
            errors += 1
            messages.append(f"{link['url']} -> {status}: {child_result.get('error', '')}")
        if args.pause:
            time.sleep(args.pause)

    return saved, errors, messages

def controlled_get(session: requests.Session, url: str, timeout: tuple[int, int], max_redirects: int) -> requests.Response:
    """GET СЃ СЂСѓС‡РЅРѕР№ РѕР±СЂР°Р±РѕС‚РєРѕР№ СЂРµРґРёСЂРµРєС‚РѕРІ, С‡С‚РѕР±С‹ РїРµСЂРІС‹Рј СЂРµР°Р»СЊРЅРѕ РїСЂРѕР±РѕРІР°Р»СЃСЏ HTTP."""
    current = url
    seen = set()
    for _ in range(max_redirects + 1):
        if current in seen:
            raise requests.TooManyRedirects(f"Redirect loop: {current}")
        seen.add(current)

        response = session.get(current, stream=True, timeout=timeout, allow_redirects=False)

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            if not location:
                return response
            current = urljoin(current, location)
            continue

        return response

    raise requests.TooManyRedirects(f"Too many redirects for {url}")


def fetch_with_http_then_https(
    session: requests.Session,
    original_url: str,
    connect_timeout: int,
    read_timeout: int,
    retries: int,
    retry_pause: float,
    max_redirects: int,
) -> tuple[requests.Response, str, str]:
    """РџСЂРѕР±СѓРµС‚ HTTP, РµСЃР»Рё РЅРµ РїРѕР»СѓС‡РёР»РѕСЃСЊ вЂ” HTTPS. Р’РѕР·РІСЂР°С‰Р°РµС‚ response, successful_url, errors_text."""
    timeout = (connect_timeout, read_timeout)
    errors = []

    for cand in candidate_urls(original_url):
        for attempt in range(1, retries + 1):
            try:
                response = controlled_get(session, cand, timeout=timeout, max_redirects=max_redirects)
                # Р•СЃР»Рё HTTP-РѕС‚РІРµС‚ 4xx/5xx, СЃС‡РёС‚Р°РµРј РїРѕРїС‹С‚РєСѓ РЅРµСѓРґР°С‡РЅРѕР№ Рё РїСЂРѕР±СѓРµРј РґР°Р»СЊС€Рµ.
                response.raise_for_status()
                return response, cand, " | ".join(errors)
            except Exception as e:
                errors.append(f"{cand} attempt {attempt}/{retries}: {type(e).__name__}: {e}")
                try:
                    # Р—Р°РєСЂС‹С‚СЊ stream-response, РµСЃР»Рё РѕРЅ СѓСЃРїРµР» РѕС‚РєСЂС‹С‚СЊСЃСЏ.
                    response.close()  # noqa: F821
                except Exception:
                    pass
                if attempt < retries:
                    time.sleep(retry_pause)

    raise RuntimeError("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРєР°С‡Р°С‚СЊ РЅРё РїРѕ HTTP, РЅРё РїРѕ HTTPS. " + " | ".join(errors[-6:]))


def write_binary_atomic(target_path: Path, response: requests.Response, first_chunk: bytes, iterator) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    try:
        with open(tmp_path, "wb") as f:
            if first_chunk:
                f.write(first_chunk)
            for chunk in iterator:
                if chunk:
                    f.write(chunk)
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def write_text_atomic(target_path: Path, text: str, encoding: str = "utf-8-sig") -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    try:
        tmp_path.write_text(text, encoding=encoding)
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def validate_saved_file(path: Path, file_type: str) -> None:
    if file_type != "PDF":
        return
    with open(path, "rb") as f:
        header = f.read(5)
    if header != b"%PDF-":
        raise RuntimeError(f"РЎРєР°С‡Р°РЅРЅС‹Р№ С„Р°Р№Р» РЅРµ РїРѕС…РѕР¶ РЅР° PDF: {path}")
    if pypdf is not None:
        try:
            reader = pypdf.PdfReader(str(path))
            if len(reader.pages) == 0:
                raise RuntimeError("PDF РЅРµ СЃРѕРґРµСЂР¶РёС‚ СЃС‚СЂР°РЅРёС†")
        except Exception as e:
            raise RuntimeError(f"РЎРєР°С‡Р°РЅРЅС‹Р№ PDF РїРѕРІСЂРµР¶РґС‘РЅ РёР»Рё РЅРµ С‡РёС‚Р°РµС‚СЃСЏ: {type(e).__name__}: {e}")


def download_one(session: requests.Session, item: dict, root_dir: Path, args) -> dict:
    target_dir = root_dir / make_subpath(item["folder"])
    result = dict(item)
    result.update({
        "status": "",
        "saved_to": "",
        "text_saved_to": "",
        "file_type": "",
        "content_type": "",
        "encoding": "",
        "attempted_url": "",
        "linked_found": "",
        "linked_saved": "",
        "linked_errors": "",
        "error": "",
    })

    if (
        is_generic_source_url(item["original_url"])
        and not args.allow_generic_pages
        and not is_allowed_generic_source_item(item)
    ):
        result["generic_source_requires_links"] = "1"

    if args.dry_run:
        result["status"] = "DRY_RUN"
        result["saved_to"] = str(target_dir)
        return result

    try:
        response, successful_url, previous_errors = fetch_with_http_then_https(
            session=session,
            original_url=preferred_content_url(item["original_url"]),
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            retries=args.retries,
            retry_pause=args.retry_pause,
            max_redirects=args.max_redirects,
        )
        result["attempted_url"] = successful_url
        content_type = response.headers.get("Content-Type", "")
        result["content_type"] = content_type

        iterator = response.iter_content(CHUNK_SIZE)
        first_chunk = next(iterator, b"")

        file_type, ext = classify_response(
            url=response.url or successful_url,
            content_type=content_type,
            first_chunk=first_chunk,
            content_disposition=response.headers.get("Content-Disposition", ""),
        )
        result["file_type"] = file_type

        filename = make_filename(item["num"], item["title"], item["original_url"], ext)
        target_path = fit_path_length(target_dir, filename, args.max_path)
        result["saved_to"] = str(target_path)
        readable_text_mode = (
            args.readable_text
            and file_type not in ("HTML_TEXT", "TEXT")
            and not item.get("save_original_format")
        )
        readable_text_path = text_sidecar_path(target_path, args.max_path) if readable_text_mode else None

        # Р“Р»Р°РІРЅРѕРµ С‚СЂРµР±РѕРІР°РЅРёРµ: РµСЃР»Рё С„Р°Р№Р» СЃ С‚Р°РєРёРј РёРјРµРЅРµРј СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓРµС‚ РІ СЌС‚РѕР№ РїР°РїРєРµ вЂ” РЅРµ СЃРєР°С‡РёРІР°С‚СЊ РЅРѕРІС‹Р№, Р° СЃРєРёРїР°С‚СЊ.
        # РСЃРєР»СЋС‡РµРЅРёРµ С‚РѕР»СЊРєРѕ РґР»СЏ .txt: РµСЃР»Рё РѕРЅ СѓР¶Рµ Р±РёС‚С‹Р№ РїРѕ РєРѕРґРёСЂРѕРІРєРµ РР›Р Р·Р°РїРёСЃР°РЅ Р±РµР· UTF-8 BOM,
        # РїРµСЂРµР·Р°РїРёСЃС‹РІР°РµРј РµРіРѕ С‡РёС‚Р°РµРјРѕР№ РІРµСЂСЃРёРµР№ РІ UTF-8-SIG, С‡С‚РѕР±С‹ Р‘Р»РѕРєРЅРѕС‚/Windows РЅРµ РѕС‚РєСЂС‹РІР°Р»Рё РµРіРѕ РєР°Рє KOI8-R/ANSI.
        repair_existing_text = False
        skip_existing_text_write = False
        if readable_text_path and readable_text_path.exists() and not args.overwrite_text:
            result["saved_to"] = str(readable_text_path)
            result["status"] = "SKIPPED_EXISTS"
            response.close()
            return result

        if target_path.exists():
            if file_type in ("HTML_TEXT", "TEXT") and not args.no_repair_bad_text:
                try:
                    if args.overwrite_text:
                        repair_existing_text = True
                    else:
                        old_bytes = target_path.read_bytes()
                        old_has_utf8_bom = old_bytes.startswith(b"\xef\xbb\xbf")
                        old_text = old_bytes.decode("utf-8-sig", errors="replace")
                        if looks_like_mojibake(old_text) or looks_like_pravo_shell_text(old_text) or not old_has_utf8_bom:
                            repair_existing_text = True
                        else:
                            if file_type == "HTML_TEXT" and args.follow_page_links and not item.get("no_follow_page_links"):
                                skip_existing_text_write = True
                            else:
                                result["status"] = "SKIPPED_EXISTS"
                                response.close()
                                return result
                except Exception:
                    # Р•СЃР»Рё СЃС‚Р°СЂС‹Р№ txt РґР°Р¶Рµ РїСЂРѕС‡РёС‚Р°С‚СЊ РЅРµ СѓРґР°Р»РѕСЃСЊ, Р»СѓС‡С€Рµ РїРµСЂРµР·Р°РїРёСЃР°С‚СЊ РµРіРѕ РЅРѕСЂРјР°Р»СЊРЅРѕР№ РІРµСЂСЃРёРµР№.
                    repair_existing_text = True
            else:
                if readable_text_mode:
                    wrote_text = replace_saved_file_with_text(target_path, file_type, args, result)
                    result["status"] = "SKIPPED_EXISTS_TEXT_EXTRACTED" if wrote_text else "SKIPPED_EXISTS"
                    response.close()
                    return result
                if args.extract_text and file_type not in ("HTML_TEXT", "TEXT"):
                    wrote_text = maybe_write_text_sidecar(target_path, file_type, args, result)
                    result["status"] = "SKIPPED_EXISTS_TEXT_EXTRACTED" if wrote_text else "SKIPPED_EXISTS"
                    response.close()
                    return result
                result["status"] = "SKIPPED_EXISTS"
                response.close()
                return result

        if file_type == "HTML_TEXT":
            # Р”Р»СЏ HTML РЅР°РґРѕ РґРѕС‡РёС‚Р°С‚СЊ РІРµСЃСЊ РѕС‚РІРµС‚, С‡С‚РѕР±С‹ РЅРѕСЂРјР°Р»СЊРЅРѕ РїРѕРґРѕР±СЂР°С‚СЊ РєРѕРґРёСЂРѕРІРєСѓ Рё РІС‹С‚Р°С‰РёС‚СЊ С‚РµРєСЃС‚.
            content = first_chunk + b"".join(chunk for chunk in iterator if chunk)
            text, enc = html_to_readable_text(content, content_type=content_type, http_encoding=response.encoding or "")
            result["encoding"] = enc
            linked = []
            if args.follow_page_links and not item.get("no_follow_page_links"):
                linked = extract_followable_links_from_html(
                    content,
                    base_url=response.url or successful_url,
                    content_type=content_type,
                    http_encoding=response.encoding or "",
                )
                ajax_document_links = extract_vsrf_ajax_document_links(session, response.url or successful_url, args)
                if ajax_document_links:
                    linked = ajax_document_links
                result["linked_found"] = str(len(linked))
            if skip_existing_text_write:
                result["status"] = "SKIPPED_EXISTS"
            else:
                write_text_atomic(target_path, text, encoding="utf-8-sig")
                result["status"] = "OK_HTML_TEXT_REPAIRED" if repair_existing_text else "OK_HTML_TEXT"
            if args.follow_page_links and not item.get("no_follow_page_links"):
                linked_saved, linked_errors, linked_messages = download_linked_documents(
                    session=session,
                    parent_item=item,
                    parent_path=target_path,
                    html_content=content,
                    base_url=response.url or successful_url,
                    content_type=content_type,
                    http_encoding=response.encoding or "",
                    root_dir=root_dir,
                    args=args,
                )
                result["linked_saved"] = str(linked_saved)
                result["linked_errors"] = str(linked_errors)
                if linked_errors:
                    append_result_error(result, "РћС€РёР±РєРё РІР»РѕР¶РµРЅРЅС‹С… СЃСЃС‹Р»РѕРє: " + " | ".join(linked_messages[:5]))
                if skip_existing_text_write and linked_saved:
                    result["status"] = "SKIPPED_EXISTS_LINKS_PROCESSED"
                if is_generic_source_url(item.get("original_url", "")) and linked_saved:
                    try:
                        target_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    result["saved_to"] = str(target_path.parent)
                    if not skip_existing_text_write:
                        result["status"] = "OK_LINKED_DOCUMENTS"
            if result.get("generic_source_requires_links") == "1":
                linked_saved_count = int(result.get("linked_saved") or 0)
                if linked_saved_count <= 0:
                    result["status"] = "ERROR_NO_LINKED_DOCUMENTS"
                    append_result_error(result, "РЎСЃС‹Р»РєР° РІРµРґС‘С‚ РЅР° СЂР°Р·РґРµР»/РєР°С‚Р°Р»РѕРі; РєРѕРЅРєСЂРµС‚РЅС‹Рµ РґРѕРєСѓРјРµРЅС‚С‹ РїРѕ СЃСЃС‹Р»РєР°Рј РІРЅСѓС‚СЂРё СЃС‚СЂР°РЅРёС†С‹ РЅРµ РЅР°Р№РґРµРЅС‹ РёР»Рё РЅРµ СЃРєР°С‡Р°РЅС‹.")
        elif file_type == "TEXT":
            content = first_chunk + b"".join(chunk for chunk in iterator if chunk)
            text, enc = decode_html(content, content_type=content_type, http_encoding=response.encoding or "")
            result["encoding"] = enc
            write_text_atomic(target_path, text, encoding="utf-8-sig")
            result["status"] = "OK_TEXT_REPAIRED" if repair_existing_text else "OK_TEXT"
        else:
            binary_errors = []
            for body_attempt in range(1, args.retries + 1):
                try:
                    if body_attempt > 1:
                        try:
                            response.close()
                        except Exception:
                            pass
                        response, successful_url, _ = fetch_with_http_then_https(
                            session=session,
                            original_url=preferred_content_url(item["original_url"]),
                            connect_timeout=args.connect_timeout,
                            read_timeout=args.read_timeout,
                            retries=1,
                            retry_pause=0,
                            max_redirects=args.max_redirects,
                        )
                        result["attempted_url"] = successful_url
                        content_type = response.headers.get("Content-Type", "")
                        result["content_type"] = content_type
                        iterator = response.iter_content(CHUNK_SIZE)
                        first_chunk = next(iterator, b"")
                        retry_file_type, retry_ext = classify_response(
                            url=response.url or successful_url,
                            content_type=content_type,
                            first_chunk=first_chunk,
                            content_disposition=response.headers.get("Content-Disposition", ""),
                        )
                        if retry_file_type != file_type or retry_ext != ext:
                            raise RuntimeError(
                                f"РџСЂРё РїРѕРІС‚РѕСЂРЅРѕР№ Р·Р°РіСЂСѓР·РєРµ РёР·РјРµРЅРёР»СЃСЏ С‚РёРї С„Р°Р№Р»Р°: {file_type}/{ext} -> {retry_file_type}/{retry_ext}"
                            )
                    write_binary_atomic(target_path, response, first_chunk, iterator)
                    validate_saved_file(target_path, file_type)
                    break
                except Exception as e:
                    binary_errors.append(f"attempt {body_attempt}/{args.retries}: {type(e).__name__}: {e}")
                    try:
                        response.close()
                    except Exception:
                        pass
                    if body_attempt >= args.retries:
                        raise RuntimeError("РќРµ СѓРґР°Р»РѕСЃСЊ РїРѕР»РЅРѕСЃС‚СЊСЋ СЃРєР°С‡Р°С‚СЊ Рё РїСЂРѕРІРµСЂРёС‚СЊ С„Р°Р№Р». " + " | ".join(binary_errors[-3:]))
                    if args.retry_pause:
                        time.sleep(args.retry_pause)
            result["status"] = "OK_" + file_type

            if readable_text_mode:
                if replace_saved_file_with_text(target_path, file_type, args, result):
                    result["status"] = "OK_" + file_type + "_TEXT"
            elif args.extract_text:
                maybe_write_text_sidecar(target_path, file_type, args, result)

        response.close()
        return result

    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = f"{type(e).__name__}: {e}"
        return result


def save_error_log(error_rows: list[dict], fieldnames: list[str], log_path: Path) -> None:
    with open(log_path, "w", newline="", encoding="utf-8-sig") as log_file:
        writer = csv.DictWriter(log_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in error_rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def normalize_cli_args(argv: list[str]) -> list[str]:
    normalized = []
    for arg in argv:
        if arg.startswith("--") and arg.endswith(("\\", "/")):
            normalized.append(arg.rstrip("\\/"))
        else:
            normalized.append(arg)
    return normalized


def main():
    parser = argparse.ArgumentParser(
        description="РЎРєР°С‡Р°С‚СЊ РґРѕРєСѓРјРµРЅС‚С‹/СЃС‚СЂР°РЅРёС†С‹ РїРѕ РіРёРїРµСЂСЃСЃС‹Р»РєР°Рј РёР· DOCX-СЂРµРµСЃС‚СЂР° Рё СЂР°Р·Р»РѕР¶РёС‚СЊ РїРѕ РїР°РїРєР°Рј РёР· РєРѕР»РѕРЅРєРё 'РџР°РїРєР°'. v14: pravo.gov.ru docbody -> doc_itself РґР»СЏ РїРѕР»РЅРѕРіРѕ С‚РµРєСЃС‚Р°."
    )
    parser.add_argument("docx", help="РџСѓС‚СЊ Рє docx-С„Р°Р№Р»Сѓ СЂРµРµСЃС‚СЂР° РёР»Рё Рє РїР°РїРєРµ СЃ СЂРµРµСЃС‚СЂР°РјРё .docx")
    parser.add_argument("root", help="РљРѕСЂРЅРµРІР°СЏ РїР°РїРєР°, РЅР°РїСЂРёРјРµСЂ: C:\\Users\\Varvara\\Desktop\\1Р Р°Р±РѕС‡РёРµ РјР°С‚РµСЂРёР°Р»С‹")
    parser.add_argument("--dry-run", action="store_true", help="РўРѕР»СЊРєРѕ РїРѕРєР°Р·Р°С‚СЊ, С‡С‚Рѕ Р±СѓРґРµС‚ РѕР±СЂР°Р±Р°С‚С‹РІР°С‚СЊСЃСЏ, Р±РµР· Р·Р°РіСЂСѓР·РєРё")
    parser.add_argument("--connect-timeout", type=int, default=60, help="РўР°Р№РјР°СѓС‚ РїРѕРґРєР»СЋС‡РµРЅРёСЏ РІ СЃРµРєСѓРЅРґР°С…")
    parser.add_argument("--read-timeout", type=int, default=90, help="РўР°Р№РјР°СѓС‚ С‡С‚РµРЅРёСЏ РІ СЃРµРєСѓРЅРґР°С…")
    parser.add_argument("--retries", type=int, default=3, help="РљРѕР»РёС‡РµСЃС‚РІРѕ РїРѕРїС‹С‚РѕРє РґР»СЏ РєР°Р¶РґРѕРіРѕ РІР°СЂРёР°РЅС‚Р° URL")
    parser.add_argument("--retry-pause", type=float, default=2.0, help="РџР°СѓР·Р° РјРµР¶РґСѓ РїРѕРІС‚РѕСЂР°РјРё")
    parser.add_argument("--pause", type=float, default=0.2, help="РџР°СѓР·Р° РјРµР¶РґСѓ СЂР°Р·РЅС‹РјРё СЃСЃС‹Р»РєР°РјРё")
    parser.add_argument("--max-redirects", type=int, default=8, help="РњР°РєСЃРёРјСѓРј СЂРµРґРёСЂРµРєС‚РѕРІ")
    parser.add_argument("--max-path", type=int, default=DEFAULT_MAX_PATH, help="РњР°РєСЃРёРјР°Р»СЊРЅР°СЏ РґР»РёРЅР° РїРѕР»РЅРѕРіРѕ РїСѓС‚Рё Windows")
    parser.add_argument("--error-log", default="download_errors.csv", help="CSV СЃ РѕС€РёР±РєР°РјРё. РЎРѕР·РґР°С‘С‚СЃСЏ С‚РѕР»СЊРєРѕ РµСЃР»Рё Р±С‹Р»Рё РѕС€РёР±РєРё")
    parser.add_argument("--no-error-log", action="store_true", help="РќРµ СЃРѕС…СЂР°РЅСЏС‚СЊ CSV СЃ РѕС€РёР±РєР°РјРё, С‚РѕР»СЊРєРѕ РїРµС‡Р°С‚Р°С‚СЊ РІ РєРѕРЅСЃРѕР»СЊ")
    parser.add_argument("--no-repair-bad-text", action="store_true", help="РќРµ РёСЃРїСЂР°РІР»СЏС‚СЊ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РёРµ .txt СЃ СЏРІРЅС‹РјРё РєСЂР°РєРѕР·СЏР±СЂР°РјРё РєРѕРґРёСЂРѕРІРєРё")
    parser.add_argument("--save-original-format", dest="readable_text", action="store_false", help="РЎРѕС…СЂР°РЅСЏС‚СЊ РёСЃС…РѕРґРЅС‹Р№ RTF/DOCX/PDF/XLS Р±РµР· РїСЂРµРѕР±СЂР°Р·РѕРІР°РЅРёСЏ РІ С‡РёС‚Р°РµРјС‹Р№ TXT")
    parser.add_argument("--extract-text", action="store_true", help="Р”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕ СЃРѕР·РґР°РІР°С‚СЊ СЂСЏРґРѕРј .txt СЃ РёР·РІР»РµС‡С‘РЅРЅС‹Рј С‚РµРєСЃС‚РѕРј, РµСЃР»Рё РІРєР»СЋС‡С‘РЅ --save-original-format")
    parser.add_argument("--overwrite-text", action="store_true", help="РџРµСЂРµР·Р°РїРёСЃС‹РІР°С‚СЊ СѓР¶Рµ СЃСѓС‰РµСЃС‚РІСѓСЋС‰РёРµ .txt-РІС‹Р¶РёРјРєРё СЂСЏРґРѕРј СЃ Р±РёРЅР°СЂРЅС‹РјРё С„Р°Р№Р»Р°РјРё")
    parser.add_argument("--ocr-engine", choices=("tesseract", "easyocr", "auto", "none"), default="none", help="Совместимость со старой командой; OCR сейчас отключен")
    parser.add_argument("--allow-generic-pages", action="store_true", help="Р Р°Р·СЂРµС€РёС‚СЊ СЃРѕС…СЂР°РЅСЏС‚СЊ РѕР±С‰РёРµ СЃС‚СЂР°РЅРёС†С‹ РїРѕСЂС‚Р°Р»РѕРІ, РґР°Р¶Рµ РµСЃР»Рё СЃСЃС‹Р»РєР° РЅРµ РІРµРґС‘С‚ РЅР° РєРѕРЅРєСЂРµС‚РЅС‹Р№ РґРѕРєСѓРјРµРЅС‚")
    parser.add_argument("--no-follow-page-links", dest="follow_page_links", action="store_false", help="РќРµ СЃРєР°С‡РёРІР°С‚СЊ РґРѕРєСѓРјРµРЅС‚С‹ РїРѕ СЃСЃС‹Р»РєР°Рј РІРЅСѓС‚СЂРё HTML-СЃС‚СЂР°РЅРёС†")
    parser.add_argument("--max-page-links", type=int, default=0, help="РњР°РєСЃРёРјСѓРј РІР»РѕР¶РµРЅРЅС‹С… СЃСЃС‹Р»РѕРє РґР»СЏ РѕРґРЅРѕР№ HTML-СЃС‚СЂР°РЅРёС†С‹; 0 = РІСЃРµ СѓРЅРёРєР°Р»СЊРЅС‹Рµ РґРѕРєСѓРјРµРЅС‚С‹")
    parser.add_argument("--max-link-depth", type=int, default=2, help="РњР°РєСЃРёРјСѓРј РІРЅСѓС‚СЂРµРЅРЅРёС… РїРµСЂРµС…РѕРґРѕРІ РїРѕ HTML-СЂР°Р·РґРµР»Р°Рј: 1 = С‚РѕР»СЊРєРѕ РїСЂСЏРјС‹Рµ СЃСЃС‹Р»РєРё, 2 = СЂР°Р·РґРµР» -> РґРѕРєСѓРјРµРЅС‚")
    parser.add_argument("--max-chain-candidates", type=int, default=0, help="Сколько внутренних ссылок-кандидатов пробовать на каждом уровне HTML-раздела; 0 = все")
    parser.add_argument("--only-nums", default="", help="Обработать только номера строк реестра через запятую, например: 27,114,115,122")
    parser.add_argument("--start-index", type=int, default=1, help="РЎ РєР°РєРѕР№ РЅР°Р№РґРµРЅРЅРѕР№ СЃСЃС‹Р»РєРё РЅР°С‡Р°С‚СЊ РѕР±СЂР°Р±РѕС‚РєСѓ")
    parser.add_argument("--limit", type=int, default=0, help="РћР±СЂР°Р±РѕС‚Р°С‚СЊ РЅРµ Р±РѕР»СЊС€Рµ N СЃСЃС‹Р»РѕРє; 0 = Р±РµР· РѕРіСЂР°РЅРёС‡РµРЅРёСЏ")
    parser.set_defaults(follow_page_links=True, readable_text=False)
    args = parser.parse_args(normalize_cli_args(sys.argv[1:]))

    docx_path = Path(args.docx)
    root_dir = Path(args.root)
    error_log_path = Path(args.error_log)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,application/msword,application/vnd.ms-excel,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        "Connection": "close",
    })

    if docx_path.is_dir():
        registry_paths = sorted(docx_path.glob("*.docx"))
    else:
        registry_paths = [docx_path]

    rows = []
    for registry_path in registry_paths:
        registry_rows = list(iter_registry_rows(registry_path))
        for row in registry_rows:
            row["registry"] = registry_path.name
        rows.extend(registry_rows)

    total_rows = len(rows)
    if args.only_nums.strip():
        wanted_nums = {part.strip() for part in args.only_nums.split(",") if part.strip()}
        rows = [row for row in rows if str(row.get("num", "")).strip() in wanted_nums]
    if args.start_index > 1 or args.limit:
        start = max(args.start_index - 1, 0)
        end = start + args.limit if args.limit else None
        rows = rows[start:end]

    if len(registry_paths) == 1:
        print(f"Найдено ссылок: {total_rows}")
    else:
        print(f"Найдено реестров: {len(registry_paths)}")
        print(f"Найдено ссылок всего: {total_rows}")
    if len(rows) != total_rows:
        print(f"Будет обработано в этом запуске: {len(rows)}")

    fieldnames = [
        "registry", "table_no", "row_no", "num", "title", "folder",
        "original_url", "download_url", "attempted_url", "status", "file_type",
        "saved_to", "text_saved_to", "linked_found", "linked_saved", "linked_errors",
        "content_type", "encoding", "error"
    ]

    stats = {}
    ok = skipped = errors = 0
    error_rows = []

    for i, item in enumerate(rows, start=1):
        registry_prefix = f"{item.get('registry', docx_path.name)} | "
        print(f"[{i}/{len(rows)}] {registry_prefix}{item['num']} -> {item['download_url']}")
        result = download_one(session, item, root_dir, args)

        status = result.get("status", "")
        stats[status] = stats.get(status, 0) + 1

        if status.startswith("OK"):
            ok += 1
            print(f"  OK {result.get('file_type', '')}: {result.get('saved_to', '')}" + (f" | encoding={result.get('encoding')}" if result.get('encoding') else ""))
            if result.get("text_saved_to"):
                print(f"  TXT: {result.get('text_saved_to')}")
            if result.get("linked_found"):
                print(f"  LINKS: found={result.get('linked_found')} saved/skipped={result.get('linked_saved')} errors={result.get('linked_errors')}")
            if result.get("error"):
                print(f"  WARN: {result.get('error')}")
        elif status.startswith("SKIPPED"):
            skipped += 1
            if status == "SKIPPED_EXISTS":
                print(f"  SKIP: уже есть -> {result.get('saved_to', '')}")
            else:
                print(f"  SKIP: {status}" + (f" | {result.get('error')}" if result.get("error") else ""))
            if result.get("linked_found"):
                print(f"  LINKS: found={result.get('linked_found')} saved/skipped={result.get('linked_saved')} errors={result.get('linked_errors')}")
        elif status == "DRY_RUN":
            print(f"  DRY: папка {result.get('saved_to', '')}")
        else:
            errors += 1
            error_rows.append(result)
            print(f"  ERROR: {result.get('error', '')}")

        if args.pause and not args.dry_run:
            time.sleep(args.pause)

    print("\nГотово.")
    print(f"Сохранено новых файлов/страниц: {ok}")
    print(f"Пропущено существующих/дублей: {skipped}")
    print(f"Ошибок: {errors}")

    if stats:
        print("По статусам:")
        for k, v in sorted(stats.items()):
            print(f"  {k}: {v}")

    if errors:
        if args.no_error_log:
            print("\nCSV-лог ошибок не сохранён из-за --no-error-log.")
        else:
            save_error_log(error_rows, fieldnames, error_log_path)
            print(f"\nЛог ошибок сохранён: {error_log_path.resolve()}")
    else:
        # Р•СЃР»Рё СЃС‚Р°СЂС‹Р№ error-log Р»РµР¶РёС‚ РѕС‚ РїСЂРѕС€Р»РѕРіРѕ Р·Р°РїСѓСЃРєР°, СѓРґР°Р»РёРј, С‡С‚РѕР±С‹ РЅРµ РїСѓС‚Р°Р».
        if error_log_path.exists() and not args.no_error_log:
            try:
                error_log_path.unlink()
            except Exception:
                pass
        print("\nОшибок нет — лог-файл не создавался.")


if __name__ == "__main__":
    main()
