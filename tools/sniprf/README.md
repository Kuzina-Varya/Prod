# Поисковая тулза sniprf.ru

`sniprf_search_tool.py` - локальный инструмент для поиска строительных
нормативных документов на сайте `sniprf.ru`.

Скрипт рассчитан на будущую интеграцию с LLM: на вход передаётся обычный
текстовый запрос, скрипт ищет документы только на `sniprf.ru` /
`www.sniprf.ru`, ранжирует найденные кандидаты и возвращает результат в JSON.

Извлечение чистого текста из HTML вынесено в отдельный модуль проекта:

```text
html_legal_text_parser.extract_main_text_from_html
```

Эта тулза отвечает за поиск, фильтрацию ссылок, оценку релевантности и
диагностику. Она не должна искать по внешним сайтам и не должна заменять собой
HTML-парсер.

## Файлы

- `sniprf_search_tool.py` - основной поисковый скрипт.
- `sniprf_last_result.json` - последний результат ручного тестового запуска.
- `requirements.txt` - зависимости для этой тулзы.
- `../../html_legal_text_parser.py` - общий парсер, который очищает уже
  загруженный HTML и достаёт основной текст документа.

## Установка

Запускать из корня репозитория:

```powershell
pip install -r requirements.txt
pip install -r tools\sniprf\requirements.txt
```

Если используется локальное виртуальное окружение, сначала активируйте его:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Запуск ручных тестов

Из корня репозитория:

```powershell
python .\tools\sniprf\sniprf_search_tool.py
```

Команда запускает тестовый блок в конце файла и сохраняет последний результат
сюда:

```text
tools\sniprf\sniprf_last_result.json
```

## Вызов из Python

Такой вариант нужен для будущей LLM-интеграции:

```python
import json

from tools.sniprf.sniprf_search_tool import search_documents_for_construction

raw_result = search_documents_for_construction(
    search_query="СП 22.13330 основания зданий",
    iteration_count=1,
    max_pages=1,
    limit=5,
    min_score=0.15,
    include_text=True,
    include_full_text=False,
)

result = json.loads(raw_result)
documents = result["documents"]
```

Однострочная команда для PowerShell:

```powershell
python -c "from tools.sniprf.sniprf_search_tool import search_documents_for_construction; print(search_documents_for_construction('СП 22.13330 основания зданий', max_pages=1, limit=5, include_text=False))"
```

## Главная функция

```python
search_documents_for_construction(
    search_query: str,
    iteration_count: int = 1,
    max_pages: int = 3,
    limit: int = 5,
    min_score: float = 0.15,
    include_text: bool = True,
    include_full_text: bool = False,
) -> str
```

Параметры:

- `search_query` - обычный текстовый запрос от пользователя или LLM.
- `iteration_count` - номер страницы результатов, начиная с `1`.
- `max_pages` - сколько страниц внутреннего поиска `sniprf.ru` проверять.
- `limit` - сколько документов вернуть за один вызов.
- `min_score` - минимальная оценка релевантности кандидата.
- `include_text` - добавлять `text_preview` через `html_legal_text_parser`.
- `include_full_text` - добавлять полный извлечённый текст HTML-документа.

Функция возвращает JSON-строку, а не Python-словарь.

## Как работает поиск

Скрипт совмещает три способа поиска.

1. Прямые URL для СП.
   Если в запросе есть номер документа, например `СП 22.13330`, скрипт сразу
   пробует вероятные страницы вида `/sp22-13330-2016` и `/sp22-13330-2011`.

2. Парсинг каталогов.
   Скрипт открывает известные страницы-каталоги на `sniprf.ru` и достаёт из них
   ссылки на документы. Сами страницы-каталоги в итоговую выдачу не попадают.

3. Внутренний поиск сайта.
   Скрипт строит URL внутреннего поиска `sniprf.ru` и парсит найденные страницы.
   Google, Yandex и другие внешние поисковики не используются.

## Формат результата JSON

Важные поля верхнего уровня:

- `status` - `"success"` или `"error"` для пустого запроса.
- `documents` - документы, отранжированные для текущей итерации.
- `total_candidates_found` - сколько кандидатов найдено до пагинации.
- `documents_returned` - сколько документов вернул текущий вызов.
- `has_more_documents` - есть ли ещё результаты для следующей итерации.
- `pages_checked` - сколько страниц-источников было проверено.
- `page_errors` - количество неожиданных ошибок.
- `page_skipped` - количество ожидаемо пропущенных страниц.
- `page_reports` - подробная диагностика по каждой проверенной странице.

Важные поля документа:

- `title` - название документа или лучший текст ссылки.
- `url` - ссылка на документ на `sniprf.ru`.
- `document_type` - примерный тип: `СП`, `ГОСТ`, `HTML`, `PDF`.
- `score` - оценка релевантности от `0.0` до `1.0`.
- `matched_keywords` - найденные слова из запроса и словаря синонимов.
- `exact_identifier_matches` - точные совпадения номера СП из запроса.
- `source_mode` - источник: `direct_sp_url` или `catalog_or_search`.
- `text_preview` - очищенный фрагмент текста, если `include_text=True`.

## Диагностика и 404

Некоторые 404 являются нормальными. Например, скрипт может попробовать:

```text
http://sniprf.ru/sp2025
http://sniprf.ru/sp2024
```

Если такой страницы-каталога на сайте нет, это не считается ошибкой поиска.
В отчёте будет:

```json
{
  "status": "skipped",
  "severity": "info",
  "ignored": true,
  "error_kind": "expected_missing_catalog_page"
}
```

Такие случаи можно игнорировать, если `documents_returned > 0`.

Реальной проблемой считается только отчёт вида:

```json
{
  "status": "error",
  "severity": "error",
  "ignored": false
}
```

## Как использовать результат в LLM

- Передавать в контекст LLM массив `documents`.
- Если пользователь просит конкретный номер СП, отдавать приоритет документам с
  непустым `exact_identifier_matches`.
- `page_skipped` не является ошибкой.
- Если `documents_returned == 0`, лучше переформулировать или сузить запрос.
- Для другого сайта нужно делать отдельную тулзу, эту оставляем только под
  `sniprf.ru`.
