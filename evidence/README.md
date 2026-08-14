# Evidence

Run logs are JSONL — one event per line, one file per run, named
`<kind>-<timestamp>-<id>.jsonl`. Screenshots share the run id, so a failure and its
picture are always findable from each other.

This set is **curated**, not everything that was ever produced: one discovery run,
one full replay tour covering every runtime condition, and one escalation pair. Runs
from earlier iterations of the code were removed so the set reads cleanly.
Everything here is regenerable — see the bottom of this file.

Everything passed through the redactor on the way to disk. You will see
`[redacted:account]` in the discovery log where the model was shown a member record:
the model saw the real screen, the log did not.

## What to read, in order

### 1. The discovery run — the requirement that cannot be stubbed

`discovery-20260814T060106Z-050de2.jsonl`

A real Gemini run against the live UI. Filter for `model.call` to see the decisions
in sequence:

```bash
python3 -c "
import json
for line in open('evidence/discovery-20260814T060106Z-050de2.jsonl'):
    e=json.loads(line)
    if e['type']=='model.call': print(e['turn'], e['tool'], e['args'])"
```

Four turns: fill the member number, click Search, read the SAVINGS row, `finish`.
Note the `container_name` the model chose for the read — and then read the
canonicalisation note in `discovery/recorder.py`, because the raw choice was not
the one that ended up in the artifact.

### 2. The discovered capability replayed for a *different* member

`replay-member_savings_lookup_discovered-20260814T060113Z-846491.jsonl`

The same run, immediately verified: the freshly recorded draft replayed with
`member_id=10003` and **no model**, returning `61150.73`. This is the only check
that distinguishes a capability that generalises from one that memorised the case it
was recorded on.

### 3. The replay tour — every runtime condition

The eight `replay-member_savings_lookup-20260814T0638*` / `T0639*` files, in
timestamp order:

| Run | Input | `replay.finish` status |
|---|---|---|
| `T063845Z-b65d64` | 10001 | `success` |
| `T063845Z-0c928d` | 10002 | `success` |
| `T063845Z-5cfd76` | 99999 | `business_outcome` → `MEMBER_NOT_FOUND` |
| `T063846Z-55889c` | 20001 | `business_outcome` → `PERMISSION_DENIED` |
| `T063846Z-70c105` | 30001 | `success`, after `recovery.done` |
| `T063846Z-41c338` | 40001 | `success` (absorbed a 3s stall) |
| `T063850Z-3e7642` | 50001 | `failure` + screenshot |
| `T063900Z-00eeb5` | abc | `failure` (`invalid_input`) |

(Run ids are random, not sequential — several of these finished inside the same
second, so the id order does not follow the input order.)

Two things worth opening:

- **The business-outcome runs** end in `outcome.detected`, with
  `status: business_outcome` — *not* `failure`. A caller receiving
  `MEMBER_NOT_FOUND` should tell the member their number is wrong; a caller
  receiving `failure` should retry or page someone. Collapsing the two is the
  mistake the brief names.
- **The `50001` run** is the exceptional-state replay. Read `expected` and
  `observed` on its `failure` event: it names the step, what it was looking for, and
  what the screen actually said (`Reference: SYS-500 / CORE-LINK`). Its
  `-s3-stuck.png` is the richer signal.

### 4. The human handoff

`replay-member_savings_lookup-20260814T063919Z-0b3bdc.jsonl`

```
escalation.raised    step=s3  reason=screen is not a state this capability declares
control.transferred  to=human
control.returned     to=automation  resumed=true
                     navigations=['main -> http://127.0.0.1:5001/member/30001']
                     note='acknowledged the maintenance notice and returned control'
outputs.extracted    {'savings_balance': 5580.0}
```

The `navigations` list is **observed** from the live page during the takeover, not
reported by the operator. The run ends `success` — the human's single click let the
automation finish, on the same session it had been using.

`T063930Z-661bc8` is the other ending: an operator who looks and **cannot** fix it
(a core-banking outage is nobody's clicking error). It ends `failure`, and still
records that a person was consulted. Note it has no `control.transferred` event —
that operator declined rather than taking the session, and the log says so.

## Regenerating all of this

```bash
.venv/bin/python -m flask --app target_app.app run --port 5001   # terminal 1

.venv/bin/python -m scripts.demo_replay --all                    # terminal 2
.venv/bin/python -m scripts.demo_escalation
.venv/bin/python -m scripts.run_discovery \
    --goal "Look up member 10001 and read their current savings balance" \
    --param member_id=10001 --verify-with member_id=10003
```

Only the last needs `GOOGLE_API_KEY`. Free-tier daily quotas are small and metered
per model — if one is spent, `--model gemini-2.5-flash-lite` has its own.
