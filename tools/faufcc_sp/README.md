# Поисковая тулза faufcc.ru: реестр сводов правил

`faufcc_sp_search_tool.py` - локальный инструмент для поиска записей в реестре
сводов правил ФАУ «ФЦС»:

```text
https://faufcc.ru/deiatelnost/normirovanie-i-standartizatsiia/reestr-svodov-pravil
```

Сайт открывается как SPA, а сами данные реестра грузятся из API:

```text
https://api.faufcc.ru/api/pages/find
https://api.faufcc.ru/api/pages/<page_id>?include=sections.type,sections.groups.fields.value,registry
https://api.faufcc.ru/api/registries/categories
https://api.faufcc.ru/api/registries/entries
```

Скрипт не оценивает, насколько документ подходит по смыслу. Он собирает записи
реестра и отдает их пачками, а нейронка смотрит массив `documents`.

## Файлы

- `faufcc_sp_search_tool.py` - основной поисковый скрипт.
- `faufcc_sp_last_result.json` - последний результат ручного тестового запуска.
- `requirements.txt` - зависимости для этой тулзы.

## Установка

Запускать из корня репозитория:

```powershell
pip install -r requirements.txt
pip install -r tools\faufcc_sp\requirements.txt
```

## Запуск ручных тестов

Из корня репозитория:

```powershell
python .\tools\faufcc_sp\faufcc_sp_search_tool.py
```

Команда запускает позитивные и граничные проверки из блока `__main__` и
сохраняет последний результат сюда:

```text
tools\faufcc_sp\faufcc_sp_last_result.json
```

Последний проверенный запуск:

- `exit_code=0`;
- длительность около `12` сек;
- лог: `tools\_test_logs\faufcc_sp_run.log`;
- итоговый JSON: `tools\faufcc_sp\faufcc_sp_last_result.json`;
- последний JSON сохранен по запросу `СП 20.13330.2016`, но выдача все равно
  идет обычной пачкой по порядку реестра.

## Главная функция

```python
from tools.faufcc_sp.faufcc_sp_search_tool import search_documents_for_construction

raw_json = search_documents_for_construction(
    search_query="СП 20.13330.2016",
    iteration_count=1,
    max_pages=2,
    limit=5,
    include_text=False,
)
```

Функция возвращает JSON-строку, а не Python-словарь.

Параметры:

- `search_query` - обычный текстовый запрос или номер СП.
- `iteration_count` - номер пачки результатов, начиная с `1`.
- `max_pages` - сколько страниц записей API проверять в каждой категории.
- `limit` - сколько документов вернуть за один вызов, обычно `5`.
- `min_score` - устаревший параметр совместимости; сейчас игнорируется.
- `include_text`, `include_full_text` - параметры совместимости; PDF не
  разбираются внутри этой тулзы.

## Как ищет

1. Проверяет стартовую страницу `faufcc.ru`.
2. Через `/api/pages/find` находит страницу `reestr-svodov-pravil`.
3. Через `/api/pages/<id>?include=...registry` получает настоящий `registry_id`.
4. Через `/api/registries/categories` рекурсивно собирает категории реестра.
5. Через `/api/registries/entries` вызывает тот же поиск реестра, что и сайт:
   `filters={"registry":"...","search":"<запрос>"}`.
6. Возвращает записи в порядке, который отдал API сайта, без дополнительной
   сортировки, смысловой оценки и специальных ускорений для точного номера СП.

Важно: фильтр `filters` отправляется компактным JSON без пробелов, как делает
сам сайт:

```json
{"registry":"...","search":"..."}
```

Если API временно отвечает `429 Too Many Requests`, скрипт использует
известный `page_id`/`registry_id` реестра и внутрипроцессный кэш уже полученных
категорий/записей.

## Пачки по 5 документов

Логика такая же, как в остальных тулзах:

1. Первый вызов: `iteration_count=1`, `limit=5`.
2. Нейронка смотрит `documents`.
3. Если информации недостаточно и `has_more_documents=true`, вызывается
   `iteration_count=2`.
4. Потом `iteration_count=3` и так далее.

Скрипт не ранжирует документы по score и не отбирает их по смыслу. Поля
`matched_keywords` и `exact_identifier_matches` нужны только для диагностики.

## Структура JSON

Главные поля:

- `status` - `success` или `error`.
- `query` - исходный запрос.
- `registry_id` / `registry_type` / `registry_title` - найденный реестр.
- `categories_found` - для этой версии `null`, потому что выдача идет через
  поиск сайта, а не через ручной обход категорий.
- `documents` - текущая пачка записей.
- `total_candidates_found` - сколько кандидатов собрано до нарезки на пачки.
- `documents_returned` - сколько документов вернул текущий вызов.
- `has_more_documents` - есть ли следующая пачка.
- `page_reports` - диагностика по запросам к API.

У каждого документа:

- `title` - номер и название СП.
- `number` - номер СП.
- `name` - название.
- `state` - статус записи.
- `category_path` - путь категории реестра.
- `asset.download_url` - ссылка на скачивание PDF, если есть.
- `content_status` - обычно `binary_document_not_parsed_here`.
