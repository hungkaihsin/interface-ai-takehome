# Testing guide — verify this yourself, step by step

> For anyone who would rather check the claims than take them on trust. Work
> through it in order; each step builds on the last.
>
> **Time:** about 25 minutes for steps 0–7. Step 8 (discovery) needs an API key.

Every step has the same four parts:

**Run** → **Expect** → **What it proves** → **If it fails**

---

## The three questions worth asking

Before any command, know what you're doing. For any piece of software, ask:

1. **Does it run at all?**
2. **Does it do the right thing on good input?**
3. **Does it refuse bad input?** ← most people skip this, and it's where the value is

Question 3 is why steps 5 and 7 exist. Software that works on good input is easy.
Software that correctly *refuses* is what you actually trust with a bank's data.

---

## Step 0 — Setup check

**Run**

```bash
cd <repo>
.venv/bin/python --version
.venv/bin/python -c "import flask, playwright, pydantic, google.genai; print('deps ok')"
```

**Expect** — Python 3.11 or higher, then `deps ok`.

**If it fails** — rebuild the environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

---

> **Using Docker?** Steps 1, 4, 6 and 7 collapse into one command:
> `docker compose run --rm app`. Step 2 needs `docker compose up app` so you can
> reach http://localhost:5001 in your own browser, and the hands-on takeover at the
> end of step 6 needs the local virtualenv, because a container has no display.

## Step 1 — Start the target application

This is the fake bank system. It must be running for almost everything else.

**Run** — in a terminal you will leave open:

```bash
.venv/bin/python -m flask --app target_app.app run --port 5001
```

**Expect** — `Running on http://127.0.0.1:5001`. Leave it. Open a **second** terminal
for every other step.

**Check it's up**, from the second terminal:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5001/login   # 200
```

**If it fails** — `Address already in use` means it's already running; carry on. If
you need to stop an old one: `pkill -f "flask --app target_app.app"`.

---

## Step 2 — Look at what's being automated (do this by hand)

Skipping this makes everything after it abstract. Spend five minutes here.

**Run** — open <http://127.0.0.1:5001> and sign in with `operator` / `letmein`.

Type each of these into **Member Number** and press Search:

| Enter | Expect | Ask yourself |
|---|---|---|
| `10001` | Full record. Savings balance **18432.19** | This is the number the automation extracts |
| `10002` | Different record, balance **743.08** | Same screen, different data — that's why one recipe works for both |
| `99999` | "No member found for number 99999" | **Is this an error, or an answer?** (an answer) |
| `20001` | "Entitlement check failed" | Same question. The record exists; you may not see it |
| `30001` | A maintenance notice → click **Acknowledge** → the record appears | A pop-up between you and where you were going |
| `30001` again | Straight to the record, **no notice** | Why it can't be hardcoded — it only appears sometimes |
| `40001` | Hangs about 3 seconds, then loads | Why waits must be "wait until ready", never "sleep 1 second" |
| `50001` | Red **System Error** screen | The only one that's genuinely broken |

Then, still on member `10001`:

- Click **Open Sub-Account**, submit it **empty** → three validation errors. Another
  *answer*, not a crash.
- Fill it in properly (any product, any nickname, deposit `250`) → a confirmation
  screen with a new account number. **That confirmation is the proof the action
  committed**, and opening an account is the irreversible thing safety gates.
- Look at the **SSN** printed in the clear on the record. That's deliberate — it's
  what redaction has to strip before anything is written to a file.

**What it proves** — the target surface really does produce all three classes of
outcome, so the tests that follow are testing something real.

---

## Step 3 — See what the machine sees

**Run**

```bash
.venv/bin/python scripts/probe_a11y.py
```

**Expect** — a lot of output. Three things to find in it:

1. **The frame inventory** — three frames: the outer page, `nav`, and `main`.
2. **`=== TOP DOCUMENT a11y ===`** — it contains **no buttons and no textboxes**,
   only two `iframe` entries.
3. **Near the bottom**, the sub-account form: a `textbox` with **nothing in quotes
   after it**, sitting inside `row "Nickname"`.

**What it proves** — the two facts the whole targeting design rests on. (2) is why
every locator carries a frame path: an address without one points at an empty page.
(3) is why locators need a second tier: that box has no name of its own, but the row
around it does.

**If it fails** — if the browser can't start, re-run
`.venv/bin/python -m playwright install chromium`.

---

## Step 4 — Replay: the production path

The main event.

**Run**

```bash
.venv/bin/python -m scripts.demo_replay --all
```

**Expect** — eight rows. Check the `status` column against this:

| Input | Status | Meaning |
|---|---|---|
| `10001` | `success` → `18432.19` | worked |
| `10002` | `success` → `743.08` | **same recipe, different member** |
| `99999` | `business_outcome` → `MEMBER_NOT_FOUND` | an answer, not a crash |
| `20001` | `business_outcome` → `PERMISSION_DENIED` | an answer, not a crash |
| `30001` | `success`, `[recovered: MAINTENANCE_INTERSTITIAL]` | fixed itself and carried on |
| `40001` | `success` → `12007.65` | waited out the stall |
| `50001` | `failure` (`unknown_state`) | correctly the **only** broken one |
| `abc` | `failure` (`invalid_input`) | rejected before the browser moved |

**What it proves** — requirement 3.3 and the whole three-bucket result contract. The
two `business_outcome` rows are the design trap the brief calls *"the most common
design mistake"*; they are not failures and the system says so.

**If it fails** — if `10001` fails, the target app probably isn't running (step 1).
If member `30001` fails instead of recovering, restart the target app: the
interstitial fires once per session and a previous run may have consumed it.

---

## Step 5 — Prove no AI is involved in replay

This is the single most important check in this guide, because it verifies the
project's central claim rather than trusting it.

**Run**

```bash
grep -rniE 'genai|anthropic|openai|GOOGLE_API_KEY' replay/ surface/ capability/ observability/

