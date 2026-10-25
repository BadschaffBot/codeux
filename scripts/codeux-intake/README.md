# CW-1069 Code UX intake control

This package is the operational boundary around the existing Code UX GitHub
intake flow. It does not dispatch a provider, merge a pull request, mutate the
Code UX databases, or alter schedules. The application and settings databases
are always opened in SQLite read-only mode.

Dispatch authority and provenance:

- Original dispatch origin: James chat request 2026-10-24.
- Health wakes include `DISPATCHED BY: Codex session <CODEX_SESSION_ID>` from
  the live environment (`CODEX_THREAD_ID` fallback).
- Set `CODEUX_DISPATCH_ORIGIN="James chat request 2026-10-24"` in the unit so
  that the durable original origin is preserved.
- Health sends only to `Claude-Primary-Worker` by default, matching pump lane C.

## Commands

All commands use the production runtime paths by default. Label and health are
dry-run by default; only `--apply` permits GitHub labels, AMUX wakes, or local
health-state writes.

```sh
python3 control.py startup
python3 control.py label --manifest /absolute/path/reviewed.json
python3 control.py label --manifest /absolute/path/reviewed.json --apply
python3 control.py health
python3 control.py health --apply
```

For systemd, use `startup` as `ExecStartPre`, run the existing service, then run
`health --apply` from its health timer. Run `label --manifest ... --apply` from a
separate timer or reviewed deployment step. Common overrides are `--db`,
`--settings-db`, and `--lock`; health also accepts `--state`, `--service`, and
`--wake-target`.

Bootstrap the three GitHub labels once in each reviewed manifest repository:
`codeux:reserved`, `codeux:ready`, and `codeux:offline-safe`. This tool does not
create labels. It owns no CW-1066 label or manifest, and direct board dispatch
must not select the same issues.

## Reviewed manifest

The manifest is human-reviewed authorization, not a classifier. Issue content
is untrusted and safety is never inferred from keywords. `content_sha256` is the
lowercase SHA-256 of the exact UTF-8 bytes `title + "\n" + body` observed during
review.

```json
{
  "version": 1,
  "reviewed_by": "Sonnet-W1",
  "reviewed_at": "2026-10-24T12:00:00Z",
  "issues": [
    {
      "repository": "SchaffTrade/example",
      "number": 123,
      "files": ["src/example.py", "tests/test_example.py"],
      "test_command": "pytest -q tests/test_example.py",
      "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "offline_safe_approved": true
    }
  ]
}
```

Each issue must name one to three relative files and one deterministic test
command. Its repository must exactly match an active scheduled project's
`repo_url`. The labeler reads every candidate's state, assignees, labels,
title/body, and complete timeline before any write. It rejects linked pull
requests (including non-closing timeline cross-references), prior local intake
dispatches, ownership/provider/hold labels, changed content, and capacity over
five open issues bearing both readiness labels across all scheduled repos.

Apply is two phase: add `codeux:reserved`, re-read content and timeline, then add
both readiness labels in one edit and recheck all predicates, the local ledger,
and the exact global count. A failure or changed issue after reservation leaves
the issue reserved but not newly ready. The reserved label is exempt from the
deny list, so a later run recovers a stale reservation deterministically. If a
post-readiness check fails, the run removes only readiness labels it just added;
it never removes the reservation label.

Every issue already bearing both readiness labels must be in the current
manifest. A still-eligible manifest issue is preserved. An ineligible or changed
manifest issue with no dispatch history has both readiness labels revoked and
is reported; a previously dispatched issue is reported but left untouched.

## Health semantics

Health reports the real sum of successful node output `admitted` counts over the
last 24 hours. It reports pull-request URLs found through complete GitHub issue
timelines and explicitly does not treat those pull requests as merge-worthy.
It wakes the coordinator when the service is inactive, a scheduled row failed,
the latest flow or node run for a scheduled project failed, a recent intake
record's latest state is failed, a startup assertion fails, GitHub health reads
fail, or ready issues have had no admission for 24 hours. Superseded historical
failures are not alerts. A new installation records its observation start and
does not raise starvation until a full 24 hours has elapsed.

Alert fingerprints are stable keys included in each wake. Before sending, the
tool atomically persists a pending attempt with its timestamp. Failed and
ambiguous sends remain `failed` or `unknown`, never successful, and are retried
no more often than every 15 minutes. Successful fingerprints are deduplicated.
State updates hold the same flock used by the labeler.

## Safety assertions

Startup requires every active scheduled clone to be its exact non-shallow, non-partial Git
root, with `origin` matching the declared `https://github.com/SchaffTrade/...`
repository. Protected repos `relaybot`, `schwab-auth`, and `tastytrade-read`
must not be actively scheduled. Existing paused rows are ignored and untouched.
All effective `featurePrAutoMergeMode` values must be `OFF` or `CREATE_PR`; a
non-string value or legacy `autoMergeFeaturePrWhenGreen=true` fails closed.

Run checks with:

```sh
pytest -q test_control.py
ruff format --check control.py test_control.py
ruff check control.py test_control.py
mypy control.py
```
