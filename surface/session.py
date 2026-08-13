"""Establishing an authenticated session -- deliberately outside the artifact.

Signing on is not part of any capability, and that separation is a safety decision
rather than a tidiness one. If logging in were recorded as steps, the artifact
would contain either credentials or a flow that only works for one operator, and
the file is meant to be shareable across tenants and reviewable by humans.

So: the artifact declares that it needs a session (see
`RecoverableCondition.reestablish_session`), and the platform supplies one. The
same recorded flow then runs for any institution whose credentials are configured
in that institution's environment.

Credentials come from the environment, default to the target app's published test
account, and are registered with the run's redactor so they cannot reach a log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from capability.locator import Locator, RoleNameStrategy

from .base import SurfaceError


def _field(name: str) -> Locator:
    return Locator(
        describes=f"the {name} field on the sign-on screen",
        strategies=[RoleNameStrategy(role="textbox", name=name)],
    )


@dataclass
class MeridianSessionProvider:
    """Signs on to the MeridianCU console.

    Tied to one app's sign-on screen on purpose. A generic "log in to anything"
    abstraction would be guesswork; a per-app provider is a small, honest amount
    of code that a new tenant's onboarding would supply alongside its credentials.
    """

    base_url: str = "http://127.0.0.1:5001"
    username: str = os.environ.get("TARGET_APP_USER", "operator")
    password: str = os.environ.get("TARGET_APP_PASSWORD", "letmein")

    def secrets(self) -> list[str]:
        """Values the redactor must scrub from anything persisted."""
        return [self.password]

    def establish(self, surface) -> None:
        surface.goto(f"{self.base_url}/login")
        if not surface.fill(_field("Operator ID"), self.username).resolved:
            raise SurfaceError("sign-on screen did not present an Operator ID field")
        surface.fill(_field("Password"), self.password)
        signon = Locator(
            describes="the Sign On button",
            strategies=[RoleNameStrategy(role="button", name="Sign On")],
        )
        if not surface.click(signon).resolved:
            raise SurfaceError("sign-on screen did not present a Sign On button")
