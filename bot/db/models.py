from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
import uuid as _uuid_module

from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql.expression import ColumnElement


class MessageVersionSearchVectorExpression(ColumnElement[str]):
    """Dialect-aware generated-column expression for ``message_versions.search_tsv``."""

    inherit_cache = True


@compiles(MessageVersionSearchVectorExpression, "postgresql")
def _compile_search_vector_postgresql(
    element: MessageVersionSearchVectorExpression,
    compiler,
    **kwargs,
) -> str:
    return "to_tsvector('russian', coalesce(normalized_text,'') || ' ' || coalesce(caption,''))"


@compiles(MessageVersionSearchVectorExpression)
def _compile_search_vector_default(
    element: MessageVersionSearchVectorExpression,
    compiler,
    **kwargs,
) -> str:
    return "coalesce(normalized_text,'') || ' ' || coalesce(caption,'')"


class KnowledgeCardBodyTsvExpression(ColumnElement[str]):
    """Dialect-aware generated-column expression for ``knowledge_cards.body_tsv``.

    PostgreSQL: ``to_tsvector('russian', coalesce(body_markdown, ''))`` —
    matches the Phase 4 baseline (``message_versions.search_tsv`` and
    PHASE6_PLAN.md §5.A Q4). SQLite (test fallback): the unparsed body
    text, so Base.metadata.create_all does not fail on SQLite-only test
    paths even though FTS itself is Postgres-only.
    """

    inherit_cache = True


@compiles(KnowledgeCardBodyTsvExpression, "postgresql")
def _compile_card_body_tsv_postgresql(
    element: KnowledgeCardBodyTsvExpression,
    compiler,
    **kwargs,
) -> str:
    return "to_tsvector('russian', coalesce(body_markdown, ''))"


