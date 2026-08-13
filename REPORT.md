# Design write-up

## Architecture

Five packages, one direction of dependency, and one hard rule: **no LLM below the discovery layer.**

```
discovery/     Gemini observe→decide→act loop. Emits a draft artifact.  [only place a model runs]
      │
      ▼
capability/    The artifact: typed, versioned, serialisable. Pydantic models.
      │
      ▼
replay/        Deterministic execution + escalation. Imports no model library.
      │
      ▼
surface/       Perception & action. Only web.py knows a browser exists.
      │
      ▼
target_app/    The stand-in legacy bank console being driven (not part of the system).

observability/ Structured logging + redaction, used by discovery and replay alike.
```

The seam that matters is `surface/base.py`. Everything above it addresses controls by **accessibility role, accessible name, and containment** — never CSS, never XPath, never coordinates. Those are not merely discouraged; they are absent from the interface, so no layer above can express them and no artifact can accidentally store one.

**Key trade-offs.**

*Single process, synchronous.* No queue, no service split. The brief explicitly does not reward scaling infrastructure, and a synchronous design buys a real property: the human-handoff control model needs no locking, because the automation is blocked inside a call for exactly as long as a human holds the session.

*Playwright over a computer-use model.* Gemini offers `gemini-2.5-computer-use-preview`. It was rejected because it perceives screenshots and returns pixel coordinates, and coordinates cannot survive into a replayable artifact — they break on window size, font scaling, or an inserted row. Driving the accessibility tree instead means the model's chosen action is *already* in the artifact's vocabulary, so recording is a projection rather than a translation.

*Gemini 2.5 Flash.* Chosen for prior production experience with its failure modes and a free tier that keeps the unstubbable requirement ungated. The honest cost: its tool-calling is less rigid than some alternatives, so the loop is built defensively (below).

## Artifact schema

`capability/schema.py`. Four shaping decisions:

**1. A contract, not a macro.** A click recording would replay one flow. It would not let an AI agent decide *whether to call it*. So `inputs`, `outputs`, `success` and `business_outcomes` are top-level fields, and the step list sits underneath them as implementation detail. An agent can read the contract without parsing a single step.

**2. Expected outcomes are declared data.** `business_outcomes` sits beside `success` as a first-class field, each with a `code` the caller branches on and a `detect` condition. "No such member" is an answer the capability announces it can return. A taxonomy that lived only in `try/except` would be invisible to the agent deciding whether to invoke it at all.

**3. Two independent version numbers.** `schema_version` versions the format so old artifacts stay loadable; `version` versions the capability so a re-recording after a vendor upgrade is a new revision of the same named thing. Conflating them means one format change orphaning every stored recipe.

**4. Values are templates.** A step fills `{{ member_id }}`, never `10001`. That is what makes it a parameterised capability rather than a transcript — and a privacy property, since no member's identifier enters the stored file.

**Locators** (`capability/locator.py`) are an ordered ladder of three tiers, each measured against the real surface:

| Tier | Strategy | Why it exists |
|---|---|---|
| 1 | role + accessible name | `button "Search"` — clean, readable, preferred |
| 2 | role scoped to a named container | The sub-account form exposes a bare `textbox` with *no* name inside `row "Nickname"`. The control is anonymous; its neighbourhood is not |
| 3 | role + ordinal, with written justification | Last resort; the artifact must say in prose why |

Tier 2 is what earns the design. The CSS equivalent, `table tr:nth-child(2) input`, encodes *position*, so inserting a row above silently retargets it — in a banking form, typing a deposit amount into a nickname field. Scoping to the row labelled "Nickname" encodes *meaning*.

`frame_path` is mandatory, not decoration: the target app's top-level document contains **no controls at all**, only two `iframe` nodes, so an address without a frame identifies nothing.

The schema validates its own integrity at load time — undeclared `{{ params }}`, duplicate step ids, actions outside the policy allowlist, and (most importantly) an artifact whose steps are risky while its policy claims otherwise. That last check is what keeps the approval gate from being decorative.

## Determinism & error handling

**Determinism** rests on three rules:

