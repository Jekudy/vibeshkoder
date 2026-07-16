# Semantic Q&A: migration, rollout и rollback

Этот runbook — обязательная последовательность для issue #404. Semantic Q&A
остаётся выключенным, пока не пройдены restore rehearsal, миграция, backfill,
coverage audit, shadow/eval и Telegram E2E.

## Контракт production

- PostgreSQL остаётся на major version 15. Меняется только image с
  `postgres:15-alpine` на
  `ghcr.io/jekudy/vibe-gatekeeper-postgres:sha-<release-sha>@sha256:<digest>`.
  Этот image собирается `Dockerfile.postgres` из точного production parent
  `postgres@sha256:1c52f5ad23db5d7648a63634444af76de48e63b860fccbe3e3a5458b2812eaed`
  (PG15 Alpine) и pgvector `0.8.2`; смена image на том же volume не меняет
  major version, libc или layout данных.
- Production DB: `vibe_gatekeeper`, user `vibe`, Coolify DB/container UUID
  `hdazvm5fz836xj9mdyn8c629`.
- Миграция `089` создаёт extension `vector`, semantic tables и nullable
  `users.is_bot`. Векторный поиск использует exact cosine; HNSW в этом rollout
  не создаётся.
- Rollout flag: `memory.qa.semantic.enabled`. Отсутствующий или выключенный
  флаг сохраняет существующий FTS Q&A path.
- Embeddings используют существующий `OPENAI_API_KEY`. Новые настройки не
  переименовывать: `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`,
  `EMBEDDING_DAILY_USD_CEILING`, `EMBEDDING_MONTHLY_USD_CEILING`.
- Все operator-артефакты содержат только counters, hashes, source IDs, ranks,
  latency и ledger IDs. Вопросы и source text запрещено печатать в terminal,
  CI artifact или issue/PR.

## 1. Stop conditions

Немедленно остановить rollout и оставить semantic flag выключенным, если:

- последний backup старше 24 часов, `pg_restore --list` не проходит или нет
  успешного restore rehearsal;
- production image не предоставляет extension `vector` версии `0.8.2`;
- `alembic current` не показывает ожидаемый release head;
- backfill/audit возвращает `status != pass`, coverage ниже 100% или
  `unexpected_active > 0`, `unresolved_claims > 0`;
- frozen eval не проходит хотя бы один blocking gate issue #404;
- retrieval p95 выше 2 секунд или full-attempt p95 выше 15 секунд;
- найден cross-chat, bot-authored, forgotten, redacted, stale или non-normal
  источник;
- embedding/LLM вызов не связан с usage/cost audit;
- Telegram-ответ не имеет валидной source-ссылки.

## 2. Pre-migration backup

На VPS запустить существующий root-only backup и проверить свежий артефакт:

```bash
ssh foodzy-vps
sudo /usr/local/sbin/shkoder-pg-backup.sh
sudo tail -20 /var/log/shkoder-pg-backup.log
sudo ls -lt /data/coolify/backups/shkoder-postgres/ | head
```

Зафиксировать вне git имя dump, размер и SHA-256:

```bash
DUMP=/data/coolify/backups/shkoder-postgres/shkoder-pg-<UTC>.dump
sudo test -s "$DUMP"
sudo sha256sum "$DUMP"
sudo dd if="$DUMP" status=none | sudo docker exec -i hdazvm5fz836xj9mdyn8c629 \
  pg_restore --list >/dev/null
```

Не продолжать только на основании `pg_restore --list`: нужен полный restore в
одноразовый PostgreSQL.

## 3. Restore и migration rehearsal

Rehearsal выполняется на том же pinned PG15+pgvector image, который пойдёт в
production. `<release-image>` — immutable bot image этого PR по tag или digest.

