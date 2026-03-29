"""Cross-division escalation helpers — pure Python, no LLM."""
import logging

log = logging.getLogger(__name__)

# Urgency thresholds
_URGENCY_LEVELS = ("low", "normal", "high", "critical")


def classify_urgency(
    *,
    escalate: bool = False,
    breach_active: bool = False,
    tier_a_count: int = 0,
    failure_rate: float = 0.0,
) -> str:
    """
    Classify urgency based on cross-division signals.

    Returns one of: 'low', 'normal', 'high', 'critical'.
    """
    if breach_active:
        return "critical"
    if escalate and tier_a_count >= 3:
        return "critical"
    if escalate or tier_a_count >= 1:
        return "high"
    if failure_rate > 0.5:
        return "high"
    if failure_rate > 0.2:
        return "normal"
    return "low"


def should_escalate(
    *,
    status: str = "success",
    breach_active: bool = False,
    tier_a_count: int = 0,
    consecutive_failures: int = 0,
) -> bool:
    """
    Determine whether a packet warrants escalation to Matthew.

    Considers breach state, tier-A job count, and failure streaks.
    """
    if breach_active:
        return True
    if tier_a_count > 0:
        return True
    if status == "failed" and consecutive_failures >= 2:
        return True
    return False
