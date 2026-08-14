"""MeridianCU Back-Office: a deliberately hostile stand-in for a legacy bank app.

The target surface, not part of the automation system. Content lives inside named
iframes, layout is carried by nested tables, there are no test IDs or meaningful
class names, and records render SSNs and account numbers in the clear so redaction
has real work to do.

The search form uses proper <label for> bindings; the sub-account form does not.
That asymmetry exercises both the clean accessible-name path and the degraded one.

Binds to localhost. Credentials are fake. Never point this at anything real.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import faults
from .data import (
    OPERATOR_PASSWORD,
    OPERATOR_USERNAME,
    Account,
    find_member,
)

app = Flask(__name__)
app.secret_key = os.environ.get("TARGET_APP_SECRET") or secrets.token_hex(16)

#: Short enough that a long automation run can plausibly trip it.
SESSION_MINUTES = 30

#: In memory: persistence is not what this app demonstrates. Creating one is its
#: only irreversible action, which is what makes it interesting for the allowlist.
CREATED_SUBACCOUNTS: dict[str, list[Account]] = {}

PRODUCT_CODES = {
    "SAV-STD": "Standard Savings",
    "SAV-HY": "High-Yield Savings",
    "SAV-MIN": "Minor Savings",
}


# --------------------------------------------------------------------------
# session handling
# --------------------------------------------------------------------------


def _session_is_live() -> bool:
    if not session.get("operator"):
        return False
    raw_expiry = session.get("expires_at")
    if not raw_expiry:
        return False
    if datetime.now(timezone.utc).timestamp() > float(raw_expiry):
        session.clear()
        return False
    return True


def _start_session(username: str) -> None:
    session.clear()
    session["operator"] = username
    session["expires_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=SESSION_MINUTES)
    ).timestamp()


def _require_session() -> None:
    """Raises rather than returning a redirect, so views can use it as a guard
    clause without threading a return value through."""
    if not _session_is_live():
        abort(redirect(url_for("login", expired="1")))


# --------------------------------------------------------------------------
# authentication and the frame shell
# --------------------------------------------------------------------------


@app.route("/")
def root():
    if not _session_is_live():
        return redirect(url_for("login"))
    return redirect(url_for("shell"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == OPERATOR_USERNAME and password == OPERATOR_PASSWORD:
            _start_session(username)
            return redirect(url_for("shell"))
        error = "Invalid operator ID or password."
    return render_template(
        "login.html",
        error=error,
        expired=request.args.get("expired") == "1",
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/shell")
def shell():
    _require_session()
    return render_template("shell.html", operator=session.get("operator"))


@app.route("/nav")
def nav():
    _require_session()
    return render_template("nav.html")


# --------------------------------------------------------------------------
# member search -> detail
# --------------------------------------------------------------------------


@app.route("/search", methods=["GET", "POST"])
def search():
    _require_session()
    if request.method == "GET":
        return render_template("search.html", result=None, searched=False)

    member_id = request.form.get("member_id", "").strip()
    if not member_id:
        return render_template(
            "search.html",
            result=None,
            searched=True,
            validation_error="Member number is required.",
        )

    member = find_member(member_id)
    if member is None:
        # A legitimate business outcome, rendered as an ordinary result screen --
        # not an HTTP error. Conflating the two is the mistake the brief names.
        return render_template(
            "search.html", result=None, searched=True, queried_id=member_id
        )
    return redirect(url_for("member_detail", member_id=member.member_id))


@app.route("/member/<member_id>")
def member_detail(member_id: str):
    _require_session()

    if faults.is_server_error(member_id):
        raise RuntimeError(
            f"CORE-BANKING LINK FAILURE: unable to materialise record {member_id}"
        )

    if faults.should_show_interstitial(member_id):
        return render_template("interstitial.html", member_id=member_id)

    member = find_member(member_id)
    if member is None:
        return render_template("search.html", result=None, searched=True,
                               queried_id=member_id)

    if faults.is_permission_denied(member_id):
        return render_template("denied.html", member_id=member_id), 403

    faults.apply_slow_load(member_id)

    accounts = list(member.accounts) + CREATED_SUBACCOUNTS.get(member_id, [])
    return render_template("member_detail.html", member=member, accounts=accounts)


@app.route("/interstitial/<member_id>/dismiss", methods=["POST"])
def dismiss_interstitial(member_id: str):
    _require_session()
    return redirect(url_for("member_detail", member_id=member_id))


# --------------------------------------------------------------------------
# sub-account opening -- the app's one irreversible action
# --------------------------------------------------------------------------


@app.route("/member/<member_id>/subaccount/new", methods=["GET", "POST"])
def subaccount_new(member_id: str):
    _require_session()
    member = find_member(member_id)
    if member is None:
        abort(404)

    if request.method == "GET":
        return render_template(
            "subaccount_new.html", member=member, products=PRODUCT_CODES, errors={}
        )

    product = request.form.get("product_code", "")
    nickname = request.form.get("nickname", "").strip()
    deposit_raw = request.form.get("initial_deposit", "").strip()

    errors: dict[str, str] = {}
    if product not in PRODUCT_CODES:
        errors["product_code"] = "Select a product."
    if not nickname:
        errors["nickname"] = "Nickname is required."
    elif len(nickname) > 24:
        errors["nickname"] = "Nickname must be 24 characters or fewer."

    try:
        deposit = float(deposit_raw)
        if deposit < 25.00:
            errors["initial_deposit"] = "Minimum opening deposit is $25.00."
    except ValueError:
        errors["initial_deposit"] = "Enter a dollar amount, digits only."
        deposit = 0.0

    if errors:
        return render_template(
            "subaccount_new.html",
            member=member,
            products=PRODUCT_CODES,
            errors=errors,
            submitted=request.form,
        )

    existing = len(member.accounts) + len(CREATED_SUBACCOUNTS.get(member_id, []))
    created = Account(
        account_number=f"SAV-{member_id}-{existing + 1:05d}",
        kind="SAVINGS",
        balance=deposit,
        opened=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )
    CREATED_SUBACCOUNTS.setdefault(member_id, []).append(created)

    return render_template(
        "subaccount_confirm.html",
        member=member,
        account=created,
        product=PRODUCT_CODES[product],
        nickname=nickname,
    )


# --------------------------------------------------------------------------
# Out-of-band test hooks. Reached over HTTP directly, never by clicking, so a
# session timeout can be produced at an exact moment during a replay.
# --------------------------------------------------------------------------


@app.route("/_control/expire-session", methods=["POST"])
def control_expire_session():
    session.clear()
    return {"expired": True}, 200


@app.route("/_control/reset", methods=["POST"])
def control_reset():
    CREATED_SUBACCOUNTS.clear()
    session.clear()
    return {"reset": True}, 200


@app.errorhandler(500)
def on_server_error(exc):  # pragma: no cover - exercised via member 50001
    return render_template("app_error.html"), 500


def create_app() -> Flask:
    return app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