```bash
RELEASE_GIT_SHA='<40-character-release-git-sha>'
IMAGE='ghcr.io/jekudy/vibe-gatekeeper-postgres:sha-'"$RELEASE_GIT_SHA"'@sha256:<digest>'
sudo docker run -d --name shkoder-semantic-restore \
  --network coolify \
  -e POSTGRES_USER=vibe \
  -e POSTGRES_PASSWORD=restore-only-password \
  -e POSTGRES_DB=vibe_gatekeeper \
  "$IMAGE"

until sudo docker exec shkoder-semantic-restore \
  pg_isready -U vibe -d vibe_gatekeeper; do sleep 1; done

sudo dd if="$DUMP" status=none | sudo docker exec -i shkoder-semantic-restore \
  pg_restore --exit-on-error --no-owner --no-privileges \
  --dbname=vibe_gatekeeper -U vibe
```

Проверить доступность pgvector до миграции:

```bash
sudo docker exec shkoder-semantic-restore psql -U vibe -d vibe_gatekeeper -v ON_ERROR_STOP=1 \
  -c "SELECT name, default_version, installed_version
      FROM pg_available_extensions WHERE name='vector';"
```

Прогнать `up → down → up` release-кодом. До первого downgrade нельзя запускать
backfill/shadow: migration `089` намеренно запрещает downgrade при наличии
любых semantic index/audit rows или `semantic_embedding` ledger rows.

```bash
RELEASE_IMAGE='ghcr.io/jekudy/vibe-gatekeeper-bot@sha256:<release-digest>'
DB_URL='postgresql+asyncpg://vibe:restore-only-password@shkoder-semantic-restore:5432/vibe_gatekeeper'

sudo docker run --rm --network coolify --entrypoint alembic \
  -e DATABASE_URL="$DB_URL" "$RELEASE_IMAGE" upgrade head
sudo docker run --rm --network coolify --entrypoint alembic \
  -e DATABASE_URL="$DB_URL" "$RELEASE_IMAGE" downgrade 088
sudo docker run --rm --network coolify --entrypoint alembic \
  -e DATABASE_URL="$DB_URL" "$RELEASE_IMAGE" upgrade head
```

Проверить extension, Alembic и exact cosine без HNSW:

```bash
sudo docker exec -i shkoder-semantic-restore \
  psql -U vibe -d vibe_gatekeeper -v ON_ERROR_STOP=1 <<'SQL'
SELECT extname, extversion FROM pg_extension WHERE extname='vector';
SELECT version_num FROM alembic_version;
CREATE TEMP TABLE vector_smoke (id integer PRIMARY KEY, embedding vector(3));
INSERT INTO vector_smoke VALUES (1, '[1,0,0]'), (2, '[0,1,0]');
SELECT id FROM vector_smoke ORDER BY embedding <=> '[1,0,0]'::vector, id;
SQL
```

Ожидается `vector 0.8.2`, release head и порядок smoke rows `1, 2`. Затем
сверить ключевые row counts с production dump и удалить rehearsal-контейнер:

```bash
sudo docker exec shkoder-semantic-restore psql -U vibe -d vibe_gatekeeper -v ON_ERROR_STOP=1 \
  -c "SELECT 'users' AS table_name, count(*) FROM users
      UNION ALL SELECT 'chat_messages', count(*) FROM chat_messages
      UNION ALL SELECT 'message_versions', count(*) FROM message_versions;"
sudo docker rm -f shkoder-semantic-restore
```

Результат drill записать отдельным датированным файлом в `docs/ops/` без
секретов и пользовательского контента.

## 4. Production image и migration

1. В Coolify оставить bot/web остановленными на время DB image switch.
2. Изменить image существующей DB на pinned PG15+pgvector image из этого
   runbook. Не подключать production volume к PostgreSQL другой major version и
   не создавать вторую production DB.
3. Запустить DB и проверить `pg_isready`, PG major и доступность `vector`:

```bash
sudo docker exec hdazvm5fz836xj9mdyn8c629 pg_isready -U vibe -d vibe_gatekeeper
sudo docker exec hdazvm5fz836xj9mdyn8c629 psql -U vibe -d vibe_gatekeeper -v ON_ERROR_STOP=1 \
  -c "SHOW server_version; SELECT name, default_version
      FROM pg_available_extensions WHERE name='vector';"
```

4. Deploy immutable bot/web release с semantic flag всё ещё OFF.
5. Запустить `alembic upgrade head` release-кодом и проверить:

