# Воспроизводимые запуски

Команды ниже выполняются из корня репозитория на Python 3.10+. Установка
пакетов не требуется. Каталоги `results/` и `replays/` создаются автоматически
и исключены из git.

## Baseline

```text
python -m planner.cli --scenario data/P01_intro.json --planner baseline --output results/p01-baseline.json
```

Проверка: процесс завершается с кодом 0, stdout содержит `steps_executed: 48`, а
в результате `schema_version` равен `cosmo-B-ops-result-1.0` и
`blocked_command_count` равен 0. Для текущего P01 ожидаются 11 завершённых
заданий и выручка 151.24.

## Strategic

```text
python -m planner.cli --scenario data/P01_intro.json --planner strategic --goal priority --output results/p01-priority.json
python -m planner.cli --scenario data/P01_intro.json --planner strategic --goal revenue --output results/p01-revenue.json
```

Проверка: `run_metadata.algorithm` равен `strategic`, записаны `goal`, версия и
параметры, отклонённых команд нет. На P01 обе цели дают одинаковую сводку; это
корректный результат, а не требование искусственно различать планы.

## Analysis

```text
python -m planner.analysis --scenario data/P03_energy.json --goal revenue --output results/p03-analysis.json
```

Проверка: отчёт имеет schema `cosmo-B-planner-analysis-1.0`, секции `baseline`,
`strategic`, `delta`, счётчики причин, незавершённые задания и ресурсные
индикаторы. `delta` означает `strategic - baseline`. Время выполнения зависит от
машины и не является детерминированной метрикой.

## Replay result

Сначала создайте компактный результат, затем воспроизведите его:

```text
python scripts/demo.py --output results/demo-result.json
python model/operations.py --result results/demo-result.json --output replays/demo-replay.json
```

Проверка: обе команды завершаются с кодом 0; `summary` и `trace` в
`results/demo-result.json` и `replays/demo-replay.json` совпадают точно. Поле
`demo_report` является дополнительным и содержит сравнение двух ветвей; replay
восстанавливает обязательные поля стандартного результата.

Раздельная форма replay также поддерживается. `commands.json` может быть
массивом команд или объектом с полем `commands`:

```text
python model/operations.py --scenario data/P02_shift.json --events examples/events_demo.json --commands commands.json --output replays/p02.json
```

## events_demo

```text
python -m planner.cli --scenario data/P02_shift.json --events examples/events_demo.json --planner strategic --goal priority --output results/p02-events.json
python -m planner.analysis --scenario data/P02_shift.json --events examples/events_demo.json --goal priority --output results/p02-events-analysis.json
```

Проверка: приняты четыре события в исходном порядке, включая новые задания,
отказ двух аппаратов и закрытие downlink; планировщик получает каждое событие
только на его `at_step`. В result нет отклонённых команд, а replay даёт тот же
trace. Файл событий относится только к `P02_shift`.

## Demo P01/P02

```text
python scripts/demo.py
python scripts/demo.py --scenario P02
```

Оба запуска останавливаются на границе, вызывают `apply_event()` вручную и
создают из одного checkpoint ветви `priority` и `revenue`. P01 выбран по
умолчанию для быстрой демонстрации; P02 выполняет суточный сценарий. В конце
должна появиться строка `Replay verified: exact summary and trace for both
branches`.

## Web

```text
python -m web.app --host 127.0.0.1 --port 8000
```

Откройте `http://127.0.0.1:8000/` и выполните: создать запуск, advance до
границы, отправить событие с совпадающим `at_step`, переключить цель или сделать
fork, выполнить ещё шаг, запросить explain и скачать result. Быстрая проверка
сервера: `http://127.0.0.1:8000/health` возвращает `{"status":"ok", ...}`.

## Полная проверка

```text
python -m compileall -q model planner web scripts tests
python -m unittest discover -s tests -v
```

Тесты проверяют все четыре сценария и обе цели, события на границах,
независимость fork, атомарность ошибок, HTTP API, нагрузочный P04, CLI demo и
точное совпадение summary/trace после replay.
