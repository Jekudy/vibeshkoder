# Vibe Coders Telegram Gatekeeper Bot — Technical Specification

## 1. Project Structure

```
vibe-gatekeeper/
├── docker-compose.yml
├── Dockerfile.bot
├── Dockerfile.web
├── .env.example
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
├── bot/
│   ├── __init__.py
│   ├── __main__.py              # entry point
│   ├── config.py                # pydantic-settings
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py            # async SQLAlchemy engine
│   │   ├── models.py            # all ORM models
│   │   └── repos/
│   │       ├── __init__.py
│   │       ├── user.py
│   │       ├── questionnaire.py
│   │       ├── vouch.py
│   │       ├── message.py
│   │       └── application.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── sheets.py            # Google Sheets sync
│   │   ├── invite.py            # invite link logic
│   │   └── scheduler.py         # APScheduler jobs
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── start.py             # /start command
│   │   ├── questionnaire.py     # FSM for 7 questions
│   │   ├── vouch.py             # "Ручаюсь" callback
│   │   ├── admin.py             # /chatid, /stats
│   │   ├── forward_lookup.py    # forwarded message → intro
│   │   └── chat_messages.py     # save all group messages
│   ├── keyboards/
│   │   ├── __init__.py
│   │   └── inline.py
│   ├── states/
│   │   ├── __init__.py
│   │   └── questionnaire.py     # StatesGroup
│   ├── middlewares/
│   │   ├── __init__.py
│   │   └── db_session.py
│   ├── filters/
│   │   ├── __init__.py
│   │   └── chat_type.py
│   └── texts.py                 # all user-facing strings
├── web/
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py                   # FastAPI app
│   ├── config.py
│   ├── auth.py                  # Telegram Login Widget
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── dashboard.py
│   │   └── members.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── login.html
│   │   ├── dashboard.html
│   │   └── members.html
│   └── static/
│       └── style.css
├── tests/
│   ├── conftest.py
│   ├── test_questionnaire.py
│   ├── test_vouch.py
│   ├── test_sheets.py
│   └── test_scheduler.py
└── pyproject.toml
```

## 2. Database Schema (PostgreSQL, SQLAlchemy 2.0 async)

### 2.1 Table: `users`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BIGINT | PK | Telegram user ID |
| `username` | VARCHAR(255) | NULLABLE | @username |
| `first_name` | VARCHAR(255) | NOT NULL | |
| `last_name` | VARCHAR(255) | NULLABLE | |
| `is_member` | BOOLEAN | DEFAULT false | Currently in chat |
| `is_admin` | BOOLEAN | DEFAULT false | Manually flagged |
| `joined_at` | TIMESTAMPTZ | NULLABLE | |
| `left_at` | TIMESTAMPTZ | NULLABLE | |
| `created_at` | TIMESTAMPTZ | DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | DEFAULT now() | |

### 2.2 Table: `applications`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | SERIAL | PK | |
| `user_id` | BIGINT | FK → users.id | Applicant |
| `status` | VARCHAR(20) | NOT NULL | `filling`, `pending`, `vouched`, `added`, `rejected`, `privacy_block` |
| `questionnaire_message_id` | BIGINT | NULLABLE | Message ID in community chat |
| `vouched_by` | BIGINT | FK → users.id, NULLABLE | |
| `vouched_at` | TIMESTAMPTZ | NULLABLE | |
| `notified_admin_at` | TIMESTAMPTZ | NULLABLE | 48h notification sent |
| `nudged_newcomer_at` | TIMESTAMPTZ | NULLABLE | 48h nudge sent |
| `rejected_at` | TIMESTAMPTZ | NULLABLE | |
| `added_at` | TIMESTAMPTZ | NULLABLE | |
| `created_at` | TIMESTAMPTZ | DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | DEFAULT now() | |

Index: `(user_id, status)`

