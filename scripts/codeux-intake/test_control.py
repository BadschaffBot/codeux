from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).with_name("control.py")
SPEC = importlib.util.spec_from_file_location("codeux_control", MODULE_PATH)
assert SPEC and SPEC.loader
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


class FakeRunner:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[list[str], str | None]] = []

    def run(
        self, argv: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append((args, input_text))
        return self.handler(args, input_text)


def completed(
    argv: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def make_databases(
    tmp_path: Path, *, schedule_error: str | None = None, admitted: int = 0
) -> tuple[Path, Path, Path]:
    clone = tmp_path / "clone"
    clone.mkdir()
    db_path = tmp_path / "app.db"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE projects(id TEXT PRIMARY KEY, slug TEXT, base_dir TEXT, repo_url TEXT);
        CREATE TABLE scheduler_entries(project_id TEXT, status TEXT, last_error TEXT);
        CREATE TABLE github_intake_items(id TEXT, repository TEXT, external_number INTEGER, dispatch_id TEXT,
          sprint_id TEXT, task_id TEXT, status TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE node_flow_runs(id TEXT PRIMARY KEY, project_id TEXT, status TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE node_flow_node_runs(id TEXT, run_id TEXT, project_id TEXT, status TEXT, finished_at TEXT,
          output_json TEXT, created_at TEXT, updated_at TEXT);
    """)
    db.execute(
        "INSERT INTO projects VALUES(?,?,?,?)",
        ("p1", "repo", str(clone), "https://github.com/SchaffTrade/repo.git"),
    )
    db.execute(
        "INSERT INTO scheduler_entries VALUES(?,?,?)",
        ("p1", "scheduled", schedule_error),
    )
    when = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO node_flow_runs VALUES('r1','p1','succeeded',?,?)", (when, when)
    )
    db.execute(
        "INSERT INTO node_flow_node_runs VALUES('n1','r1','p1','succeeded',?,?,?,?)",
        (when, json.dumps({"admitted": admitted}), when, when),
    )
    db.commit()
    db.close()
    settings = tmp_path / "settings.db"
    config = sqlite3.connect(settings)
    config.executescript("""
        CREATE TABLE system_settings(payload TEXT);
        CREATE TABLE project_settings(project_id TEXT, payload TEXT);
        CREATE TABLE sprint_settings(sprint_id TEXT, payload TEXT);
    """)
    config.execute(
        "INSERT INTO system_settings VALUES(?)",
        (
            json.dumps(
                {"defaults": {"ciIntelligence": {"featurePrAutoMergeMode": "OFF"}}}
            ),
        ),
    )
    config.commit()
    config.close()
    return db_path, settings, clone


def issue(
    number: int = 7,
    *,
    labels: list[str] | None = None,
    title: str = "Bounded fix",
    body: str = "Exact reviewed body",
) -> dict[str, Any]:
    return {
        "number": number,
        "state": "OPEN",
        "url": f"https://github.com/SchaffTrade/repo/issues/{number}",
        "title": title,
        "body": body,
        "assignees": [],
        "labels": [{"name": x} for x in labels or []],
        "closedByPullRequestsReferences": [],
    }


def manifest(tmp_path: Path, view: dict[str, Any]) -> Path:
    digest = control.current_digest(view)
    path = tmp_path / "reviewed.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "reviewed_by": "Sonnet-W1",
                "reviewed_at": "2026-10-24T12:00:00Z",
                "issues": [
                    {
                        "repository": "SchaffTrade/repo",
                        "number": view["number"],
                        "files": ["src/a.py"],
                        "test_command": "pytest -q test_a.py",
                        "content_sha256": digest,
                        "offline_safe_approved": True,
                    }
                ],
            }
        ),
        encoding="ascii",
    )
    return path


def handler_for(
    clone: Path,
    view: dict[str, Any],
    ready: list[dict[str, Any]] | None = None,
    *,
    service_active: bool = True,
    amux_ok: bool = True,
):
    ready_rows = list(ready or [])

    def handler(
        argv: list[str], input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git" and argv[-1] == "--show-toplevel":
            return completed(argv, str(clone) + "\n")
        if argv[0] == "git" and argv[-1] == "--is-shallow-repository":
            return completed(argv, "false\n")
        if argv[0] == "git" and argv[-3:] == ["remote", "get-url", "origin"]:
            return completed(argv, "https://github.com/SchaffTrade/repo.git\n")
        if argv[0] == "git" and "--get-regexp" in argv:
            return completed(argv, returncode=1)
        if argv[:3] == ["gh", "issue", "list"]:
            return completed(argv, json.dumps(ready_rows))
        if argv[:3] == ["gh", "issue", "view"]:
            return completed(argv, json.dumps(view))
        if argv[:3] == ["gh", "issue", "edit"]:
            number = int(argv[3])
            if (
                "--add-label" in argv
                and argv[-1] == "codeux:ready,codeux:offline-safe"
                and not any(int(row["number"]) == number for row in ready_rows)
            ):
                ready_rows.append({"number": number, "url": f"u{number}", "labels": []})
            if "--remove-label" in argv:
                ready_rows[:] = [
                    row for row in ready_rows if int(row["number"]) != number
                ]
            return completed(argv)
        if argv[:2] == ["gh", "api"]:
            return completed(argv, "[]")
        if argv[0] == "systemctl":
            return completed(
                argv,
                "active\n" if service_active else "inactive\n",
                0 if service_active else 3,
            )
        if argv[0] == "amux":
            return completed(
                argv,
                returncode=0 if amux_ok else 1,
                stderr="send failed" if not amux_ok else "",
            )
        raise AssertionError(argv)

    return handler


def test_startup_rejects_protected_scheduled_repo(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE projects SET repo_url='https://github.com/SchaffTrade/relaybot.git'"
    )
    connection.commit()
    connection.close()
    runner = FakeRunner(handler_for(clone, issue()))
    with pytest.raises(control.ControlError, match="protected"):
        control.startup(runner, db, settings)
    assert not runner.calls


def test_label_apply_reserves_then_rechecks_then_readies(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    view = issue()
    runner = FakeRunner(handler_for(clone, view))
    result = control.label(
        runner, db, settings, manifest(tmp_path, view), tmp_path / "lock", apply=True
    )
    edits = [call[0] for call in runner.calls if call[0][:3] == ["gh", "issue", "edit"]]
    views = [call for call in runner.calls if call[0][:3] == ["gh", "issue", "view"]]
    assert result["accepted"][0]["number"] == 7
    assert edits[0][-1] == "codeux:reserved"
    assert edits[1][-1] == "codeux:ready,codeux:offline-safe"
    assert len(views) == 3


def test_label_changed_content_after_reserve_never_adds_readiness(
    tmp_path: Path,
) -> None:
    db, settings, clone = make_databases(tmp_path)
    original, changed = issue(), issue(body="changed")
    seen = 0
    base = handler_for(clone, original)

    def handler(argv: list[str], text: str | None) -> subprocess.CompletedProcess[str]:
        nonlocal seen
        if argv[:3] == ["gh", "issue", "view"]:
            seen += 1
            return completed(argv, json.dumps(original if seen == 1 else changed))
        return base(argv, text)

    runner = FakeRunner(handler)
    with pytest.raises(control.ControlError, match="post-reservation"):
        control.label(
            runner,
            db,
            settings,
            manifest(tmp_path, original),
            tmp_path / "lock",
            apply=True,
        )
    edits = [c[0] for c in runner.calls if c[0][:3] == ["gh", "issue", "edit"]]
    assert len(edits) == 1 and edits[0][-1] == "codeux:reserved"


def test_cap_rejects_before_any_write(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    view = issue()
    existing = [{"number": n, "url": f"u{n}", "labels": []} for n in range(1, 7)]
    runner = FakeRunner(handler_for(clone, view, existing))
    with pytest.raises(control.ControlError, match="exceeds cap"):
        control.label(
            runner,
            db,
            settings,
            manifest(tmp_path, view),
            tmp_path / "lock",
            apply=True,
        )
    assert not any(c[0][:3] == ["gh", "issue", "edit"] for c in runner.calls)


def test_prior_dispatch_is_rejected(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO github_intake_items VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "i1",
            "SchaffTrade/repo",
            7,
            None,
            "s1",
            None,
            "failed",
            "2026-10-24T00:00:00Z",
            "2026-10-24T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    view = issue()
    runner = FakeRunner(handler_for(clone, view))
    result = control.label(
        runner, db, settings, manifest(tmp_path, view), tmp_path / "lock", apply=True
    )
    assert not result["accepted"]
    assert "prior dispatch" in result["rejected"][0]["reason"]
    assert not any(c[0][:3] == ["gh", "issue", "edit"] for c in runner.calls)


def test_health_starvation_wakes_once_and_reports_actual_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, settings, clone = make_databases(tmp_path, admitted=0)
    ready = [
        {
            "number": 7,
            "url": "https://github.com/SchaffTrade/repo/issues/7",
            "labels": [],
        }
    ]
    runner = FakeRunner(handler_for(clone, issue(), ready))
    state = tmp_path / "state.json"
    now = datetime(2026, 10, 24, tzinfo=UTC)
    state.write_text(
        json.dumps({"observation_started_at": (now - timedelta(hours=25)).isoformat()}),
        encoding="ascii",
    )
    monkeypatch.setenv("CODEX_SESSION_ID", "real-session")
    first = control.health(
        runner,
        db,
        settings,
        state,
        tmp_path / "lock",
        "svc",
        "Claude-Primary-Worker",
        apply=True,
        now=now,
    )
    second = control.health(
        runner,
        db,
        settings,
        state,
        tmp_path / "lock",
        "svc",
        "Claude-Primary-Worker",
        apply=True,
        now=now,
    )
    sends = [c for c in runner.calls if c[0][0] == "amux"]
    assert first["admitted_last_24h"] == 0 and first["wake_sent"]
    assert not second["wake_sent"] and len(sends) == 1
    assert sends[0][1] is not None
    assert "DISPATCHED BY: Codex session real-session" in sends[0][1]


def test_failed_wake_does_not_record_success(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path, schedule_error="boom")
    runner = FakeRunner(handler_for(clone, issue(), amux_ok=False))
    state = tmp_path / "state.json"
    with pytest.raises(control.ControlError, match="wake failed"):
        control.health(
            runner,
            db,
            settings,
            state,
            tmp_path / "lock",
            "svc",
            "Claude-Primary-Worker",
            apply=True,
        )
    saved = json.loads(state.read_text(encoding="ascii"))
    assert saved["pending_alert"]["status"] == "failed"
    assert "alert_fingerprint" not in saved


def test_unmanifested_ready_issue_fails_closed(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    view = issue()
    outside = [{"number": 8, "url": "u8", "labels": []}]
    runner = FakeRunner(handler_for(clone, view, outside))
    with pytest.raises(control.ControlError, match="outside reviewed manifest"):
        control.label(
            runner,
            db,
            settings,
            manifest(tmp_path, view),
            tmp_path / "lock",
            apply=True,
        )
    assert not any(c[0][:3] == ["gh", "issue", "edit"] for c in runner.calls)


def test_global_count_race_after_reservation_stops_before_readiness(
    tmp_path: Path,
) -> None:
    db, settings, clone = make_databases(tmp_path)
    view = issue()
    base = handler_for(clone, view)
    lists = 0

    def handler(argv: list[str], text: str | None) -> subprocess.CompletedProcess[str]:
        nonlocal lists
        if argv[:3] == ["gh", "issue", "list"]:
            lists += 1
            rows = [] if lists == 1 else [{"number": 8, "url": "u8", "labels": []}]
            return completed(argv, json.dumps(rows))
        return base(argv, text)

    runner = FakeRunner(handler)
    with pytest.raises(control.ControlError, match="appeared outside"):
        control.label(
            runner,
            db,
            settings,
            manifest(tmp_path, view),
            tmp_path / "lock",
            apply=True,
        )
    edits = [c[0] for c in runner.calls if c[0][:3] == ["gh", "issue", "edit"]]
    assert len(edits) == 1 and edits[0][-1] == "codeux:reserved"


def test_post_readiness_mismatch_removes_only_new_ready_labels(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    original, changed = issue(), issue(body="changed after readiness")
    base = handler_for(clone, original)
    views = 0

    def handler(argv: list[str], text: str | None) -> subprocess.CompletedProcess[str]:
        nonlocal views
        if argv[:3] == ["gh", "issue", "view"]:
            views += 1
            return completed(argv, json.dumps(changed if views >= 3 else original))
        return base(argv, text)

    runner = FakeRunner(handler)
    with pytest.raises(
        control.ControlError, match="newly added readiness labels removed"
    ):
        control.label(
            runner,
            db,
            settings,
            manifest(tmp_path, original),
            tmp_path / "lock",
            apply=True,
        )
    edits = [c[0] for c in runner.calls if c[0][:3] == ["gh", "issue", "edit"]]
    assert [edit[-2] for edit in edits] == [
        "--add-label",
        "--add-label",
        "--remove-label",
    ]
    assert edits[0][-1] == "codeux:reserved"
    assert edits[-1][-1] == "codeux:ready,codeux:offline-safe"


def test_stale_manifest_ready_issue_is_revoked_without_dispatch(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path)
    reviewed, changed = issue(), issue(body="scope changed")
    ready = [{"number": 7, "url": "u7", "labels": []}]
    runner = FakeRunner(handler_for(clone, changed, ready))
    result = control.label(
        runner,
        db,
        settings,
        manifest(tmp_path, reviewed),
        tmp_path / "lock",
        apply=True,
    )
    assert result["revoked_ready"][0]["number"] == 7
    edits = [c[0] for c in runner.calls if c[0][:3] == ["gh", "issue", "edit"]]
    assert len(edits) == 1 and edits[0][-2:] == [
        "--remove-label",
        "codeux:ready,codeux:offline-safe",
    ]


def test_latest_runtime_failure_ignores_superseded_failures(tmp_path: Path) -> None:
    db, _settings, _clone = make_databases(tmp_path)
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO node_flow_runs VALUES('r0','p1','failed',?,?)", (old, old)
    )
    connection.execute(
        "UPDATE node_flow_runs SET updated_at=? WHERE id='r1'", (recent,)
    )
    connection.execute(
        "INSERT INTO github_intake_items VALUES(?,?,?,?,?,?,?,?,?)",
        ("i0", "SchaffTrade/repo", 9, None, None, None, "failed", old, old),
    )
    connection.execute(
        "INSERT INTO github_intake_items VALUES(?,?,?,?,?,?,?,?,?)",
        ("i1", "SchaffTrade/repo", 9, None, None, None, "completed", recent, recent),
    )
    connection.commit()
    connection.close()
    assert control.latest_runtime_failures(db, now) == []
    connection = sqlite3.connect(db)
    connection.execute("UPDATE node_flow_runs SET status='failed' WHERE id='r1'")
    connection.commit()
    connection.close()
    assert control.latest_runtime_failures(db, now) == ["node_flow_run:repo"]


def test_ambiguous_wake_is_pending_and_throttled(tmp_path: Path) -> None:
    db, settings, clone = make_databases(tmp_path, schedule_error="boom")
    base = handler_for(clone, issue())

    def handler(argv: list[str], text: str | None) -> subprocess.CompletedProcess[str]:
        if argv[0] == "amux":
            raise control.ControlError("timeout")
        return base(argv, text)

    runner = FakeRunner(handler)
    state = tmp_path / "state.json"
    now = datetime(2026, 10, 24, tzinfo=UTC)
    with pytest.raises(control.ControlError, match="outcome unknown"):
        control.health(
            runner,
            db,
            settings,
            state,
            tmp_path / "lock",
            "svc",
            "Claude-Primary-Worker",
            apply=True,
            now=now,
        )
    saved = json.loads(state.read_text(encoding="ascii"))
    assert saved["pending_alert"]["status"] == "unknown"
    result = control.health(
        runner,
        db,
        settings,
        state,
        tmp_path / "lock",
        "svc",
        "Claude-Primary-Worker",
        apply=True,
        now=now + timedelta(minutes=1),
    )
    assert result["wake_status"] == "throttled-pending"
    assert len([c for c in runner.calls if c[0][0] == "amux"]) == 1


@pytest.mark.parametrize(
    "name",
    ["claimed:gpt", "claude:ready", "claude:running", "claude:needs-james", "needsyou"],
)
def test_lane_owned_issue_never_gets_readiness(tmp_path: Path, name: str) -> None:
    db, settings, clone = make_databases(tmp_path)
    view = issue(labels=[name])
    runner = FakeRunner(handler_for(clone, view))
    result = control.label(
        runner, db, settings, manifest(tmp_path, view), tmp_path / "lock", apply=True
    )
    assert not result["accepted"]
    assert "ownership/hold" in result["rejected"][0]["reason"]
    assert not any(c[0][:3] == ["gh", "issue", "edit"] for c in runner.calls)


@pytest.mark.parametrize("name", ["RelayBot", "SCHWAB-AUTH", "TastyTrade-Read"])
def test_startup_rejects_protected_repo_case_insensitively(
    tmp_path: Path, name: str
) -> None:
    db, settings, clone = make_databases(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE projects SET repo_url=?",
            (f"https://github.com/SchaffTrade/{name}.git",),
        )
    with pytest.raises(control.ControlError, match="protected"):
        control.startup(FakeRunner(handler_for(clone, issue())), db, settings)


@pytest.mark.parametrize(
    "config",
    [
        "extensions.partialclone origin",
        "remote.origin.promisor true",
        "remote.origin.partialclonefilter blob:none",
    ],
)
def test_startup_rejects_partial_clone(tmp_path: Path, config: str) -> None:
    db, settings, clone = make_databases(tmp_path)
    base = handler_for(clone, issue())

    def handler(argv: list[str], text: str | None) -> subprocess.CompletedProcess[str]:
        if argv[0] == "git" and "--get-regexp" in argv:
            return completed(argv, config + "\n")
        return base(argv, text)

    with pytest.raises(control.ControlError, match="partial/promisor"):
        control.startup(FakeRunner(handler), db, settings)
