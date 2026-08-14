# Computer-Use Automation System

A vertical slice of the layer that gives an AI agent hands inside legacy bank
software that has no API.

> **The model discovers once. The successful run becomes a typed, reusable
> capability. After that it replays deterministically, with no model in the
> decision loop.**

If you've trained a model and then served predictions from it, you already know the
shape: discovery is training — expensive, slow, done once. Replay is inference —
cheap, fast, run constantly. This repo is that split applied to driving a UI.

---

## Table of contents

- [What this actually does](#what-this-actually-does) · [Setup](#setup) · [Demo path](#demo-path-the-exact-commands)
- [Running without live services](#running-without-live-services) · [How to verify it](#how-to-verify-it-yourself)
- [The target application](#the-target-application) · [Repo layout](#repo-layout) · [Design](/REPORT.md)

---

## What this actually does

```
  GOAL (natural language)
    │
    ▼
  DISCOVERY ─ Gemini reads the accessibility tree, decides, acts        ← costs money, once
    │         "type 10001 → click Search → read the SAVINGS row"
    ▼
  ARTIFACT  ─ a typed, versioned capability: inputs, outputs,           ← artifacts/*.json
    │         steps, success condition, expected outcomes, policy
    ▼
  REPLAY    ─ same steps, any member, no model, milliseconds            ← the production path
    │
    ├── success            → {"savings_balance": 743.08}
    ├── business outcome   → MEMBER_NOT_FOUND  (an answer, not a crash)
    ├── recovered          → dismissed an interstitial and carried on
    └── stuck              → hand the live session to a human, resume afterwards
```

**Requirement coverage** (brief §3):

| | Requirement | Status |
|---|---|---|
| 3.1 | Goal-driven LLM agent loop with stopping conditions | ✅ real Gemini run, evidence in `/evidence/` |
| 3.2 | Typed, versioned, serializable artifact | ✅ `capability/schema.py` |
| 3.3 | Deterministic replay, no LLM in the loop | ✅ verified with `GOOGLE_API_KEY` unset |
| 3.4 | Allowlist, safe/risky split, no persisted secrets or PII | ✅ enforced, tested |
| 3.5 | Structured logs + richer signal on failure | ✅ JSONL per run + screenshots |
| 3.6 | Detect stuck, route, human takes over the same live session | ✅ both endings demonstrated |
| 3.7 | Heterogeneity & multi-tenant (design only) | ✅ [`REPORT.md`](/REPORT.md#heterogeneity--multi-tenant); seam built, backends not |

---

## Setup

Everything runs locally; nothing touches a real service. Two ways in.

### Option A — Docker (nothing to install but Docker)

```bash
git clone <this-repo> && cd interface-ai-takehome
docker compose run --rm app
```

That builds the image, starts the legacy back-office app inside the container,
runs the full replay tour, runs the escalation demo, and runs the tests. First
build takes a few minutes (it downloads Chromium); after that it is cached.

To browse the legacy console yourself while it runs:

```bash
docker compose up app          # publishes http://localhost:5001
```

Sign in with `operator` / `letmein`. Other entry points:

```bash
docker compose run --rm app replay --all      # just the replay tour
docker compose run --rm app escalation        # just the human-takeover demo
docker compose run --rm app test              # just the tests
docker compose run --rm app serve             # just the legacy app, foreground
docker compose run --rm -e GOOGLE_API_KEY=... app discovery
```

`evidence/` and `artifacts/` are bind-mounted, so runs inside the container leave
their logs in your working copy where you can read them.

> **One thing Docker cannot do here:** `demo_escalation --headed` hands a real
> browser window to a human, and a container has no display. Everything else works
> identically; for a genuine hands-on takeover use Option B. This is why the
> container is a convenience rather than the only supported path.

### Option B — local virtualenv

Requires **Python 3.11+**.

```bash
git clone <this-repo> && cd interface-ai-takehome

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

### Configuration

```bash
cp .env.example .env
```

| Variable | Needed for | Notes |
|---|---|---|
| `GOOGLE_API_KEY` | **discovery only** | Free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `TARGET_APP_USER` / `TARGET_APP_PASSWORD` | optional | Defaults to the mock app's published test login |

Under Docker, pass the key through with `-e GOOGLE_API_KEY=...` or put it in a
`.env` file beside `docker-compose.yml` — compose reads it automatically, and
`.env` is gitignored.

**Replay needs no key at all.** That is a design property, not a convenience — see
[Running without live services](#running-without-live-services).

> **Free-tier quotas are small and per-model.** Measured: `gemini-2.5-flash` allows
> **20 requests per day**, and one discovery run costs 4–6. The loop distinguishes
> the two kinds of 429 — a per-minute limit is retried using the delay the API
> itself supplies, while a per-day quota fails immediately with advice, because no
> amount of backoff clears something that resets at midnight.
>
> If you hit the daily cap, pass **`--model gemini-2.5-flash-lite`** (its own
> quota), or skip discovery entirely: a recorded run is in `/evidence/` and replay
> never needs a key.

---

## Demo path (the exact commands)

> Running via Docker? The whole of this section is `docker compose run --rm app`.
> The commands below are the local-virtualenv equivalents.

**Terminal 1 — start the target application.** Leave it running.

```bash
.venv/bin/python -m flask --app target_app.app run --port 5001
```

Visit <http://127.0.0.1:5001> and sign in with `operator` / `letmein` to see what
the agent is driving.

**Terminal 2 — the four commands that are the whole project.**

```bash
# 1. DISCOVERY — a real LLM run against the live UI, recorded as a capability,
#    then immediately replayed with a DIFFERENT member to prove it generalises.
.venv/bin/python -m scripts.run_discovery \
    --goal "Look up member 10001 and read their current savings balance" \
    --param member_id=10001 \
    --capability member_savings_lookup_discovered \
    --verify-with member_id=10003

# 2. REPLAY — the production path. Every runtime condition, no model.
.venv/bin/python -m scripts.demo_replay --all

# 3. ESCALATION — a capability that meets a screen it doesn't know,
#    a human takes over the same live session, hands control back.
.venv/bin/python -m scripts.demo_escalation

# 4. TESTS
.venv/bin/pytest tests/ -v
```

<details>
<summary><b>What you should see</b> (click to expand)</summary>

Discovery:
```
  [turn 1] fill({'name': 'Member Number', 'value': '10001', 'frame': 'main', ...})
  [turn 2] click({'name': 'Search', 'role': 'button', 'frame': 'main'})
  [turn 3] read_text({'container_name': 'SAVINGS', 'index': 2, ...})
  [turn 4] finish({...})

  artifact   : artifacts/member_savings_lookup_discovered.v1.json
  approved   : False  (drafts are not)
  concrete value '10001' in steps/outputs: absent (parameterised)

  VERIFICATION: replaying the draft with member_id=10003 (no model)
  status   : success
  outputs  : {'savings_balance': 61150.73}
```

Replay tour:
```
  input    status            result
  10001    success           SUCCESS outputs={'savings_balance': 18432.19}
  10002    success           SUCCESS outputs={'savings_balance': 743.08}
  99999    business_outcome  BUSINESS_OUTCOME MEMBER_NOT_FOUND
  20001    business_outcome  BUSINESS_OUTCOME PERMISSION_DENIED
  30001    success           SUCCESS  [recovered: ['MAINTENANCE_INTERSTITIAL']]
  40001    success           SUCCESS outputs={'savings_balance': 12007.65}
  50001    failure           FAILURE unknown_state at step s3
  abc      failure           FAILURE invalid_input
```
</details>

**A real human takeover** (opens a browser window and hands it to you):

```bash
.venv/bin/python -m scripts.demo_escalation --headed
```

Acknowledge the notice yourself in the window, then answer `y`. The run finishes
using the state *you* left behind — same session, same cookies.

---

## Running without live services

The only external dependency is the Gemini API, and only discovery uses it.

```bash
env -u GOOGLE_API_KEY .venv/bin/python -m scripts.demo_replay --all
```

This is the check worth running first, because it verifies the central claim rather
than taking it on trust. There is no fallback path and no cached model response —
`replay/`, `surface/`, `capability/` and `observability/` import no model library at
all:

```bash
grep -rniE 'genai|anthropic|openai|GOOGLE_API_KEY' replay/ surface/ capability/ observability/
# (no output)
```

If you don't want to spend a key at all, `/evidence/` contains a complete recorded
discovery run, and `artifacts/member_savings_lookup.v1.json` is a hand-authored
reference capability that replays without one.

---

## How to verify it yourself

For each claim, the command that tests it. **Question 3 is the one most people
skip, and it's where the value is** — software that works on good input is easy;
software that correctly *refuses* bad input is what you trust.

### 1. Does the target app really present the conditions it claims?

Open <http://127.0.0.1:5001> (`operator` / `letmein`) and enter each member number:

| Enter | Expect | The question it answers |
|---|---|---|
| `10001` | full record, savings **18432.19** | the value the capability extracts |
| `99999` | "No member found", **HTTP 200** | is this an error, or an answer? *(an answer)* |
| `20001` | "Entitlement check failed" | same question |
| `30001` | maintenance notice → Acknowledge → record. **Search it twice** — no notice the second time | why a once-only interstitial can't be hardcoded |
| `40001` | ~3-second stall, then loads | why waits are condition-based, never `sleep` |
| `50001` | red "System Error" | the only genuinely broken one |

Then **View Source** on a member record and look for an `id`, a `class`, or a
`data-testid` worth selecting on. There isn't one — which is why locators are built
on the accessibility tree.

### 2. Does replay classify all of those correctly?

```bash
.venv/bin/python -m scripts.demo_replay --all
```

Check the `status` column against the table above. Two `success`, two
`business_outcome`, one recovered `success`, one slow `success`, two `failure`.

### 3. Does it refuse what it should?

```bash
.venv/bin/pytest tests/ -v          # 21 tests
```

Then break something on purpose — this teaches more than reading the tests. Open
`artifacts/member_savings_lookup.v1.json`, find step `s3`, change `"risk": "safe"`
to `"risky"`, save, and run:

```bash
.venv/bin/python -c "
from capability.schema import CapabilityArtifact
CapabilityArtifact.model_validate_json(open('artifacts/member_savings_lookup.v1.json').read())"
```

It refuses: the artifact contains a risky step while its policy claims none. **You
just made a capability lie about being dangerous, and the schema caught it** — which
is what keeps the approval gate from being decorative. Restore with:

```bash
.venv/bin/python -m scripts.build_example_artifact
```

### 4. Is regulated data really kept out of what gets written down?

```bash
grep -rlE '[0-9]{3}-[0-9]{2}-[0-9]{4}|letmein|AIzaSy' evidence/    # no output
git check-ignore -v .env                                          # .env is ignored
```

The member records on screen *do* contain SSNs and full account numbers — that's
deliberate, so redaction has real work to do.

### 5. Does a discovered capability generalise, or did it just memorise?

This is the question a discovery run cannot answer about itself, so `--verify-with`
replays the fresh draft against a **different** input:

```bash
.venv/bin/python -m scripts.run_discovery \
    --goal "Look up member 10001 and read their current savings balance" \
    --param member_id=10001 --verify-with member_id=10002
```

A real run of this caught genuine overfitting: the model targeted the balance cell
**by its own value** (`cell name "18432.19"`), which replays perfectly for the
member it was recorded on and fails for everyone else. Two independent guards now
catch that — a structural fix in `discovery/recorder.py` that strips
self-referential locators and narrows over-specific containers, and this
verification replay. See [`REPORT.md` → Cuts](/REPORT.md#cuts).

### 6. What does the machine actually see?

```bash
.venv/bin/python scripts/probe_a11y.py
```

Two things to look for: the top-level document contains **no controls, only two
`iframe` nodes** (which is why frame identity is part of every locator), and the
sub-account form has a `textbox` with **no accessible name** inside `row "Nickname"`
(which is why locators need a container-scoped tier).

---

## The target application

`target_app/` is a deliberately hostile stand-in for a legacy bank back-office —
**not part of the system**, it's the thing being driven. Written rather than
borrowed so that runtime conditions can be produced on demand, which is what makes
the error-handling evidence reproducible.

It is unpleasant in specific, intentional ways: content inside named `<iframe>`s,
layout carried by nested `<table>`s, no test IDs or meaningful class names, mixed
labelling (the search form binds `<label for>`; the sub-account form doesn't), and
records that render SSNs and full account numbers in the clear.

All data is synthetic. SSNs use the `900-xx-xxxx` range, which has never been
issued. Credentials are fake and published on the app's own login screen. It binds
to `127.0.0.1` only.

`/_control/expire-session` and `/_control/reset` are out-of-band test hooks reached
over HTTP, never by clicking — they exist so a session timeout can be produced at an
exact moment.

---

## Repo layout

```
target_app/      the hostile legacy app being driven (not part of the system)
surface/         perception & action. base.py is the heterogeneity seam;
                 web.py is the only file that knows browsers exist
capability/      the artifact: schema.py (contract) + locator.py (targeting)
replay/          deterministic execution: engine.py, result.py, escalation.py
                 — imports no model library, by design
discovery/       the only place an LLM runs: agent.py, tools.py, recorder.py
observability/   structured logging + redaction at the durability boundary
scripts/         the demo entry points
artifacts/       saved capabilities (hand-authored reference + discovered draft)
evidence/        run logs and failure screenshots
tests/           schema guardrails, redaction, recorder defences
docker/          container entrypoint
docs/            the assignment brief + a step-by-step testing guide
```

Two artifacts ship deliberately:

- **`member_savings_lookup.v1.json`** — hand-authored reference. Carries declared
  business outcomes and recoverable conditions, and is approved for unattended use.
  It exists because it let the replay engine be built and tested before the
  discovery loop existed, so a non-deterministic model and a new executor never had
  to be debugged at the same time.
- **`member_savings_lookup_discovered.v1.json`** — produced by an actual Gemini run.
  Marked `approved_for_unattended: false`, because discovery sees only the happy
  path and cannot know which steps are irreversible. **A machine proposes; a human
  classifies risk and declares outcomes.**

---

## Design write-up

[**`REPORT.md`**](/REPORT.md) — architecture, artifact schema, determinism & error
handling, heterogeneity & multi-tenant, escalation & handoff, safety, and cuts.

Want to check the claims rather than take them on trust?
[`docs/TESTING_GUIDE.md`](docs/TESTING_GUIDE.md) walks through verifying each one,
with the command, the expected output, and what it proves.