```sql
SELECT version_num FROM alembic_version;
SELECT extname, extversion FROM pg_extension WHERE extname='vector';
SELECT table_name
FROM information_schema.tables
WHERE table_schema='public'
  AND table_name IN (
    'semantic_index_runs',
    'semantic_retrieval_units',
    'semantic_retrieval_unit_sources',
    'semantic_qa_attempts',
    'semantic_retrieval_traces'
  )
ORDER BY table_name;
```

Migration `089` помечает human/bot author только по explicit Telegram
`from_user.is_bot` boolean в `chat_messages.raw_json`, `message` или
`edited_message`; `true` имеет приоритет. Membership/admin flags и импортный
`from_id=userN` не являются доказательством человека, поэтому неизвестные и
channel authors корректно остаются `NULL`. Сверить распределение без требования
`classified_author_rows = total_message_rows`:

```sql
SELECT count(*) AS total_message_rows,
       count(*) FILTER (WHERE author.is_bot IS NOT NULL) AS classified_author_rows,
       count(*) FILTER (WHERE author.is_bot IS FALSE) AS human_message_rows,
       count(*) FILTER (WHERE author.is_bot IS TRUE) AS bot_message_rows
FROM chat_messages AS cm
JOIN users AS author ON author.id=cm.user_id;
```

`human_message_rows + bot_message_rows > total_message_rows` или неожиданный
скачок `bot_message_rows` — stop condition. `NULL` rows ожидаемы и индексом не
допускаются.

6. Создать глобальный OFF-row, если его ещё нет:

```sql
INSERT INTO feature_flags (flag_key, scope_type, scope_id, enabled)
SELECT 'memory.qa.semantic.enabled', NULL, NULL, FALSE
WHERE NOT EXISTS (
  SELECT 1 FROM feature_flags
  WHERE flag_key='memory.qa.semantic.enabled'
    AND scope_type IS NULL AND scope_id IS NULL
);
```

Любой неожиданный enabled row до rollout — stop condition.

## 5. Idempotent backfill и coverage audit

Команды выполнять внутри deployed bot container, где уже заданы `DATABASE_URL`,
`OPENAI_API_KEY` и embedding ceilings. Для полного production backfill требуется
явный `--all-chats`; случайно запустить глобальную обработку без него нельзя.

```bash
umask 077
python -m scripts.backfill_semantic_index backfill \
  --all-chats \
  --batch-size 64 \
  --report /tmp/semantic-backfill-<UTC>.json
```

Успех первого запуска:

- exit code `0`, `status=pass`, `failed=0`;
- `coverage.status=pass`, `coverage.coverage_percent=100.0`;
- `coverage.missing=0`, `coverage.unexpected_active=0`,
  `coverage.unresolved_claims=0`;
- `eligible > 0` для текущей непустой production history;
- `eligible = indexed + skipped`.

Источник длиннее provider limit не обрезается и не пропускается: backfill
детерминированно создаёт непустые последовательные chunks до 800 символов с тем же
`source_id`, полным provenance и проверяемыми `chunk_index/chunk_count`.
Coverage считается по chunks; vector retrieval выбирает лучший chunk каждого
источника и не позволяет одному длинному документу вытеснить остальные.

Скопировать no-content report из контейнера в root-only operator storage. Не
загружать его в публичный CI artifact: source IDs и chat IDs остаются
внутренними идентификаторами сообщества.

Повторить ту же команду. Идемпотентность подтверждена, только если:

- `indexed=0`;
- `skipped=eligible`;
- количество `semantic_embedding` provider calls/ledger rows не выросло.

Отдельный read-only coverage/leakage audit не вызывает provider API:

```bash
python -m scripts.backfill_semantic_index audit \
  --all-chats \
  --batch-size 64 \
  --report /tmp/semantic-coverage-<UTC>.json
```

Для поэтапной обработки одного чата заменить `--all-chats` на
`--chat-id <telegram-chat-id>`.

## 6. Shadow retrieval

Shadow CLI вызывает только query embedding и hybrid retrieval. Он не вызывает
DeepSeek synthesis, не отвечает в Telegram, не расходует пользовательскую
двухзапросную квоту и не меняет feature flag.

Private input JSONL хранится вне git с mode `0600`. Один вопрос на строку:

```json
{"question_id":"semantic-001","chat_id":-1001234567890,"query":"<private question>","exclude_chat_message_id":123}
```

`question_id` должен быть opaque ASCII ID, а не текст вопроса. Запуск:

```bash
umask 077
python -m scripts.backfill_semantic_index shadow \
  --input /run/secrets/semantic-shadow-questions.jsonl \
  --output /tmp/semantic-shadow-<UTC>.jsonl \
  --max-queries 50 \
  --candidate-limit 20 \
  --limit 5
```

Output не содержит `query` или snippets. Он содержит `query_sha256`, FTS/vector/
hybrid source IDs, ranks, latency и `embedding_llm_call_id`. Проверить файл
автоматически перед сохранением:

```bash
if grep -Eq '"query"[[:space:]]*:|"snippet"[[:space:]]*:' \
  /tmp/semantic-shadow-<UTC>.jsonl; then
  echo 'STOP: raw content field in shadow report' >&2
  exit 1
fi
```

После shadow прогнать frozen eval из ≥50 реальных privately-labelled вопросов.
Все пять категорий обязательны: `semantic`, `exact`, `multi_source`,
`no_answer`, `privacy_governance`. У privacy cases отдельно перечислить
`forbidden_source_ids`; runner вычисляет leakage как пересечение с реально
возвращёнными canonical keys, поэтому reviewer не может обнулить leakage.

Private input JSONL содержит `case_id`, `category`, `chat_id`, `query`,
`expected_source_ids`, `forbidden_source_ids`, `expected_abstain` и
`exclude_chat_message_id`. Source keys имеют только формы
`message:<positive-id>` и `card:<canonical-lowercase-uuid>`. Запускать на exact
release commit через живые OpenAI embedding → hybrid retrieval → DeepSeek V4
Flash synthesis. Runner не резервирует пользовательскую quota, но каждый
provider call обязан иметь durable ledger row:

```bash
umask 077
RELEASE_GIT_SHA='<40-character-release-git-sha>'
python -m scripts.run_semantic_qa_eval \
  --input /run/secrets/semantic-eval-private.jsonl \
  --cases-output /run/secrets/semantic-eval-cases.jsonl \
  --observations-output /run/secrets/semantic-eval-observations.jsonl \
  --review-output /run/secrets/semantic-eval-review.jsonl \
  --release-sha "$RELEASE_GIT_SHA"
```

`semantic-eval-review.jsonl` содержит raw query/answer/snippets и остаётся
только на operator host с mode `0600`; его запрещено логировать, прикладывать к
CI или копировать в issue. Reviewer заполняет только link/claim annotations в
`reviewed_result`. Runner-issued `semantic-eval-observations.jsonl` содержит
только immutable objective fields (FTS/hybrid IDs, abstention, leakage и
latency), не содержит raw text и после запуска не редактируется. После review
создать `semantic-eval-results.jsonl`: header с `record_type=header`,
`schema_version=1`, `contains_raw_text=false`, exact `release_sha`,
`dataset_sha256`, SHA-256 observations-файла в `observations_sha256` и
`case_count`; остальные строки содержат только `case_id`,
`valid_source_links`, `invalid_source_links`, `unsupported_claims`,
`total_claims`. Objective поля в reviewer sidecar запрещены. Evaluator
fail-closed отклонит results другого release/dataset/observations, изменённый
objective artifact, отсутствующий header или несовпадающий case count. Затем
построить sanitized report, привязанный к hashes всех трёх артефактов:

```bash
python -m scripts.evaluate_semantic_qa evaluate \
  --dataset /run/secrets/semantic-eval-private.jsonl \
  --cases /run/secrets/semantic-eval-cases.jsonl \
  --observations /run/secrets/semantic-eval-observations.jsonl \
  --results /run/secrets/semantic-eval-results.jsonl \
  --release-sha "$RELEASE_GIT_SHA" \
  --report /tmp/semantic-eval-<UTC>.json
```

Успех — exit code `0`, `status=pass`, `case_count >= 50` и пустой
`violations`. Exit code `1` означает blocking gate; `2` — невалидный или
неполный input. Report mode `0600`, `contains_raw_text=false`.

