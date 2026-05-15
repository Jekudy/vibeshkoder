"""Digest synthesis prompt template — v0.1.0.

Used by `bot/services/llm_gateway.py::synthesize_digest` (T7-02 / Phase 7).
Output format is enforced by parsing in the caller — if the LLM violates
format, the caller raises and the digest is marked `failed`.
"""

from __future__ import annotations

PROMPT_VERSION = "digest-v0.1.0"

SYSTEM_PROMPT = """You are writing a daily digest for a private community chat.

Output format (strict):
  Line 1-3: TL;DR — 3 short sentences in Russian, prose.
  Blank line.
  Then 5-7 bullets, each:
    - Topic title (≤8 words).
    - 1-2 sentence summary.
    - Citation tokens: [[cs:UUID]] for an approved card source, [[mv:INT]] for
      a raw message version. EVERY bullet MUST contain at least one citation
      token. Citation tokens MUST reference verbatim ids from the input below.

Use Russian. Be neutral. Do not invent facts.
Citations MUST reference input ids verbatim. Do not invent ids.
If the input has no cards and no messages, return exactly: EMPTY_WINDOW
"""


def build_user_prompt(
    *,
    window_start_msk: str,
    window_end_msk: str,
    cards: list,
    messages: list,
) -> str:
    """Compose the user-side of the digest prompt. Returns a single string."""
    lines = [
        f"Window: {window_start_msk} .. {window_end_msk} (Europe/Moscow)",
        f"Cards ({len(cards)}):",
    ]
    for c in cards:
        # c is DigestContextCard from bot.services.digest_context
        sids_csv = ", ".join(str(s) for s in c.card_source_ids)
        lines.append(f'  Card "{c.title}" (approved). Source ids you may cite: {sids_csv}')
        lines.append(f"  Card body: {c.body_markdown}")
        lines.append("  ---")
    lines.append(f"Messages ({len(messages)}):")
    for m in messages:
        # m is DigestContextMessage
        lines.append(
            f"  [mv:{m.message_version_id}] {m.author_display},"
            f" {m.ts.isoformat()}: {m.text}"
        )
        lines.append("  ---")
    return "\n".join(lines)
