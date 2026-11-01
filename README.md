# Award Seat Alerter

**Problem:** scarce inventory from an external provider can appear and vanish within minutes, so an alert only helps if a person hears it in time, and only once.\
**What it shows:** fixes for four failures in a private monitor: a duplicate wake, an ACK that never stopped the repeats, a one-shot alert that silently stopped, and a false fault that lasted about 32 hours.\
**Run it in about 60 seconds:** clone, then `make test` (Python 3.10+; no third-party packages; synthetic data, sends nothing).

[![tests](https://github.com/dyjhhh/award-seat-alerter/actions/workflows/test.yml/badge.svg)](https://github.com/dyjhhh/award-seat-alerter/actions/workflows/test.yml)

## Applying this on an engineering team

- **Idempotent intake from an external provider.** Email and API events are normalized into one event key, so the same item arriving from two sources opens one alert ([`candidates.py`](candidates.py)).
- **Human-in-the-loop acknowledgement.** Channels repeat until a bare ACK lands on one durable inbound queue, each under its own send ceiling; an old ACK cannot silence a new item ([`wake.py`](wake.py)).
- **Irreversible-action boundary.** There is no booking, payment or transfer code, and a test parses every module and fails on an action-shaped function name or a network import ([`tests/test_boundary.py`](tests/test_boundary.py)).
- **Gated re-arming.** A one-shot provider alert is recreated only from a fresh hit that names it, under one worker's lease, and the replacement is read back field by field ([`rearm.py`](rearm.py)).

Examples are a returned ticket, a released seat or a freed-up reservation slot. A monitor also has to avoid waking a person twice for the same item, and make clear that nothing was bought on their behalf. I built a private monitor for this and kept running into the same failures: a duplicate wake for one item, a reply that never stopped the repeats, a one-shot provider alert that silently stopped firing, and health checks that cried wolf.

This public edition keeps the parts that decide things; each is a row in [Decisions worth inspecting](#decisions-worth-inspecting). It runs offline on synthetic data and sends nothing.

## Try it

To run the demo and the unit tests separately:

```sh
python3 demo.py
python3 -m unittest discover -s tests -v   # 66 unittest cases for the seven modules
```

`make test` runs both suites, including 5 cases for the pattern scanner in `tools/`.

The demo reads one seat from an email and from an API row, scores it against a strict threshold, wakes three fake channels while one of them is down, stops on an ACK from the inbound queue, re-arms a one-shot alert that fired twice, and runs 32 hours of health checks that find only an aged physical test. All state lives in a temporary directory.

## Decisions worth inspecting

| Problem | Implementation | Check |
|---|---|---|
| The same seat arrives by email and by API, from two overlapping provider alerts, and wakes the operator twice | [`candidates.event_key`](candidates.py) hashes what is bookable (program, route, flights, date, cabin) and ignores source, message and alert group; [`WakeLedger.open`](wake.py) merges a second group into the escalation that already exists | Email and API rows share a key; a different cabin, date or flight does not; the second group returns `MERGED` and nothing extra is sent |
| A score rounded before comparison ties the threshold, or a tie counts as a pass | [`ValuePolicy.evaluate`](policy.py) compares the exact score with `>`; rounding is for display only | 70,000 points scores exactly 2.50 and fails; 69,999 points passes |
| A missing point balance is read as zero, or as plenty | Each configured pool must have a known balance; a missing or null balance is `balance_unknown` | Unknown, insufficient and unfundable cases each report their own reason |
| Repeats continue forever, or one dead channel stops the rest | Each channel has a send ceiling and a separate failure budget; a raised exception counts against that channel only | Ceilings of 3, 3 and 2 sends; a raising channel stops at its 2 failures while the others finish |
| A second process polling the chat bot steals the operator's reply | The bridge never polls the provider; the existing listener appends to [`InboundQueue`](inbound.py) and the bridge gets a read-only view | The reader has no consume or write method; a write through its connection fails; reading leaves the file byte-identical |
| "ACK later" or "ok" stops the alarm | Only the bare token counts, ignoring case and whitespace | Lookalike replies leave repeats running to the ceiling |
| An old ACK silences a new seat, or an ACK arrives mid-tick and the next channel still fires | An ACK closes escalations opened at or before it; the queue is read before every send | A seat opened after the ACK keeps repeating; an ACK written during the chat send stops the phone send |
| A one-shot alert fired and expired; recreating it from a guess makes duplicates or widens the search | [`RearmGate`](rearm.py): a fresh hit must name the tracked alert; one worker holds a lease; attempts are bounded; the replacement is read back field by field | Disappearance alone opens nothing; a second worker waits for the lease; an attempt past the limit quarantines; a wider or inactive replacement quarantines |
| The provider's expired page shows no ids, and an alert that fired twice leaves two rows | One row equal to the frozen spec, or 2 to 20 rows that are all identical to it, authorize; anything else is `expired_row_ambiguous` | A differing field, a repeated marker, a missing field, an empty page or 21 rows all quarantine |
| A flapping check sends ⚠, ✅, ⚠, ✅ | [`HealthGuard`](health.py) sends one ⚠ per incident and holds the ✅ until faults stay clear for 30 minutes; a return inside that window re-opens silently | A five-minute flap for two hours produces one ⚠ and, after it settles, one ✅ |
| An aged physical test is reported as a broken pipeline for a day and a half | Faults are classified as `failure` or `stale_evidence`; stale evidence goes to a separate 🟡 retest channel and never opens an incident | 33 hourly checks of an aged phone test send one 🟡 and no ⚠ |
| A bug or a confusing message leads to a purchase | No booking, holding, payment, transfer, cancellation or contact code exists; [`boundary.py`](boundary.py) refuses those actions and every alert says checkout was not reached | [`tests/test_boundary.py`](tests/test_boundary.py) parses every module and fails on an action-shaped function name or a network import |

Start with [`wake.py`](wake.py) and its [tests](tests/test_wake.py), then [`rearm.py`](rearm.py) and its [tests](tests/test_rearm.py). The [design notes](docs/design.md) cover the state machines and the trade-offs.

## Where it came from

The private monitor reads provider mail and an API feed, wakes the operator through a chat bot, a phone relay and a desktop alarm, and keeps a set of one-shot provider alerts alive. Its scripts are machine-specific and are not published. This edition keeps the decisions and drops the integrations: callers pass dicts and timestamps, transports are objects with a `send` method, and provider writes happen in an adapter that is not here. Several rules come straight from incidents in the private system: a second chat consumer that took replies away from the first, a one-shot alert that fired twice and left two indistinguishable expired rows, and a health check that reported a physical test past its validity as a pipeline fault for about 32 hours.

## Scope and limits

- This is a redesign, not the private deployment. It is not wired to any provider, mailbox or chat service, and nothing here has run against live inventory.
- `send` returning `True` means the transport accepted the message. It does not mean a phone rang or a person heard it; physical acceptance is a separate test, which is why the health guard tracks it as evidence that can go stale.
- The wake ledger and re-arm gate store JSON. The re-arm gate takes a POSIX file lock for each operation; the wake ledger assumes one process. Neither is a distributed queue.
- An ACK cannot retract a message a transport already accepted, and cannot stop a timer a phone already started.
- The value score is a points proxy relative to a configured baseline. It is not a cash valuation, and point pools are counted as units, not as equal money.
- `freshness` trusts the provider's observation time. A clock that is wrong in the provider's favour inside the skew allowance is not detected.
- Re-arming covers one-shot alerts only. A continuous alert is never touched, and a request never widens a spec: the replacement must equal it.
- The health guard returns notices; it does not deliver them, retry them or decide what the faults are. A check that could not run is `None`, which changes nothing; alerting on a check that has stopped running is the caller's job.
- The boundary test reads the source for action-shaped names and network imports. It is a guard against accidents, not a proof about code that is added later under other names.

The tests are deterministic failure-path checks with synthetic data, not a latency benchmark or evidence of delivery to a person.

## Related work

[Agent OS](https://github.com/dyjhhh/agent-os) · [Agent reliability](https://github.com/dyjhhh/agent-reliability) · [Evaluation gates](https://github.com/dyjhhh/agent-eval-gates) · [Security hooks](https://github.com/dyjhhh/agent-security-hooks) · [Feedback loops](https://github.com/dyjhhh/self-improving-loops) · [Careers watch](https://github.com/dyjhhh/career-watcher)

Personal project by Yujia Dong · AI-assisted development. Design choices and review are mine; Claude Code and Codex assisted with implementation, including the public extraction and tests.

MIT license. See [LICENSE](LICENSE).