1. *No model in the loop.* `grep` finds zero LLM imports across `replay/`, `surface/`, `capability/`, `observability/`. Replay runs with `GOOGLE_API_KEY` unset — a reviewer can verify the central claim by deleting the key.
2. *Strict uniqueness.* A strategy resolves only if it matches **exactly one** element. Playwright's default is to take the first match; that default is rejected, because taking the first is how automation reads the wrong row the day a member opens a second savings account — silently, forever. Genuine multiplicity must be declared with an explicit index.
3. *No sleeps.* There is no duration-based wait in the schema or the engine. Waiting is always waiting *for a condition*, so a 3-second stall and an 8-second stall are both absorbed correctly.

**Error handling** is the three-way split the brief names, made structural in `replay/result.py` — `status` has exactly three values and there is no way to express "worked" and "no such member" with the same one.

| Class | Example on this surface | Response |
|---|---|---|
| **Business outcome** | `MEMBER_NOT_FOUND`, `PERMISSION_DENIED` | Report a code the caller branches on |
| **Recoverable** | maintenance interstitial, 3-second stall, expired session | Apply the declared remedy, bounded by `max_attempts` |
| **Hard failure** | HTTP 500 from the core banking link | Stop; report step, expected, observed |

Recovery is deliberately *not* a fourth status: a run that dismissed an interstitial and then read the balance succeeded. It is recorded in `recoveries` so the evidence shows the detour.

Two execution rules were forced by measurement, not theory:

- **Never look once.** After an action, the screen may legitimately be any of several states, or not have arrived yet. Checking a condition immediately after clicking was *intermittently* correct — worse than consistently wrong, because it yields flaky automation. The engine polls the whole known state space until one member matches or the budget expires.
- **Restarting is gated on reversibility.** Recovering a lost session means replaying from the top, which is safe only while every executed step was reversible. If a risky step has committed, restarting could open a second account, so the engine escalates instead. This is the one place the safe/risky classification does real work.

A worked failure, straight from `/evidence/`:

```
expected : The member record is displayed and shows a SAVINGS row.
observed : [main] System Error ... Reference: SYS-500 / CORE-LINK ...
step     : s3 (Submit the search)
```

## Heterogeneity & multi-tenant

**Surface abstraction.** The seam is `surface/base.py`. Its vocabulary — role, name, containment, frame path — was chosen because it is exactly what Windows UI Automation (`ControlType`, `Name`) and macOS AX (`AXRole`, `AXTitle`, `AXChildren`) expose. Extending to a desktop app means writing a second class satisfying that protocol; the artifact schema, replay engine, and error taxonomy are untouched, and no stored artifact needs re-recording. `SurfaceBinding.kind` (`web` | `desktop`) is where an artifact declares which backend it needs. `frame_path` generalises directly to a window/pane hierarchy — which is why frames were kept in the abstraction rather than hidden.

This is also why the computer-use model was rejected: a coordinate-based design has no such seam.

**Multi-tenant reuse.** `SurfaceBinding` separates `app_id` (the vendor product) from `tenant_id` (the institution). Two credit unions on the same core product share an `app_id`, which is what makes "record once, reuse across tenants" expressible at all. The intended model, designed but **not built**:

- A capability is published against an `app_id`. Tenants inherit it.
- A tenant needing a difference publishes an artifact with `extends: "<base_capability_id>"` overriding only the steps or locators that differ. The field exists in the schema today so this needs no format change.
- Drift is detected by the same mechanism as everything else: `StepRecord.resolution` records which locator tier resolved **even on success**. A tenant whose tier-1 strategy silently stopped working and has been carried by tier 2 for a month is visible in aggregate long before it breaks. That signal is free, and it is the answer to "how do you know a tenant has drifted."
- Credentials never live in artifacts (`surface/session.py`), so the same file is portable across institutions by construction.

## Escalation & handoff

**Detecting stuck** is not a heuristic. It is three defined states: the surface settled into something the capability does not declare; a remedy exhausted its attempt budget; or a session was lost after an irreversible step committed.

**Routing** produces an `InterventionRequest` carrying which capability and goal, which step and its intent, why it stopped, the redacted current screen, and a screenshot.

**Taking control of the same session** is real, and it is the part that is easy to fake. The engine blocks; the human works in the *same* Chromium context; nothing is torn down or re-created, so cookies, session, scroll position and half-filled forms survive. `TerminalOperatorConsole` hands a real browser window to a real person.

**The control model** is enforced structurally rather than by convention: the transfer is a context manager, and the automation is inside a blocking call for exactly as long as the human holds control, so both sides physically cannot act at once. No locking is required — which is a direct benefit of the synchronous architecture.

