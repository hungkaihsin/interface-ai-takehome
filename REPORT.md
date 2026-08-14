# Design write-up

## Architecture

Five packages, one direction of dependency, and one hard rule: **no LLM below the discovery layer.**

```
discovery/     Gemini observe→decide→act loop; emits a draft artifact  [the only model call]
capability/    the artifact: typed, versioned, serialisable
replay/        deterministic execution + escalation; imports no model library
surface/       perception & action; only web.py knows a browser exists
target_app/    the legacy bank console being driven (not part of the system)
observability/ structured logging + redaction
```

The load-bearing seam is `surface/base.py`. Everything above it addresses controls by **accessibility role, name and containment** — never CSS, XPath or coordinates. Those aren't discouraged, they're absent from the interface, so no layer above can express one and no artifact can store one by accident.

**Trade-offs.** *Single process, synchronous* — no queues, which the brief says aren't rewarded, and it buys a real property: the human-handoff control model needs no locking because the automation is blocked inside a call for exactly as long as a human holds the session. *Playwright over Gemini's computer-use model* — that model perceives screenshots and returns pixel coordinates, and coordinates cannot survive into a replayable artifact; driving the accessibility tree means the model's chosen action is already in the artifact's vocabulary, so recording is a projection rather than a translation. *Gemini 2.5 Flash* — chosen for prior production experience with its failure modes and a free tier that keeps the unstubbable requirement ungated; the cost is looser tool-calling, so the loop is built defensively.

## Artifact schema

`capability/schema.py`. Four shaping decisions:

**A contract, not a macro.** A click recording would replay one flow but wouldn't let an agent decide *whether to call it*. So `inputs`, `outputs`, `success` and `business_outcomes` are top-level; the step list sits underneath as implementation detail.

**Expected outcomes are declared data.** `business_outcomes` sits beside `success`, each with a `code` the caller branches on and a `detect` condition. A taxonomy living only in `try/except` is invisible to the agent deciding whether to invoke the capability at all.

**Two version numbers.** `schema_version` versions the format so old artifacts stay loadable; `version` versions the capability so a re-recording after a vendor upgrade is a new revision of the same named thing.

**Values are templates.** A step fills `{{ member_id }}`, never `10001` — what makes it parameterised rather than a transcript, and a privacy property, since no member's identifier enters the file.

Locators are an ordered ladder, each tier measured against the real surface:

| Tier | Strategy | Why |
|---|---|---|
| 1 | role + accessible name | `button "Search"` — clean, preferred |
| 2 | role scoped to a named container | the sub-account form exposes a bare `textbox` with **no** name inside `row "Nickname"` |
| 3 | role + ordinal, with written justification | last resort; must say why in prose |

Tier 2 earns the design. The CSS equivalent, `tr:nth-child(2) input`, encodes *position*, so inserting a row silently retargets it — in a banking form, typing a deposit into a nickname field. The row's label encodes *meaning*.

`frame_path` is mandatory: the target app's top-level document contains **no controls at all**, only two `iframe` nodes, so an address without a frame identifies nothing.

The schema validates itself at load: undeclared `{{ params }}`, duplicate step ids, actions outside the policy allowlist, and an artifact whose steps are risky while its policy claims otherwise. That last check keeps the approval gate from being decorative.

## Determinism & error handling

Determinism rests on three rules. **No model in the loop** — `grep` finds zero LLM imports across `replay/`, `surface/`, `capability/`, `observability/`, and replay runs with `GOOGLE_API_KEY` unset. **Strict uniqueness** — a strategy resolves only if it matches exactly one element; Playwright's default of taking the first is how automation reads the wrong row the day a member opens a second savings account, silently, forever. **No sleeps** — waiting is always waiting *for a condition*, so a 3-second stall and an 8-second stall are both absorbed.

Error handling is the three-way split, made structural in `replay/result.py`: `status` has exactly three values and there is no way to express "worked" and "no such member" with the same one.

| Class | Example here | Response |
|---|---|---|
| **Business outcome** | `MEMBER_NOT_FOUND`, `PERMISSION_DENIED` | report a code the caller branches on |
| **Recoverable** | maintenance interstitial, slow load, expired session | apply the declared remedy, bounded by `max_attempts` |
| **Hard failure** | HTTP 500 from the core banking link | stop; report step, expected, observed |

Recovery is deliberately not a fourth status — a run that dismissed an interstitial and then read the balance succeeded — but it is recorded so the evidence shows the detour.

Two rules came from measurement, not theory. **Never look once:** checking a condition immediately after clicking was *intermittently* correct, which is worse than consistently wrong; the engine now polls the whole known state space until one member matches. **Restarting is gated on reversibility:** recovering a lost session means replaying from the top, safe only while every executed step was reversible; if a risky step has committed, the engine escalates rather than risk a second account. That is the one place the safe/risky split does real work.

A failure from `/evidence/`:

```
expected : The member record is displayed and shows a SAVINGS row.
observed : [main] System Error ... Reference: SYS-500 / CORE-LINK ...
step     : s3 (Submit the search)
```

## Heterogeneity & multi-tenant

