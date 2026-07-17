# Prod

Набор локальных скриптов для поиска и скачивания нормативных документов, а
также поиска пространственных объектов НСПД. Общая идея поисковых тулз: скрипт
собирает кандидаты и отдает их пачками, а смысловую проверку делает
LLM/нейронка по массиву `documents`.

## Инструменты

- [Поисковая тулза sniprf.ru](tools/sniprf/README.md) - поиск строительных нормативных
  документов на `sniprf.ru` для будущей LLM-интеграции.
- [Поисковая тулза docs.cntd.ru](tools/cntd/README.md) - поиск нормативных
  документов на `docs.cntd.ru`.
- [Поисковая тулза normativ.kontur.ru](tools/kontur/README.md) - поиск
  нормативных документов на `normativ.kontur.ru`.
- [Поисковая тулза economy.gov.ru: ФГИС ТП](tools/economy_fgis_tp/README.md) -
  поиск материалов и нормативного обеспечения по странице ФГИС ТП
  Минэкономразвития.
- [Поисковая тулза nspd.gov.ru](tools/nspd/README.md) - поиск пространственных
  объектов НСПД: земельных участков, зданий, кадастровых кварталов, зон и
  территорий.
- [Поисковая тулза faufcc.ru: реестр сводов правил](tools/faufcc_sp/README.md) -
  поиск записей СП в реестре сводов правил ФАУ «ФЦС».

## Общая логика поисковых тулз

Все поисковые тулзы сделаны под один сценарий:

1. На вход подается обычный текстовый запрос пользователя.
2. Скрипт вводит этот исходный запрос в поиск своего сайта или официальный
   поисковый API сайта.
3. Скрипт возвращает JSON-строку с полями `documents`, `page_reports`,
   `documents_returned`, `has_more_documents`.
4. Скрипт не решает, подходит документ по смыслу или нет. Это должна делать
   нейронка.
5. Если в первой пачке нет нужной информации, вызывается следующая пачка:
   `iteration_count=2`, потом `iteration_count=3` и так далее.

Обычно используется `limit=5`: одна итерация возвращает 5 документов или
объектов. Параметр `min_score` оставлен только для совместимости со старым
кодом и сейчас не используется для фильтрации.

## Установка зависимостей

Из корня репозитория:

```powershell
pip install -r requirements.txt
```

Для отдельной тулзы можно дополнительно поставить ее локальные зависимости:

```powershell
pip install -r tools\sniprf\requirements.txt
pip install -r tools\cntd\requirements.txt
pip install -r tools\kontur\requirements.txt
pip install -r tools\economy_fgis_tp\requirements.txt
pip install -r tools\nspd\requirements.txt
pip install -r tools\faufcc_sp\requirements.txt
```

## Запуск ручных проверок

Каждая тулза содержит тестовый блок `__main__` с позитивными и граничными
проверками. Команды запуска из корня репозитория:

```powershell
python .\tools\sniprf\sniprf_search_tool.py
python .\tools\cntd\cntd_search_tool.py
python .\tools\kontur\kontur_search_tool.py
python .\tools\economy_fgis_tp\economy_fgis_tp_search_tool.py
python .\tools\nspd\nspd_search_tool.py
python .\tools\faufcc_sp\faufcc_sp_search_tool.py
```

После запуска каждая тулза сохраняет последний результат в своей папке:

```text
tools\sniprf\sniprf_last_result.json
tools\cntd\cntd_last_result.json
tools\kontur\kontur_last_result.json
tools\economy_fgis_tp\economy_fgis_tp_last_result.json
tools\nspd\nspd_last_result.json
tools\faufcc_sp\faufcc_sp_last_result.json
```

## Общий парсер HTML

Файл `html_legal_text_parser.py` используется поисковыми тулзами для очистки
HTML и извлечения основного текста страницы. PDF/DOC/DOCX сами поисковые тулзы
не разбирают: для таких документов они возвращают ссылку и статус вроде
`binary_document_not_parsed_here`.
