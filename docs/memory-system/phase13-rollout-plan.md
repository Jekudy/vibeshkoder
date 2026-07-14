# Phase 13: production rollout памяти Шкодера

## Проблема

Код памяти, поиска, дайджестов и wiki существует в репозитории, но production
остаётся на старой схеме и старых образах. Raw-архив не включён, weekly digest
требует ручного approval, wiki не компилируется автоматически, а публичный web
контур связан с VPS. Telegram export от 2026-07-14 предоставлен в HTML, тогда
как существующий importer принимает только JSON.

## Целевой результат

1. Все полученные сообщения людей сохраняются в raw и нормализованном слоях.
2. Сообщения самого Шкодера не попадают в derived memory и не зацикливают
   дайджесты.
3. Картинки получают постоянную source-ссылку и LLM-описание; голосовые не
   транскрибируются.
4. Daily и weekly дайджесты автоматически публикуются в исходный чат в 09:00
   Europe/Moscow без approval.
5. DeepSeek V4 Flash поддерживает текстовые LLM-вызовы; OpenAI используется для
   vision. Все вызовы проходят через cost ledger и budget guard.
6. LLM поддерживает связанную Markdown-wiki в стиле Karpathy; статическая
   read-only версия и локальный поисковый индекс публикуются на Cloudflare Pages
   односторонней загрузкой, без runtime-доступа к VPS, БД или LLM.
7. Вопрос к Шкодеру срабатывает только по mention или reply на сообщение бота,
   доступен участникам сообщества и ограничен двумя LLM-вопросами в сутки.
8. В анкете источник знакомства принимается как `nickname`, `@nickname` или
   `t.me/nickname`, сохраняется и публикуется как `@nickname`.

## Неподвижные границы

- Никаких API-ключей или Telegram content в логах, git и generated reports.
- Публичный поиск не вызывает LLM.
- Q&A не имеет tools, shell, Butler actions или произвольного HTTP-доступа.
- Raw-архив остаётся приватным; публичная wiki — производная проекция.
- Нормальный workflow не содержит ручного approval. Ручная правка после
  публикации допустима и версионируется.
- Применение миграций и import в production возможно только после restore drill
  и dry-run reconciliation.

## Зафиксированные продуктовые решения

- Храним всю историю. Старые off-record/forget-механизмы больше не создают
  исключений из памяти; незавершённые старые запросы становятся `superseded`.
- Исключение от зацикливания применяется только к точному имени автора
  `Shkoder`: raw-событие остаётся, нормализованная и derived-память не создаётся.
  Человеческий автор `Лиля Шкодер` не исключается.
- Telegram HTML export является источником исторического backfill. В нём нет
  самих файлов вложений, поэтому старым фотографиям честно ставится
  `missing_source`; новые фотографии получают Telegram source-link и описание.
- Голосовые сохраняются как сообщения, но не транскрибируются.
- Тихий день или неделя всё равно создают короткий автоматический дайджест без
  LLM-вызова. Любой содержательный дайджест публикуется без approval.
- `recall` — это извлечение подходящих фрагментов прошлого контекста. В Шкодере
  `/recall` и обычный вопрос через mention/reply сначала делают детерминированный
  поиск; LLM только формулирует ответ по найденным источникам. После двух
  LLM-вопросов за день поиск остаётся доступным без LLM.
- Эмбеддинг — числовое представление смысла текста, полезное для поиска по
  близким формулировкам. В первом rollout отдельный embedding API не нужен:
  PostgreSQL Russian FTS и статический индекс wiki дают бесплатный поиск без
  LLM. У официального DeepSeek API на 2026-07-14 нет документированного
  embedding endpoint, поэтому DeepSeek используется для extraction, digest,
  Q&A и wiki-компиляции, а не для каждого поискового запроса.
- Публичная wiki на первом rollout статическая и `noindex`, но не требует
  Telegram-login. Будущий member-only режим должен проверять Telegram identity
  на Cloudflare edge и отдавать тот же статический артефакт; соединение edge →
  VPS для чтения wiki запрещено.