Передать exact sanitized report как repository secret и запустить manual-only
workflow на том же commit (daily schedule намеренно отсутствует):

```bash
gh secret set SEMANTIC_EVAL_REPORT_JSON < /tmp/semantic-eval-<UTC>.json
gh workflow run evals.yml --ref "$RELEASE_GIT_SHA"
gh run watch --exit-status
```

Workflow fail-closed при отсутствующем/невалидном secret, несовпадении
`release_sha` с `GITHUB_SHA`, неверных hashes/schema или любом blocking metric.
Artifact содержит только `contains_raw_text=false` contract и sanitized frozen
report; private input/review ни при каких условиях не загружаются.

## 7. Limited Telegram E2E

Canary включается только для одного заранее выбранного Telegram user ID при
глобальном semantic flag OFF. Handler сначала проверяет global row, затем exact
`scope_type='user'`, `scope_id=<telegram-user-id>`; другие участники остаются на
существующем FTS path.

1. Выбрать период низкой активности и предупредить тестового участника.
2. Зафиксировать global и scoped flags:

```sql
SELECT flag_key, enabled, updated_at
FROM feature_flags
WHERE flag_key IN (
  'memory.qa.llm_synthesis.enabled',
  'memory.qa.semantic.enabled'
)
  AND (
    (scope_type IS NULL AND scope_id IS NULL)
    OR (scope_type='user' AND scope_id='<canary-tg-user-id>')
  );
```

3. Оставить global semantic OFF и включить только canary user:

```sql
UPDATE feature_flags SET enabled=FALSE, updated_at=clock_timestamp()
WHERE flag_key='memory.qa.semantic.enabled'
  AND scope_type IS NULL AND scope_id IS NULL;

INSERT INTO feature_flags
  (flag_key, scope_type, scope_id, enabled, config_json, updated_at)
VALUES
  ('memory.qa.semantic.enabled', 'user', '<canary-tg-user-id>', TRUE, NULL,
   clock_timestamp())
ON CONFLICT (flag_key, scope_type, scope_id)
DO UPDATE SET enabled=EXCLUDED.enabled, updated_at=clock_timestamp();
```

Проверить exact global OFF и scoped ON. `memory.qa.llm_synthesis.enabled` global
должен быть ON: верхний handler gate проверяет его первым.

4. Реальный участник задаёт боту перефразированный вопрос в production
Telegram-группе через mention/reply surface.
5. Ответ должен быть коротким, grounded и содержать хотя бы одну кликабельную
Telegram source-ссылку. Evidence должен относиться только к текущему чату.
6. Выполнить ещё один успешный semantic вопрос, затем третий. Третий должен
показать явно обозначенный обычный FTS/wiki fallback без новых embedding и
DeepSeek ledger rows.
7. Сразу вернуть scoped semantic flag в OFF и сверить audit rows. Технический failure
проверяется отдельно управляемым staging smoke, а не намеренной поломкой
production credentials.

```sql
UPDATE feature_flags
SET enabled=FALSE, updated_at=clock_timestamp()
WHERE flag_key='memory.qa.semantic.enabled'
  AND scope_type='user' AND scope_id='<canary-tg-user-id>';
```

### Provider-failure smoke и точное освобождение quota

Сначала выполнить автоматизированный handler/DB smoke на release commit:

```bash
uv run pytest -q \
  tests/handlers/test_qa_mentions.py::test_semantic_embedding_failure_releases_slot \
  tests/handlers/test_qa_mentions.py::test_semantic_technical_release_precedes_failing_ordinary_fallback \
  tests/services/test_semantic_quota_postgres.py::test_technical_failure_releases_slot_for_reuse
```

Затем только в staging с отдельным bot token и DB включить scoped flag одному
test user и временно задать заведомо невалидный provider credential. До вопроса
снять exact baseline (не aggregate по всему чату):

```sql
SELECT coalesce(max(id), 0) AS before_attempt_id
FROM semantic_qa_attempts
WHERE user_tg_id=<staging-test-user-id>;

SELECT coalesce(max(id), 0) AS before_ledger_id
FROM llm_usage_ledger;
```