### 2.3 Table: `questionnaire_answers`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | SERIAL | PK | |
| `user_id` | BIGINT | FK → users.id | |
| `application_id` | INT | FK → applications.id, NULLABLE | NULL for existing-member intros |
| `question_index` | SMALLINT | NOT NULL | 0-6 |
| `question_text` | TEXT | NOT NULL | |
| `answer_text` | TEXT | NOT NULL | |
| `created_at` | TIMESTAMPTZ | DEFAULT now() | |
| `is_current` | BOOLEAN | DEFAULT true | false after refresh |

Index: `(user_id, is_current)`

### 2.4 Table: `intros`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | SERIAL | PK | |
| `user_id` | BIGINT | FK → users.id, UNIQUE | |
| `intro_text` | TEXT | NOT NULL | Formatted intro |
| `vouched_by_name` | VARCHAR(255) | NOT NULL | Display name or "времена до бота" |
| `sheets_row_number` | INT | NULLABLE | |
| `last_synced_at` | TIMESTAMPTZ | NULLABLE | |
| `created_at` | TIMESTAMPTZ | DEFAULT now() | |
| `updated_at` | TIMESTAMPTZ | DEFAULT now() | |

### 2.5 Table: `chat_messages`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BIGSERIAL | PK | |
| `message_id` | BIGINT | NOT NULL | Telegram msg ID |
| `chat_id` | BIGINT | NOT NULL | |
| `user_id` | BIGINT | FK → users.id | |
| `text` | TEXT | NULLABLE | |
| `date` | TIMESTAMPTZ | NOT NULL | |
| `raw_json` | JSONB | NULLABLE | |
| `created_at` | TIMESTAMPTZ | DEFAULT now() | |

Index: `(chat_id, message_id)` UNIQUE

### 2.6 Table: `intro_refresh_tracking`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | SERIAL | PK | |
| `user_id` | BIGINT | FK → users.id | |
| `cycle_started_at` | TIMESTAMPTZ | NOT NULL | |
| `reminders_sent` | SMALLINT | DEFAULT 0 | |
| `last_reminder_at` | TIMESTAMPTZ | NULLABLE | |
| `phase` | VARCHAR(20) | NOT NULL | `daily`, `every_2_days`, `done` |
| `completed` | BOOLEAN | DEFAULT false | |

### 2.7 Table: `vouch_log`

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | SERIAL | PK | |
| `voucher_id` | BIGINT | FK → users.id | |
| `vouchee_id` | BIGINT | FK → users.id | |
| `application_id` | INT | FK → applications.id | |
| `created_at` | TIMESTAMPTZ | DEFAULT now() | |

## 3. FSM States for Questionnaire

```python
class QuestionnaireForm(StatesGroup):
    q1_name = State()
    q2_location = State()
    q3_source = State()
    q4_experience = State()
    q5_projects = State()
    q6_hardest = State()
    q7_goals = State()
    confirm = State()
```

### 3.1 /start Logic

```
/start (private chat)
  ├── No active application AND not member → new applicant, start q1
  ├── Active application (filling) → resume from last question
  ├── Active application (pending) → "waiting for vouch"
  ├── Active application (privacy_block) → show "Я готов" button
  ├── Is member AND no intro → existing member flow, start q1
  ├── Is member AND has intro → "already have intro, use /refresh"
  └── Previously rejected → allow new application
```

### 3.2 Confirm Handler

- "Подтвердить": assemble intro, post to chat (new) or save directly (existing member)
- "Заполнить заново": delete answers, restart from q1

## 4. Handler Specifications

### 4.1 Vouch Handler

Trigger: `CallbackQuery` with `vouch:{application_id}`
1. Verify clicker is member and not the applicant
2. Optimistic lock: `UPDATE applications SET status='vouched' WHERE id=:id AND status='pending'`
3. Insert vouch_log
4. Delete questionnaire message from chat
5. Send invite link to applicant via DM

### 4.2 Join Detection

Trigger: `ChatMemberUpdated` → new status = `member`
1. Update application to `added`
2. Set `users.is_member = true`
3. Post intro in community chat
4. Sync to Google Sheets

### 4.3 Privacy Block

