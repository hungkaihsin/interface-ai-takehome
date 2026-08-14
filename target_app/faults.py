"""Deliberate misbehaviour, triggered by member ID.

The app knows how to misbehave but nothing about how the automation classifies it.
Sorting these into business outcomes, recoverable conditions and hard failures is
the replay engine's job, reading the screen the way it would against a real vendor
app that has never heard of our taxonomy.

    20001  permission denied      30001  modal interstitial
    40001  slow load              50001  server error
"""

from __future__ import annotations

import time
from typing import Final

from flask import session

#: Long enough that a naive one-second timeout fails and a real wait strategy works.
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

    Once per session, not every time: a notice that always appears is trivially
    special-cased, one that appears unpredictably is not.
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
