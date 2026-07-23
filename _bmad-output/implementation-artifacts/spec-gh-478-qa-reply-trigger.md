---
title: 'Bare reply не запускает generic Q&A'
type: 'bugfix'
created: '2026-07-23'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'd92f8da'
context:
  - '{project-root}/docs/memory-system/phase13-rollout-plan.md'
  - '{project-root}/docs/runbooks/semantic-qa.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Generic community Q&A из #411 принимает любой reply на сообщение Шкодера за вопрос. Legacy retrieval сначала сохраняет этот reply, затем может вернуть его же как единственное свидетельство и показать PostgreSQL `<b>`-маркеры буквальным текстом.

**Approach:** По issue #478 сделать generic Q&A mention-only. Bare reply должен пройти в обычный collector без Q&A-побочных эффектов; reply с точным `@username` остаётся валидным mention-trigger. Во всех legacy-поисках исключить сохранённый request и разрешить только human-authored evidence.

## Boundaries & Constraints

**Always:** Сохранять bare reply обычным governed persistence path; не создавать для него Q&A trace, quota reservation или provider call. Сохранить действующие privacy/forget/semantic-Q&A инварианты и совместимость с будущим contextual flow #410. Удалять только управляемые PostgreSQL `<b>`/`</b>` headline-маркеры до truncation и HTML escaping.

**Ask First:** Любое изменение публичной команды `/recall`, квоты 2/day, feature flags, схемы БД, SQL retrieval, Telegram mention-entity contract или routing специализированных intents.

**Never:** Не определять вопрос эвристиками по `?` или словам. Не реализовывать context-anchor retrieval, conversation mode или другие части #410. Не менять defaults `run_qa`, общий search SQL, Phase 4–6 renderer или индексировать bot-authored evidence.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Bare reply | Human отвечает на любое bot-authored сообщение без `@username` | Q&A filter возвращает false; сообщение обрабатывает обычный collector | Бот молчит |
| Reply + mention | Reply содержит точный `@username` и непустой запрос | Generic mention Q&A запускается; `via_reply=True` сохраняется как metadata | Действующие Q&A guardrails |
| Mention without reply | Обычное сообщение содержит точный `@username` | Текущее generic Q&A поведение сохраняется | Действующие Q&A guardrails |
| Legacy retrieval | Mention request уже сохранён в `chat_messages` | Search получает request id в exclusion и `human_only=True` | При независимых evidence нет — честный abstention |
| Highlighted snippet | FTS snippet содержит `<b>слово</b>` | Footer показывает читаемое `слово`, без literal FTS tags | Остальной HTML экранируется |

</frozen-after-approval>

## Code Map

- `bot/services/qa_trigger.py` — единственный trigger/filter; здесь bare reply ошибочно считается Q&A.
- `bot/handlers/qa.py` — request persistence, legacy/semantic routing и bounded source renderer.
- `bot/services/qa.py` — уже передаёт `exclude_chat_message_id` и `human_only` в search; менять не нужно.
- `bot/handlers/chat_messages.py` — нижестоящий collector, который сохраняет не-Q&A reply.
- `tests/services/test_qa_trigger.py` — trigger contract.
- `tests/handlers/test_qa_mentions.py` — legacy retrieval и renderer regressions.
- `docs/memory-system/phase13-rollout-plan.md` — зафиксированный продуктовый контракт Phase 13.

## Tasks & Acceptance

**Execution:**
- [x] `bot/services/qa_trigger.py` — требовать mention для match, сохранив `via_reply` для reply+mention; обновить docstring.
- [x] `bot/handlers/qa.py` — передать persisted request id и `human_only=True` в legacy `run_qa`; удалить headline-маркеры перед безопасным rendering; обновить user-facing hint/docstring.
- [x] `tests/services/test_qa_trigger.py` — заменить bare-reply happy path на rejection и добавить reply+mention regression.
- [x] `tests/handlers/test_qa_mentions.py` — зафиксировать retrieval kwargs и отсутствие literal `<b>` markers.
- [x] `bot/__main__.py`, `tests/test_memory_runtime_wiring.py` — синхронизировать runtime wording с mention-only contract.
- [x] `docs/memory-system/phase13-rollout-plan.md`, `docs/runbooks/semantic-qa.md` — заменить `mention/reply` на explicit mention; reply context требует mention.

**Acceptance Criteria:**
- Given bare reply без mention, when aiogram проверяет Q&A filter, then handler не матчится и не может вызвать trace/quota/provider.
- Given reply с точным mention, when filter извлекает вопрос, then query очищен от mention, `via_mention=True` и `via_reply=True`.
- Given persisted mention request в legacy path, when выполняется retrieval, then request исключён, evidence human-only и self-citation невозможна.
- Given FTS headline с `<b>` markers, when строится bounded footer, then пользователь видит только читаемый текст, а остальной HTML остаётся escaped.
- Given существующие semantic/privacy/forget сценарии, when проходят регрессии, then их поведение не меняется.

## Spec Change Log

## Verification

**Commands:**
- `pytest -q tests/services/test_qa_trigger.py tests/handlers/test_qa_mentions.py tests/test_memory_runtime_wiring.py` — expected: все целевые regression-тесты green.
- `ruff check bot/services/qa_trigger.py bot/handlers/qa.py tests/services/test_qa_trigger.py tests/handlers/test_qa_mentions.py tests/test_memory_runtime_wiring.py` — expected: zero findings.
- `ruff format --check bot/services/qa_trigger.py bot/handlers/qa.py tests/services/test_qa_trigger.py tests/handlers/test_qa_mentions.py tests/test_memory_runtime_wiring.py` — expected: форматирование green.
- `bash scripts/precommit-privacy-allowlist.sh` — expected: privacy allowlist gate green.

## Suggested Review Order

**Trigger contract**

- Mention-only gate rejects bare replies while retaining reply metadata.
  [`qa_trigger.py:100`](../../bot/services/qa_trigger.py#L100)

**Retrieval and rendering safety**

- Persisted request and non-human evidence are excluded at the legacy boundary.
  [`qa.py:2047`](../../bot/handlers/qa.py#L2047)

- PostgreSQL headline markers are removed before truncation and HTML escaping.
  [`qa.py:366`](../../bot/handlers/qa.py#L366)

**Regression evidence**

- Trigger tests cover bare reply and reply-plus-mention behavior.
  [`test_qa_trigger.py:56`](../../tests/services/test_qa_trigger.py#L56)

- Handler tests pin retrieval filters and safe marker rendering.
  [`test_qa_mentions.py:243`](../../tests/handlers/test_qa_mentions.py#L243)

**Product contract**

- Phase 13 now states explicit mention semantics consistently.
  [`phase13-rollout-plan.md:25`](../../docs/memory-system/phase13-rollout-plan.md#L25)