- По официальному тарифу на 2026-07-14 DeepSeek V4 Flash стоит $0.14 за 1 млн
  cache-miss input tokens и $0.28 за 1 млн output tokens. Это заметно дешевле
  текущего default `claude-haiku-4-5` ($1/$5) и дешевле `gpt-4o-mini` по
  output при сопоставимой цене input, поэтому DeepSeek выбран для текстовых
  задач. `gpt-5-nano` дешевле на input ($0.05), но дороже на output ($0.40);
  он остаётся для vision, где DeepSeek не подходит.

## TDD и доказательства

Для каждого vertical slice сохраняются:

1. failing test до реализации;
2. минимальная реализация;
3. green unit/integration набор;
4. независимый review агентом, который не писал slice;
5. live evidence после deployment.

## План и критерии приёмки

| Этап | Что делаем | Критерии приёмки | Доказательство |
|---|---|---|---|
| 1. Baseline и safety | Инвентаризация production, backup и restore drill, фиксация export | Известны production SHA/schema/counts; backup восстановлен в отдельную БД; до drill нет production-миграций | Restore-команда, schema/version и контрольные counts |
| 2. Полная история | Миграции, HTML parser, dry-run и apply | Все 9 403 user-message records классифицированы; 107 exact `Shkoder` остаются raw-only; `Лиля Шкодер` сохранена; good/bad/good chunk не продвигает checkpoint; повторный apply даёт только объяснимые duplicates | Import report, reconciliation SQL, повторный dry-run |
| 3. Live memory | Middleware, картинки, QA, анкета, daily/weekly | DB → raw → normalized порядок доказан; сообщения бота не входят в derived memory; новые фото получают safe link+description; 09:00 MSK daily и Monday weekly публикуются автоматически, включая тихое окно; QA member-only, mention/reply, 2 LLM/day; referral нормализован | Runtime tests, Telegram message ids, ledger/quota SQL |
| 4. Extraction и backfill | Event-time окна, DeepSeek, auto-promotion | Только source chat; окна не пересекаются и возобновляются без повторного LLM; один сбой останавливает backfill; кандидаты атомарно становятся approved cards; invalid rows не создают starvation | PostgreSQL concurrency/resume tests, run/card counts, ledger reconciliation |
| 5. LLM-wiki | Stable topic slugs, ревизии, citations, автоматическая сборка | Изменённая тема создаёт ровно одну новую ревизию; неизменённая не вызывает LLM; каждый факт имеет разрешённый source; redacted/stale source блокирует export; исчезновение последней automatic-page не оставляет старую публичную статью | Compiler/orchestrator tests, revision/source SQL |
| 6. Статическая публикация | Export и Cloudflare Pages | Только HTML/CSS/JS/JSON; локальный поиск не вызывает VPS/БД/LLM; CSP запрещает внешние соединения; secret/network scanner green; включая zero-page generation, upload идемпотентен; public smoke совпадает с manifest | Static audit hash, Cloudflare deployment audit, ledger/VPS request delta = 0 при поиске |
| 7. Production rollout | PR/CI, deploy GHCR через Coolify, flags и live acceptance | Release разрешён только после trusted push-CI; bot/web собраны из одного SHA и закреплены разными OCI digest; production на Alembic head; все flags включены явно; импорт/backfill завершены; оба digest smoke опубликованы; wiki доступна; QA и quota проверены; после 087 rollback делается flags-off/forward-fix, а возврат старого образа — только вместе с восстановлением pre-migration backup | GitHub run, OCI labels/digests, Coolify health/logs, Telegram/Cloudflare URLs, restore evidence |

## Критерий полного завершения

Phase 13 закрывается только когда все тесты и CI зелёные, production работает на
точном ожидаемом SHA и schema head, import reconciliation не имеет
необъяснённых пропусков, оба дайджеста опубликованы, wiki доступна и ищется без
LLM, Q&A соблюдает квоту/guardrails, новая анкета публикует нормализованный
`@username`, а публичный трафик не создаёт запросов к VPS, БД и LLM ledger.