env -u GOOGLE_API_KEY .venv/bin/python -m scripts.demo_replay --all
```

**Expect** — the `grep` prints **nothing at all**. The replay then runs and produces
the same eight rows as step 4.

**What it proves** — no model, no API key, no cost, no fallback path. Remember the
comparison: discovery is *training* (expensive, once), replay is *inference* (cheap,
constant). This is what makes that true rather than aspirational.

**If it fails** — if `grep` prints anything, a model dependency has leaked into the
production path and the claim is broken. That would be a real bug worth fixing
before submitting.

---

## Step 6 — The human takeover

**Run**

```bash
.venv/bin/python -m scripts.demo_escalation
```

**Expect** — two scenarios.

*Scenario A*: an `INTERVENTION REQUIRED` block naming the capability, the step, why
it stopped, and what's on screen. Then `[operator] acknowledged the maintenance
notice`, and:

```
  -> status    : success
  -> escalated : True
  -> outputs   : {'savings_balance': 5580.0}
```

*Scenario B*: another intervention, then `[operator] reviewed; not resolvable from
the UI`, and `status: failure`, `escalated: True`.

**What it proves** — requirement 3.6, with both honest endings. A human who *can*
fix it (the run completes using the state they left behind), and one who **can't** —
a core-banking outage is nobody's clicking error, and the run reports a clean
failure that still records a person was consulted.

**Then read the recorded handoff:**

```bash
.venv/bin/python -c "
import json,glob
for f in sorted(glob.glob('evidence/*.jsonl')):
    for line in open(f):
        e=json.loads(line)
        if e['type'].startswith('control'):
            print(e['type'], {k:v for k,v in e.items() if k not in ('ts','run_id','type')})"
```

Look for `navigations`. That was **observed** from the live browser during the
takeover — not something the operator typed in a form. If a human claims they made
one click and actually visited four screens, both facts are in the evidence.

### Doing the takeover yourself

```bash
.venv/bin/python -m scripts.demo_escalation --headed
```

A real Chrome window opens. When it pauses, **you** click Acknowledge in that
window, return to the terminal, answer `y`, and type what you did. The run finishes
using the state you left behind — **the same session**, same cookies, nothing
recreated. That is the part of requirement 3.6 that's easy to fake and isn't.

---

## Step 7 — Break it on purpose

Reading tests is passive. Breaking something teaches more.

**Run** — open `artifacts/member_savings_lookup.v1.json`, find step `s3`, and change:

```json
"risk": "safe"      →      "risk": "risky"
```

Save, then:

```bash
.venv/bin/python -c "
from capability.schema import CapabilityArtifact
CapabilityArtifact.model_validate_json(open('artifacts/member_savings_lookup.v1.json').read())
print('ACCEPTED - this would be a bug')"
```

**Expect** — it refuses:

```
artifact contains risky steps but policy.has_irreversible_steps is False
```

**What it proves** — you just made a capability **lie about being dangerous**, and
the schema caught it. That flag is what the approval gate reads before allowing
unattended replay; if the two could drift apart, the guardrail would be decorative.

**Put it back:**

```bash
.venv/bin/python -m scripts.build_example_artifact
```

**Now the automated version:**

```bash
.venv/bin/pytest tests/ -v
```

**Expect** — **21 passed**. Each test takes a good artifact, breaks it one specific
way, and asserts the refusal.

---

## Step 8 — Discovery (needs an API key)

The one part that costs money and can't be faked.

**Quota first.** Free-tier limits are per-model and small — `gemini-2.5-flash` is
**20 requests per day**, and one run costs 4–6. Seeing `rate limited; waiting 8s`
once or twice between successful turns is a per-*minute* limit and is fine. Seeing
the daily-quota message means switching model, not waiting.

**Run**

```bash
.venv/bin/python -m scripts.run_discovery \
    --goal "Look up member 10001 and read their current savings balance" \
    --param member_id=10001 \
    --capability member_savings_lookup_discovered \
    --verify-with member_id=10003