**Recording what the human did** is *observed*, not self-reported: navigations are captured by listening on the live page during the takeover window. Evidence from a real run:

```
escalation.raised    step=s3  reason=screen is not a state this capability declares
control.transferred  to=human
control.returned     to=automation  resumed=true
                     navigations=['main -> http://127.0.0.1:5001/member/30001']
                     note='acknowledged the maintenance notice and returned control'
outputs.extracted    {'savings_balance': 5580.0}
```

Both endings are exercised: a human who resolves it (run completes, `escalated=true`), and a human who cannot — a core-banking outage is nobody's clicking error — where the run ends as a clean failure that still records that a person was consulted.

**Mocked deliberately:** the operator *console UI*. Production is a co-browsing surface with an intervention queue. Here an operator is a terminal prompt or a scripted stand-in (so the evidence is reproducible without a human at a keyboard). Both go through the identical handoff path; the mock is the interface a human sits behind, never the mechanism.

## Safety

Four layers, each doing work:

**Allowlist.** `policy.allowed_origins` is carried *in the artifact*, so permissions travel with the thing being permitted and a reviewer sees the blast radius on the same page as the steps. Enforced before the first navigation, on every subsequent navigation, and inside the discovery loop — where a refusal is fed back to the model as an observation so it re-routes rather than dying.

**Reversibility, not danger.** `safe` means doing it twice is indistinguishable from doing it once. `risky` means it changes state the institution or member can see. The split is about *reversibility* because that is the property deciding whether an automatic retry is safe. Risky capabilities are blocked from unattended replay unless explicitly approved, and a schema validator makes it impossible for an artifact to carry a risky step while declaring itself reversible.

**Redaction at the durability boundary.** Applied in the log writer and evidence writer — the one place data becomes permanent — rather than at forty call sites, one of which would eventually be forgotten. Deny-by-pattern on the way out (SSN, account numbers, card-like digit runs, emails, phones, API keys) plus exact-match on any argument declared `sensitive` and on session credentials. Verified: no evidence file in the repo contains an SSN, a password, or an API key.

**Drafts are not approved.** A discovered artifact is emitted with `approved_for_unattended=False` and a review note, because discovery cannot know which steps are irreversible and sees only the happy path. A machine proposes; a human classifies risk and declares outcomes.

**Stated limits.** Pattern redaction has false negatives — a member's *name* is PII and matches no regex, so names are not redacted; production would pair this with field-level tagging from the app's data dictionary. Redaction protects logs and evidence, never the live session, which necessarily sees everything. The allowlist is origin-prefix based, not a full URL policy. And the safe/risky classification on a discovered artifact is a human judgement the system cannot make for itself.

## Cuts

**Deliberately not built:**

- *Multi-tenant plumbing.* Designed above, with the `extends` field and `app_id`/`tenant_id` split present in the schema so it needs no format change. The brief says building this is not rewarded.
- *A desktop backend.* The seam is real and the vocabulary was chosen for it; the second implementation is not written.
- *A co-browsing operator console.* Mocked at a clean seam, as the brief permits.
- *Business-outcome discovery.* Discovery emits `business_outcomes: []` because one happy-path run cannot observe them. The reference artifact's outcomes are human-declared. Automating this via negative probe runs — deliberately searching a nonexistent member and asking the model to classify the resulting screen — is the single highest-value next step, and the `--verify-with` flag is the beginning of that machinery.
- *Queues, workers, retry infrastructure.* Explicitly not rewarded.

**What I would build next, in order:**

1. **Outcome discovery by negative probing** (above) — it closes the one real gap between a discovered draft and the reference artifact.
2. **Approval workflow** — `draft → reviewed → approved`, with the risk classification captured as a human decision and the artifact hash pinned at approval, so an approved capability cannot be silently edited.
3. **Drift telemetry** — aggregate `StepRecord.resolution` across replays per tenant. The data is already recorded; nothing consumes it yet.
4. **Bounded single-step LLM recovery** — on an unrecognised screen, one policy-checked model call proposing a single action, recorded as evidence, never open-ended.

**Known rough edges, stated plainly:** the discovery loop's parameterisation is by value-matching against declared examples, which would mis-handle a parameter whose value coincidentally equals unrelated text on screen. `RoleOrdinalStrategy` is implemented but never emitted by discovery. And the target app is a stand-in I wrote, so it is hostile in the ways I anticipated rather than in the ways a real vendor product would surprise me.
