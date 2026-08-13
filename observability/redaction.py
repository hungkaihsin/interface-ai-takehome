"""Redaction of regulated data before anything is written down.

Requirement 3.4 says never persist secrets or raw sensitive data into artifacts or
logs. The target app renders SSNs and full account numbers in the clear, exactly as
a real back-office screen does, so every observation the system takes is
contaminated by default. Redaction is therefore applied at the *boundary where
data becomes durable* -- the log writer and the evidence writer -- rather than at
each call site, because a rule that has to be remembered at forty call sites is a
rule that will be forgotten at one of them.

The design choice worth defending: **deny by pattern, on the way out.**

The alternative is to tag sensitive fields and redact only those. That is cleaner
in principle and wrong in practice here, because the risky data does not arrive
through fields we control -- it arrives inside a page-text blob scraped from a
screen we did not design. A pattern sweep over outbound text catches an SSN that
appears somewhere nobody anticipated, which is the case that actually leaks.

Its limits, stated plainly because a guardrail nobody understands is worse than
none:

  * Pattern matching has false negatives. A member name is PII and matches no
    regex. Names are not redacted here, and on a real system this layer would be
    paired with field-level tagging from the app's own data dictionary.
  * It has false positives, which are cheap: a redacted string that was harmless
    costs nothing but readability.
  * It protects logs and evidence. It does not protect the live browser session,
    which necessarily sees everything on screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Pattern

#: Ordered because the first match wins on overlapping spans. Account numbers are
#: matched before bare digit runs so that a structured account number is labelled
#: as one rather than as an anonymous number.
_RULES: list[tuple[str, Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("account", re.compile(r"\b[A-Z]{2,4}-\d{3,6}-\d{3,6}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")),
    # Bearer tokens and API keys, in case a URL or header ever reaches a log.
    ("secret", re.compile(r"\b(?:AIza|sk-|Bearer\s+)[A-Za-z0-9_\-]{16,}")),
]

MASK = "[redacted:{}]"


@dataclass
class Redactor:
    """Applies pattern rules, plus any literal values declared sensitive.

    `literals` carries the concrete argument values of parameters marked
    `sensitive` in the artifact. They are redacted by exact substring so that a
    value which happens to match no pattern -- a password, a one-time code -- is
    still removed from anything persisted.
    """

    literals: set[str] = field(default_factory=set)

    def add_literal(self, value: Any) -> None:
        text = str(value).strip()
        # Very short values are refused: redacting "1" would blank out half of
        # every log line for no privacy gain.
        if len(text) >= 4:
            self.literals.add(text)

    def add_literals(self, values: Iterable[Any]) -> None:
        for value in values:
            self.add_literal(value)

    def text(self, value: str) -> str:
        for literal in self.literals:
            value = value.replace(literal, MASK.format("input"))
        for label, pattern in _RULES:
            value = pattern.sub(MASK.format(label), value)
        return value

    def value(self, value: Any) -> Any:
        """Redact recursively through the structures we actually log."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.value(v) for v in value)
        return value


def redact(value: Any) -> Any:
    """Convenience for callers with no per-run literals to add."""
    return Redactor().value(value)
