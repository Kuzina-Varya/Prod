# Поисковая тулза normativ.kontur.ru

`kontur_search_tool.py` - отдельный инструмент для поиска нормативных документов
на `normativ.kontur.ru`. На вход подается обычный вопрос пользователя, на выходе
возвращается JSON-строка с найденными документами, диагностикой поиска и,
при необходимости, текстовым превью страницы документа. Скрипт не оценивает,
насколько документ подходит по смыслу: это делает LLM.

## Файлы

- `kontur_search_tool.py` - основной поисковый скрипт.
- `kontur_last_result.json` - последний результат ручного тестового запуска.
- `requirements.txt` - зависимости для этой тулзы.
- `../../html_legal_text_parser.py` - общий парсер, который очищает уже
  загруженный HTML и достаёт основной текст документа.

## Установка

Запускать из корня репозитория:

```powershell
pip install -r requirements.txt
pip install -r tools\kontur\requirements.txt
```

Если используется локальное виртуальное окружение, сначала активируйте его:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Запуск ручных тестов

Из корня репозитория:

```powershell
python .\tools\kontur\kontur_search_tool.py
```

Команда запускает позитивные и граничные проверки из блока `__main__` и
сохраняет последний результат сюда:

```text
tools\kontur\kontur_last_result.json
```

## Главная функция

```python
from tools.kontur.kontur_search_tool import search_documents_for_construction

raw_json = search_documents_for_construction(
    search_query="Какие документы нужны для строительства фундамента?",
    iteration_count=1,
    max_pages=2,
    limit=5,
    include_text=True,
    include_full_text=False,
)
```

Функция возвращает JSON-строку, а не Python-словарь. Формат близок к
`tools.sniprf.sniprf_search_tool` и `tools.cntd.cntd_search_tool`: `documents`,
`page_reports`, `matched_keywords`, `text_preview`, `has_more_documents`.

## Как ищет

1. Формирует URL поисковой страницы, куда пользователь попадает после ввода
   запроса в поле поиска:

   ```text
   https://normativ.kontur.ru/?searching=true&query=<запрос>&sortby=1
   ```

2. Забирает результаты тем же AJAX-маршрутом, который использует сайт:

   ```text
   POST https://normativ.kontur.ru/search-results?searching=true&query=<запрос>&sortby=1
   ```

   В запрос добавляются `X-Requested-With: XMLHttpRequest` и `Referer` на
   поисковую страницу. Обычный `GET /search-results` у сайта возвращает 404,
   поэтому здесь нужен именно POST.

3. Парсит HTML-выдачу через BeautifulSoup. Основная структура:

   ```text
   ul.search-result__items
   li.js-found_doc
   a.js-found_doc-link[href]
   ```

4. Не расширяет запрос своими вариантами и не угадывает отдельные номера
   документов. В поиск сайта уходит ровно тот текст, который пришёл в
   `search_query`.

## Пачки по 5 документов

Скрипт возвращает кандидатов в порядке получения от сайта и не сортирует их по
собственной оценке релевантности. `min_score` оставлен в функции только для
совместимости со старыми вызовами и сейчас игнорируется.

Основной сценарий для LLM:

1. Вызвать `search_documents_for_construction(..., iteration_count=1, limit=5)`.
2. Передать `documents` нейронке.
3. Если в этих документах нет нужной информации, нейронка отвечает, что данных
   недостаточно.
4. Вызвать тот же поиск с `iteration_count=2`, затем `3` и так далее, пока
   `has_more_documents=True`.

`matched_keywords` и `exact_identifier_matches` являются только диагностическими
подсказками. Они не означают, что документ точно подходит.

## Проверочные запросы

Обычный вопрос без номера документа:

```powershell
.\.venv\Scripts\python.exe -c "from tools.kontur.kontur_search_tool import search_documents_for_construction; print(search_documents_for_construction('Какие документы нужны для строительства фундамента?', max_pages=2, limit=10, include_text=False))"
```

Нейронка должна проверить возвращённые документы и при нехватке информации
запросить следующую пачку.

Точный запрос:

```powershell
.\.venv\Scripts\python.exe -c "from tools.kontur.kontur_search_tool import search_documents_for_construction; print(search_documents_for_construction('СП 22.13330 основания зданий', max_pages=3, limit=8, include_text=False))"
```

Нейронка сама проверяет, есть ли среди возвращённых документов нужный СП.

Запрос с контекстом вечномерзлых грунтов:

```powershell
.\.venv\Scripts\python.exe -c "from tools.kontur.kontur_search_tool import search_documents_for_construction; print(search_documents_for_construction('Какие нормы нужны для строительства фундаментов на вечномерзлых грунтах?', max_pages=2, limit=8, include_text=False))"
```

Нейронка сама проверяет, есть ли среди возвращённых документов нужная информация
по вечномерзлым грунтам.

## Кодировка PowerShell

Если русский текст в консоли отображается как `РџРѕРёСЃРє`, перед запуском можно
выполнить:

```powershell
chcp 65001
$env:PYTHONIOENCODING="utf-8"
```

На сам поиск это не влияет, проблема только в отображении текста в терминале.

## Последний результат

Файл `kontur_last_result.json` хранит последний ручной тестовый JSON. Его можно
открыть и посмотреть структуру ответа без повторного сетевого запроса.
