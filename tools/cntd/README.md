# Поисковая тулза docs.cntd.ru

`cntd_search_tool.py` - локальный инструмент для поиска нормативных документов
на `docs.cntd.ru`. Он рассчитан на будущую LLM-интеграцию: на вход подается
обычный вопрос пользователя, на выходе возвращается JSON-строка с найденными
документами, диагностикой поиска и, при необходимости, текстовым превью.
Скрипт не оценивает, насколько документ подходит по смыслу: это делает LLM.

## Файлы

- `cntd_search_tool.py` - основной поисковый скрипт.
- `cntd_last_result.json` - последний результат ручного тестового запуска.
- `requirements.txt` - зависимости для этой тулзы.
- `../../html_legal_text_parser.py` - общий парсер, который очищает уже
  загруженный HTML и достаёт основной текст документа.

## Установка

Запускать из корня репозитория:

```powershell
pip install -r requirements.txt
pip install -r tools\cntd\requirements.txt
```

Если используется локальное виртуальное окружение, сначала активируйте его:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Запуск ручных тестов

Из корня репозитория:

```powershell
python .\tools\cntd\cntd_search_tool.py
```

Команда запускает позитивные и граничные проверки из блока `__main__` и
сохраняет последний результат сюда:

```text
tools\cntd\cntd_last_result.json
```

## Главная функция

```python
from tools.cntd.cntd_search_tool import search_documents_for_construction

raw_json = search_documents_for_construction(
    search_query="Какие документы нужны для строительства фундамента?",
    iteration_count=1,
    max_pages=2,
    limit=5,
    include_text=True,
    include_full_text=False,
)
```

Функция возвращает JSON-строку, а не Python-словарь. Формат похож на
`tools.sniprf.sniprf_search_tool`: `documents`, `page_reports`,
`matched_keywords`, `text_preview`, `has_more_documents`.

## Как ищет

1. Открывает поисковую страницу сайта:

   ```text
   https://docs.cntd.ru/search?q=<запрос>
   ```

   Это тот же маршрут, куда пользователь попадает после ввода запроса в поле
   поиска на `docs.cntd.ru`. В JSON такие проверки помечаются как
   `mode: "site_search_page"` и `search_input_submitted: true`.

2. Парсит SSR HTML-выдачу через BeautifulSoup.

3. Добирает результаты через публичный API, который использует сам сайт:

   ```text
   https://api.docs.cntd.ru/v1/search?query=<запрос>
   ```

   API-страницы идут через cursor-пагинацию. `max_pages` ограничивает количество
   API-кусков на один вариант запроса.

4. Не расширяет запрос своими вариантами и не угадывает отдельные номера
   документов. В поиск сайта уходит ровно тот текст, который пришёл в
   `search_query`.

## Пачки по 5 документов

Скрипт возвращает кандидатов в порядке получения от сайта/API и не сортирует их
по собственной оценке релевантности. `min_score` оставлен в функции только для
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
.\.venv\Scripts\python.exe -c "from tools.cntd.cntd_search_tool import search_documents_for_construction; print(search_documents_for_construction('Какие документы нужны для строительства фундамента?', max_pages=2, limit=10, include_text=False))"
```

Нейронка должна проверить возвращённые документы и при нехватке информации
запросить следующую пачку.

Точный запрос:

```powershell
.\.venv\Scripts\python.exe -c "from tools.cntd.cntd_search_tool import search_documents_for_construction; print(search_documents_for_construction('СП 22.13330 основания зданий', max_pages=2, limit=8, include_text=False))"
```

Нейронка сама проверяет, есть ли среди возвращённых документов нужный СП.

Запрос с контекстом вечномерзлых грунтов:

```powershell
.\.venv\Scripts\python.exe -c "from tools.cntd.cntd_search_tool import search_documents_for_construction; print(search_documents_for_construction('Какие документы нужны для строительства фундамента на вечномерзлых грунтах?', max_pages=2, limit=10, include_text=False))"
```

Нейронка сама проверяет, есть ли среди возвращённых документов нужная информация
по вечномерзлым грунтам.

## Кодировка PowerShell

Если в консоли русский текст отображается как `РЎРџ`, перед запуском можно
выполнить:

```powershell
chcp 65001
$env:PYTHONIOENCODING="utf-8"
```

На сам поиск это не влияет, проблема только в отображении текста в терминале.

## Последний результат

Файл `cntd_last_result.json` хранит последний ручной тестовый результат. Его
можно открыть и посмотреть структуру JSON без повторного сетевого запроса.
