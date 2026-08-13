# Evidence

Run logs are JSONL — one event per line, one file per run, named
`<kind>-<timestamp>-<id>.jsonl`. Screenshots share the run id, so a failure and its
picture are always findable from each other.

Everything here passed through the redactor on the way to disk. You will see
`[redacted:ssn]` and `[redacted:account]` in the discovery log where the model was
shown a member record — the model saw the real screen, the log did not.

## What to read, in order

**1. The discovery run** — `discovery-*.jsonl`

The requirement that cannot be stubbed: a real Gemini run against the live UI.
Filter for `model.call` to see the model's decisions in sequence — it fills the
member number, clicks Search, reads the SAVINGS row, and calls `finish`.

```bash
python3 -c "
import json,glob
for line in open(sorted(glob.glob('evidence/discovery-*.jsonl'))[-1]):
    e=json.loads(line)
    if e['type']=='model.call': print(e['turn'], e['tool'], e['args'])"
```

**2. A successful replay** — the `replay-member_savings_lookup-*.jsonl` files ending
in `outputs.extracted`.

Same flow, no model. Note `action.fill` logs the resolved value while the *artifact*
stores only `{{ member_id }}`.

**3. A replay that hits an exceptional state** — the run whose last events are
`failure` with `kind: unknown_state`, paired with a `-s3-stuck.png` screenshot.

This is member `50001`, whose core-banking link is down. Read the `expected` and
`observed` fields: the failure names the step, what it was looking for, and what the
screen actually said (`Reference: SYS-500 / CORE-LINK`).

**4. A business outcome, which is not a failure** — runs ending in
`outcome.detected` with `code: MEMBER_NOT_FOUND` or `PERMISSION_DENIED`.

These are `status: business_outcome`, not `failure`. That distinction is the point:
a caller receiving `MEMBER_NOT_FOUND` should tell the member their number is wrong;
a caller receiving `failure` should retry or page someone.

**5. The human handoff** — the run containing `control.transferred`.

```
escalation.raised    step=s3  reason=screen is not a state this capability declares
control.transferred  to=human
control.returned     to=automation  resumed=true
                     navigations=['main -> http://127.0.0.1:5001/member/30001']
outputs.extracted    {'savings_balance': 5580.0}
```

The `navigations` list is *observed* from the live page during the takeover, not
reported by the operator. Note the run ends `success` — the human's click let the
automation finish, on the same session it had been using.

## Regenerating all of this

```bash
.venv/bin/python -m flask --app target_app.app run --port 5001   # terminal 1

.venv/bin/python -m scripts.demo_replay --all                    # terminal 2
.venv/bin/python -m scripts.demo_escalation
.venv/bin/python -m scripts.run_discovery \
    --goal "Look up member 10001 and read their current savings balance" \
    --param member_id=10001 --verify-with member_id=10003
```

Only the last needs `GOOGLE_API_KEY`.
