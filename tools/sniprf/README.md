# Поисковая тулза sniprf.ru

`sniprf_search_tool.py` - локальный инструмент для поиска строительных
нормативных документов на сайте `sniprf.ru`.

Скрипт рассчитан на будущую интеграцию с LLM: на вход передаётся обычный
текстовый запрос, скрипт ищет документы только на `sniprf.ru` /
`www.sniprf.ru`, собирает найденные кандидаты пачками и возвращает результат в JSON.

Извлечение чистого текста из HTML вынесено в отдельный модуль проекта:

```text
html_legal_text_parser.extract_main_text_from_html
```

Эта тулза отвечает за поиск, фильтрацию служебных ссылок и диагностику. Она не
оценивает, насколько документ подходит по смыслу: это делает LLM по тексту
документов. Она также не должна искать по внешним сайтам и не должна заменять
собой HTML-парсер.

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
- `limit` - сколько документов вернуть за один вызов, обычно `5`.
- `min_score` - устаревший параметр совместимости; сейчас игнорируется.
- `include_text` - добавлять `text_preview` через `html_legal_text_parser`.
- `include_full_text` - добавлять полный извлечённый текст HTML-документа.

Функция возвращает JSON-строку, а не Python-словарь.

## Как работает поиск

Скрипт отправляет исходный `search_query` во внутренний поиск `sniprf.ru` и
парсит найденные страницы. Google, Yandex, прямые угадываемые URL документов и
каталожный обход не используются в основном сценарии.

Кандидаты возвращаются в порядке обнаружения в выдаче сайта. Скрипт не
сортирует документы по оценке релевантности и не отбрасывает кандидатов по
`score`: подходит документ или нет, решает LLM.

## Пачки по 5 документов

Основной сценарий для LLM:

1. Вызвать `search_documents_for_construction(..., iteration_count=1, limit=5)`.
2. Передать `documents` нейронке.
3. Если в этих документах нет нужной информации, нейронка отвечает, что данных
   недостаточно.
4. Вызвать тот же поиск с `iteration_count=2`, затем `3` и так далее, пока
   `has_more_documents=True`.

## Формат результата JSON

Важные поля верхнего уровня:

- `status` - `"success"` или `"error"` для пустого запроса.
- `documents` - очередная пачка документов для текущей итерации.
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
- `matched_keywords` - найденные слова из запроса и словаря синонимов, только
  диагностическая подсказка, не оценка релевантности.
- `exact_identifier_matches` - точные совпадения номера СП из запроса, только
  диагностическая подсказка.
- `source_mode` - источник, сейчас основной режим `site_search`.
- `text_preview` - очищенный фрагмент текста, если `include_text=True`.

## Диагностика

Реальной проблемой считается отчёт вида:

```json
{
  "status": "error",
  "severity": "error",
  "ignored": false
}
```

## Как использовать результат в LLM

- Передавать в контекст LLM массив `documents`.
- LLM сама проверяет, есть ли в текущих 5 документах нужная информация.
- Если информации недостаточно и `has_more_documents=True`, запрашивать
  следующую пачку через `iteration_count + 1`.
- `page_skipped` не является ошибкой.
- Если `documents_returned == 0`, лучше переформулировать или сузить запрос.
- Для другого сайта нужно делать отдельную тулзу, эту оставляем только под
  `sniprf.ru`.
