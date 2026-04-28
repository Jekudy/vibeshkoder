from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

    Idempotency: ``(chat_message_id, content_hash)`` should be unique in practice — the
    repo's ``insert_version`` returns the existing row if a version with the same hash
    already exists for the same message. This is checked in code; the DB still allows it
    via the looser ``(chat_message_id, version_seq)`` unique constraint, which is the
    structural invariant.

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
        UniqueConstraint(
            "chat_message_id",
            "content_hash",
            name="uq_message_versions_chat_message_content_hash",
        ),
        Index("ix_message_versions_content_hash", "content_hash"),
        Index("ix_message_versions_captured_at", "captured_at"),
        Index("ix_message_versions_chat_message_id", "chat_message_id"),
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
            "status IN ('pending','processing','completed','failed')",
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

    Status lifecycle: running → completed | failed | cancelled. Dry-runs may use
    ``status='dry_run'`` as a terminal state to make filter queries explicit.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN ('live','import','dry_run','cancelled')",
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
    import updates leave ``update_id`` NULL; the importer enforces its own idempotency
    via ``raw_hash`` + ``ingestion_run_id``.

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