Отправить один новый уникальный Telegram question этим user, дождаться fallback,
вернуть рабочий credential и сразу выключить scoped flag. Для ID строго выше
зафиксированных baseline ожидается ровно один released attempt, ни одного
active/consumed slot и provider failure в ledger:

```sql
SELECT id, status, outcome, embedding_llm_call_id, synthesis_llm_call_id
FROM semantic_qa_attempts
WHERE user_tg_id=<staging-test-user-id> AND id>:before_attempt_id
ORDER BY id;

SELECT id, call_type, error, cost_usd
FROM llm_usage_ledger
WHERE id>:before_ledger_id
ORDER BY id;
```

Pass: новая attempt имеет `status='released'`,
`outcome='technical_failure'`; exact provider ledger ID связан через
`embedding_llm_call_id` или `synthesis_llm_call_id`; повторный вопрос после
восстановления provider получает доступный slot. Raw query/answer в evidence не
копировать.

## 8. Quota, cost и latency audit

Два слота считаются по календарным суткам `Europe/Moscow`. Проверка последних
семи дней:

```sql
SELECT local_day, user_tg_id,
       count(*) FILTER (WHERE status IN ('reserved','consumed')) AS active_slots,
       count(*) FILTER (WHERE status='consumed' AND outcome='answered') AS answered,
       count(*) FILTER (WHERE status='consumed' AND outcome='abstained') AS abstained,
       count(*) FILTER (WHERE status='released' AND outcome='technical_failure') AS released,
       count(*) FILTER (WHERE status='denied' AND outcome='quota_denied') AS denied
FROM semantic_qa_attempts
WHERE local_day >= (now() AT TIME ZONE 'Europe/Moscow')::date - 7
GROUP BY local_day, user_tg_id
ORDER BY local_day DESC, user_tg_id;
```

Stop condition: `active_slots > 2`. У denied rows оба provider FK должны быть
NULL:

```sql
SELECT count(*) AS denied_with_provider_call
FROM semantic_qa_attempts
WHERE status='denied'
  AND (embedding_llm_call_id IS NOT NULL OR synthesis_llm_call_id IS NOT NULL);
```

Ожидается `0`. Embedding и synthesis usage/cost:

```sql
SELECT (created_at AT TIME ZONE 'Europe/Moscow')::date AS msk_day,
       call_type,
       count(*) AS calls,
       sum(tokens_in) AS tokens_in,
       round(sum(cost_usd), 6) AS cost_usd,
       count(*) FILTER (WHERE error IS NOT NULL) AS errors
FROM llm_usage_ledger
WHERE call_type IN ('semantic_embedding','qa_synthesis')
  AND created_at >= now() - interval '7 days'
GROUP BY msk_day, call_type
ORDER BY msk_day DESC, call_type;
```

Каждый durable provider call сначала создаёт ledger row с
`error='reserved_in_flight'`, а terminal update атомарно заменяет marker на
`NULL` или фактический provider error. До HTTP-вызова тот же commit создаёт
`semantic_retrieval_units.embedding_status='reserved'` и полный provenance;
terminal commit одновременно переводит claim в `completed` с vector и точным
`chunk_text` либо в `failed` без vector/source text. Поэтому failed/budget calls
участвуют в forget cascade, а потерянный terminal commit не вызывает повторную
оплату. Зависший marker старше 15 минут означает
неоднозначный исход (например, crash после отправки запроса) и требует ручного
расследования; повторять provider call или автоматически обнулять marker нельзя:

```sql
SELECT id, call_type, provider, model, created_at, cost_usd
FROM llm_usage_ledger
WHERE error='reserved_in_flight'
  AND created_at < now() - interval '15 minutes'
ORDER BY created_at;
```

Все unresolved claims диагностировать через ledger timestamp: `indexed_at` по
state contract равен `NULL`, а отдельного created timestamp у unit нет.

```sql
SELECT u.id AS unit_id, u.embedding_status, u.llm_usage_ledger_id,
       l.error, l.created_at, l.cost_usd
FROM semantic_retrieval_units u
JOIN llm_usage_ledger l ON l.id=u.llm_usage_ledger_id
WHERE u.embedding_status IN ('reserved','failed')
ORDER BY l.created_at, u.id;
```

