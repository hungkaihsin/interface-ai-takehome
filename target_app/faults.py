"""Deliberate misbehaviour for the mock back-office app.

The brief is explicit that the interesting failures in a stable enterprise UI are
not layout drift -- they are runtime conditions. This module is what lets us
produce those conditions on demand so replay can be shown detecting them.

Design note worth defending: this app knows *how to misbehave* but knows nothing
about how the automation classifies the misbehaviour. The three-way split the
brief asks for (expected business outcome / recoverable / hard failure) is a
judgement made by the replay engine reading the surface, exactly as it would be
against a real vendor app that has never heard of our taxonomy. Encoding the
taxonomy here instead would make replay look smarter than it is -- it would be
reading answers the real world does not hand out.

The conditions each trigger produces, and how the replay engine is expected to
read them:

    member 20001  restricted record, app renders a permission-denied screen
    member 30001  a modal interstitial appears before the record loads
    member 40001  the record page stalls for SLOW_LOAD_SECONDS before rendering
    member 50001  the app throws an unhandled server error
    unknown id    a "no matching member" result screen
    (control)     session forced to expire, app bounces to the login screen
"""

from __future__ import annotations

import time
from typing import Final

from flask import session

#: Long enough that a naive one-second timeout fails and a considered wait
#: strategy succeeds. That gap is the point of the fault.
SLOW_LOAD_SECONDS: Final[float] = 3.0

PERMISSION_DENIED_MEMBER: Final[str] = "20001"
INTERSTITIAL_MEMBER: Final[str] = "30001"
SLOW_LOAD_MEMBER: Final[str] = "40001"
SERVER_ERROR_MEMBER: Final[str] = "50001"


def is_permission_denied(member_id: str) -> bool:
    return member_id == PERMISSION_DENIED_MEMBER


def is_server_error(member_id: str) -> bool:
    return member_id == SERVER_ERROR_MEMBER


def should_show_interstitial(member_id: str) -> bool:
    """True the first time this session opens the interstitial member's record.

    Once-per-session rather than every time, because a maintenance notice that
    reappears on every request is trivially special-cased. One that appears
    unpredictably forces replay to treat "unexpected dialog" as a state it
    checks for rather than a step it hardcodes.
    """
    if member_id != INTERSTITIAL_MEMBER:
        return False
    seen: list[str] = session.setdefault("interstitials_seen", [])
    if member_id in seen:
        return False
    seen.append(member_id)
    session.modified = True
    return True


def apply_slow_load(member_id: str) -> None:
    if member_id == SLOW_LOAD_MEMBER:
        time.sleep(SLOW_LOAD_SECONDS)
