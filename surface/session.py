"""Signing on, kept outside the artifact.

If logging in were recorded as steps, every artifact would carry either credentials
or a flow that works for one operator. Instead the artifact declares that it needs a
session and the platform supplies one, so the same recorded flow runs for any tenant
whose credentials are configured in its own environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from capability.locator import Locator, RoleNameStrategy
from capability.schema import TextPresent

from .base import SurfaceError


def _field(name: str) -> Locator:
    return Locator(
        describes=f"the {name} field on the sign-on screen",
        strategies=[RoleNameStrategy(role="textbox", name=name)],
    )


@dataclass
class MeridianSessionProvider:
    """Signs on to the MeridianCU console.

    Tied to one app's sign-on screen deliberately -- a generic "log in to anything"
    abstraction would be guesswork. A new tenant supplies its own provider alongside
    its credentials.
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

        # Clicking returns before the redirect lands, so without this the caller's
        # first action races sign-on and looks for a frame that does not exist yet.
        if not surface.wait_for(
            TextPresent(text="BACK-OFFICE CONSOLE", scope="any_frame"), 8_000
        ):
            raise SurfaceError(
                "signed on but the console did not load; credentials may be wrong"
            )
