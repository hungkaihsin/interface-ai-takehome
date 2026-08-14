"""Strip regulated data before anything is written to disk.

Applied in the log and evidence writers rather than at each call site: there is one
place data becomes durable, so there is one place to get this right.

Deny-by-pattern on the way out, rather than tagging fields as sensitive. The risky
data does not arrive through fields we control -- it arrives inside page text
scraped from a screen we did not design.

Limits: names match no pattern and are not redacted; this protects logs and
evidence, never the live session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Pattern

#: Ordered -- first match wins on overlapping spans, so structured account numbers
#: are labelled as such rather than as anonymous digit runs.
_RULES: list[tuple[str, Pattern[str]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("account", re.compile(r"\b[A-Z]{2,4}-\d{3,6}-\d{3,6}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")),
    ("secret", re.compile(r"\b(?:AIza|sk-|Bearer\s+)[A-Za-z0-9_\-]{16,}")),
]

MASK = "[redacted:{}]"


@dataclass
class Redactor:
    """Pattern rules plus any literal values declared sensitive.

    `literals` holds concrete argument values and credentials, redacted by exact
    substring so a password matching no pattern is still removed.
    """

    literals: set[str] = field(default_factory=set)

    def add_literal(self, value: Any) -> None:
        text = str(value).strip()
        if len(text) >= 4:  # redacting "1" would blank out half of every line
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
        """Redact recursively -- log payloads are dicts of lists."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.value(v) for v in value)
        return value


def redact(value: Any) -> Any:
    return Redactor().value(value)