```

**Expect** — the model's decisions, one per turn:

```
  [turn 1] fill({'name': 'Member Number', 'value': '10001', 'frame': 'main', ...})
  [turn 2] click({'name': 'Search', 'role': 'button', 'frame': 'main'})
  [turn 3] read_text({'container_name': 'SAVINGS', 'index': 2, ...})
  [turn 4] finish({...})
```

then:

```
  approved   : False  (drafts are not)
  concrete value '10001' in steps/outputs: absent (parameterised)

  VERIFICATION: replaying the draft with member_id=10003 (no model)
  status   : success
  outputs  : {'savings_balance': 61150.73}
  VERDICT: the draft generalises to a different input.
```

**What it proves** — three things at once. Requirement 3.1 (a real LLM run against a
live UI). That the recorded recipe contains **no member's actual data** — the step
stores `{{ member_id }}`. And, via `--verify-with`, that the recipe works for a
member it was *never recorded on*, which is the only question that matters and the
one a discovery run cannot answer about itself.

**Why `approved: False`** — discovery sees only the happy path, so it can't know
which steps are irreversible or which business outcomes exist. A machine proposes;
a human classifies risk and approves.

**If it fails**

| Symptom | Cause | Do this |
|---|---|---|
| `GOOGLE_API_KEY is not set` | no key | `cp .env.example .env`, add a free key from aistudio.google.com/apikey |
| `The free-tier DAILY quota ... is spent` | `gemini-2.5-flash` allows only **20 requests/day** | add `--model gemini-2.5-flash-lite` — it has its own quota. The run fails in ~2s and tells you this |
| `rate limited; waiting 8s` a few times, then it continues | per-**minute** limit, using the delay the API supplied | nothing — this is working correctly |
| `VERDICT: does NOT generalise` | the model overfitted a locator | **This is the system working.** See below |

### If verification fails, that's a feature

This genuinely happened during the build. Asked to read a balance of `18432.19`, the
model targeted the cell **by that value** — `cell name "18432.19"`. That recipe
replays perfectly for member 10001 and fails for every other member alive.

Two independent guards now catch it: the recorder strips locators that name their own
value and narrows over-broad row names to their stable token (`SAVINGS`), and this
verification replay tests the result against a different member. If you ever see that
verdict, the safeguards did their job — read the `LOCATOR STABILITY WARNINGS` block
just above it.

---

## Step 9 — Safety and data handling

**Run**

```bash
grep -rlE '[0-9]{3}-[0-9]{2}-[0-9]{4}|letmein|AIzaSy' evidence/
git check-ignore -v .env
git ls-files | grep -c '^\.env$'
```

**Expect** — the first prints **nothing**, the second confirms `.env` is ignored, the
third prints `0`.

**What it proves** — no SSN, no password, and no API key reached any file that gets
committed, even though the member records on screen contain all of them.

**See redaction working directly:**

```bash
.venv/bin/python -c "
from observability.redaction import Redactor
r = Redactor(); r.add_literals(['letmein'])
print(r.text('SSN 900-31-4402 acct SAV-4471-00812 pw=letmein d.a@example.invalid'))"
```

**Expect** — every value replaced with `[redacted:...]`.

**The known gap, which you should be able to state out loud:** a member's *name* is
PII and matches no pattern, so names are **not** redacted. Pattern matching has false
negatives; a production system pairs this with field-level tagging from the app's own
data dictionary. Saying this yourself is stronger than being asked.

---

## Checklist

Tick these off before you submit:

- [ ] Target app runs, and all seven member IDs behave as the table says (step 2)
- [ ] The top document has no controls, only iframes (step 3)
- [ ] Replay classifies all eight cases correctly (step 4)
- [ ] `grep` finds no model library in the replay path (step 5)
- [ ] Replay works with `GOOGLE_API_KEY` unset (step 5)
- [ ] Escalation completes, and `navigations` is recorded (step 6)
- [ ] A risky step with a reversible policy is **refused** (step 7)
- [ ] `pytest` → 21 passed (step 7)
- [ ] A discovery run exists in `/evidence/` (step 8)
- [ ] No SSN, password, or API key in `evidence/` or the repo (step 9)
- [ ] Pushed to a public GitHub repo

---

## What to do if something is wrong

Don't paper over it. The brief says you must be able to defend everything you
submit, which cuts both ways: a known, documented weakness you can explain is worth
more than a hidden one.

If a step fails and you can't fix it, the right move is a line in `REPORT.md` under
**Cuts** saying what doesn't work and why. That is a stronger submission than
pretending, and it's the same judgement the assignment is actually testing.
