"""Identify Telegram commands that must stay out of derived knowledge."""

from __future__ import annotations

import re

_SQL_ALIAS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def control_message_excludes_sql_fragment(alias: str = "mv") -> str:
    """Return a SQL predicate that accepts only non-command messages."""
    if _SQL_ALIAS_RE.fullmatch(alias) is None:
        raise ValueError("message-version SQL alias must be a simple identifier")
    raw_payload = f"{alias}.entities_json::jsonb"
    payload = (
        "(CASE "
        f"WHEN jsonb_typeof({raw_payload}) = 'string' "
        f"THEN ({raw_payload} #>> '{{}}')::jsonb "
        f"ELSE {raw_payload} END)"
    )
    text_value = f"COALESCE(NULLIF({alias}.normalized_text, ''), {alias}.text, {alias}.caption, '')"
    return f"""(
        NOT ({text_value} ~ '^/[A-Za-z0-9_]+(@[A-Za-z0-9_]+)?([[:space:]]|$)')
        AND NOT COALESCE(
            jsonb_path_exists(
                {payload},
                '$[*] ? (@.type == "bot_command" && @.offset == 0)'
            ),
            FALSE
        )
    )"""
