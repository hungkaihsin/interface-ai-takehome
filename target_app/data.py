"""Synthetic member records for the mock back-office app.

Every value here is fabricated. The SSNs are deliberately drawn from the 900-xx-xxxx
range, which the SSA has never issued, so nothing in this file can collide with a
real person's identifier. They exist so the redaction layer has something realistic
to bite on -- a system that only ever sees clean data cannot demonstrate that it
protects dirty data.

Member IDs double as the fault-injection switchboard; see faults.py. Choosing IDs
as the trigger (rather than a separate chaos endpoint) keeps the hostile behaviour
reachable through the normal UI, which is how these conditions actually surface in
a bank back-office: the operator types an ID and the app misbehaves.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Account:
    account_number: str
    kind: str
    balance: float
    opened: str
    status: str = "OPEN"


@dataclass(frozen=True)
class Member:
    member_id: str
    first_name: str
    last_name: str
    ssn: str
    phone: str
    email: str
    joined: str
    branch: str
    status: str = "ACTIVE"
    accounts: list[Account] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def account_of_kind(self, kind: str) -> Account | None:
        return next((a for a in self.accounts if a.kind == kind), None)


MEMBERS: dict[str, Member] = {
    "10001": Member(
        member_id="10001",
        first_name="Dolores",
        last_name="Ashgrove",
        ssn="900-31-4402",
        phone="(209) 555-0142",
        email="d.ashgrove@example.invalid",
        joined="1998-04-17",
        branch="Stockton Main",
        accounts=[
            Account("SAV-4471-00812", "SAVINGS", 18432.19, "1998-04-17"),
            Account("CHK-4471-00813", "CHECKING", 2204.55, "1998-04-17"),
            Account("CD-4471-01190", "CERTIFICATE", 25000.00, "2021-11-02"),
        ],
    ),
    "10002": Member(
        member_id="10002",
        first_name="Marcus",
        last_name="Ilminster",
        ssn="900-64-1178",
        phone="(209) 555-0198",
        email="m.ilminster@example.invalid",
        joined="2007-09-30",
        branch="Lodi Branch",
        accounts=[
            Account("SAV-8820-04417", "SAVINGS", 743.08, "2007-09-30"),
        ],
    ),
    "10003": Member(
        member_id="10003",
        first_name="Priya",
        last_name="Venkataraman",
        ssn="900-08-7731",
        phone="(415) 555-0173",
        email="p.venkat@example.invalid",
        joined="2019-02-11",
        branch="Stockton Main",
        accounts=[
            Account("SAV-9014-02255", "SAVINGS", 61150.73, "2019-02-11"),
            Account("CHK-9014-02256", "CHECKING", 8912.40, "2019-02-11"),
        ],
    ),
    # --- members that exist but drive an exceptional path (see faults.py) ---
    "20001": Member(
        member_id="20001",
        first_name="Harold",
        last_name="Quist",
        ssn="900-77-2210",
        phone="(209) 555-0110",
        email="h.quist@example.invalid",
        joined="1986-06-01",
        branch="Executive",
        status="RESTRICTED",
        accounts=[Account("SAV-0001-00001", "SAVINGS", 1284900.00, "1986-06-01")],
    ),
    "30001": Member(
        member_id="30001",
        first_name="Ana",
        last_name="Brightwater",
        ssn="900-45-9083",
        phone="(916) 555-0164",
        email="a.brightwater@example.invalid",
        joined="2014-08-22",
        branch="Elk Grove",
        accounts=[Account("SAV-3310-07741", "SAVINGS", 5580.00, "2014-08-22")],
    ),
    "40001": Member(
        member_id="40001",
        first_name="Teodor",
        last_name="Vasquez",
        ssn="900-19-5526",
        phone="(209) 555-0187",
        email="t.vasquez@example.invalid",
        joined="2011-01-19",
        branch="Tracy Branch",
        accounts=[Account("SAV-6602-03318", "SAVINGS", 12007.65, "2011-01-19")],
    ),
    "50001": Member(
        member_id="50001",
        first_name="Corinne",
        last_name="Delacroix",
        ssn="900-52-6614",
        phone="(209) 555-0155",
        email="c.delacroix@example.invalid",
        joined="2003-05-05",
        branch="Manteca Branch",
        accounts=[Account("SAV-7728-00904", "SAVINGS", 33421.00, "2003-05-05")],
    ),
}

# The single operator account. Fake, and the app is bound to localhost.
OPERATOR_USERNAME = "operator"
OPERATOR_PASSWORD = "letmein"


def find_member(member_id: str) -> Member | None:
    return MEMBERS.get(member_id.strip())