Stop condition: любой такой row до canary/global enable. В evidence сохранять
только ledger ID и технические поля из запроса выше, без пользовательского
контента.

Автоматического reset/retry нет. После расследования operator отдельно решает,
допустима ли возможная повторная оплата `reserved` call. Для явного repair сначала
редактировать hashes старого ledger, затем удалить только выбранный claim; cost,
tokens, latency и error остаются audit evidence. Выполнять в одной транзакции,
проверив `unit_id` и `llm_usage_ledger_id` вручную:

```sql
BEGIN;
DO $$
BEGIN
  PERFORM 1
  FROM semantic_retrieval_units u
  JOIN llm_usage_ledger l ON l.id=u.llm_usage_ledger_id
  WHERE u.id=<reviewed-unit-id>
    AND u.llm_usage_ledger_id=<reviewed-ledger-id>
    AND u.embedding_status IN ('reserved','failed')
  FOR UPDATE OF u, l;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'reviewed semantic claim/ledger pair does not match';
  END IF;

  UPDATE llm_usage_ledger
  SET prompt_hash=NULL, response_hash=NULL
  WHERE id=<reviewed-ledger-id>;

  DELETE FROM semantic_retrieval_units
  WHERE id=<reviewed-unit-id>
    AND llm_usage_ledger_id=<reviewed-ledger-id>
    AND embedding_status IN ('reserved','failed');
  IF NOT FOUND THEN
    RAISE EXCEPTION 'reviewed semantic claim was not deleted';
  END IF;
END $$;
COMMIT;
```

После repair сначала запустить read-only coverage audit: он должен показать
`unresolved_claims=0` и ровно ожидаемые missing identities. Затем rerun backfill
является явным новым provider attempt; после него coverage обязан вернуться к
100%. Массовое удаление claims или reset статуса запрещены.

Retrieval и full-attempt p95:

```sql
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY total_latency_ms)
       AS retrieval_p95_ms
FROM semantic_retrieval_traces
WHERE created_at >= now() - interval '24 hours';

SELECT percentile_cont(0.95) WITHIN GROUP (
         ORDER BY extract(epoch FROM (finalized_at - reserved_at)) * 1000
       ) AS full_attempt_p95_ms
FROM semantic_qa_attempts
WHERE finalized_at IS NOT NULL
  AND status='consumed'
  AND reserved_at >= now() - interval '24 hours';
```

Gates: retrieval p95 ≤ `2000 ms`, full attempt p95 ≤ `15000 ms`. HNSW можно
проектировать только после зафиксированного нарушения retrieval gate на
production-like dataset; он не является автоматическим fallback.

Следить за structured events (они не должны содержать query/source text):

- `semantic_index_tick_completed` — каждые 15 минут после enable; нормальный
  steady state имеет `indexed=0` и `failed=0` между edit/image/card changes;
- `semantic_index_tick_failed` и `semantic_index_tick_session_failed` — stop
  signal для freshness индекса;
- `semantic_index_database_failed` — DB failure ручного backfill/reindex;
- `semantic_qa_embedding_failed`, `semantic_qa_database_failed`,
  `semantic_qa_synthesis_config_failed` — technical failure; соответствующий
  quota slot должен перейти в `released`.

Отсутствие успешного `semantic_index_tick_completed` дольше 30 минут при
включённом flag — alert: выключить semantic path до восстановления freshness.

## 9. Privacy/leakage audit

Активные units не должны ссылаться на stale, non-normal, redacted или bot/
unknown-authored sources:

```sql
SELECT count(*) AS invalid_active_units
FROM semantic_retrieval_units u
WHERE u.invalidated_at IS NULL
  AND u.embedding_status='completed'
  AND EXISTS (
    SELECT 1
    FROM semantic_retrieval_unit_sources s
    JOIN message_versions mv ON mv.id=s.message_version_id
    JOIN chat_messages cm ON cm.id=mv.chat_message_id
    LEFT JOIN users author ON author.id=cm.user_id
    WHERE s.unit_id=u.id
      AND (
        cm.chat_id <> u.chat_id
        OR cm.current_version_id IS DISTINCT FROM mv.id
        OR cm.memory_policy <> 'normal'
        OR coalesce(cm.message_kind, 'text') IN ('voice','audio')
        OR cm.is_redacted
        OR mv.is_redacted
        OR author.is_bot IS DISTINCT FROM FALSE
      )
  );
```

