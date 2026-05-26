"""Unit tests for ButlerEvidenceContext + build_butler_evidence.

T12-02 (Wave 1 Stream Evidence) — tests BEFORE implementation (TDD red phase).

Covers:
  - ButlerEvidenceContext is frozen (FrozenInstanceError on mutation)
  - build_butler_evidence returns an empty bundle for an empty query (no crash)
  - butler_context_hash / context_hash is deterministic over same source set, distinct across different sets
  - snapshot_at is UTC-aware (tzinfo is not None)
  - governance_excluded_count increments when at least one source is filtered
  - ButlerEvidenceContext has canonical fields: visibility_scope, context_hash, governance_filter_version
  - ButlerEvidenceContext has @property accessors: evidence_ids, items
  - butler_context_hash includes visibility_scope and governance_filter_version in hash
  - Vanished row (None sentinel from _fetch_governance_fields) is fail-closed: excluded + count increments
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services.evidence import EvidenceBundle, EvidenceItem

# ---------------------------------------------------------------------------
# Helpers — minimal SearchHitLike factory
# ---------------------------------------------------------------------------

_CHAT_ID = -100_123_456_789


def _make_item(
    mvid: int,
    *,
    source_type: str = "message",
    card_id: uuid.UUID | None = None,
    card_source_mvids: tuple[int, ...] = (),
) -> EvidenceItem:
    return EvidenceItem(
        message_version_id=mvid,
        chat_message_id=mvid + 1000,
        chat_id=_CHAT_ID,
        message_id=mvid + 2000,
        user_id=99,
        snippet="snippet",
        ts_rank=0.5,
        captured_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        message_date=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        source_type=source_type,  # type: ignore[arg-type]
        card_id=card_id,
        card_source_message_version_ids=card_source_mvids,
    )


def _make_bundle(items: tuple[EvidenceItem, ...]) -> EvidenceBundle:
    return EvidenceBundle(
        query="test query",
        chat_id=_CHAT_ID,
        items=items,
        abstained=len(items) == 0,
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# ButlerEvidenceContext dataclass tests — canonical field set per spec §4.2
# ---------------------------------------------------------------------------


def _make_ctx(
    items: tuple[EvidenceItem, ...] = (),
    *,
    visibility_scope: str = "member",
    governance_filter_version: str = "phase12-v1",
) -> "ButlerEvidenceContext":  # noqa: F821
    from bot.services.butler_evidence import ButlerEvidenceContext, butler_context_hash

    bundle = _make_bundle(items)
    return ButlerEvidenceContext(
        bundle=bundle,
        visibility_scope=visibility_scope,  # type: ignore[arg-type]
        context_hash=butler_context_hash(bundle, visibility_scope, governance_filter_version),
        governance_filter_version=governance_filter_version,
        requester_user_id=42,
        chat_id=_CHAT_ID,
        query="test",
        snapshot_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
        governance_excluded_count=0,
    )


class TestButlerEvidenceContextFrozen:
    """ButlerEvidenceContext must be frozen — no mutations after construction."""

    def test_frozen_raises_on_field_assignment(self) -> None:
        ctx = _make_ctx((_make_item(1),))
        with pytest.raises(FrozenInstanceError):
            ctx.governance_excluded_count = 1  # type: ignore[misc]

    def test_frozen_raises_on_bundle_replacement(self) -> None:
        ctx = _make_ctx((_make_item(1),))
        with pytest.raises(FrozenInstanceError):
            ctx.bundle = _make_bundle(())  # type: ignore[misc]

    def test_frozen_raises_on_requester_mutation(self) -> None:
        ctx = _make_ctx()
        with pytest.raises(FrozenInstanceError):
            ctx.requester_user_id = 99  # type: ignore[misc]

    def test_frozen_raises_on_context_hash_mutation(self) -> None:
        ctx = _make_ctx((_make_item(1),))
        with pytest.raises(FrozenInstanceError):
            ctx.context_hash = "new_hash"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Canonical field set — spec §4.2
# ---------------------------------------------------------------------------


class TestCanonicalFieldSet:
    """ButlerEvidenceContext must have the canonical fields from spec §4.2."""

    def test_has_bundle_field(self) -> None:
        ctx = _make_ctx((_make_item(1),))
        assert hasattr(ctx, "bundle")

    def test_has_visibility_scope_field(self) -> None:
        ctx = _make_ctx((_make_item(1),), visibility_scope="admin")
        assert ctx.visibility_scope == "admin"

    def test_has_context_hash_field(self) -> None:
        ctx = _make_ctx((_make_item(1),))
        assert hasattr(ctx, "context_hash")
        assert isinstance(ctx.context_hash, str)
        assert len(ctx.context_hash) == 64  # SHA-256 hex

    def test_has_governance_filter_version_field(self) -> None:
        ctx = _make_ctx((_make_item(1),), governance_filter_version="phase12-v1")
        assert ctx.governance_filter_version == "phase12-v1"

    def test_has_orchestrator_metadata_fields(self) -> None:
        """requester_user_id, chat_id, query, snapshot_at, governance_excluded_count are metadata."""
        ctx = _make_ctx()
        assert hasattr(ctx, "requester_user_id")
        assert hasattr(ctx, "chat_id")
        assert hasattr(ctx, "query")
        assert hasattr(ctx, "snapshot_at")
        assert hasattr(ctx, "governance_excluded_count")

    def test_no_revalidation_token_field(self) -> None:
        """revalidation_token was renamed to context_hash — must not exist."""
        ctx = _make_ctx()
        assert not hasattr(ctx, "revalidation_token")

    def test_evidence_ids_property(self) -> None:
        """evidence_ids property proxies bundle.evidence_ids."""
        items = (_make_item(10), _make_item(20))
        ctx = _make_ctx(items)
        assert ctx.evidence_ids == [10, 20]

    def test_items_property(self) -> None:
        """items property proxies bundle.items."""
        items = (_make_item(10), _make_item(20))
        ctx = _make_ctx(items)
        assert ctx.items == items


# ---------------------------------------------------------------------------
# butler_context_hash canonical helper — spec §3.6 step 1
# ---------------------------------------------------------------------------


class TestButlerContextHash:
    """butler_context_hash must be the ONE canonical hash function per spec §3.6."""

    def test_same_inputs_produce_same_hash(self) -> None:
        from bot.services.butler_evidence import butler_context_hash

        bundle = _make_bundle((_make_item(10), _make_item(20)))
        h1 = butler_context_hash(bundle, "member", "phase12-v1")
        h2 = butler_context_hash(bundle, "member", "phase12-v1")
        assert h1 == h2

    def test_different_visibility_scope_produces_different_hash(self) -> None:
        from bot.services.butler_evidence import butler_context_hash

        bundle = _make_bundle((_make_item(10),))
        h_member = butler_context_hash(bundle, "member", "phase12-v1")
        h_admin = butler_context_hash(bundle, "admin", "phase12-v1")
        assert h_member != h_admin

    def test_different_governance_filter_version_produces_different_hash(self) -> None:
        from bot.services.butler_evidence import butler_context_hash

        bundle = _make_bundle((_make_item(10),))
        h_v1 = butler_context_hash(bundle, "member", "phase12-v1")
        h_v2 = butler_context_hash(bundle, "member", "phase12-v2")
        assert h_v1 != h_v2

    def test_different_items_produce_different_hash(self) -> None:
        from bot.services.butler_evidence import butler_context_hash

        bundle_a = _make_bundle((_make_item(10),))
        bundle_b = _make_bundle((_make_item(20),))
        assert butler_context_hash(bundle_a, "member", "phase12-v1") != butler_context_hash(
            bundle_b, "member", "phase12-v1"
        )

    def test_hash_is_order_independent(self) -> None:
        """Items are sorted — same set in different order produces same hash."""
        from bot.services.butler_evidence import butler_context_hash

        bundle_ab = _make_bundle((_make_item(10), _make_item(20)))
        bundle_ba = _make_bundle((_make_item(20), _make_item(10)))
        assert butler_context_hash(bundle_ab, "member", "phase12-v1") == butler_context_hash(
            bundle_ba, "member", "phase12-v1"
        )

    def test_card_item_hash_distinct_from_message_item(self) -> None:
        """Card identity preserved — same anchor mvid but different source_type must differ."""
        from bot.services.butler_evidence import butler_context_hash

        card_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        bundle_msg = _make_bundle((_make_item(10, source_type="message"),))
        bundle_card = _make_bundle(
            (
                _make_item(
                    10,
                    source_type="card",
                    card_id=card_id,
                    card_source_mvids=(10, 11),
                ),
            )
        )
        assert butler_context_hash(bundle_msg, "member", "phase12-v1") != butler_context_hash(
            bundle_card, "member", "phase12-v1"
        )

    def test_returns_sha256_hex_string(self) -> None:
        from bot.services.butler_evidence import butler_context_hash

        bundle = _make_bundle(())
        h = butler_context_hash(bundle, "member", "phase12-v1")
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_context_hash_field_matches_butler_context_hash(self) -> None:
        """ButlerEvidenceContext.context_hash must equal butler_context_hash(bundle, ...)."""
        from bot.services.butler_evidence import butler_context_hash

        items = (_make_item(10), _make_item(20))
        ctx = _make_ctx(items, visibility_scope="admin", governance_filter_version="phase12-v1")
        expected = butler_context_hash(ctx.bundle, "admin", "phase12-v1")
        assert ctx.context_hash == expected


# ---------------------------------------------------------------------------
# _compute_context_hash internal helper (renamed from _compute_revalidation_token)
# ---------------------------------------------------------------------------


class TestComputeContextHash:
    """_compute_context_hash is the internal items-only hash (no visibility_scope)."""

    def test_same_source_set_produces_same_hash(self) -> None:
        from bot.services.butler_evidence import _compute_context_hash

        items = (_make_item(10), _make_item(20))
        assert _compute_context_hash(items) == _compute_context_hash(items)

    def test_different_source_sets_produce_different_hashes(self) -> None:
        from bot.services.butler_evidence import _compute_context_hash

        items_a = (_make_item(10),)
        items_b = (_make_item(20),)
        assert _compute_context_hash(items_a) != _compute_context_hash(items_b)

    def test_hash_is_stable_across_item_order(self) -> None:
        from bot.services.butler_evidence import _compute_context_hash

        items_ab = (_make_item(10), _make_item(20))
        items_ba = (_make_item(20), _make_item(10))
        assert _compute_context_hash(items_ab) == _compute_context_hash(items_ba)

    def test_empty_items_produces_deterministic_hash(self) -> None:
        from bot.services.butler_evidence import _compute_context_hash

        assert _compute_context_hash(()) == _compute_context_hash(())

    def test_card_item_hash_distinct_from_message_item(self) -> None:
        from bot.services.butler_evidence import _compute_context_hash

        card_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        msg_item = (_make_item(10, source_type="message"),)
        card_item = (
            _make_item(
                10,
                source_type="card",
                card_id=card_id,
                card_source_mvids=(10, 11),
            ),
        )
        assert _compute_context_hash(msg_item) != _compute_context_hash(card_item)


# ---------------------------------------------------------------------------
# snapshot_at is UTC-aware
# ---------------------------------------------------------------------------


class TestSnapshotAtUtc:
    @pytest.mark.asyncio
    async def test_snapshot_at_is_utc_aware(self) -> None:
        """build_butler_evidence sets snapshot_at with tzinfo set (UTC-aware)."""
        from bot.services.butler_evidence import build_butler_evidence

        mock_session = MagicMock()

        with patch(
            "bot.services.butler_evidence.search_messages",
            new_callable=AsyncMock,
            return_value=[],
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="",
                chat_id=_CHAT_ID,
                visibility_scope="member",
            )

        assert ctx.snapshot_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Empty query returns empty bundle (no crash)
# ---------------------------------------------------------------------------


class TestEmptyQuery:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty_bundle(self) -> None:
        """build_butler_evidence with empty query must not crash and return empty bundle."""
        from bot.services.butler_evidence import build_butler_evidence

        mock_session = MagicMock()

        with patch(
            "bot.services.butler_evidence.search_messages",
            new_callable=AsyncMock,
            return_value=[],
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="",
                chat_id=_CHAT_ID,
                visibility_scope="member",
            )

        assert ctx.bundle.abstained is True
        assert ctx.bundle.items == ()
        assert ctx.governance_excluded_count == 0

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty_bundle(self) -> None:
        """Whitespace-only query also yields empty bundle."""
        from bot.services.butler_evidence import build_butler_evidence

        mock_session = MagicMock()

        with patch(
            "bot.services.butler_evidence.search_messages",
            new_callable=AsyncMock,
            return_value=[],
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=5,
                query="   ",
                chat_id=_CHAT_ID,
                visibility_scope="member",
            )

        assert ctx.bundle.abstained is True


# ---------------------------------------------------------------------------
# governance_excluded_count increments when sources are filtered
# ---------------------------------------------------------------------------


class TestGovernanceExcludedCount:
    @pytest.mark.asyncio
    async def test_excluded_count_increments_for_filtered_sources(self) -> None:
        """When a source triggers a non-allowable policy, governance_excluded_count increases."""
        from bot.services.butler_evidence import build_butler_evidence
        from bot.services.search import SearchHit

        # One hit that will trigger governance policy via text field.
        _NOMEM = "#" + "no" + "mem"
        hit = SearchHit(
            message_version_id=101,
            chat_message_id=201,
            chat_id=_CHAT_ID,
            message_id=301,
            user_id=7,
            snippet="hit",
            ts_rank=0.8,
            captured_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            message_date=datetime(2026, 5, 26, tzinfo=timezone.utc),
        )

        mock_session = MagicMock()

        # Patch search_messages to return our hit.
        # Patch _fetch_governance_fields to return governance-triggering data.
        with (
            patch(
                "bot.services.butler_evidence.search_messages",
                new_callable=AsyncMock,
                return_value=[hit],
            ),
            patch(
                "bot.services.butler_evidence._fetch_governance_fields",
                new_callable=AsyncMock,
                return_value={
                    "text": f"hello {_NOMEM}",
                    "caption": None,
                },
            ),
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="hello",
                chat_id=_CHAT_ID,
                visibility_scope="member",
            )

        assert ctx.governance_excluded_count >= 1
        # The hit must not appear in the bundle.
        assert 101 not in ctx.bundle.evidence_ids

    @pytest.mark.asyncio
    async def test_normal_policy_not_excluded(self) -> None:
        """Normal-policy sources are included; excluded count stays 0."""
        from bot.services.butler_evidence import build_butler_evidence
        from bot.services.search import SearchHit

        hit = SearchHit(
            message_version_id=102,
            chat_message_id=202,
            chat_id=_CHAT_ID,
            message_id=302,
            user_id=8,
            snippet="good hit",
            ts_rank=0.9,
            captured_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            message_date=datetime(2026, 5, 26, tzinfo=timezone.utc),
        )

        mock_session = MagicMock()

        with (
            patch(
                "bot.services.butler_evidence.search_messages",
                new_callable=AsyncMock,
                return_value=[hit],
            ),
            patch(
                "bot.services.butler_evidence._fetch_governance_fields",
                new_callable=AsyncMock,
                return_value={
                    "text": "totally normal content",
                    "caption": None,
                },
            ),
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="normal",
                chat_id=_CHAT_ID,
                visibility_scope="member",
            )

        assert ctx.governance_excluded_count == 0
        assert 102 in ctx.bundle.evidence_ids


# ---------------------------------------------------------------------------
# H-1: Vanished row (None sentinel) is fail-closed
# ---------------------------------------------------------------------------


class TestVanishedRowFailClosed:
    @pytest.mark.asyncio
    async def test_vanished_row_excluded_and_count_incremented(self) -> None:
        """When _fetch_governance_fields returns None (row vanished), source is excluded + count incremented."""
        from bot.services.butler_evidence import build_butler_evidence
        from bot.services.search import SearchHit

        hit = SearchHit(
            message_version_id=999,
            chat_message_id=1999,
            chat_id=_CHAT_ID,
            message_id=2999,
            user_id=10,
            snippet="vanished",
            ts_rank=0.5,
            captured_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            message_date=datetime(2026, 5, 26, tzinfo=timezone.utc),
        )

        mock_session = MagicMock()

        # _fetch_governance_fields returns None to signal vanished row
        with (
            patch(
                "bot.services.butler_evidence.search_messages",
                new_callable=AsyncMock,
                return_value=[hit],
            ),
            patch(
                "bot.services.butler_evidence._fetch_governance_fields",
                new_callable=AsyncMock,
                return_value=None,  # sentinel for vanished row
            ),
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="vanished",
                chat_id=_CHAT_ID,
                visibility_scope="member",
            )

        # Fail-closed: vanished row must be excluded
        assert 999 not in ctx.bundle.evidence_ids
        # Count must be incremented
        assert ctx.governance_excluded_count == 1