If invite fails → set `privacy_block`, show "Я готов" button
On "Я готов" click → generate new invite, retry

### 4.4 Forward Lookup

Trigger: forwarded message in private chat
1. Extract text
2. `SELECT user_id FROM chat_messages WHERE text = :text ORDER BY date DESC LIMIT 1`
3. Return author's intro or error

### 4.5 Chat Message Collector

All messages in community chat → save to `chat_messages` (lowest priority handler)

### 4.6 Admin Commands

- `/chatid` — reply with chat ID (group only)
- `/stats` — funnel counts (private, admin only)
- `/force_refresh` — trigger refresh cycle (admin only)

## 5. Scheduled Jobs (APScheduler)

### 5.1 Vouch Deadline Checker (every 15 min)

- ≥72h pending → auto-reject, delete message, DM applicant
- ≥48h pending (not yet notified) → DM admin + nudge newcomer

### 5.2 Intro Refresh (daily at 10:00 UTC)

For each member with intro older than 90 days:
- Phase `daily`: send reminder, up to 5 days
- Phase `every_2_days`: 3 more reminders
- Phase `done`: stop until next cycle

### 5.3 Google Sheets Sync (every 5 min)

- Read all rows, compare with local DB
- Sheet edits → update local DB (sheet is source of truth)
- New local intros → append to sheet
- Update status column: "есть интро" / "нет интро"

## 6. Google Sheets Structure

| Telegram ID | Username | Имя | Локация | Откуда узнал | Опыт | Проекты | Самое сложное | Цели | Кто поручился | Статус |

## 7. Web Interface

### Auth: Telegram Login Widget → HMAC-SHA256 verification → signed cookie
### Routes:
- GET /login — Telegram widget
- GET /dashboard — funnel stats
- GET /members — member list with intro status

### Tech: FastAPI + Jinja2 + Bootstrap 5 CDN

## 8. Docker Compose

Services: `bot`, `web`, `db` (postgres:16-alpine), `redis` (redis:7-alpine)

FSM storage: `RedisStorage` (persistent across restarts)

## 9. Configuration

```python
class Settings(BaseSettings):
    BOT_TOKEN: str
    COMMUNITY_CHAT_ID: int
    ADMIN_IDS: list[int]
    DATABASE_URL: str
    GOOGLE_SHEETS_CREDS_FILE: str
    GOOGLE_SHEET_ID: str
    WEB_BASE_URL: str
    VOUCH_TIMEOUT_HOURS: int = 72
    NUDGE_TIMEOUT_HOURS: int = 48
    INTRO_REFRESH_DAYS: int = 90
```

## 10. Edge Cases

| # | Scenario | Handling |
|---|---|---|
| 1 | New user, not in chat | Start questionnaire |
| 2 | Abandoned mid-questionnaire | Resume on /start |
| 3 | 48h no vouch | Nudge newcomer + notify admin |
| 4 | 72h no vouch | Auto-reject |
| 5 | Rejected user re-applies | Allow new application |
| 6 | Member leaves chat | Set is_member=false, keep intro |
| 7 | Left member re-joins | Treat as new applicant |
| 8 | Existing member, no intro | Existing member flow |
| 9 | Existing member, has intro | Direct to /refresh |
| 10 | Double vouch (race) | Optimistic locking |
| 11 | Non-member vouches | Reject |
| 12 | Self-vouch | Reject |
| 13 | Privacy block on join | "Я готов" flow |
| 14 | Forward with no text | Error message |
| 15 | Forward text not in DB | Error message |
| 16 | Sheets API down | Log, retry next cycle |
| 17 | Admin edits in Sheets | Sync picks up change |
| 18 | Bot restart mid-questionnaire | Redis FSM survives |

## 11. Callback Data

```python
class VouchCallback(CallbackData, prefix="vouch"):
    application_id: int

class ReadyCallback(CallbackData, prefix="ready"):
    application_id: int

class ConfirmCallback(CallbackData, prefix="confirm"):
    action: str  # "yes" or "redo"
```