Ожидается `0`. Отдельно проверить active units, попавшие под durable forget
tombstone:

```sql
SELECT count(DISTINCT u.id) AS forgotten_active_units
FROM semantic_retrieval_units u
JOIN semantic_retrieval_unit_sources s ON s.unit_id=u.id
JOIN message_versions mv ON mv.id=s.message_version_id
JOIN chat_messages cm ON cm.id=mv.chat_message_id
JOIN forget_events fe
  ON (
    (fe.target_type='message' AND fe.target_id=cm.id::text)
    OR (fe.target_type='user' AND fe.target_id=cm.user_id::text)
    OR (fe.target_type='message_hash' AND fe.target_id=mv.content_hash)
  )
WHERE u.invalidated_at IS NULL
  AND u.embedding_status='completed'
  AND fe.status IN ('pending','processing','completed');
```

Ожидается `0`. Pending/processing/completed forget должен блокироваться read-side
немедленно; дополнительно выполнить binding leakage/forget tests release commit.
При любом leakage signal: semantic flag OFF, сохранить только no-content IDs/
hashes для расследования, raw source text не копировать в issue.

## 10. Enable для всех участников

Разрешено только после:

- restore drill и migration round-trip PASS;
- backfill coverage 100%, повторный backfill idempotent;
- frozen eval и blocking leakage/citation/abstention gates PASS;
- limited production E2E PASS;
- quota/cost audit и p95 gates PASS.

Включить global flag отдельной exact-командой и оставить ON:

```sql
UPDATE feature_flags
SET enabled=TRUE, updated_at=clock_timestamp()
WHERE flag_key='memory.qa.semantic.enabled'
  AND scope_type IS NULL AND scope_id IS NULL;
```

Проверить, что затронута ровно одна строка; scoped canary row оставить OFF.
Первые 24 часа
проверять каждые 2 часа: attempts/denials/releases, embedding+synthesis spend,
empty/abstention rate, retrieval/full p95, provider errors и leakage query.

## 11. Rollback

### Обычный rollback (предпочтительный)

1. Немедленно поставить `memory.qa.semantic.enabled=FALSE`.
2. Проверить, что новые `semantic_qa_attempts` перестали появляться.
3. Оставить PG15+pgvector image и migration `089` на месте: схема аддитивна,
   а выключенный feature flag возвращает existing FTS Q&A path.
4. При необходимости откатить bot/web на предыдущий immutable image только
   после flag-off.

Это не удаляет audit/index data и допускает безопасное повторное включение.

### Migration downgrade

`alembic downgrade 088` допустим только до любых semantic attempts/traces и
`semantic_embedding` ledger rows. Guard migration `089` обязан отказать после
первого backfill/shadow/live вызова. Не обходить guard ручным DELETE.

После появления semantic audit data DB rollback выполняется только полным
restore pre-migration backup в отдельный clean volume с оценкой потери всех
записей после backup. Перед destructive restore нужен отдельный operator
sign-off. Старый `postgres:15-alpine` image нельзя запускать на production
volume с установленным `vector`: для image rollback восстановить pre-migration
dump в чистую original-image DB.

Полная restore-процедура описана в `docs/ops/db-backup-runbook.md`.

## 12. Evidence для issue #404

В итоговый комментарий приложить без raw content:

- release commit/image digest, backup filename/size/SHA-256;
- restore drill и `089 → 088 → 089` evidence;
- `vector` version и exact cosine smoke;
- оба backfill report: первый и idempotent rerun;
- coverage/shadow/frozen-eval summary по каждому threshold;
- migration/test suite commands и counts;
- Telegram E2E message/source IDs, но не текст вопроса/ответа;
- quota, cost и latency aggregate queries;
- timestamps limited rollout, final enable и rollback flag verification.

Issue закрывается только после merge, production E2E и финального global enable.
