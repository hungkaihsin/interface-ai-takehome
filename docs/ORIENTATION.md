# Orientation — what this project is, in plain language

> **Who this is for:** Daniel. This is a working explainer, not a deliverable.
> The graded documents are `/README.md`, `/REPORT.md`, and `/evidence/`.
> Decide before submission whether to keep this file (see the note at the end).

---

## 1. The business problem interface.ai is actually solving

interface.ai sells AI agents to banks and credit unions. An agent might be asked
*"what's this member's savings balance?"* or *"open a sub-account for them."*

To answer, the agent has to reach into the bank's internal software. When that
software has an API, you call the API — easy, and explicitly **out of scope here**.

The problem is that a huge amount of banking software has **no API at all**. Core
banking screens, servicing tools, admin consoles — much of it written decades ago,
and the only way in is to drive the screen the way a human teller would: look at
it, click, type, read the result.

So this project builds **the hands**. The agent-facing product decides *what* to
do. This system is *how it gets done* inside software that offers no other door.

---

## 2. The whole idea in one sentence

> **The model figures it out once. That gets saved as a reusable capability.
> After that, it runs with no model involved.**

This is the sentence to memorise. Everything in the repo serves it.

**Why it's built this way** — three reasons, and you should be able to give all three:

1. **Cost.** An LLM reasoning through six screens costs real money and takes
   seconds. A recorded flow costs nothing and takes milliseconds. At a bank doing
   this thousands of times a day, that gap is the entire business case.
2. **Reliability.** An LLM might do it slightly differently each time. A bank
   cannot accept "slightly differently" when the action is opening an account.
3. **Reviewability.** A compliance officer can read a saved flow and approve it.
   Nobody can audit "the model will figure it out."

The brief puts it as: *the model discovers, the artifact becomes a reusable
capability, deterministic replay is how the agent invokes it in production.*

---

## 3. The same idea, concretely

**Discovery run — happens once, costs money, uses Gemini:**

> Goal: *"Look up member 10001 and read their savings balance."*
>
> The model sees the screen, and decides step by step: type `10001` into the
> field named "Member Number" → click "Search" → the record appears → read the
> balance from the SAVINGS row. Goal met.

**We then save what it learned** — not the model's chat transcript, but a clean
structured recipe: the ordered steps, how to find each control, what input it
needs (`member_id`), what it gives back (`savings_balance`), and how to confirm
it actually worked.

**Replay — happens thousands of times, costs nothing, no model:**

> `replay(capability="member_savings_lookup", member_id="10002")`
> → `{"savings_balance": 743.08}`

Same steps, every time. The model is gone. That's the product.

---

## 4. What the six requirements mean, translated

| # | Brief's wording | What it actually means |
|---|---|---|
| 3.1 | Goal-driven agent loop | The LLM drives a real UI until it succeeds or hits a stopping condition. Must be genuinely real — **this is the one thing that can't be faked.** |
| 3.2 | Typed, versioned artifact | The saved recipe. **Graded hardest.** Needs typed inputs, typed outputs, a success condition, and a version. |
| 3.3 | Deterministic replay | Re-run the recipe with no model. Same input → same steps → same output. Must handle things going wrong. |
| 3.4 | Safety | An allowlist of what's permitted; treat irreversible actions carefully; never write SSNs or passwords into files. |
| 3.5 | Evidence | Logs of what happened and why, plus something richer (a screenshot) when it fails. |
| 3.6 | Human-in-the-loop | When stuck, hand the **same live browser session** to a person, let them fix it, take control back. Not a fresh session — the same one. |
| 3.7 | Heterogeneity (design only) | Explain how this would work on a desktop app, and across hundreds of banks running the same vendor software. Writing, not code. |

---

## 5. The trap they set on purpose

Their glossary calls this out as *"the most common design mistake here."*

If you search for a member who doesn't exist, the app says **"no member found."**
That is **not a bug**. It's a correct, useful answer that the caller needs.