**Surface abstraction.** `surface/base.py`'s vocabulary — role, name, containment, frame path — was chosen because it is what Windows UI Automation (`ControlType`, `Name`) and macOS AX (`AXRole`, `AXTitle`, `AXChildren`) expose. A desktop backend is a second class satisfying that protocol; the schema, engine and taxonomy are untouched and no stored artifact needs re-recording. `SurfaceBinding.kind` declares which backend an artifact needs, and `frame_path` generalises to a window/pane hierarchy — which is why frames stayed in the abstraction rather than being hidden. This is also why the coordinate-based computer-use model was rejected: it has no such seam.

**Multi-tenant reuse.** `SurfaceBinding` separates `app_id` (vendor product) from `tenant_id` (institution). Two credit unions on the same core share an `app_id`, which is what makes "record once, reuse across tenants" expressible. Designed, **not built**: a tenant needing a difference publishes an artifact with `extends: "<base_id>"` overriding only what differs — the field is in the schema today, so this needs no format change. Drift is detected by the mechanism already running: `StepRecord.resolution` records which tier resolved *even on success*, so a tenant whose tier-1 strategy quietly died and has been carried by tier 2 for a month is visible in aggregate long before it breaks. Credentials never live in artifacts, so the same file is portable by construction.

## Escalation & handoff

**Stuck** is three defined states, not a heuristic: the surface settled into something the capability doesn't declare, a remedy exhausted its attempt budget, or a session was lost after an irreversible step committed.

**Routing** produces an `InterventionRequest` carrying the capability and goal, the step and its intent, why it stopped, the redacted screen, and a screenshot.

**Taking control of the same session** is the part that's easy to fake and isn't. The engine blocks; the human works in the *same* Chromium context; nothing is torn down, so cookies, session, scroll position and half-filled forms survive. The transfer is a context manager, so both sides physically cannot act at once and no locking is required.

**What the human did is observed, not self-reported** — navigations are captured by listening on the live page:

```
escalation.raised    step=s3  reason=screen is not a state this capability declares
control.transferred  to=human
control.returned     to=automation  resumed=true
                     navigations=['main -> http://127.0.0.1:5001/member/30001']
outputs.extracted    {'savings_balance': 5580.0}
```

Both endings are exercised: a human who resolves it, and one who can't — a core-banking outage is nobody's clicking error — where the run ends a clean failure that still records the consultation.

**Mocked deliberately:** the operator *console UI*. Production is a co-browsing surface with an intervention queue. Here an operator is a terminal prompt or a scripted stand-in (so evidence is reproducible without a human at a keyboard). Both go through the identical handoff path.

## Safety

**Allowlist.** `policy.allowed_origins` lives *in the artifact*, so permissions travel with the thing permitted and a reviewer sees the blast radius beside the steps. Enforced before the first navigation, on every subsequent one, and inside the discovery loop — where a refusal is fed back to the model as an observation so it re-routes rather than dying.

**Reversibility, not danger.** `safe` means doing it twice is indistinguishable from doing it once. The split is about reversibility because that's what decides whether a retry is safe. Risky capabilities are blocked from unattended replay unless approved, and a validator makes it impossible for an artifact to carry a risky step while declaring itself reversible.

**Redaction at the durability boundary** — in the log and evidence writers, the one place data becomes permanent, rather than at forty call sites one of which would be forgotten. Deny-by-pattern outbound (SSN, account numbers, card-like runs, emails, phones, API keys) plus exact-match on `sensitive` arguments and session credentials. No evidence file in the repo contains an SSN, password or key.

**Drafts are not approved.** Discovered artifacts carry `approved_for_unattended=False`: discovery cannot know which steps are irreversible and sees only the happy path.

**Limits.** Pattern redaction has false negatives — a member's *name* matches no regex and is not redacted; production would pair this with field-level tagging from the app's data dictionary. It protects logs and evidence, never the live session. The allowlist is origin-prefix based. And risk classification on a discovered artifact is a human judgement the system cannot make for itself.

## Cuts

**Not built:** multi-tenant plumbing (designed above; the `extends` field and `app_id`/`tenant_id` split exist so it needs no format change); a desktop backend (the seam is real, the second implementation isn't); a co-browsing operator console; business-outcome discovery — discovery emits `business_outcomes: []` because one happy-path run cannot observe them, so the reference artifact's outcomes are human-declared; queues and workers.

**Next, in order:** (1) **outcome discovery by negative probing** — deliberately search a nonexistent member and ask the model to classify the resulting screen; this closes the one real gap between a discovered draft and the reference artifact, and `--verify-with` is the start of that machinery. (2) **Approval workflow** — `draft → reviewed → approved`, with the artifact hash pinned at approval so an approved capability can't be silently edited. (3) **Drift telemetry** — aggregate `StepRecord.resolution` per tenant; the data is already recorded, nothing consumes it. (4) **Bounded single-step LLM recovery** on an unrecognised screen, policy-checked and recorded, never open-ended.

**Rough edges, stated plainly.** Parameterisation is by value-matching against declared examples, so it would mis-handle a parameter whose value coincidentally equals unrelated text on screen. `RoleOrdinalStrategy` is implemented but never emitted by discovery. The container-narrowing heuristic in the recorder picks the longest alphabetic token, which is right for an accounts table and guesswork elsewhere — it is safe only because `--verify-with` catches a wrong guess. And the target app is one I wrote, so it is hostile in the ways I anticipated rather than the ways a real vendor product would surprise me.
