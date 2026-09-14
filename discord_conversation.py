"""Deterministic, read-only Discord conversation routing.

This module returns labels only. It has no network access, state, credentials,
or dispatch helpers, and cannot turn conversational requests into writes.
"""

from __future__ import annotations

import re


_EXACT_READ_ONLY_ACTIONS = {
    "help": "help",
    "!help": "help",
    "status": "status",
    "show status": "status",
    "health": "health",
    "show health": "health",
    "portfolio": "portfolio",
    "show portfolio": "portfolio",
}
_MUTATION_VERBS = (
    r"enable|disable|change|set|increase|decrease|pause|resume|stop|start|"
    r"cancel|confirm|run|refresh|analyse|analyze|buy|purchase|order|sell"
)
_MUTATION_WORD = re.compile(rf"\b(?:{_MUTATION_VERBS})\b")
_EXPLICIT_MUTATION = re.compile(
    # Only command-like positions qualify here. Questions such as "When will
    # you buy?" contain the same verbs but ask for existing schedule information.
    rf"(?:^|[.!?;:,\n]|\b(?:and|then|also|but)\s+)\s*"
    rf"(?:(?:please|kindly)\s+)?"
    rf"(?:(?:can|could|would|will)\s+you\s+)?"
    rf"(?:(?:please|kindly)\s+)?(?:{_MUTATION_VERBS})\b"
    rf"|\b(?:i\s+(?:want|need)|i['’]d\s+like)\s+(?:you\s+)?to\s+"
    rf"(?:{_MUTATION_VERBS})\b"
)
_READ_ONLY_PURCHASE_QUESTION = re.compile(
    r"^(?:(?:please\s+)?(?:tell\s+me|explain|show\s+me)\s+)?"
    r"(?:when\b|why\b|how\s+(?:much|many|often)\b|what\s+(?:time|amount)\b|"
    r"(?:did|have|has)\b)"
)
_READ_ONLY_PURCHASE_PHRASE = re.compile(
    r"\b(?:next|last|previous|scheduled|planned|missed|pending)\s+"
    r"(?:buy|purchase|order)\b|\border\s+status\b"
)


def _normalized_text(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".!?。！？").strip()


def exact_read_only_action(text: str) -> str | None:
    """Resolve only complete, familiar read commands; never partial matches."""

    return _EXACT_READ_ONLY_ACTIONS.get(_normalized_text(text))


def rule_based_read_only_intent(text: str) -> dict[str, str]:
    """Understand common questions without making AI availability a prerequisite.

    Explicit or ambiguous requests to modify trading receive the controls
    explanation. Purchase questions ask about state and can return status.
    Every possible action remains read-only.
    """

    lowered = text.casefold().strip()
    normalized = _normalized_text(text)
    exact = exact_read_only_action(text)
    if exact:
        return {"action": exact, "topic": "capabilities"}
    if not normalized:
        return {"action": "unknown", "topic": "capabilities"}

    # Never reinterpret a mixed or malformed exact trading command as a read.
    if re.search(r"(?<!\w)!dca\b", lowered) or _EXPLICIT_MUTATION.search(lowered):
        return {"action": "chat", "topic": "controls"}

    mutation_words = set(_MUTATION_WORD.findall(lowered))
    purchase_question = bool(
        _READ_ONLY_PURCHASE_QUESTION.search(normalized)
        or _READ_ONLY_PURCHASE_PHRASE.search(normalized)
    )
    if mutation_words and not (
        mutation_words <= {"buy", "purchase", "order"} and purchase_question
    ):
        return {"action": "chat", "topic": "controls"}

    if re.search(r"\b(?:help|commands?|instructions?)\b|\bhow\s+do\s+i\b", lowered):
        action, topic = "help", "capabilities"
    elif re.search(r"\b(?:portfolio|balances?|holdings?)\b", lowered):
        action, topic = "portfolio", "capabilities"
    elif not re.search(
        r"\b(?:current|today|now|btc|bitcoin|eth|ethereum|sol|solana|doge|dogecoin)\b",
        lowered,
    ) and re.search(
        r"\b(?:explain|describe|understand)\b.*\b(?:regimes?|trends?|uptrend|downtrend|sideways)\b|"
        r"\bwhat\s+(?:is|are)\s+(?:(?:a|an|the)\s+)?(?:regimes?|uptrend|downtrend|sideways)\b|"
        r"\bhow\b.*\bregimes?\b.*\bwork\b",
        lowered,
    ):
        action, topic = "chat", "regimes"
    elif re.search(
        r"\b(?:health|healthy|online|offline|scheduler)\b|"
        r"\b(?:are\s+you|is\s+(?:the\s+)?bot)\s+(?:still\s+)?"
        r"(?:working|running|responding|okay|ok|alive)\b",
        lowered,
    ):
        action, topic = "health", "capabilities"
    elif re.search(
        r"\b(?:status|enabled|disabled|regimes?|trends?|uptrend|downtrend|sideways|"
        r"amounts?|budgets?|buy|buying|bought|buys|purchase|purchases|purchasing|"
        r"purchased|orders?|execution)\b",
        lowered,
    ):
        action, topic = "status", "capabilities"
    elif re.fullmatch(
        r"(?:hi|hello|hey)(?:\s+(?:there|bot|dca\s+bot))?|"
        r"good\s+(?:morning|afternoon|evening)|how\s+are\s+you(?:\s+doing)?",
        normalized,
    ):
        action, topic = "chat", "greeting"
    elif re.search(r"\b(?:risk|risks|safe|safety)\b", lowered):
        action, topic = "chat", "risk"
    elif re.search(r"\b(?:time|times|timing|schedule|scheduled)\b", lowered):
        action, topic = "chat", "timing"
    elif re.search(r"\b(?:markets?|pairs?)\b", lowered):
        action, topic = "chat", "markets"
    elif re.search(r"\bdca\b", lowered):
        action, topic = "chat", "dca"
    else:
        action, topic = "chat", "capabilities"
    return {"action": action, "topic": topic}