A naive system throws an exception and reports failure. That's wrong — it turns a
legitimate business answer into a false alarm, and at scale it means an ops team
chasing "errors" that are just customers who don't exist.

So results must land in **three distinct buckets**:

| Bucket | Meaning | Example in our app | Right response |
|---|---|---|---|
| **Business outcome** | A real answer, just not the happy one | "no member found"; permission denied; validation rejected | Report it cleanly to the caller |
| **Recoverable** | A hiccup with a known remedy | maintenance pop-up; slow page | Handle it and carry on |
| **Hard failure** | Genuinely broken | HTTP 500 from the core banking link | Stop, and say what step, what was expected, what was seen |

Getting this three-way split right is worth more than any extra feature.

---

## 6. What exists right now

**The fake bank system — `target_app/`.**

This is the thing being automated, not the automation. It's a Flask app
impersonating a legacy back-office console, and it is *deliberately unpleasant*:

- content sits inside `<iframe>`s, so there's no single flat page to read
- layout is nested `<table>`s — the markup carries appearance, never meaning
- **zero test IDs**, no useful class names
- records show SSNs and full account numbers, so redaction has real work to do

It can produce every condition on demand, triggered by member ID:

| You enter | You get | Bucket |
|---|---|---|
| `10001`, `10002`, `10003` | a normal record | happy path |
| `99999` (or any unknown) | "No member found" | business outcome |
| `20001` | entitlement denial | business outcome |
| `30001` | maintenance pop-up, first visit only | recoverable |
| `40001` | 3-second stall | recoverable |
| `50001` | HTTP 500 | hard failure |

**Also settled: how we find things on screen.**

Not CSS selectors. We use the **accessibility tree** — the description browsers
already build for screen readers. Three reasons, in order of how much they matter:

1. It's the *only* thing that also exists on desktop apps, which §3.7 asks about.
2. It survives markup changes that shatter CSS selectors.
3. The brief's own glossary recommends it.

A probe against the live app (`scripts/probe_a11y.py`) proved two things:

- The top-level page contains **no controls at all** — only two `iframe` nodes.
  So *which frame* an element lives in has to be part of its address. Proven, not
  assumed.
- Labelled controls expose clean names (`textbox "Member Number"`). Unlabelled
  ones expose **nothing** — but the table row around them still says `row
  "Nickname"`. So we can scope to the row, then take the box inside it.

That yields a three-tier way of finding any control, all of it accessibility-native:

1. role + name — `button "Search"`
2. role, scoped to a row/group with a known name — the textbox inside `row "Nickname"`
3. role + position within that scope

Tier 2 is the interesting one. A CSS selector like `table tr:nth-child(2) input`
breaks the moment somebody inserts a row. "The input in the row labelled Nickname"
survives that, because it's anchored to *meaning* rather than *position*.

## 7. Status — all of it is now built

Every core requirement is implemented and evidenced. `README.md` has the commands;
this is the map of what ended up where.

| Piece | Where | What it does |
|---|---|---|
| Artifact schema | `capability/` | The recipe format. Graded hardest, so it was argued before it was written. |
| Perception + action | `surface/` | Turns a screen into text the model reads; carries out what it picks. Only `web.py` knows browsers exist. |
| Discovery loop | `discovery/` | Gemini driving the app for real. The one thing that couldn't be faked. |
| Replay engine | `replay/engine.py` | Runs the recipe with no model, sorting every result into the three buckets. |
| Safety | throughout | Allowlist, safe-vs-risky gating, redaction before anything is written down. |
| Escalation | `replay/escalation.py` | Pause, hand the live session to a human, resume, record what they did. |
| Evidence | `evidence/` | A discovery run, replays of every condition, a failure with a screenshot, a handoff. |

**Three things went wrong during the build that are worth knowing**, because they
are the best material you have for a conversation about this work:

1. **"Frame was detached."** Reloading the page destroys and rebuilds the iframes;
   the code was holding a stale reference. Fix: wait for a *live* frame.
2. **Checking the screen too fast.** After clicking Search, the code asked "is this
   not-found?" before the page had loaded. It passed *sometimes* — flaky, which is
   worse than broken. Fix: never look once; poll until the screen settles.
3. **The model overfitted.** Asked to read a balance of `18432.19`, Gemini targeted
   the cell **by that value**. That works forever for the member it was recorded on
   and fails for everyone else. Fixed structurally in `discovery/recorder.py`, and
   caught independently by replaying the fresh recipe with a *different* member.

That third one is the most interesting thing in the project. It is the difference
between "the model produced something" and "the model produced something that
generalises," and nothing but a second input can tell them apart.

---

## 8. How to verify what's built — do this yourself

### A. See the app with your own eyes (5 minutes, most useful)

```bash
cd "/Users/danielhung/Desktop/02. Projects/interface-ai-takehome"
.venv/bin/python -m flask --app target_app.app run --port 5001
```

Open **http://127.0.0.1:5001** and sign on with `operator` / `letmein`.

Now walk the conditions. In the **Member Number** box, try each of these:

| Enter | What you should see | Ask yourself |
|---|---|---|
| `10001` | Full record. Savings balance **18432.19** | This is the number the agent must extract |
| `99999` | "No member found for number 99999" | Is this an error, or an answer? (an answer) |
| `20001` | "Entitlement check failed" | Same question — an answer, not a crash |
| `30001` | Maintenance notice → Acknowledge → record appears. **Try it a second time** — no notice | Why once-only? Because a pop-up that's always there is trivial to hardcode |
| `40001` | Hangs ~3 seconds, then loads | Replay needs to wait properly, not time out at 1s |
| `50001` | Red "System Error" screen | The only one that's genuinely broken |

Then from member `10001`, click **Open Sub-Account** and submit it empty. You'll
get three validation errors. Now fill it in properly (any product, a nickname,
deposit `250`) and you reach a confirmation screen with a new account number.

That confirmation screen matters: it's the **checkpoint** for that capability, and
opening the account is the **irreversible action** the safety layer will gate.

**While you're on member `10001`, notice the SSN printed in the clear.** That's
there on purpose. It's what redaction has to strip before anything gets written
to a log or artifact.

### B. Confirm the hostile markup is really hostile

View source on the member record (`Cmd+Option+U`). Look for anything you could
reliably select on — an `id`, a `data-testid`, a meaningful class. There isn't
one. That's the point: it's why the accessibility tree isn't a preference but a
necessity.

### C. See what the machine sees

With the app still running, in a second terminal:

```bash
cd "/Users/danielhung/Desktop/02. Projects/interface-ai-takehome"
.venv/bin/python scripts/probe_a11y.py
```

This logs in, walks to a member record, and prints the accessibility tree at each
step. Three things to look for:

1. **The frame inventory** — three frames: the shell, `nav`, and `main`.
2. **The top document's tree** — it contains no buttons or textboxes, just two
   `iframe` nodes. *This is the proof that frame identity must be part of a
   locator.*
3. **The sub-account form near the bottom** — `textbox` with no name, sitting
   inside `row "Nickname"`. *This is the proof that tier 2 is needed.*

### D. Confirm no secrets are committed

```bash
git status --short          # .env must NOT appear
git check-ignore -v .env    # should print the ignoring rule
```

---

## What to decide about this file

It's written for you, not for them. Two honest options:

- **Keep it.** Reviewers see a candidate who can explain his own system in plain
  language. That's a real signal, and communication is on their criteria list.
- **Delete it before submission.** Keeps the repo to exactly the deliverables and
  removes any chance it reads as thinking-out-loud rather than finished work.

Either is defensible. If you keep it, it stays in `docs/` and never competes with
`README.md` or `REPORT.md` for a reviewer's attention.