@compiles(KnowledgeCardBodyTsvExpression)
def _compile_card_body_tsv_default(
    element: KnowledgeCardBodyTsvExpression,
    compiler,
    **kwargs,
) -> str:
    return "coalesce(body_markdown, '')"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user ID
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    is_member: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )
    # T2-NEW-B: ghost-user flag for Telegram Desktop import.
    # Set to True only by the import service for users whose Telegram account is not
    # represented by a live gatekeeper row (deleted accounts, anonymous channel posts).
    # NEVER flipped back to False; NEVER used to overwrite a live user's row.
    is_imported_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # NULL is intentionally fail-closed for semantic indexing: only authors
    # positively identified by Telegram as human have is_bot=False.
    is_bot: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    intro: Mapped[Intro | None] = relationship(
        "Intro", back_populates="user", foreign_keys="[Intro.user_id]"
    )


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (Index("ix_applications_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(
        String(20)
    )  # filling, pending, vouched, added, rejected, privacy_block
    invite_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    questionnaire_message_id: Mapped[int | None] = mapped_column(BigInteger)
    vouched_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    vouched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invite_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    notified_admin_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    nudged_newcomer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    voucher: Mapped[User | None] = relationship(foreign_keys=[vouched_by])
    answers: Mapped[list[QuestionnaireAnswer]] = relationship(back_populates="application")


class QuestionnaireAnswer(Base):
    __tablename__ = "questionnaire_answers"
    __table_args__ = (Index("ix_qa_user_current", "user_id", "is_current"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    application_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("applications.id"))
    question_index: Mapped[int] = mapped_column(SmallInteger)
    question_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    application: Mapped[Application | None] = relationship(back_populates="answers")


class Intro(Base):
    __tablename__ = "intros"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    intro_text: Mapped[str] = mapped_column(Text)
    vouched_by_name: Mapped[str] = mapped_column(String(255))
    sheets_row_number: Mapped[int | None] = mapped_column(Integer)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="intro")


class ChatMessage(Base):
    """Normalized message archive (extended in T1-05).

    The original gatekeeper bot wrote ``id, message_id, chat_id, user_id, text, date,
    raw_json, created_at`` only. T1-05 adds the normalized fields the memory system
    needs (reply / thread / caption / message_kind / policy / visibility / hash /
    audit). All new columns are nullable or have server defaults so existing rows
    survive the migration untouched.

    ``current_version_id`` is a forward-reference to ``message_versions.id`` — T1-06
    creates that table and adds the FK; for T1-05 it stays a plain integer column.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            "memory_policy IN ('normal','nomem','offrecord','forgotten')",
            name="ck_chat_messages_memory_policy",
        ),
        CheckConstraint(
            "visibility IN ('private','member','internal','public')",
            name="ck_chat_messages_visibility",
        ),
        Index("ix_chat_messages_chat_msg", "chat_id", "message_id", unique=True),
        Index("ix_chat_messages_chat_id_date", "chat_id", "date"),
        Index("ix_chat_messages_reply_to_message_id", "reply_to_message_id"),
        Index("ix_chat_messages_message_thread_id", "message_thread_id"),
        Index("ix_chat_messages_memory_policy", "memory_policy"),
        Index("ix_chat_messages_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    text: Mapped[str | None] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )

    # T1-05 additions — all nullable / default so legacy rows survive.
    raw_update_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "telegram_updates.id",
            name="fk_chat_messages_raw_update_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # T1-06 closes the forward-ref: FK to message_versions.id (defined later in this file).
    current_version_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "message_versions.id",
            name="fk_chat_messages_current_version_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    memory_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="normal", server_default="normal"
    )
    visibility: Mapped[str] = mapped_column(
        String(32), nullable=False, default="member", server_default="member"
    )
    is_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageVersion(Base):
    """Provenance + edit history of a ``chat_messages`` row (T1-06).

    Every persisted message has at least one version (v1, captured at insert time). When
    a Telegram edit arrives (T1-14) and the content hash changes, a new row is appended
    with ``version_seq = max + 1``. Citations from the q&a layer (Phase 4) point at a
    specific ``message_version_id``, not at the parent ``chat_messages`` row, so claims
    remain stable even after future edits.

    Idempotency: a DB-level UNIQUE constraint on ``(chat_message_id, content_hash)``
    (``uq_message_versions_chat_message_content_hash``) enforces idempotency at the storage
    layer. ``MessageVersionRepo.insert_version`` uses a savepoint + reselect pattern:
    concurrent callers that slip past the initial ``get_by_hash`` check hit an
    ``IntegrityError`` inside ``begin_nested()``, which rolls back only the sub-transaction;
    the loser then reselects the winner's row and returns it cleanly. The separate
    ``(chat_message_id, version_seq)`` unique constraint (``uq_message_versions_chat_message_seq``)
    remains the structural sequence invariant.

    On ``forget`` (Phase 3), versions are hard-deleted (CASCADE from chat_messages) or
    redacted in place (``is_redacted=True``, content fields nulled). The ON DELETE
    SET NULL on ``chat_messages.current_version_id`` keeps the message row visible
    even when its versions are wiped.
    """

    __tablename__ = "message_versions"
    __table_args__ = (
        UniqueConstraint(
            "chat_message_id",
            "version_seq",
            name="uq_message_versions_chat_message_seq",
        ),
        Index(
            "uq_message_versions_chat_message_content_hash_active",
            "chat_message_id",
            "content_hash",
            unique=True,
            postgresql_where=text("is_redacted = false"),
        ),
        Index("ix_message_versions_content_hash", "content_hash"),
        Index("ix_message_versions_captured_at", "captured_at"),
        Index("ix_message_versions_chat_message_id", "chat_message_id"),
        Index(
            "ix_message_versions_search_tsv",
            "search_tsv",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "chat_messages.id",
            name="fk_message_versions_chat_message_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    version_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    edit_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_update_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "telegram_updates.id",
            name="fk_message_versions_raw_update_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    is_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # T2-03 / #103 (per #106): provenance flag for rows constructed from a static
    # Telegram Desktop archive. TRUE iff this row was written by an import run;
    # FALSE for live ingestion. See docs/memory-system/import-edit-history.md.
    imported_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    search_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite"),
        Computed(MessageVersionSearchVectorExpression(), persisted=True),
        nullable=True,
    )


class MessageMedia(Base):
    """Telegram photo provenance and its bounded vision description."""

    __tablename__ = "message_media"
    __table_args__ = (
        UniqueConstraint("chat_message_id", name="uq_message_media_chat_message_id"),
        CheckConstraint("media_kind = 'photo'", name="ck_message_media_kind"),
        CheckConstraint(
            "description_status IN ('pending','processing','ready','failed','missing_source')",
            name="ck_message_media_description_status",
        ),
        CheckConstraint(
            "(description_status = 'processing') = "
            "(description_claim_token IS NOT NULL AND description_claimed_at IS NOT NULL)",
            name="ck_message_media_processing_claim",
        ),
        Index("ix_message_media_description_status", "description_status"),
        Index(
            "ix_message_media_pending_due",
            "description_status",
            "next_attempt_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "chat_messages.id",
            name="fk_message_media_chat_message_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    media_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_status: Mapped[str] = mapped_column(String(32), nullable=False)
    description_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description_attempts: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        server_default="0",
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    description_claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    description_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_usage_ledger_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "llm_usage_ledger.id",
            name="fk_message_media_llm_usage_ledger_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ImageDescriptionResolution(Base):
    """Append-only operator decision for one ambiguous image provider claim."""

    __tablename__ = "image_description_resolutions"
    __table_args__ = (
        UniqueConstraint(
            "message_media_id",
            "attempt_no",
            name="uq_image_description_resolutions_media_attempt",
        ),
        CheckConstraint(
            "action IN ('risk_accepted_retry','abandon')",
            name="ck_image_description_resolutions_action",
        ),
        CheckConstraint(
            "attempt_no >= 1",
            name="ck_image_description_resolutions_attempt_no_positive",
        ),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500",
            name="ck_image_description_resolutions_reason_bounded",
        ),
        CheckConstraint(
            "evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_image_description_resolutions_evidence_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(action = 'abandon' AND accept_memory_gap) "
            "OR (action <> 'abandon' AND NOT accept_memory_gap)",
            name="ck_image_description_resolutions_gap_acceptance",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_media_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "message_media.id",
            name="fk_image_description_resolutions_message_media_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_image_description_resolutions_actor_user_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    accept_memory_gap: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OffrecordMark(Base):
    """Audit row for a ``#nomem`` / ``#offrecord`` detection (T1-13).

    Created by the chat_messages handler (and future import / admin paths) whenever
    ``governance.detect_policy`` returns a non-normal policy. The row records WHO
    triggered the mark, WHAT mark, WHERE in the data model, HOW it was detected and
    WHEN. Status lifecycle: active → expired | revoked. Phase 3 admin actions add
    revoke flows.

    Cascades:
    - chat_message_id → chat_messages.id ON DELETE CASCADE: forget cascade wipes the
      message and its mark together
    - set_by_user_id → users.id ON DELETE SET NULL: keep the audit row even if the
      user record is later anonymized (forget_me)
    """

    __tablename__ = "offrecord_marks"
    __table_args__ = (
        CheckConstraint(
            "mark_type IN ('nomem','offrecord')",
            name="ck_offrecord_marks_mark_type",
        ),
        CheckConstraint(
            "scope_type IN ('message','thread','chat')",
            name="ck_offrecord_marks_scope_type",
        ),
        CheckConstraint(
            "status IN ('active','expired','revoked')",
            name="ck_offrecord_marks_status",
        ),
        Index("ix_offrecord_marks_mark_type_status", "mark_type", "status"),
        Index("ix_offrecord_marks_chat_message_id", "chat_message_id"),
        Index("ix_offrecord_marks_scope", "scope_type", "scope_id"),
        # Issue #67: partial unique index so ON CONFLICT DO NOTHING + SELECT is a
        # true no-op on duplicate delivery. NULL chat_message_id rows (thread/chat
        # scope) are excluded so thread-scope marks stay unrestricted.
        Index(
            "ix_offrecord_marks_chat_message_id_mark_type",
            "chat_message_id",
            "mark_type",
            unique=True,
            postgresql_where=text("chat_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mark_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "chat_messages.id",
            name="fk_offrecord_marks_chat_message_id",
            ondelete="CASCADE",
        ),
        nullable=True,
    )
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    set_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_offrecord_marks_set_by_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    detected_by: Mapped[str] = mapped_column(String(128), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )


class ForgetEvent(Base):
    """Tombstone record for a forget/erasure request (T3-01).

    Each row represents a single forget intent — WHO issued it (actor_user_id /
    authorized_by), WHAT is being forgotten (target_type / target_id / tombstone_key),
    and WHERE the cascade has reached (status / cascade_status). The tombstone_key is
    globally unique; re-issuing a forget for the same target returns the existing row
    (idempotent).

    Tombstone key format (HANDOFF §10):
    - ``message:<chat_id>:<message_id>``
    - ``message_hash:<sha256>``
    - ``user:<tg_id>``
    - ``export:<source>:<export_msg_id>``

    Status lifecycle: pending → processing → completed | failed.
    cascade_status is a per-layer progress map, e.g.::

        {'chat_messages': 'completed', 'message_versions': 'pending'}

    Populated by Sprint 3 (#96) cascade worker; schema created here (Sprint 1 / T3-01).

    actor_user_id → users.id ON DELETE SET NULL: keep the audit row even if the user
    record is later anonymized (forget_me).
    """

    __tablename__ = "forget_events"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('message','message_hash','user','export')",
            name="ck_forget_events_target_type",
        ),
        CheckConstraint(
            "authorized_by IN ('self','admin','system','gdpr_request')",
            name="ck_forget_events_authorized_by",
        ),
        CheckConstraint(
            "policy IN ('forgotten','offrecord_propagated')",
            name="ck_forget_events_policy",
        ),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','superseded')",
            name="ck_forget_events_status",
        ),
        UniqueConstraint("tombstone_key", name="uq_forget_events_tombstone_key"),
        Index("ix_forget_events_status_created_at", "status", "created_at"),
        Index("ix_forget_events_target_type_target_id", "target_type", "target_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_forget_events_actor_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    authorized_by: Mapped[str] = mapped_column(String(64), nullable=False)
    tombstone_key: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="forgotten", server_default="forgotten"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    cascade_status: Mapped[dict | None] = mapped_column(
        # JSONB on postgres (enables future GIN indexing); JSON elsewhere for test compat.
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class QaTrace(Base):
    __tablename__ = "qa_traces"
    __table_args__ = (
        Index("ix_qa_traces_user_tg_id", "user_tg_id"),
        Index("ix_qa_traces_chat_id_created_at", "chat_id", "created_at"),
        Index("ix_qa_traces_llm_call_id", "llm_call_id"),  # Phase 5 / 025
        Index(
            "uq_qa_traces_source_chat_message_id",
            "source_chat_message_id",
            unique=True,
            postgresql_where=text("source_chat_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_chat_message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "chat_messages.id",
            name="fk_qa_traces_source_chat_message_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    query_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
        server_default="'[]'",  # align ORM with migration 022 (PG uses ::jsonb in migration)
    )
    abstained: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    # ── Phase 5 / alembic 025 LLM extension columns ────────────────────────
    # Populated by ``QaTraceRepo.update_llm_fields`` from the gateway's
    # ``SynthesisResult`` per handler 4-step ORDER (contracts.md §6.1).
    # ``llm_response_summary`` is the raw answer text; durability is bounded by
    # the ``_cascade_qa_traces_llm`` cascade layer which NULLs it on forget.
    llm_call_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "llm_usage_ledger.id",
            name="fk_qa_traces_llm_call_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    llm_response_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_response_redacted: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        default=False,
        server_default="false",
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)


class SemanticIndexRun(Base):
    __tablename__ = "semantic_index_runs"
    __table_args__ = (
        CheckConstraint("run_type IN ('backfill','reindex')", name="ck_semantic_index_runs_type"),
        CheckConstraint(
            "status IN ('running','completed','failed')",
            name="ck_semantic_index_runs_status",
        ),
        CheckConstraint("embedding_dimensions > 0", name="ck_semantic_index_runs_dimensions"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    eligible_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    indexed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reason_counts: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=dict,
        server_default="{}",
    )
    cursor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SemanticRetrievalUnit(Base):
    __tablename__ = "semantic_retrieval_units"
    __table_args__ = (
        CheckConstraint("source_type IN ('message','card')", name="ck_semantic_units_source_type"),
        CheckConstraint("embedding_dimensions = 1536", name="ck_semantic_units_dimensions"),
        CheckConstraint(
            "(invalidated_at IS NULL) = (invalidation_reason IS NULL)",
            name="ck_semantic_units_invalidation_pair",
        ),
        UniqueConstraint(
            "source_type",
            "source_id",
            "source_revision",
            "content_hash",
            "embedding_model",
            name="uq_semantic_units_identity",
        ),
        Index("ix_semantic_units_chat_active", "chat_id", "invalidated_at"),
        Index("ix_semantic_units_source", "source_type", "source_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    llm_usage_ledger_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("llm_usage_ledger.id", ondelete="RESTRICT"), nullable=False
    )
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SemanticRetrievalUnitSource(Base):
    __tablename__ = "semantic_retrieval_unit_sources"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_semantic_unit_sources_position"),
        UniqueConstraint("unit_id", "position", name="uq_semantic_unit_sources_position"),
        Index("ix_semantic_unit_sources_message_version_id", "message_version_id"),
    )

    unit_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("semantic_retrieval_units.id", ondelete="CASCADE"),
        primary_key=True,
    )
    message_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("message_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class SemanticQaAttempt(Base):
    __tablename__ = "semantic_qa_attempts"
    __table_args__ = (
        CheckConstraint(
            "slot_number IS NULL OR slot_number IN (1,2)", name="ck_semantic_attempts_slot"
        ),
        CheckConstraint(
            "status IN ('denied','reserved','consumed','released')",
            name="ck_semantic_attempts_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered','abstained','technical_failure','quota_denied')",
            name="ck_semantic_attempts_outcome",
        ),
        CheckConstraint(
            "(status = 'denied' AND slot_number IS NULL AND outcome = 'quota_denied' "
            "AND delivery_started_at IS NULL) OR "
            "(status = 'reserved' AND slot_number IS NOT NULL AND finalized_at IS NULL AND "
            "((outcome IS NULL AND delivery_started_at IS NULL) OR "
            "(outcome IN ('answered','abstained') AND delivery_started_at IS NOT NULL))) OR "
            "(status = 'consumed' AND slot_number IS NOT NULL "
            "AND outcome IN ('answered','abstained') AND delivery_started_at IS NOT NULL "
            "AND finalized_at IS NOT NULL) OR "
            "(status = 'released' AND slot_number IS NOT NULL "
            "AND outcome = 'technical_failure' AND finalized_at IS NOT NULL)",
            name="ck_semantic_attempts_state",
        ),
        UniqueConstraint("idempotency_key", name="uq_semantic_qa_attempts_idempotency_key"),
        Index(
            "uq_semantic_qa_attempts_active_slot",
            "user_tg_id",
            "local_day",
            "slot_number",
            unique=True,
            postgresql_where=text("status IN ('reserved','consumed')"),
        ),
        Index("ix_semantic_qa_attempts_user_day", "user_tg_id", "local_day", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    user_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_chat_message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    slot_number: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    qa_trace_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("qa_traces.id", ondelete="SET NULL"), nullable=True
    )
    embedding_llm_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("llm_usage_ledger.id", ondelete="SET NULL"), nullable=True
    )
    synthesis_llm_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("llm_usage_ledger.id", ondelete="SET NULL"), nullable=True
    )
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivery_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SemanticRetrievalTrace(Base):
    __tablename__ = "semantic_retrieval_traces"
    __table_args__ = (
        CheckConstraint(
            "retrieval_mode IN ('hybrid','fts_fallback','shadow')",
            name="ck_semantic_retrieval_traces_mode",
        ),
        CheckConstraint(
            "fts_latency_ms >= 0 AND vector_latency_ms >= 0 "
            "AND fusion_latency_ms >= 0 AND total_latency_ms >= 0",
            name="ck_semantic_retrieval_traces_latency",
        ),
        UniqueConstraint("attempt_id", name="uq_semantic_retrieval_traces_attempt_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("semantic_qa_attempts.id", ondelete="CASCADE"), nullable=False
    )
    qa_trace_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("qa_traces.id", ondelete="SET NULL"), nullable=True
    )
    query_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_ranks: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    result_source_ids: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    fts_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    fusion_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    total_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IntroRefreshTracking(Base):
    __tablename__ = "intro_refresh_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    cycle_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reminders_sent: Mapped[int] = mapped_column(SmallInteger, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(20))  # daily, every_2_days, done
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class VouchLog(Base):
    __tablename__ = "vouch_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    voucher_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    vouchee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    application_id: Mapped[int] = mapped_column(Integer, ForeignKey("applications.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )


class InviteOutbox(Base):
    __tablename__ = "invite_outbox"
    __table_args__ = (
        Index("ix_invite_outbox_status", "status"),
        Index(
            "ix_invite_outbox_pending_unique",
            "application_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    invite_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeatureFlag(Base):
    """Persistent rollout flag for memory surfaces (T1-01).

    Logical key: ``(flag_key, scope_type, scope_id)``. Global flags use ``scope_type=None``
    and ``scope_id=None``. Per-chat / per-user flags pin a non-null scope.

    The DB-level unique index ``uq_feature_flags_key_scope`` uses ``NULLS NOT DISTINCT``
    so global-scope rows are actually unique per flag_key (postgres 15+ feature; postgres
    16 is the runtime). The model's ``__table_args__`` declares the unique index with the
    same flag so ``Base.metadata.create_all`` (used by ``bot/__main__.py::_init_db`` in
    dev) produces the same shape as the alembic migration.

    All ``memory.*`` flag keys default to OFF — the migration does not seed any rows, and
    ``FeatureFlagRepo.get`` returns ``False`` for missing flags. Operators enable flags
    explicitly via SQL until an admin UI lands in a later phase.
    """

    __tablename__ = "feature_flags"
    __table_args__ = (
        Index("ix_feature_flags_enabled", "enabled"),
        Index(
            "uq_feature_flags_key_scope",
            "flag_key",
            "scope_type",
            "scope_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flag_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scope_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class IngestionRun(Base):
    """Tracks one ingestion run (T1-02).

    Every ``telegram_updates`` / ``chat_messages`` row written during a run carries the
    run's id (added in later tickets). One long-lived ``run_type='live'`` row exists per
    bot process; ``run_type='import'`` rows are created per Telegram Desktop import (T2-01
    dry-run / T2-03 apply). ``run_type='dry_run'`` for import dry-runs.
    ``run_type='rolled_back'`` rows are audit records for T2-NEW-G logical rollback.

    Status lifecycle: running → completed | failed | cancelled. Dry-runs may use
    ``status='dry_run'`` as a terminal state to make filter queries explicit.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN ('live','import','dry_run','cancelled','rolled_back')",
            name="ck_ingestion_runs_run_type",
        ),
        CheckConstraint(
            "status IN ('running','completed','failed','dry_run','cancelled')",
            name="ck_ingestion_runs_status",
        ),
        Index(
            "ix_ingestion_runs_run_type_started_at",
            "run_type",
            "started_at",
        ),
        Index("ix_ingestion_runs_status", "status"),
        # T2-NEW-E (#101): at most one RUNNING import run per source_hash.
        # Completed/failed rows for the same source_hash are allowed.
        Index(
            "ix_ingestion_runs_source_hash_running",
            "source_hash",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # T2-NEW-E (#101): SHA-256 of the export file bytes. Used by checkpoint/resume logic
    # to locate a prior partial run for the same source file. NULL for live and dry_run rows.
    source_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="running",
        server_default="running",
    )
    stats_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TelegramUpdate(Base):
    """Raw source-of-truth archive of one Telegram update (T1-03).

    Filled by the live ingestion service (T1-04) and by the Telegram Desktop importer
    (T2-01 dry-run / T2-03 apply). Live updates carry a non-null ``update_id`` (Telegram
    guarantees uniqueness per bot), and the partial unique index
    ``ix_telegram_updates_update_id`` prevents duplicates on polling retries. Synthetic
    import updates leave ``update_id`` NULL; migration 088 enforces one canonical
    ``import_message`` row per non-null ``(chat_id, message_id)`` source identity.

    No content is logged here; ``raw_json`` is the unmodified Telegram payload until the
    governance detector (T1-12) marks it offrecord, at which point ``is_redacted`` and
    ``redaction_reason`` are set and the redacted columns are nulled in the same
    transaction (per AUTHORIZED_SCOPE.md §`#offrecord` ordering rule).
    """

    __tablename__ = "telegram_updates"
    __table_args__ = (
        Index(
            "ix_telegram_updates_update_id",
            "update_id",
            unique=True,
            postgresql_where=text("update_id IS NOT NULL"),
        ),
        Index(
            "ix_telegram_updates_update_type_received_at",
            "update_type",
            "received_at",
        ),
        Index(
            "uq_telegram_updates_import_message_source",
            "update_type",
            "chat_id",
            "message_id",
            unique=True,
            postgresql_where=text(
                "update_id IS NULL AND update_type = 'import_message' "
                "AND chat_id IS NOT NULL AND message_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_telegram_updates_chat_id_message_id",
            "chat_id",
            "message_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    update_type: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("ingestion_runs.id", name="fk_telegram_updates_ingestion_run_id"),
        nullable=True,
    )
    is_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    redaction_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )


class LlmUsageLedger(Base):
    """Per-call audit log for every LLM gateway invocation (T5-02 / alembic 024).

    Written by ``bot/services/llm_gateway.py`` for every call outcome, including
    cache hits, abstentions, budget refusals, and errors. Most calls remain in
    the caller transaction; paid semantic embedding/synthesis calls durably
    commit an explicit ``reserved_in_flight`` row before provider dispatch and
    replace that marker with the terminal audit outcome afterward.

    All numeric fields (tokens, cost, latency) use server defaults of 0 so that
    partial rows created for budget-guard placeholders are self-consistent.

    ``prompt_hash`` and ``response_hash`` are SHA-256 hex digests (64 chars each).
    The Phase 5 cascade layer ``_cascade_llm_usage_ledger`` NULLs both on a
    forget-me event while preserving the row for budget reconciliation.
    """

    __tablename__ = "llm_usage_ledger"
    __table_args__ = (
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0 AND cost_usd >= 0 AND latency_ms >= 0",
            name="ck_llm_usage_ledger_nonnegative_usage",
        ),
        Index("ix_llm_usage_ledger_qa_trace_id", "qa_trace_id"),
        Index("ix_llm_usage_ledger_model_created_at", "model", "created_at"),
        Index("ix_llm_usage_ledger_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    qa_trace_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "qa_traces.id",
            name="fk_llm_usage_ledger_qa_trace_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    # ``prompt_hash`` relaxed to nullable in alembic 025 so the
    # ``_cascade_llm_usage_ledger`` cascade layer can NULL it on user forget
    # while preserving cost / token aggregates for budget audit.
    prompt_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # call_type added in migration 064; migration 080 adds wiki/image rollout calls.
    # 'unknown' (legacy / default), 'qa_synthesis', 'digest_daily', 'digest_weekly',
    # 'graph_projection', 'extract_candidates', 'butler_decision', 'butler_summary',
    # 'wiki_compilation', 'image_description', 'semantic_embedding'.
    # Caller SHOULD always pass explicitly; 'unknown' is the fallback only for legacy rows.
    call_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'unknown'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    qa_trace: Mapped[QaTrace | None] = relationship(
        "QaTrace",
        foreign_keys=[qa_trace_id],
    )


class LlmSynthesisCache(Base):
    """DB-backed answer cache for LLM synthesis results (T5-02 / alembic 024).

    Keyed by ``input_hash`` = sha256(query_normalized || sorted(citation_ids)
    || model_id || prompt_template_version). Cache rows are invalidated (deleted)
    by ``SynthesisCacheRepo.invalidate_by_citation`` when a forget event covers any
    cited ``message_version_id`` in ``citation_ids``.

    ``citation_ids`` is a JSONB array of ``message_version_id`` integers — the same
    ids returned by ``EvidenceBundle.evidence_ids``.
    """

    __tablename__ = "llm_synthesis_cache"
    __table_args__ = (UniqueConstraint("input_hash", name="uq_llm_synthesis_cache_input_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    input_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    citation_ids: Mapped[list[int]] = mapped_column(
        # JSONB on postgres (enables @> containment ops); JSON elsewhere for sqlite test compat.
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_hit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    hit_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )


# ─── Phase 6: knowledge cards / extraction (T6-01 / alembic 030-034) ─────────


class ExtractionRun(Base):
    """One LLM extraction pass over a time window (T6-01 / alembic 030).

    Written by ``bot/services/extractor.py::run_extraction_pass``. Tracks the
    window boundaries, how many candidates were produced, terminal status,
    and an optional FK to the Phase 5 LLM usage ledger entry for the audited
    LLM call.

    Constraints (DB-level, see migration 030):

    * ``candidate_count >= 0``
    * ``run_status='completed'`` requires both ``ingestion_window_*``
      timestamps to be non-null.
    """

    __tablename__ = "extraction_runs"
    __table_args__ = (
        CheckConstraint(
            "run_status IN ('running','completed','failed')",
            name="ck_extraction_runs_status",
        ),
        CheckConstraint(
            "candidate_count >= 0",
            name="ck_extraction_runs_candidate_count_nonneg",
        ),
        CheckConstraint(
            "run_status <> 'completed' OR "
            "(ingestion_window_start IS NOT NULL AND ingestion_window_end IS NOT NULL)",
            name="ck_extraction_runs_completed_has_window",
        ),
        Index("ix_extraction_runs_created_at", "created_at"),
        Index("ix_extraction_runs_run_status", "run_status"),
        Index(
            "ix_extraction_runs_source_chat_id_window_end",
            "source_chat_id",
            "ingestion_window_end",
            postgresql_where=text("source_chat_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(semantic_key IS NULL AND source_snapshot_hash IS NULL "
            "AND prompt_template_version IS NULL AND provider IS NULL "
            "AND model IS NULL AND selection_mode IS NULL) OR "
            "(semantic_key IS NOT NULL AND source_snapshot_hash IS NOT NULL "
            "AND prompt_template_version IS NOT NULL AND provider IS NOT NULL "
            "AND model IS NOT NULL AND selection_mode IS NOT NULL)",
            name="ck_extraction_runs_semantic_identity_complete",
        ),
        CheckConstraint(
            "(semantic_key IS NULL OR semantic_key ~ '^[0-9a-f]{64}$') AND "
            "(source_snapshot_hash IS NULL "
            "OR source_snapshot_hash ~ '^[0-9a-f]{64}$')",
            name="ck_extraction_runs_semantic_hashes",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(selection_mode IS NULL "
            "AND cursor_start_message_version_id IS NULL "
            "AND cursor_end_message_version_id IS NULL) OR "
            "(selection_mode = 'event_time' "
            "AND cursor_start_message_version_id IS NULL "
            "AND cursor_end_message_version_id IS NULL) OR "
            "(selection_mode = 'version_cursor' "
            "AND source_chat_id IS NOT NULL "
            "AND cursor_start_message_version_id IS NOT NULL "
            "AND cursor_end_message_version_id IS NOT NULL "
            "AND cursor_start_message_version_id >= 0 "
            "AND cursor_end_message_version_id >= cursor_start_message_version_id)",
            name="ck_extraction_runs_selection_cursor",
        ),
        CheckConstraint(
            "attempt_no >= 1",
            name="ck_extraction_runs_attempt_no_positive",
        ),
        CheckConstraint(
            "(attempt_no = 1 AND retry_of_run_id IS NULL) OR "
            "(attempt_no > 1 AND retry_of_run_id IS NOT NULL AND retry_of_run_id <> id)",
            name="ck_extraction_runs_retry_link",
        ),
        CheckConstraint(
            "dispatch_state IN "
            "('not_dispatched','rejected_pre_accept','response_received','unknown')",
            name="ck_extraction_runs_dispatch_state",
        ),
        UniqueConstraint(
            "semantic_key",
            "attempt_no",
            name="uq_extraction_runs_semantic_attempt",
        ),
        Index(
            "ix_extraction_runs_unresolved_cursor",
            "source_chat_id",
            "cursor_start_message_version_id",
            postgresql_where=text(
                "selection_mode = 'version_cursor' AND run_status IN ('running','failed')"
            ),
        ),
    )

    id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    ingestion_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingestion_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    run_status: Mapped[str] = mapped_column(Text, nullable=False)
    llm_usage_ledger_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "llm_usage_ledger.id",
            name="fk_extraction_runs_llm_usage_ledger_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    # Admin who triggered the pass via /admin_extract (alembic 035).
    # NULL when the pass was scheduler-driven (no operator). Stored as
    # Telegram user id (no FK to users.id — see migration 035 rationale).
    operator_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Source chat boundary for per-chat scheduler watermarks (alembic 083).
    # Legacy/manual all-chat runs remain NULL and never advance targeted jobs.
    source_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Durable semantic identity for exactly-once provider spend (alembic 085).
    # Legacy runs keep all identity columns NULL and are never guessed/matched.
    semantic_key: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    source_snapshot_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selection_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cursor_start_message_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_end_message_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    retry_of_run_id: Mapped[_uuid_module.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey(
            "extraction_runs.id",
            name="fk_extraction_runs_retry_of_run_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    dispatch_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="not_dispatched",
        server_default=text("'not_dispatched'"),
    )
    # Provider-level error from llm_gateway.extract_candidates (alembic 036).
    # NULL on success or empty-bundle short-circuit. Non-NULL means the gateway
    # returned ``gateway_error`` — extractor sets run_status='failed' and
    # persists this string for post-hoc debugging. Truncated to 2000 chars in
    # the gateway to avoid DB bloat from giant stack traces.
    gateway_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ExtractionRunResolution(Base):
    """Append-only operator decision for one non-completed extraction attempt."""

    __tablename__ = "extraction_run_resolutions"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_extraction_run_resolutions_run_id"),
        CheckConstraint(
            "action IN ('safe_retry','risk_accepted_retry','abandon')",
            name="ck_extraction_run_resolutions_action",
        ),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500",
            name="ck_extraction_run_resolutions_reason_bounded",
        ),
        CheckConstraint(
            "evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_extraction_run_resolutions_evidence_hash",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(action = 'abandon' AND accept_memory_gap) "
            "OR (action <> 'abandon' AND NOT accept_memory_gap)",
            name="ck_extraction_run_resolutions_gap_acceptance",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        ForeignKey(
            "extraction_runs.id",
            name="fk_extraction_run_resolutions_run_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_extraction_run_resolutions_actor_user_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    evidence_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    accept_memory_gap: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ExtractionCursor(Base):
    """Per-chat high-water mark for live current-version extraction."""

    __tablename__ = "extraction_cursors"
    __table_args__ = (
        CheckConstraint(
            "last_message_version_id >= 0",
            name="ck_extraction_cursors_last_message_version_id_nonnegative",
        ),
    )

    source_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_message_version_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ExtractionCandidate(Base):
    """LLM-extracted fact pending human review (T6-01 / alembic 031).

    Renamed from DRAFT's ``memory_candidates`` per Phase 6 decision D2 —
    Phase 8 ``memory_candidates`` (reflection cluster queue) is a distinct
    concept.

    Constraints (DB-level, see migration 031):

    * ``status IN ('pending','approved','rejected','superseded')``
    * ``source_message_version_ids`` must be a JSONB array.
    * ``pending`` ⇒ reviewer columns NULL; terminal status ⇒ reviewer
      columns non-null.

    ``source_message_version_ids`` is staging only. At ``/approve`` time
    (§5.C), the elements are promoted to ``card_sources`` FK rows.
    """

    __tablename__ = "extraction_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','superseded')",
            name="ck_extraction_candidates_status",
        ),
        # jsonb_typeof is Postgres-only — production runs on PG, so the
        # invariant is enforced there. The CHECK is suppressed on SQLite
        # so the test-helper ``Base.metadata.create_all`` path works.
        CheckConstraint(
            "jsonb_typeof(source_message_version_ids) = 'array'",
            name="ck_extraction_candidates_source_ids_is_array",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR "
            "(status IN ('approved','rejected','superseded') "
            " AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_extraction_candidates_reviewer_consistency",
        ),
        Index("ix_extraction_candidates_status", "status"),
        Index("ix_extraction_candidates_extraction_run_id", "extraction_run_id"),
        Index("ix_extraction_candidates_created_at", "created_at"),
        CheckConstraint(
            "payload_schema_version IS NULL OR payload_schema_version = 'karpathy-wiki-v1'",
            name="ck_extraction_candidates_payload_schema_version",
        ),
        Index(
            "ix_extraction_candidates_pending_legacy",
            "created_at",
            "id",
            postgresql_where=text("status = 'pending' AND payload_schema_version IS NULL"),
        ),
    )

    def __init__(self, **kwargs: object) -> None:
        # Ensure source_message_version_ids is always a list in-memory,
        # even before a DB round-trip populates the server_default.
        kwargs.setdefault("source_message_version_ids", [])
        super().__init__(**kwargs)

    id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    extraction_run_id: Mapped[_uuid_module.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey(
            "extraction_runs.id",
            name="fk_extraction_candidates_extraction_run_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    # JSONB on postgres (enables jsonb_typeof CHECK + future @> ops);
    # JSON elsewhere for sqlite test compat.
    candidate_json: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    source_message_version_ids: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default="'[]'",  # align ORM with migration (PG migration uses ::jsonb cast)
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL means a pre-085 legacy payload whose shape must not be guessed.
    payload_schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_extraction_candidates_reviewed_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeCard(Base):
    """Admin-approved canonical knowledge unit (T6-01 / alembic 032).

    Citation-eligible only when ``card_status='approved'`` AND at least one
    ``card_sources`` row exists (033). Body stored as Telegram MarkdownV2
    (PHASE6_PLAN.md Q1). Russian-language FTS via generated ``body_tsv``
    matches the Phase 4 baseline (``message_versions.search_tsv``).

    Constraints (DB-level, see migration 032):

    * ``card_status IN ('draft','approved','archived')`` — Q3 collapsed
      ``deprecated`` into ``archived`` with the nullable ``archived_reason``
      column populated when archived.
    * ``card_status='approved'`` requires both ``approved_by_user_id`` and
      ``approved_at`` to be set.

    Source-set requirement lives in ``card_sources`` (033), NOT here —
    keeps the ``/approve`` promotion transaction atomic (§5.C step 5+6).
    """

    __tablename__ = "knowledge_cards"
    __table_args__ = (
        CheckConstraint(
            "card_status IN ('draft','approved','archived')",
            name="ck_knowledge_cards_status",
        ),
        CheckConstraint(
            "card_status <> 'approved' OR "
            "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_knowledge_cards_approved_attribution",
        ),
        CheckConstraint(
            "topic_slug IS NULL OR ("
            "char_length(topic_slug) BETWEEN 1 AND 100 "
            "AND topic_slug = lower(topic_slug) "
            "AND topic_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'"
            ")",
            name="ck_knowledge_cards_topic_slug",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_knowledge_cards_body_tsv",
            "body_tsv",
            postgresql_using="gin",
        ),
        Index("ix_knowledge_cards_card_status", "card_status"),
        Index(
            "ix_knowledge_cards_topic_slug",
            "topic_slug",
            postgresql_where=text("topic_slug IS NOT NULL"),
        ),
        Index("ix_knowledge_cards_created_at", "created_at"),
    )

    id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    topic_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    body_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR().with_variant(Text(), "sqlite"),
        Computed(KnowledgeCardBodyTsvExpression(), persisted=True),
        nullable=True,
    )
    card_status: Mapped[str] = mapped_column(Text, nullable=False)
    archived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_knowledge_cards_approved_by_user_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CardSource(Base):
    """FK-normalized link from a card to a source ``message_versions`` row
    (T6-01 / alembic 033).

    Replaces the DRAFT's inline ``source_message_version_ids jsonb`` column
    on ``knowledge_cards`` per D1. The candidate's staging JSONB
    (``extraction_candidates.source_message_version_ids``) is promoted to
    one row per element here at ``/approve`` time (§5.C step 6).

    FK semantics:

    * ``card_id`` → ON DELETE CASCADE (deleting the card scrubs its links).
    * ``message_version_id`` → ON DELETE RESTRICT (prevents accidental
      orphan deletes; the §5.A.5 cascade demote path DELETEs the
      ``card_sources`` row explicitly).

    Indexes:

    * ``UNIQUE(card_id, message_version_id)`` — promotes idempotency of
      ``/approve`` re-runs; at most one link per pair.
    * Reverse index on ``message_version_id`` — supports
      ``_cascade_card_sources_on_forget`` (§5.A.5) which selects affected
      cards by ``message_version_id``.
    """

    __tablename__ = "card_sources"
    __table_args__ = (
        UniqueConstraint(
            "card_id",
            "message_version_id",
            name="uq_card_sources_card_id_message_version_id",
        ),
        Index("ix_card_sources_message_version_id", "message_version_id"),
    )

    id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    card_id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        ForeignKey(
            "knowledge_cards.id",
            name="fk_card_sources_card_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    message_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "message_versions.id",
            name="fk_card_sources_message_version_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExtractionDecision(Base):
    """Audit trail of an admin ``/approve`` / ``/reject`` terminal decision
    (T6-01 / alembic 034).

    Exactly one decision per candidate (``UNIQUE(candidate_id)``); appeals
    out of scope per PHASE6_PLAN.md §11. R3-block (deterministic
    re-validation aborts approval) is NOT a decision and writes NO row
    here — the candidate stays ``pending`` and the failure is structured-
    logged only (§5.C, §8).

    FK semantics:

    * ``candidate_id`` → ON DELETE CASCADE (audit trail scoped to its
      candidate; in practice candidates are not deleted).
    * ``decided_by`` → ON DELETE SET NULL (audit row survives admin
      soft-delete). ``decided_by_username`` is the NOT-NULL human-readable
      shadow snapshotted at decision time so the audit trail survives the
      FK nullification.
    """

    __tablename__ = "extraction_decisions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('approved','rejected')",
            name="ck_extraction_decisions_action",
        ),
        UniqueConstraint(
            "candidate_id",
            name="uq_extraction_decisions_candidate_id",
        ),
        Index("ix_extraction_decisions_decided_at", "decided_at"),
    )

    id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    candidate_id: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        ForeignKey(
            "extraction_candidates.id",
            name="fk_extraction_decisions_candidate_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_extraction_decisions_decided_by",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    decided_by_username: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Digest(Base):
    """One generated digest window (T7-01 / alembic 037).

    Keyed by ``(type, window_start, window_end)`` for idempotent re-runs.
    ``body_markdown`` is NULL while ``status='running'`` and NOT NULL in all
    user-visible states (draft, posting, posted, redacted, redacted_edit_failed).
    ``citations`` is a JSONB array of ``{kind, id, position}`` objects per
    PHASE7_PLAN.md §5.A — never raw message text.

    Status lifecycle:
      running → draft → posting → posted
                     ↘ failed / skipped / cost_exceeded / skipped_no_destination
    Terminal redaction states: redacted, redacted_edit_failed.

    Constraints (DB-level, see migration 037):

    * ``type IN ('daily','weekly')`` — 'weekly' schema-ready, daily-only runtime.
    * ``status`` enum — full set including transient 'posting'.
    * ``status IN (draft,posting,posted,redacted,redacted_edit_failed)``
      implies ``body_markdown IS NOT NULL``.
    * ``status='posted'`` implies ``posted_chat_id``, ``posted_message_id``,
      ``posted_at`` all NOT NULL.
    """

    __tablename__ = "digests"
    __table_args__ = (
        UniqueConstraint(
            "type",
            "window_start",
            "window_end",
            name="uq_digests_type_window",
        ),
        CheckConstraint(
            "type IN ('daily','weekly')",
            name="ck_digests_type",
        ),
        # T8-01 / Phase 8: status enum widened to 14 values. The 4 new entries
        # (awaiting_review, approved_for_publish, rejected_by_admin,
        # rejected_by_reaper) cover the weekly editorial review-gate state
        # machine. See alembic migration 038.
        CheckConstraint(
            "status IN ("
            "'running','draft','posting','posted','failed','skipped',"
            "'cost_exceeded','skipped_no_destination','redacted',"
            "'redacted_edit_failed',"
            "'awaiting_review','approved_for_publish',"
            "'rejected_by_admin','rejected_by_reaper'"
            ")",
            name="ck_digests_status",
        ),
        # T8-01: body required across the audit-trail review statuses too.
        CheckConstraint(
            "status NOT IN ("
            "'draft','posting','posted','redacted','redacted_edit_failed',"
            "'awaiting_review','approved_for_publish','rejected_by_admin',"
            "'rejected_by_reaper'"
            ")"
            " OR body_markdown IS NOT NULL",
            name="ck_digests_body_markdown_not_null_for_visible_statuses",
        ),
        CheckConstraint(
            "status <> 'posted'"
            " OR (posted_chat_id IS NOT NULL"
            " AND posted_message_id IS NOT NULL"
            " AND posted_at IS NOT NULL)",
            name="ck_digests_posted_fields_required",
        ),
        # Manual weekly approval requires attribution. Automatic weekly
        # publishing moves draft → posting → posted without an admin.
        CheckConstraint(
            "status <> 'approved_for_publish'"
            " OR type <> 'weekly'"
            " OR (published_by_admin_id IS NOT NULL"
            " AND approved_at IS NOT NULL)",
            name="ck_digests_approved_audit",
        ),
        Index(
            "ix_digests_status_draft",
            "status",
            postgresql_where=text("status = 'draft'"),
        ),
        Index(
            "ix_digests_citations_gin",
            "citations",
            postgresql_using="gin",
            postgresql_ops={"citations": "jsonb_path_ops"},
        ),
        Index(
            "ix_digests_posting_started_at",
            "posting_started_at",
            postgresql_where=text("status = 'posting'"),
        ),
        # T8-01: stale-review reaper drives off this partial index.
        Index(
            "ix_digests_status_awaiting_review",
            "awaiting_review_at",
            postgresql_where=text("status = 'awaiting_review'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    body_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSONB on postgres (enables GIN jsonb_path_ops containment for forget cascade);
    # JSON elsewhere for sqlite test compat.
    citations: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default="'[]'",  # align ORM with migration (PG migration uses ::jsonb cast)
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    llm_usage_ledger_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "llm_usage_ledger.id",
            name="fk_digests_llm_usage_ledger_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    posted_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    posted_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posting_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T8-01 / Phase 8: weekly review-gate workflow columns.
    awaiting_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DigestRun(Base):
    """Append-only audit log of one ``run_digest()`` invocation (T7-01 / alembic 037).

    One row per scheduler tick or admin ``/digest_now`` call. ``digest_id`` FK is
    ON DELETE SET NULL so audit rows survive if the parent digest is manually removed.

    Status lifecycle: running → finished | failed | skipped | cost_exceeded |
                                skipped_no_destination.
    """

    __tablename__ = "digest_runs"
    __table_args__ = (
        # T8-01 / Phase 8: 5 new audit values cover the review-gate state
        # transitions (awaiting_review, approved_for_publish, rejected_by_admin,
        # rejected_by_reaper) plus operator regeneration audit
        # (regenerated_by_admin). See alembic migration 038.
        CheckConstraint(
            "status IN ("
            "'running','finished','failed','skipped',"
            "'cost_exceeded','skipped_no_destination',"
            "'awaiting_review','approved_for_publish',"
            "'rejected_by_admin','rejected_by_reaper','regenerated_by_admin'"
            ")",
            name="ck_digest_runs_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    digest_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "digests.id",
            name="fk_digest_runs_digest_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ─── Phase 10: graph projection runs (T10-W0-A / alembic 060) ─────────────────


class GraphProjectionRun(Base):
    """Audit row for one graph projection pass (W0-A / alembic 060).

    One row per projector invocation (dry_run, incremental, full_rebuild, repair).
    Tracks source counts, projected node/edge counts, skip counts, token usage,
    cost estimates, and terminal status.

    Status lifecycle: running → completed | failed | cancelled | cost_exceeded |
                                dry_run_complete

    Mode values: dry_run, incremental, full_rebuild, repair.

    This table is the Postgres-side audit anchor for everything Neo4j-related.
    graph_provenance (migration 061), graph_edges (062), and graph_purge_pending (063)
    all reference this table's id.
    """

    __tablename__ = "graph_projection_runs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('dry_run', 'incremental', 'full_rebuild', 'repair')",
            name="ck_graph_projection_runs_mode",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', "
            "'cost_exceeded', 'dry_run_complete')",
            name="ck_graph_projection_runs_status",
        ),
        Index("ix_graph_projection_runs_started_at", text("started_at DESC")),
        # Partial index: only index rows we need to look up for monitoring/operations
        Index(
            "ix_graph_projection_runs_status",
            "status",
            postgresql_where=text("status IN ('running', 'failed')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="running", server_default="running"
    )
    source_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_card_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    source_message_version_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    projected_node_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    projected_edge_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    skipped_policy_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    skipped_budget_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    llm_prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    llm_completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    actual_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # started_by is stored as a free-text label (e.g. 'scheduler', 'admin:149820031')
    # NOT a FK — the projector can be triggered without a users row (scheduler, CLI).
    started_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class GraphProvenance(Base):
    """Source-to-graph mapping; one row per projected source triple.

    Maps each projected source (message_version or knowledge_card) to the Neo4j
    graph. Used by forget cascade (logical source_table/source_pk lookup) and by
    drift detection (active non-purged rows vs Neo4j node count).

    source_table / source_pk are logical application-code refs — NOT typed FK
    columns. FK ON DELETE CASCADE on source_card_id / source_message_version_id
    is a safety net only. See PHASE10_PLAN.md §5.A for rationale.

    Migration 061.
    """

    __tablename__ = "graph_provenance"
    __table_args__ = (
        CheckConstraint(
            "source_table IN ('message_versions', 'knowledge_cards')",
            name="ck_graph_provenance_source_table",
        ),
        CheckConstraint(
            "(source_message_version_id IS NOT NULL AND source_card_id IS NULL)"
            " OR "
            "(source_message_version_id IS NULL AND source_card_id IS NOT NULL)",
            name="ck_graph_provenance_has_source",
        ),
        CheckConstraint(
            "graph_store IN ('neo4j', 'networkx_dev')",
            name="ck_graph_provenance_graph_store",
        ),
        # Cascade forget lookup: find all provenance rows for a given message_version
        Index(
            "ix_graph_provenance_mvid",
            "source_message_version_id",
            postgresql_where=text("source_message_version_id IS NOT NULL"),
        ),
        # Cascade forget lookup: find all provenance rows for a given card
        Index(
            "ix_graph_provenance_card_id",
            "source_card_id",
            postgresql_where=text("source_card_id IS NOT NULL"),
        ),
        # Drift detection: active (non-purged) provenance rows
        Index(
            "ix_graph_provenance_active",
            "projection_run_id",
            postgresql_where=text("purged_at IS NULL"),
        ),
        # Idempotency: stable triple key within a projection run
        Index(
            "uq_graph_provenance_triple",
            "source_table",
            "source_pk",
            "triple_hash",
            unique=True,
            postgresql_where=text("purged_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    projection_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("graph_projection_runs.id", ondelete="CASCADE"), nullable=False
    )
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_pk: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("message_versions.id", ondelete="CASCADE"),
        nullable=True,
    )
    source_card_id: Mapped[_uuid_module.UUID | None] = mapped_column(
        Uuid, ForeignKey("knowledge_cards.id", ondelete="CASCADE"), nullable=True
    )
    source_content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_store: Mapped[str] = mapped_column(
        Text, nullable=False, default="neo4j", server_default="neo4j"
    )
    graph_node_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_edge_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    triple_hash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    governance_policy: Mapped[str] = mapped_column(
        Text, nullable=False, default="normal", server_default="normal"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class GraphEdge(Base):
    """Postgres-side edge registry for idempotency and drift detection.

    Neo4j holds the traversable graph; this table proves every Neo4j edge has
    a Postgres-side provenance record.

    Predicate vocabulary is CHECK-constrained to ALLOWED_PREDICATES from
    bot/services/graph_common.py.

    Migration 062.
    """

    __tablename__ = "graph_edges"
    __table_args__ = (
        CheckConstraint(
            "predicate IN ("
            "'MENTIONS', 'AUTHORED', 'KNOWS_ABOUT', 'ASKED', 'ANSWERED', "
            "'DECIDED', 'RELATED_TO', 'SUPPORTS', 'DERIVED_FROM', "
            "'PART_OF', 'CONTRADICTS', 'SUPERSEDES'"
            ")",
            name="ck_graph_edges_predicate",
        ),
        CheckConstraint(
            "confidence_score >= 0.00 AND confidence_score <= 1.00",
            name="ck_graph_edges_confidence",
        ),
        # Drift detection: Neo4j edge count vs graph_edges count must match
        Index(
            "ix_graph_edges_active",
            "graph_provenance_id",
            postgresql_where=text("purged_at IS NULL"),
        ),
        # Idempotent MERGE lookup: is this edge already projected?
        Index(
            "uq_graph_edges_key",
            "edge_key",
            unique=True,
            postgresql_where=text("purged_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    graph_provenance_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("graph_provenance.id", ondelete="CASCADE"),
        nullable=False,
    )
    subject_node_key: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_node_key: Mapped[str] = mapped_column(Text, nullable=False)
    # stable MERGE key: SHA-256(subject+predicate+object)
    edge_key: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.50"), server_default="0.50"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GraphPurgePending(Base):
    """Async purge queue for Neo4j bolt DELETE (T10-06 / alembic 063).

    Written atomically in the same Postgres transaction as the Postgres-side forget
    cascade. graph_purge_worker consumes rows and executes Neo4j DETACH DELETE via
    bolt. graph_query.py checks this table before any traversal — fails closed
    (abstained=True) while any non-purged row exists for query result nodes.

    RFC-001:415 fail-closed invariant: purged_at IS NULL means the Neo4j purge has
    NOT yet completed; graph queries must not return those nodes.

    Migration 063.
    """

    __tablename__ = "graph_purge_pending"
    __table_args__ = (
        CheckConstraint(
            "source_table IN ('message_versions', 'knowledge_cards', 'card_sources')",
            name="ck_graph_purge_pending_source_table",
        ),
        # CRITICAL-1 fix (T10-06): include graph_provenance_id so multiple provenance rows
        # for same (source_table, source_pk) each get their own purge_pending row.
        # Previously: (forget_event_id, source_table, source_pk) — collapsed multi-provenance.
        # Migration 065 drops old constraint and creates this one.
        UniqueConstraint(
            "forget_event_id",
            "source_table",
            "source_pk",
            "graph_provenance_id",
            name="uq_graph_purge_pending_event_source_prov",
        ),
        Index(
            "ix_graph_purge_pending_queue",
            "enqueued_at",
            postgresql_where=text("purged_at IS NULL AND failed_at IS NULL"),
        ),
        Index(
            "ix_graph_purge_pending_node_key",
            "graph_node_key",
            postgresql_where=text("purged_at IS NULL"),
        ),
        Index("ix_graph_purge_pending_forget_event", "forget_event_id"),
        Index("ix_graph_purge_pending_source", "source_table", "source_pk"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    forget_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_pk: Mapped[str] = mapped_column(Text, nullable=False)
    # known at enqueue time if provenance row exists
    graph_node_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_edge_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_provenance_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("graph_provenance.id", ondelete="SET NULL"),
        nullable=True,
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), default=0
    )


# ─── Phase 12: Butler / Action layer (T12-01 / alembic 070-073) ───────────────


class ButlerAction(Base):
    """Butler action audit row — one row per /butler request (T12-01 / alembic 070).

    Captures the full lifecycle of a Butler action from initial request through
    execution and optional undo. Status is CHECK-constrained to a fixed state
    machine (see ck_butler_actions_status). The governance_filter_version column
    is frozen at action creation time and NEVER recomputed — if the version
    changes mid-flight the action must be expired (C5/I9.e contract).

    Key constraints:
    - ck_butler_actions_ledger_required_post_plan: once LLM budget is spent
      (status NOT IN 'rejected'/'expired'/'cancelled'), llm_usage_ledger_id MUST
      exist.
    - ck_butler_actions_executed_has_inverse: executed/succeeded rows must have
      inverse_op_payload OR rollback_kind='not_reversible'.
    """

    __tablename__ = "butler_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'requested','evidence_loaded','planned','pending_confirmation',"
            "'confirmed','executing','succeeded','undone',"
            "'undo_pending','undo_succeeded','undo_failed',"
            "'rejected','expired','execution_failed','cancelled'"
            ")",
            name="ck_butler_actions_status",
        ),
        CheckConstraint(
            "tool_name IN ("
            "'recall_evidence','schedule_meeting','send_intro',"
            "'update_intro','suggest_card_creation'"
            ")",
            name="ck_butler_actions_tool_name",
        ),
        CheckConstraint(
            "rollback_kind IN ("
            "'delete_message','edit_message','followup_correction',"
            "'cancel_pending','not_reversible'"
            ")",
            name="ck_butler_actions_rollback_kind",
        ),
        CheckConstraint(
            "risk_level IN ('low','medium','high')",
            name="ck_butler_actions_risk_level",
        ),
        CheckConstraint(
            "confirmation_policy IN ('per_action','opt_in_by_button')",
            name="ck_butler_actions_confirmation_policy",
        ),
        CheckConstraint(
            "action_type IN ('meeting','intro','intro_update','card_suggestion','recall')",
            name="ck_butler_actions_action_type",
        ),
        CheckConstraint(
            "(status NOT IN ('succeeded','undo_pending','undo_succeeded')) "
            "OR (inverse_op_payload IS NOT NULL OR rollback_kind = 'not_reversible')",
            name="ck_butler_actions_executed_has_inverse",
        ),
        CheckConstraint(
            "status IN ('rejected','expired','cancelled') OR llm_usage_ledger_id IS NOT NULL",
            name="ck_butler_actions_ledger_required_post_plan",
        ),
        Index(
            "ix_butler_actions_requester_created",
            "requester_tg_id",
            "created_at",
        ),
        Index(
            "ix_butler_actions_chat_created",
            "chat_id",
            "created_at",
        ),
        Index(
            "ix_butler_actions_status_expires",
            "status",
            "expires_at",
            postgresql_where=text("status IN ('pending_confirmation','planned')"),
        ),
        Index(
            "ix_butler_actions_parent",
            "parent_action_id",
            postgresql_where=text("parent_action_id IS NOT NULL"),
        ),
        Index(
            "ix_butler_actions_llm_ledger",
            "llm_usage_ledger_id",
            postgresql_where=text("llm_usage_ledger_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_uuid: Mapped[_uuid_module.UUID] = mapped_column(
        Uuid(),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    # ON DELETE RESTRICT (not SET NULL) preserves immutable audit chain: undo operations
    # write a NEW row pointing back to the original; the parent row is never mutated or
    # deleted. RESTRICT ensures the audit history cannot be silently destroyed via parent
    # deletion. Design choice rationale: Codex HIGH #1 accepted as-is.
    parent_action_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "butler_actions.id",
            name="fk_butler_actions_parent_action_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    # Scalar tg_id (not FK to users.id) matches the chat_messages.from_user_id pattern.
    # affected_user (affected_tg_id) may not yet have a registered users row at
    # action-plan time — e.g. cross-user intro from a member to a non-registered target.
    # FK enforcement is deferred to runtime checks in the butler service layer.
    # Design choice rationale: Codex HIGH #2 accepted as-is.
    requester_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_manifest_version: Mapped[str] = mapped_column(Text, nullable=False)
    governance_filter_version: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_context_hash: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'[]'"),
    )
    approved_card_source_ids: Mapped[list] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'[]'"),
    )
    plan_summary: Mapped[str] = mapped_column(Text, nullable=False)
    action_args: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    action_args_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # migration 074: query / visibility_scope / plan_payload added in T12-04 fix cycle.
    # query: the original /butler request text forwarded to the LLM (needed for
    #   evidence re-revalidation in confirm_action without storing it elsewhere).
    # visibility_scope: frozen at plan time, replayed at confirm/execute for hash check.
    # plan_payload: the full serialized ButlerPlan; allows multi-step replay in execute_action.
    query: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    visibility_scope: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'member'")
    )
    plan_payload: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    result_payload: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    result_payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    inverse_op_payload: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    rollback_kind: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    confirmation_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'per_action'")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_context: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    llm_usage_ledger_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "llm_usage_ledger.id",
            name="fk_butler_actions_llm_usage_ledger_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ButlerToolInvocation(Base):
    """One tool invocation within a Butler action (T12-01 / alembic 070).

    A single ButlerAction may spawn multiple invocations (e.g. retries with
    incremented invocation_seq). idempotency_key is globally UNIQUE to prevent
    double-execution on process restart.

    FK to butler_actions ON DELETE RESTRICT — invocations cannot outlive their
    parent action.
    """

    __tablename__ = "butler_tool_invocations"
    __table_args__ = (
        CheckConstraint(
            "tool_name IN ("
            "'recall_evidence','schedule_meeting','send_intro',"
            "'update_intro','suggest_card_creation'"
            ")",
            name="ck_butler_tool_invocations_tool_name",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','rolled_back')",
            name="ck_butler_tool_invocations_status",
        ),
        CheckConstraint(
            "invocation_seq >= 1",
            name="ck_butler_tool_invocations_seq_positive",
        ),
        Index("ix_butler_tool_invocations_action", "action_id"),
        Index("ix_butler_tool_invocations_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "butler_actions.id",
            name="fk_butler_tool_invocations_action_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    invocation_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_payload: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    request_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    response_payload_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_context: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )
    # posted_message_id — migration 076 (T12-06-fix C2).
    # Written by send_intro / schedule_meeting after bot.send_message() succeeds.
    # NULL for tools that produce no Telegram output (recall_evidence, suggest_card_creation).
    # Used by update_intro for Butler ownership verification.
    posted_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # inverse_op_payload — migration 078 (T12-07-fix C1).
    # Written by execute_action after tool.build_inverse(result) succeeds.
    # Contains rollback_kind + tool-specific params for /butler_undo dispatch.
    # NULL for in-flight or pre-078 invocations.
    inverse_op_payload: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )


class ButlerActionConfirmation(Base):
    """Confirmation request sent to a user for a Butler action (T12-01 / alembic 070).

    One confirmation row per (action, confirmer) pair. A single action may require
    multiple confirmations (e.g. requester + affected_user + admin).

    FK to butler_actions ON DELETE RESTRICT — confirmations cannot outlive their
    parent action.
    """

    __tablename__ = "butler_action_confirmations"
    __table_args__ = (
        CheckConstraint(
            "confirmation_role IN ('requester','affected_user','admin','rollback_requester')",
            name="ck_butler_action_confirmations_role",
        ),
        CheckConstraint(
            "status IN ('pending','confirmed','rejected','expired','cancelled','revoked')",
            name="ck_butler_action_confirmations_status",
        ),
        Index("ix_butler_action_confirmations_action", "action_id"),
        Index(
            "ix_butler_action_confirmations_status_expires",
            "status",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "butler_actions.id",
            name="fk_butler_action_confirmations_action_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    confirmer_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confirmation_role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    confirmation_message_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmation_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    preview_payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # migration 074: opaque per-confirmation token (secrets.token_urlsafe(32)).
    # UNIQUE index enforced at DB level (uq_butler_action_confirmations_token).
    # confirm_action verifies presented token == stored token; bad token → bad_token error_kind.
    confirmation_token: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ButlerRateBucket(Base):
    """Calendar-window rate bucket for Butler per-user/per-chat limiting (T12-01 / alembic 072).

    Rows are written via atomic ON CONFLICT upsert (see ButlerRateBucketRepo.try_increment).
    The UNIQUE constraint on (bucket_kind, scope_id, bucket_key) makes the upsert race-free:
    concurrent inserts for the same window always converge on the same row.

    count is bounded by ceiling (ck_butler_rate_buckets_count_nonneg_under_ceiling).
    The upsert WHERE clause (count < ceiling) ensures ceiling is never exceeded.
    """

    __tablename__ = "butler_rate_buckets"
    __table_args__ = (
        CheckConstraint(
            "bucket_kind IN ("
            "'user_plans_day','user_execs_day','chat_actions_day',"
            "'tool_hour:recall_evidence','tool_hour:schedule_meeting',"
            "'tool_hour:send_intro','tool_hour:update_intro','tool_hour:suggest_card_creation'"
            ")",
            name="ck_butler_rate_buckets_kind",
        ),
        CheckConstraint(
            "window_end > window_start",
            name="ck_butler_rate_buckets_window_positive",
        ),
        CheckConstraint(
            "count >= 0 AND count <= ceiling",
            name="ck_butler_rate_buckets_count_nonneg_under_ceiling",
        ),
        CheckConstraint(
            "ceiling > 0",
            name="ck_butler_rate_buckets_ceiling_positive",
        ),
        UniqueConstraint(
            "bucket_kind",
            "scope_id",
            "bucket_key",
            name="uq_butler_rate_buckets_kind_scope_key",
        ),
        Index("ix_butler_rate_buckets_window_end", "window_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bucket_key: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ButlerCardSuggestion(Base):
    """Mapping between a Butler suggest_card_creation action and the Phase 6 review queue
    (T12-01 / alembic 073).

    UNIQUE on butler_action_id: one /butler request → exactly one suggestion row.
    extraction_candidate_id is NULLABLE (ON DELETE SET NULL) because the candidate
    may be created asynchronously after the Butler suggestion is written.

    The Phase 6 admin-review surface is unchanged — the admin sees a normal
    extraction_candidates row; this mapping table provides Butler-side audit linkage.
    """

    __tablename__ = "butler_card_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "butler_action_id",
            name="uq_butler_card_suggestions_action",
        ),
        Index(
            "ix_butler_card_suggestions_candidate",
            "extraction_candidate_id",
            postgresql_where=text("extraction_candidate_id IS NOT NULL"),
        ),
        Index("ix_butler_card_suggestions_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    butler_action_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "butler_actions.id",
            name="fk_butler_card_suggestions_butler_action_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    extraction_candidate_id: Mapped[_uuid_module.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey(
            "extraction_candidates.id",
            name="fk_butler_card_suggestions_extraction_candidate_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    suggested_card_payload: Mapped[dict] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            name="fk_butler_card_suggestions_created_by_user_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )


# ─── Phase 12: Butler undo audit (T12-07 / alembic 077) ──────────────────────


class ButlerUndoInvocation(Base):
    """Per-step undo audit row — one row per tool invocation rolled back during /butler_undo.

    The UNIQUE constraint on (butler_action_id, butler_tool_invocation_id) provides
    idempotency: re-running /butler_undo on an already-undone action returns the
    existing rows, no double-side-effect.

    FK to butler_actions ON DELETE RESTRICT — undo audit rows cannot outlive
    the parent action (immutable audit chain). Cascade integration: forget_cascade
    _cascade_butler_actions must run AFTER this layer so undo audit rows are
    processed before the parent action row is masked.
    """

    __tablename__ = "butler_undo_invocations"
    __table_args__ = (
        CheckConstraint(
            "rollback_kind IN ("
            "'not_reversible','delete_message','edit_message',"
            "'followup_correction','cancel_pending'"
            ")",
            name="ck_butler_undo_invocations_rollback_kind",
        ),
        CheckConstraint(
            "status IN ('pending','succeeded','failed','skipped_not_reversible')",
            name="ck_butler_undo_invocations_status",
        ),
        UniqueConstraint(
            "butler_action_id",
            "butler_tool_invocation_id",
            name="uq_butler_undo_invocations_action_invocation",
        ),
        Index("ix_butler_undo_invocations_butler_action_id", "butler_action_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    butler_action_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "butler_actions.id",
            name="fk_butler_undo_action_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    butler_tool_invocation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "butler_tool_invocations.id",
            name="fk_butler_undo_invocation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    requester_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rollback_kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    error_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
