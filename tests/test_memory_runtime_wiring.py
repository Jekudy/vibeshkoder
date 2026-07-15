"""Production wiring contract for the memory rollout.

These tests intentionally inspect the executable bot entry point instead of
repeating the desired router/job list in documentation.  They protect the
flag-off deploy: shipping the code must not silently enable a memory surface.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Dispatcher

from tests.conftest import import_module

pytestmark = pytest.mark.usefixtures("app_env")

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "bot" / "__main__.py"
SCHEDULER_PATH = REPO_ROOT / "bot" / "services" / "scheduler.py"
RUNTIME_LOCK_PATH = REPO_ROOT / "requirements.lock"


def _function_source(path: Path, function_name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.unparse(node)
    raise AssertionError(f"function {function_name!r} not found in {path}")


def _patch_wiki_compile_job(
    monkeypatch,
    scheduler_module,
    *,
    sessions: list[AsyncMock],
    flag_values: list[bool],
    compile_side_effect: object,
    export_result: object | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    session_iter = iter(sessions)

    @asynccontextmanager
    async def session_context():
        yield next(session_iter)

    monkeypatch.setattr(scheduler_module, "async_session", session_context)
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(side_effect=flag_values),
    )
    monkeypatch.setattr(scheduler_module, "_load_automation_actor_user_id", lambda: 42)
    monkeypatch.setattr(scheduler_module, "_require_automation_actor", AsyncMock())
    monkeypatch.setattr(
        scheduler_module,
        "load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek")),
    )
    monkeypatch.setattr(scheduler_module, "resolve_provider", Mock(return_value=object()))
    compile_topics = AsyncMock(side_effect=compile_side_effect)
    export = AsyncMock(return_value=export_result)
    monkeypatch.setattr(
        "bot.services.wiki_orchestrator.compile_changed_topics",
        compile_topics,
    )
    monkeypatch.setattr("bot.services.wiki_orchestrator.export_static_wiki", export)
    return compile_topics, export


def test_memory_middlewares_are_registered_in_runtime_order() -> None:
    """DB must wrap raw, raw must wrap normalized, all before routers run."""

    main_module = import_module("bot.__main__")
    dispatcher = Dispatcher()

    main_module._register_update_middlewares(dispatcher)

    registered = [
        type(middleware).__name__ for middleware in dispatcher.update.middleware._middlewares
    ]
    assert registered == [
        "DbSessionMiddleware",
        "RawUpdatePersistenceMiddleware",
        "NormalizedMemoryPersistenceMiddleware",
    ]

    main_source = _function_source(MAIN_PATH, "main")
    assert main_source.index("_register_update_middlewares(dp)") < main_source.index(
        "dp.include_routers("
    )


def test_runtime_lock_contains_static_wiki_dependencies() -> None:
    """Docker installs this lock with --no-deps, so transitive pins are explicit."""

    packages = {
        line.split("==", 1)[0]
        for line in RUNTIME_LOCK_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assert {"bleach", "markdown-it-py", "mdurl", "webencodings"} <= packages


def test_recall_command_is_not_publicly_registered() -> None:
    """Member Q&A is mention/reply-only; the legacy command is not a route."""

    qa = import_module("bot.handlers.qa")
    callbacks = [handler.callback for handler in qa.router.message.handlers]

    assert qa.recall_handler not in callbacks
    assert callbacks.count(qa.mention_question_handler) == 1


def test_forget_routers_are_not_imported_or_registered() -> None:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))

    imported_handlers = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "bot.handlers"
        for alias in node.names
    }
    registered_routers = {
        arg.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"include_router", "include_routers"}
        for arg in node.args
        if isinstance(arg, ast.Attribute)
        and arg.attr == "router"
        and isinstance(arg.value, ast.Name)
    }

    assert {"forget_me", "forget_reply"}.isdisjoint(imported_handlers)
    assert {"forget_me", "forget_reply"}.isdisjoint(registered_routers)


def test_forget_cascade_worker_is_not_registered(monkeypatch) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    registered: list[tuple[object, str | None]] = []

    monkeypatch.setattr(
        scheduler_module.scheduler,
        "add_job",
        lambda function, _trigger, **kwargs: registered.append((function, kwargs.get("id"))),
    )
    monkeypatch.setattr(scheduler_module.scheduler, "start", lambda: None)

    scheduler_module.start_scheduler(object())

    assert "forget_cascade_worker" not in {job_id for _, job_id in registered}
    assert not any(
        getattr(function, "__name__", "").startswith("cascade_worker") for function, _ in registered
    )

    scheduler_tree = ast.parse(SCHEDULER_PATH.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(scheduler_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "bot.services.forget_cascade" not in imported_modules


def test_memory_feature_flag_keys_are_stable() -> None:
    ingestion = import_module("bot.services.ingestion")
    scheduler_module = import_module("bot.services.scheduler")
    qa = import_module("bot.handlers.qa")
    extractor = import_module("bot.services.extractor")
    wiki = import_module("bot.handlers.wiki")

    assert ingestion.RAW_ARCHIVE_FLAG == "memory.ingestion.raw_updates.enabled"
    assert qa.QA_FEATURE_FLAG == "memory.qa.enabled"
    assert scheduler_module.PHOTO_DESCRIPTION_FEATURE_FLAG == "memory.images.description.enabled"
    assert scheduler_module.AUTO_PROMOTION_FEATURE_FLAG == "memory.candidates.auto_promote.enabled"
    assert scheduler_module.WIKI_COMPILER_FEATURE_FLAG == "memory.wiki.compiler.enabled"
    assert scheduler_module.WIKI_STATIC_PUBLISH_FEATURE_FLAG == "memory.wiki.static_publish.enabled"
    assert (
        extractor.MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG == "memory.extraction.scheduler.enabled"
    )
    assert wiki._FEATURE_FLAG == "memory.wiki.enabled"


@pytest.mark.parametrize(
    ("relative_path", "function_name", "flag_reference"),
    [
        ("bot/services/ingestion.py", "is_raw_archive_enabled", "RAW_ARCHIVE_FLAG"),
        (
            "bot/services/scheduler.py",
            "digest_daily_job",
            "memory.digests.daily.enabled",
        ),
        (
            "bot/services/scheduler.py",
            "digest_weekly_job",
            "memory.digests.weekly.enabled",
        ),
        (
            "bot/services/scheduler.py",
            "photo_description_worker_job",
            "PHOTO_DESCRIPTION_FEATURE_FLAG",
        ),
        ("bot/handlers/qa.py", "recall_handler", "QA_FEATURE_FLAG"),
        ("bot/handlers/qa.py", "mention_question_handler", "QA_FEATURE_FLAG"),
        (
            "bot/services/extractor.py",
            "extraction_scheduler_tick",
            "MEMORY_EXTRACTION_SCHEDULER_ENABLED_FLAG",
        ),
        (
            "bot/services/scheduler.py",
            "wiki_automation_job",
            "WIKI_COMPILER_FEATURE_FLAG",
        ),
        ("bot/handlers/wiki.py", "cmd_wiki_publish", "_FEATURE_FLAG"),
        ("bot/handlers/wiki.py", "cmd_wiki_unpublish", "_FEATURE_FLAG"),
        ("bot/handlers/wiki.py", "cmd_wiki_robots", "_FEATURE_FLAG"),
    ],
)
def test_memory_surfaces_keep_explicit_feature_flag_gate(
    relative_path: str,
    function_name: str,
    flag_reference: str,
) -> None:
    function_source = _function_source(REPO_ROOT / relative_path, function_name)

    assert "FeatureFlagRepo.get" in function_source
    assert flag_reference in function_source


def test_runtime_code_does_not_enable_memory_flags() -> None:
    """Flag changes remain an operator action, never a deploy side effect."""

    mutation_markers = (
        "FeatureFlagRepo.set_enabled",
        "INSERT INTO feature_flags",
        "UPDATE feature_flags",
    )
    mutating_source_parts: list[str] = []
    for path in (REPO_ROOT / "bot").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(marker in source for marker in mutation_markers):
            mutating_source_parts.append(source)
    mutating_sources = "\n".join(mutating_source_parts)
    for rollout_flag in (
        "memory.ingestion.raw_updates.enabled",
        "memory.digests.daily.enabled",
        "memory.digests.weekly.enabled",
        "memory.qa.enabled",
        "memory.images.description.enabled",
        "memory.extraction.scheduler.enabled",
        "memory.wiki.enabled",
        "memory.candidates.auto_promote.enabled",
        "memory.wiki.compiler.enabled",
        "memory.wiki.static_publish.enabled",
    ):
        assert rollout_flag not in mutating_sources


async def test_daily_digest_runtime_is_noop_while_flag_is_off(monkeypatch) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    session = AsyncMock()

    @asynccontextmanager
    async def session_context():
        yield session

    flag_get = AsyncMock(return_value=False)
    config_load = Mock(side_effect=AssertionError("gateway must stay behind flag"))
    monkeypatch.setattr(scheduler_module, "async_session", session_context)
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        flag_get,
    )
    monkeypatch.setattr(scheduler_module, "load_gateway_config", config_load)

    await scheduler_module.digest_daily_job(object())

    flag_get.assert_awaited_once_with(session, "memory.digests.daily.enabled")
    config_load.assert_not_called()
    session.commit.assert_not_awaited()


async def test_wiki_runtime_is_noop_while_flag_is_off(monkeypatch) -> None:
    wiki = import_module("bot.handlers.wiki")
    session = AsyncMock()
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=149820031),
        answer=AsyncMock(),
    )
    command = SimpleNamespace(args="memory-overview")
    flag_get = AsyncMock(return_value=False)
    monkeypatch.setattr(wiki.FeatureFlagRepo, "get", flag_get)

    await wiki.cmd_wiki_publish(message, session, command)

    flag_get.assert_awaited_once_with(session, wiki._FEATURE_FLAG)
    message.answer.assert_awaited_once_with("Wiki временно недоступна.")
    session.execute.assert_not_awaited()


async def test_wiki_automation_is_strict_noop_while_master_flag_is_off(
    monkeypatch,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    session = AsyncMock()

    @asynccontextmanager
    async def session_context():
        yield session

    flag_get = AsyncMock(return_value=False)
    forbidden_config_load = Mock(side_effect=AssertionError("config must stay behind flag"))
    monkeypatch.setattr(scheduler_module, "async_session", session_context)
    monkeypatch.setattr("bot.db.repos.feature_flag.FeatureFlagRepo.get", flag_get)
    monkeypatch.setattr(
        "bot.services.wiki_runtime.load_wiki_runtime_config",
        forbidden_config_load,
    )

    await scheduler_module.wiki_automation_job()

    flag_get.assert_awaited_once_with(session, scheduler_module.WIKI_COMPILER_FEATURE_FLAG)
    forbidden_config_load.assert_not_called()
    session.commit.assert_not_awaited()


async def test_wiki_automation_runs_promote_compile_export_publish_pipeline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    sessions = [AsyncMock(), AsyncMock(), AsyncMock()]
    session_index = 0

    @asynccontextmanager
    async def session_context():
        nonlocal session_index
        session = sessions[session_index]
        session_index += 1
        yield session

    flag_get = AsyncMock(side_effect=[True, True, True, True, True, True, True])
    require_actor = AsyncMock()
    promote = AsyncMock(return_value=[])
    compile_topics = AsyncMock(
        return_value=SimpleNamespace(
            topics_seen=2,
            compiled_topics=1,
            remaining_changed_topics=0,
        )
    )
    generation_dir = tmp_path / "generation"
    export = AsyncMock(
        return_value=SimpleNamespace(
            generation_dir=generation_dir,
            manifest_sha256="a" * 64,
            page_count=2,
        )
    )
    publish = AsyncMock(return_value=SimpleNamespace(status="succeeded"))
    runtime_config = SimpleNamespace(
        publish_dir=tmp_path / "current",
        site_title="Shkoder Wiki",
        forbidden_origins=("187.77.98.73",),
    )
    gateway_config = SimpleNamespace(provider="deepseek")

    monkeypatch.setattr(scheduler_module, "async_session", session_context)
    monkeypatch.setattr("bot.db.repos.feature_flag.FeatureFlagRepo.get", flag_get)
    monkeypatch.setattr(scheduler_module, "_load_automation_actor_user_id", lambda: 42)
    monkeypatch.setattr(scheduler_module, "_require_automation_actor", require_actor)
    monkeypatch.setattr(
        "bot.services.wiki_runtime.load_wiki_runtime_config",
        Mock(return_value=runtime_config),
    )
    monkeypatch.setattr(
        "bot.services.candidate_promotion.promote_pending_candidates",
        promote,
    )
    gateway_config_load = Mock(return_value=gateway_config)
    provider_resolve = Mock(return_value=object())
    monkeypatch.setattr(scheduler_module, "load_gateway_config", gateway_config_load)
    monkeypatch.setattr(scheduler_module, "resolve_provider", provider_resolve)
    monkeypatch.setattr("bot.services.wiki_orchestrator.compile_changed_topics", compile_topics)
    monkeypatch.setattr("bot.services.wiki_orchestrator.export_static_wiki", export)
    monkeypatch.setattr("bot.services.cloudflare_pages.publish_static_generation", publish)

    await scheduler_module.wiki_automation_job()

    gateway_config_load.assert_called_once_with(prompt_template_version="wiki-revision-v0.1.0")
    provider_resolve.assert_called_once_with(
        "deepseek",
        deepseek_max_tokens=scheduler_module.DEEPSEEK_WIKI_MAX_TOKENS,
        deepseek_json_output=True,
    )
    require_actor.assert_awaited_once_with(sessions[0], 42)
    promote.assert_awaited_once_with(sessions[0], actor_user_id=42, limit=100)
    sessions[0].commit.assert_awaited_once()
    compile_topics.assert_awaited_once_with(
        sessions[1],
        actor_user_id=42,
        gateway=compile_topics.await_args.kwargs["gateway"],
        publication_authorized=True,
        source_chat_id=scheduler_module.settings.COMMUNITY_CHAT_ID,
        max_topics=1,
    )
    sessions[1].commit.assert_awaited_once()
    export.assert_awaited_once_with(
        sessions[2],
        publish_dir=runtime_config.publish_dir,
        site_title="Shkoder Wiki",
        forbidden_origins=("187.77.98.73",),
        publication_authorized=True,
        source_chat_id=scheduler_module.settings.COMMUNITY_CHAT_ID,
    )
    publish.assert_awaited_once_with(
        generation_dir,
        expected_manifest_sha256="a" * 64,
        forbidden_origins=("187.77.98.73",),
    )
    sessions[2].commit.assert_awaited_once()


async def test_wiki_automation_publishes_empty_generation_to_remove_stale_last_page(
    monkeypatch,
    tmp_path: Path,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    sessions = [AsyncMock(), AsyncMock(), AsyncMock()]
    session_index = 0

    @asynccontextmanager
    async def session_context():
        nonlocal session_index
        session = sessions[session_index]
        session_index += 1
        yield session

    monkeypatch.setattr(scheduler_module, "async_session", session_context)
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(side_effect=[True, False, True, True, True, True, True]),
    )
    monkeypatch.setattr(scheduler_module, "_load_automation_actor_user_id", lambda: 42)
    monkeypatch.setattr(scheduler_module, "_require_automation_actor", AsyncMock())
    monkeypatch.setattr(
        scheduler_module,
        "load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek")),
    )
    monkeypatch.setattr(scheduler_module, "resolve_provider", Mock(return_value=object()))
    monkeypatch.setattr(
        "bot.services.wiki_orchestrator.compile_changed_topics",
        AsyncMock(
            return_value=SimpleNamespace(
                topics_seen=0,
                compiled_topics=0,
                remaining_changed_topics=0,
            )
        ),
    )
    monkeypatch.setattr(
        "bot.services.wiki_runtime.load_wiki_runtime_config",
        Mock(
            return_value=SimpleNamespace(
                publish_dir=tmp_path / "current",
                site_title="Shkoder Wiki",
                forbidden_origins=("187.77.98.73",),
            )
        ),
    )
    monkeypatch.setattr(
        "bot.services.wiki_orchestrator.export_static_wiki",
        AsyncMock(
            return_value=SimpleNamespace(
                generation_dir=tmp_path / "generation",
                manifest_sha256="b" * 64,
                page_count=0,
            )
        ),
    )
    publish = AsyncMock()
    monkeypatch.setattr("bot.services.cloudflare_pages.publish_static_generation", publish)

    await scheduler_module.wiki_automation_job()

    publish.assert_awaited_once_with(
        tmp_path / "generation",
        expected_manifest_sha256="b" * 64,
        forbidden_origins=("187.77.98.73",),
    )
    sessions[2].commit.assert_awaited_once()


async def test_wiki_topics_commit_individually_and_provider_errors_are_sanitized(
    monkeypatch,
    caplog,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    sessions = [AsyncMock(), AsyncMock(), AsyncMock()]
    session_index = 0

    @asynccontextmanager
    async def session_context():
        nonlocal session_index
        session = sessions[session_index]
        session_index += 1
        yield session

    monkeypatch.setattr(scheduler_module, "async_session", session_context)
    monkeypatch.setattr(
        "bot.db.repos.feature_flag.FeatureFlagRepo.get",
        AsyncMock(side_effect=[True, False, True, True]),
    )
    monkeypatch.setattr(scheduler_module, "_load_automation_actor_user_id", lambda: 42)
    monkeypatch.setattr(scheduler_module, "_require_automation_actor", AsyncMock())
    monkeypatch.setattr(
        scheduler_module,
        "load_gateway_config",
        Mock(return_value=SimpleNamespace(provider="deepseek")),
    )
    monkeypatch.setattr(scheduler_module, "resolve_provider", Mock(return_value=object()))
    compile_topics = AsyncMock(
        side_effect=[
            SimpleNamespace(
                topics_seen=2,
                compiled_topics=1,
                remaining_changed_topics=1,
            ),
            RuntimeError("sentinel-secret-provider-response"),
        ]
    )
    monkeypatch.setattr(
        "bot.services.wiki_orchestrator.compile_changed_topics",
        compile_topics,
    )

    with pytest.raises(
        scheduler_module.WikiAutomationJobError,
        match="RuntimeError",
    ) as caught:
        await scheduler_module.wiki_automation_job()

    sessions[1].commit.assert_awaited_once()
    sessions[2].commit.assert_not_awaited()
    assert "sentinel-secret" not in str(caught.value)
    assert "sentinel-secret" not in caplog.text


async def test_wiki_response_contract_failure_retries_once_in_fresh_session(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    gateway_module = import_module("bot.services.llm_gateway")
    caplog.set_level("INFO", logger="bot.services.scheduler")
    sessions = [AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()]
    export_result = SimpleNamespace(
        generation_dir=tmp_path / "generation",
        manifest_sha256="c" * 64,
        page_count=1,
    )
    compile_topics, export = _patch_wiki_compile_job(
        monkeypatch,
        scheduler_module,
        sessions=sessions,
        flag_values=[True, False, True, True, True, False],
        compile_side_effect=[
            gateway_module.WikiGatewayResponseContractError(
                "sentinel-secret-provider-response",
                llm_usage_ledger_id=101,
                topic_slug="topic-a",
            ),
            SimpleNamespace(
                topics_seen=1,
                compiled_topics=1,
                remaining_changed_topics=0,
            ),
        ],
        export_result=export_result,
    )
    monkeypatch.setattr(
        "bot.services.wiki_runtime.load_wiki_runtime_config",
        Mock(
            return_value=SimpleNamespace(
                publish_dir=tmp_path / "current",
                site_title="Shkoder Wiki",
                forbidden_origins=("187.77.98.73",),
            )
        ),
    )
    publish = AsyncMock()
    monkeypatch.setattr("bot.services.cloudflare_pages.publish_static_generation", publish)

    await scheduler_module.wiki_automation_job()

    assert compile_topics.await_count == 2
    assert compile_topics.await_args_list[0].args[0] is sessions[1]
    assert compile_topics.await_args_list[1].args[0] is sessions[2]
    assert "target_topic_slug" not in compile_topics.await_args_list[0].kwargs
    assert compile_topics.await_args_list[1].kwargs["target_topic_slug"] == "topic-a"
    sessions[1].commit.assert_not_awaited()
    sessions[2].commit.assert_awaited_once()
    export.assert_awaited_once()
    publish.assert_not_awaited()
    assert "sentinel-secret" not in caplog.text
    retry_record = next(
        record
        for record in caplog.records
        if record.message == "wiki_automation_response_contract_retry"
    )
    assert (retry_record.attempt, retry_record.max_attempts, retry_record.failed_ledger_id) == (
        1,
        2,
        101,
    )
    completed_record = next(
        record for record in caplog.records if record.message == "wiki_automation_completed"
    )
    assert (completed_record.compile_attempts, completed_record.contract_retries) == (2, 1)


async def test_wiki_response_contract_retry_exhaustion_is_sanitized_and_does_not_export(
    monkeypatch,
    caplog,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    gateway_module = import_module("bot.services.llm_gateway")
    sessions = [AsyncMock(), AsyncMock(), AsyncMock()]
    compile_topics, export = _patch_wiki_compile_job(
        monkeypatch,
        scheduler_module,
        sessions=sessions,
        flag_values=[True, False, True, True],
        compile_side_effect=[
            gateway_module.WikiGatewayResponseContractError(
                "sentinel-secret-first",
                llm_usage_ledger_id=201,
                topic_slug="topic-a",
            ),
            gateway_module.WikiGatewayResponseContractError(
                "sentinel-secret-second",
                llm_usage_ledger_id=202,
                topic_slug="topic-a",
            ),
        ],
    )

    with pytest.raises(
        scheduler_module.WikiAutomationJobError,
        match="wiki response contract attempts exhausted",
    ) as caught:
        await scheduler_module.wiki_automation_job()

    assert compile_topics.await_count == 2
    assert compile_topics.await_args_list[1].kwargs["target_topic_slug"] == "topic-a"
    export.assert_not_awaited()
    assert "sentinel-secret" not in str(caught.value)
    assert "sentinel-secret" not in caplog.text
    exhausted_record = next(
        record
        for record in caplog.records
        if record.message == "wiki_automation_response_contract_exhausted"
    )
    assert (
        exhausted_record.attempt,
        exhausted_record.max_attempts,
        exhausted_record.failed_ledger_id,
    ) == (2, 2, 202)


@pytest.mark.parametrize(
    "error_name",
    [
        "WikiGatewayContractError",
        "WikiGatewayProviderError",
        "WikiGatewaySourceStaleError",
        "WikiGatewayBudgetExceeded",
        "RuntimeError",
    ],
)
async def test_wiki_non_response_contract_errors_are_not_retried(
    monkeypatch,
    error_name: str,
) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    gateway_module = import_module("bot.services.llm_gateway")
    sessions = [AsyncMock(), AsyncMock()]

    error_cls = (
        RuntimeError if error_name == "RuntimeError" else getattr(gateway_module, error_name)
    )
    error = (
        error_cls("sentinel-secret", llm_usage_ledger_id=301)
        if error_name != "RuntimeError"
        else error_cls("sentinel-secret")
    )
    compile_topics, export = _patch_wiki_compile_job(
        monkeypatch,
        scheduler_module,
        sessions=sessions,
        flag_values=[True, False, True],
        compile_side_effect=error,
    )

    with pytest.raises(scheduler_module.WikiAutomationJobError):
        await scheduler_module.wiki_automation_job()

    compile_topics.assert_awaited_once()
    export.assert_not_awaited()


def test_wiki_automation_is_registered_for_0930_moscow(monkeypatch) -> None:
    scheduler_module = import_module("bot.services.scheduler")
    registered: list[tuple[object, dict[str, object]]] = []

    monkeypatch.setattr(
        scheduler_module.scheduler,
        "add_job",
        lambda function, _trigger, **kwargs: registered.append((function, kwargs)),
    )
    monkeypatch.setattr(scheduler_module.scheduler, "start", lambda: None)

    scheduler_module.start_scheduler(object())

    jobs = [kwargs for _function, kwargs in registered if kwargs.get("id") == "wiki_automation"]
    assert len(jobs) == 1
    assert jobs[0]["hour"] == 9
    assert jobs[0]["minute"] == 30
    assert str(jobs[0]["timezone"]) == "Europe/Moscow"
    assert jobs[0]["max_instances"] == 1
