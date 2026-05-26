"""Unit tests for ButlerEvidenceContext + build_butler_evidence.

T12-02 (Wave 1 Stream Evidence) — tests BEFORE implementation (TDD red phase).

Covers:
  - ButlerEvidenceContext is frozen (FrozenInstanceError on mutation)
  - build_butler_evidence returns an empty bundle for an empty query (no crash)
  - revalidation_token is deterministic over same source set, distinct across different sets
  - snapshot_at is UTC-aware (tzinfo is not None)
  - governance_excluded_count increments when at least one source is filtered
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
# ButlerEvidenceContext dataclass tests
# ---------------------------------------------------------------------------


class TestButlerEvidenceContextFrozen:
    """ButlerEvidenceContext must be frozen — no mutations after construction."""

    def test_frozen_raises_on_field_assignment(self) -> None:
        from bot.services.butler_evidence import ButlerEvidenceContext

        bundle = _make_bundle((_make_item(1),))
        ctx = ButlerEvidenceContext(
            bundle=bundle,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="test",
            snapshot_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            governance_excluded_count=0,
            revalidation_token="abc123",
        )

        with pytest.raises(FrozenInstanceError):
            ctx.governance_excluded_count = 1  # type: ignore[misc]

    def test_frozen_raises_on_bundle_replacement(self) -> None:
        from bot.services.butler_evidence import ButlerEvidenceContext

        bundle = _make_bundle((_make_item(1),))
        ctx = ButlerEvidenceContext(
            bundle=bundle,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="test",
            snapshot_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            governance_excluded_count=0,
            revalidation_token="abc123",
        )

        with pytest.raises(FrozenInstanceError):
            ctx.bundle = _make_bundle(())  # type: ignore[misc]

    def test_frozen_raises_on_requester_mutation(self) -> None:
        from bot.services.butler_evidence import ButlerEvidenceContext

        bundle = _make_bundle(())
        ctx = ButlerEvidenceContext(
            bundle=bundle,
            requester_user_id=42,
            chat_id=_CHAT_ID,
            query="q",
            snapshot_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            governance_excluded_count=0,
            revalidation_token="tok",
        )

        with pytest.raises(FrozenInstanceError):
            ctx.requester_user_id = 99  # type: ignore[misc]


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
            )

        assert ctx.bundle.abstained is True


# ---------------------------------------------------------------------------
# revalidation_token — deterministic, distinct across different source sets
# ---------------------------------------------------------------------------


class TestRevalidationToken:
    def _ctx_from_items(
        self,
        items: tuple[EvidenceItem, ...],
    ):  # type: ignore[no-untyped-def]
        from bot.services.butler_evidence import ButlerEvidenceContext

        bundle = _make_bundle(items)
        return ButlerEvidenceContext(
            bundle=bundle,
            requester_user_id=1,
            chat_id=_CHAT_ID,
            query="q",
            snapshot_at=datetime(2026, 5, 26, tzinfo=timezone.utc),
            governance_excluded_count=0,
            revalidation_token=_compute_token(items),
        )

    def test_same_source_set_produces_same_token(self) -> None:
        """revalidation_token is deterministic over the same set of sources."""
        from bot.services.butler_evidence import _compute_revalidation_token

        items = (_make_item(10), _make_item(20))
        token_a = _compute_revalidation_token(items)
        token_b = _compute_revalidation_token(items)

        assert token_a == token_b

    def test_different_source_sets_produce_different_tokens(self) -> None:
        """Different source sets must produce distinct tokens."""
        from bot.services.butler_evidence import _compute_revalidation_token

        items_a = (_make_item(10),)
        items_b = (_make_item(20),)

        assert _compute_revalidation_token(items_a) != _compute_revalidation_token(items_b)

    def test_token_is_stable_across_item_order(self) -> None:
        """Token is order-independent (sorted internally) for the same set."""
        from bot.services.butler_evidence import _compute_revalidation_token

        items_ab = (_make_item(10), _make_item(20))
        items_ba = (_make_item(20), _make_item(10))

        # Same canonical set: token must be equal regardless of input order.
        assert _compute_revalidation_token(items_ab) == _compute_revalidation_token(items_ba)

    def test_empty_items_produces_deterministic_token(self) -> None:
        from bot.services.butler_evidence import _compute_revalidation_token

        assert _compute_revalidation_token(()) == _compute_revalidation_token(())

    def test_card_item_token_distinct_from_message_item(self) -> None:
        """Card-sourced item with same mvid must differ from message item."""
        from bot.services.butler_evidence import _compute_revalidation_token

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

        assert _compute_revalidation_token(msg_item) != _compute_revalidation_token(card_item)


def _compute_token(items: tuple[EvidenceItem, ...]) -> str:
    """Test helper — import and call the public helper."""
    from bot.services.butler_evidence import _compute_revalidation_token

    return _compute_revalidation_token(items)


# ---------------------------------------------------------------------------
# governance_excluded_count increments when sources are filtered
# ---------------------------------------------------------------------------


class TestGovernanceExcludedCount:
    @pytest.mark.asyncio
    async def test_excluded_count_increments_for_filtered_sources(self) -> None:
        """When a source triggers a non-allowable policy, governance_excluded_count increases."""
        from bot.services.butler_evidence import build_butler_evidence
        from bot.services.search import SearchHit

        # One hit that will trigger nomem policy via text field.
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
        # Patch _fetch_message_for_governance to return governance-triggering data.
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
                    "poll_question": None,
                    "contact_name": None,
                    "forward_text": None,
                    "forward_caption": None,
                },
            ),
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="hello",
                chat_id=_CHAT_ID,
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
                    "poll_question": None,
                    "contact_name": None,
                    "forward_text": None,
                    "forward_caption": None,
                },
            ),
        ):
            ctx = await build_butler_evidence(
                mock_session,
                requester_user_id=1,
                query="normal",
                chat_id=_CHAT_ID,
            )

        assert ctx.governance_excluded_count == 0
        assert 102 in ctx.bundle.evidence_ids
