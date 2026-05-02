"""Observability hook surface — stop signal emission.

Phase 5 / T5-01 introduces the first caller of ``emit_stop_signal`` from
``bot/services/llm_gateway.py`` (structural provider failures). The
implementation is intentionally minimal: it logs at WARNING level so the
operator alarm fires through the existing logging pipeline. A future
ticket may wire this into Prometheus / Sentry / OpsGenie.

The function is a no-PII no-secret single-line entry — pass only the
event name (e.g. ``"llm_provider_structural"``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def emit_stop_signal(name: str) -> None:
    """Emit a named stop signal for operator review.

    Parameters
    ----------
    name:
        Stable, low-cardinality identifier of the alarm condition. The
        gateway emits ``"llm_provider_structural"`` for auth /
        bad_request / contract_violation / model_not_found errors that
        indicate provider-side configuration breakage.

    The function never raises — observability failures must not corrupt
    the calling control flow (HANDOFF §1 invariant #1).
    """
    try:
        logger.warning("STOP_SIGNAL %s", name)
    except Exception:  # pragma: no cover — defensive fallback
        pass


__all__ = ["emit_stop_signal"]
