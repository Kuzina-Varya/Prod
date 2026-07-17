# Поисковая тулза economy.gov.ru: ФГИС ТП

`economy_fgis_tp_search_tool.py` - локальный инструмент для поиска материалов
по странице ФГИС ТП Минэкономразвития:

```text
https://www.economy.gov.ru/material/directions/regionalnoe_razvitie/fgis_tp/
```

Скрипт рассчитан на LLM-интеграцию: он собирает документы пачками и не
оценивает, насколько документ подходит по смыслу. Это делает нейронка по
массиву `documents`.

## Файлы

- `economy_fgis_tp_search_tool.py` - основной поисковый скрипт.
- `economy_fgis_tp_last_result.json` - последний результат ручного тестового запуска.
- `requirements.txt` - зависимости для этой тулзы.
- `../../html_legal_text_parser.py` - общий парсер, который очищает уже
  загруженный HTML и достаёт основной текст документа.

## Установка

Запускать из корня репозитория:

```powershell
pip install -r requirements.txt
pip install -r tools\economy_fgis_tp\requirements.txt
```

Если используется локальное виртуальное окружение:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Запуск ручных тестов

Из корня репозитория:

```powershell
python .\tools\economy_fgis_tp\economy_fgis_tp_search_tool.py
```

Команда запускает позитивные и граничные проверки из блока `__main__` и
сохраняет последний результат сюда:

```text
tools\economy_fgis_tp\economy_fgis_tp_last_result.json
```

## Главная функция

```python
from tools.economy_fgis_tp.economy_fgis_tp_search_tool import search_documents_for_construction

raw_json = search_documents_for_construction(
    search_query="ФГИС ТП нормативное обеспечение",
    iteration_count=1,
    max_pages=2,
    limit=5,
    include_text=True,
    include_full_text=False,
)
```

Функция возвращает JSON-строку, а не Python-словарь.

Параметры:

- `search_query` - обычный текстовый запрос.
- `iteration_count` - номер пачки результатов, начиная с `1`.
- `max_pages` - сколько страниц выдачи `/search/?q=<запрос>` проверить.
- `limit` - сколько документов вернуть за один вызов, обычно `5`.
- `min_score` - устаревший параметр совместимости; сейчас игнорируется.
- `include_text` - добавлять `text_preview` через `html_legal_text_parser`.
- `include_full_text` - добавлять полный извлечённый текст HTML-документа.

## Как ищет

1. Отправляет исходный `search_query` в поиск Минэкономразвития:

   ```text
   https://www.economy.gov.ru/search/?q=<запрос>
   ```

2. Парсит найденные страницы и ссылки на материалы из выдачи.
3. Возвращает кандидатов в порядке обнаружения, без ранжирования по смыслу.

## Разрешённые хосты

Основная страница находится на `economy.gov.ru`, но нормативные материалы ФГИС
ТП ведут на связанные официальные источники. Поэтому тулза разрешает:

- `www.economy.gov.ru`
- `economy.gov.ru`
- `fgistp.economy.gov.ru`
- `pravo.gov.ru`
- `publication.pravo.gov.ru`

## Пачки по 5 документов

Основной сценарий:

1. Вызвать `search_documents_for_construction(..., iteration_count=1, limit=5)`.
2. Передать `documents` нейронке.
3. Если нужной информации нет, нейронка отвечает, что данных недостаточно.
4. Запросить следующую пачку через `iteration_count=2`, затем `3` и так далее,
   пока `has_more_documents=True`.

`matched_keywords` и `exact_identifier_matches` являются диагностическими
подсказками. Они не означают, что документ точно подходит.

## Формат результата JSON

Важные поля верхнего уровня:

- `status` - `"success"` или `"error"` для пустого запроса.
- `documents` - очередная пачка документов.
- `total_candidates_found` - сколько кандидатов найдено до пагинации.
- `documents_returned` - сколько документов вернул текущий вызов.
- `has_more_documents` - есть ли следующая пачка.
- `pages_checked` - сколько страниц-источников проверено.
- `page_errors` - количество неожиданных ошибок.
- `page_skipped` - количество ожидаемо пропущенных страниц.
- `page_reports` - диагностика по каждой странице.

Важные поля документа:

- `title` - название документа или лучший текст ссылки.
- `url` - ссылка.
- `document_type` - примерный тип: `HTML`, `PDF`, `Приказ`, `Постановление`,
  `Федеральный закон`, `Кодекс`.
- `matched_keywords` - найденные слова запроса.
- `exact_identifier_matches` - найденные номера актов из запроса.
- `source_mode` - источник, сейчас основной режим `site_search`.
- `text_preview` - очищенный фрагмент текста, если `include_text=True`.

## Кодировка PowerShell

Если русский текст в консоли отображается неправильно:

```powershell
chcp 65001
$env:PYTHONIOENCODING="utf-8"
```
