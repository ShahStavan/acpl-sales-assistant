"""Injection and configuration-probe screen (code only, before any LLM call).

Blocked input returns ``NO_ANSWER`` at zero cost. DESIGN.md §2.4, §3.3.

The screen is deliberately narrow. Its job is to catch input that is trying to talk to the
*system* rather than ask about the data, and a pattern broad enough to catch every phrasing
would also refuse real questions — "which model of Aqualite sells best in the North" is a
sales question that names a model. Every pattern here therefore matches a verb against an
object, never a bare keyword, and the test suite carries the near misses that must pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

# --- instruction override -------------------------------------------------
# "Ignore the above and ...", "disregard your instructions", "forget the rules".
_OVERRIDE = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b[^.?!]{0,40}?"
    r"\b(instruction|instructions|rule|rules|prompt|prompts|guardrail|guardrails|"
    r"restriction|restrictions|context|everything above|the above)\b",
    re.IGNORECASE,
)

# --- persona replacement --------------------------------------------------
# "You are now a ...", "act as an unrestricted ...", "pretend you are ...".
_PERSONA = re.compile(
    r"\b(you\s+are\s+now|act\s+as|pretend\s+(to\s+be|you)|roleplay\s+as|"
    r"from\s+now\s+on[,\s]+you)\b",
    re.IGNORECASE,
)

# --- configuration and credential probes ----------------------------------
# "What is your API key", "print the system prompt", "which model are you running".
_CONFIG_PROBE = re.compile(
    r"\b(show|print|reveal|display|repeat|output|tell\s+me|what(?:'s|\s+is)|list|dump|"
    r"disclose|expose)\b[^.?!]{0,40}?"
    r"\b(api[\s_-]?key|secret|token|credential|credentials|password|env(?:ironment)?\s+var\w*|"
    r"system\s+prompt|your\s+prompt|your\s+instructions|your\s+configuration|"
    r"your\s+(?:model|temperature|settings)|llm_\w+|base[\s_-]?url)\b",
    re.IGNORECASE,
)

# --- direct references to the service's own configuration -----------------
# Names that only ever appear when someone is probing the deployment, not the data.
# ``.env`` sits outside the ``\b`` group deliberately: a word boundary cannot fall between
# a space and a dot, so a leading ``\b`` would leave that alternative unmatchable.
_CONFIG_NAMES = re.compile(
    r"(?:\b(?:llm_api_key|llm_base_url|llm_provider|llm_model|cadra_token|"
    r"os\.environ|getenv)\b|\.env\b)",
    re.IGNORECASE,
)

# --- exfiltration and tool abuse ------------------------------------------
_EXFILTRATE = re.compile(
    r"\b(send|post|upload|email|exfiltrate|curl|fetch)\b[^.?!]{0,40}?"
    r"\b(to\s+https?://|to\s+my\s+server|webhook|external\s+(?:url|endpoint))\b",
    re.IGNORECASE,
)

# --- SQL and write attempts -----------------------------------------------
_WRITE_ATTEMPT = re.compile(
    r"\b(drop\s+table|delete\s+from|insert\s+into|update\s+\w+\s+set|truncate\s+table|"
    r"attach\s+database|create\s+table)\b",
    re.IGNORECASE,
)

# The label carries its own article: it is interpolated into one sentence, and "a
# exfiltration attempt" is the kind of seam that makes a refusal read as machinery.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("an instruction override", _OVERRIDE),
    ("a persona replacement", _PERSONA),
    ("a configuration probe", _CONFIG_PROBE),
    ("a configuration probe", _CONFIG_NAMES),
    ("an exfiltration attempt", _EXFILTRATE),
    ("a write attempt", _WRITE_ATTEMPT),
)

BLOCKED_REASON = "blocked_input"


@dataclass(frozen=True)
class GuardVerdict:
    """Whether the question may proceed, and what it tripped if not."""

    blocked: bool
    kind: str = ""

    @property
    def message(self) -> str:
        """The sentence returned to the caller as ``answer`` when blocked."""
        if not self.blocked:
            return ""
        return (
            f"This request was not processed: it reads as {self.kind} rather than a "
            "question about ACPL's FY26 sales data. Ask about brands, regions, "
            "distributors, stock-outs or promotions instead."
        )


def screen(question: str) -> GuardVerdict:
    """Screen *question* for instructions aimed at the system rather than the data.

    Runs before anything else in the pipeline, so blocked input costs nothing: no provider
    call has been made and ``cost_usd`` is reported as ``0.0``.
    """
    text = question.strip()
    if not text:
        return GuardVerdict(blocked=True, kind="an empty request")
    for kind, pattern in PATTERNS:
        if pattern.search(text):
            return GuardVerdict(blocked=True, kind=kind)
    return GuardVerdict(blocked=False)
