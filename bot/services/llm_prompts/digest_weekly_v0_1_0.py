"""Weekly digest synthesis prompt template — v0.1.0 (Phase 8 / T8-02).

Used by `bot/services/llm_gateway.py::synthesize_digest(type='weekly')`
per `docs/memory-system/PHASE8_PLAN.md` §5.F. Output format is enforced by
parsing in the caller — if the LLM violates the bullet-level citation
invariant the caller raises `DigestCitationValidationError` and the digest
is marked `failed`.

Section header format is a SOFT contract: the prompt asks the LLM to use
one of five Russian titles (`SECTION_NAME_ALLOWLIST`), but the caller emits
a structured warning rather than failing when an off-allowlist title is
returned. Hard enforcement is a Phase 8.5 backlog item.
"""

from __future__ import annotations

PROMPT_VERSION = "digest-weekly-v0.1.0"

# §5.F M1 — five canonical Russian section titles. The prompt instructs the
# LLM to use only these; the caller emits a structured warning if a returned
# section header carries an off-allowlist title (soft contract).
SECTION_NAME_ALLOWLIST: frozenset[str] = frozenset(
    {
        "Объявления",
        "Обсуждения",
        "Знания и ресурсы",
        "Встречи и события",
        "Прочее",
    }
)

SYSTEM_PROMPT = """You are writing an automatic WEEKLY digest for a private community chat.
It will be published without manual approval, so include only facts supported
by the provided evidence and make uncertainty explicit.

Output format (strict):
  Line 1-4: TL;DR — 3-4 short sentences in Russian, prose. Cover the
            week's main themes at a high level.
  Blank line.
  Then 2-5 SECTIONS, each separated by a blank line. Section header
  format (strict, used by the renderer to bold the heading):
    ## Раздел: {section_title}
  Section title MUST be one of these allowed prefixes (in Russian):
    - Объявления
    - Обсуждения
    - Знания и ресурсы
    - Встречи и события
    - Прочее
  Within each section, 3-7 bullets:
    - Topic title (≤10 words).
    - 1-2 sentence summary.
    - Citation tokens: [[cs:UUID]] for an approved card source,
      [[mv:INT]] for a raw message version. EVERY bullet MUST contain
      at least one citation token referencing input ids verbatim.
  Skip a section entirely if there is no material for it. Do NOT
  invent section names. Do NOT cite ids absent from the input.

Use Russian. Be neutral. Do not invent facts.
If the input has no cards and no messages, return exactly: EMPTY_WINDOW
"""


def build_user_prompt(
    *,
    window_start_msk: str,
    window_end_msk: str,
    cards: list,
    messages: list,
) -> str:
    """Compose the user-side of the weekly digest prompt. Returns a single string.

    Mirrors `digest_v0_1_0.build_user_prompt` shape — cards section + messages
    section, separated by `---`. Window framing names "ISO week" to clarify
    the weekly cadence to the LLM.
    """
    lines = [
        f"Window: {window_start_msk} .. {window_end_msk} (Europe/Moscow, ISO week)",
        f"Cards ({len(cards)}):",
    ]
    for c in cards:
        sids_csv = ", ".join(str(s) for s in c.card_source_ids)
        lines.append(f'  Card "{c.title}" (approved). Source ids you may cite: {sids_csv}')
        lines.append(f"  Card body: {c.body_markdown}")
        lines.append("  ---")
    lines.append(f"Messages ({len(messages)}):")
    for m in messages:
        lines.append(
            f"  [mv:{m.message_version_id}] {m.author_display}, {m.ts.isoformat()}: {m.text}"
        )
        lines.append("  ---")
    return "\n".join(lines)
