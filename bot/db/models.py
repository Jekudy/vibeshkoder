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
    func,
    text,
)
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

    intro: Mapped[Intro | None] = relationship("Intro", back_populates="user", foreign_keys="[Intro.user_id]")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (Index("ix_applications_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20))  # filling, pending, vouched, added, rejected, privacy_block
    questionnaire_message_id: Mapped[int | None] = mapped_column(BigInteger)
    vouched_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    vouched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_chat_msg", "chat_id", "message_id", unique=True),
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
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
        Integer, ForeignKey("ingestion_runs.id"), nullable=True
    )
    is_redacted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    redaction_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), server_default=func.now()
    )
