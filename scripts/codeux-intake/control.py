#!/usr/bin/env python3
"""Fail-closed operations control for bounded Code UX GitHub intake."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
RUNTIME = Path(
    "/home/badschaff/development-offload-20260925/worktrees/codeux-runtime-mirror-20260926"
)
DEFAULT_DB = RUNTIME / "runtime-home/.code-ux/app.db"
DEFAULT_SETTINGS = RUNTIME / "runtime-home/.code-ux/settings.db"
DEFAULT_STATE = HERE / "state/health.json"
DEFAULT_LOCK = HERE / "state/control.lock"
DEFAULT_SERVICE = "codeux-shadow-mirror-20260926.service"
DEFAULT_WAKE_TARGET = "Claude-Primary-Worker"
READY = ("codeux:ready", "codeux:offline-safe")
RESERVED = "codeux:reserved"
PROTECTED = frozenset({"relaybot", "schwab-auth", "tastytrade-read"})
SAFE_MERGE_MODES = frozenset({"OFF", "CREATE_PR"})
DENIED_LABEL_PARTS = (
    "assigned",
    "blocked",
    "claimed",
    "claude:",
    "direct-dispatch",
    "hold",
    "jules",
    "lane",
    "needs:you",
    "needsyou",
    "needs-james",
    "provider",
    "wip",
)
MAX_READY = 5
TIMEOUT = 20
ALERT_RETRY = timedelta(minutes=15)


class ControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssueSpec:
    repository: str
    number: int
    files: tuple[str, ...]
    test_command: str
    content_sha256: str


class Runner:
    def run(
        self, argv: Sequence[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(argv),
                input=input_text,
                text=True,
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ControlError(f"command failed: {argv[0]}: {exc}") from exc


def checked(
    runner: Runner, argv: Sequence[str], *, input_text: str | None = None
) -> str:
    result = runner.run(argv, input_text=input_text)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise ControlError(f"command failed ({argv[0]}): {detail}")
    return result.stdout


def json_command(runner: Runner, argv: Sequence[str]) -> Any:
    try:
        return json.loads(checked(runner, argv))
    except json.JSONDecodeError as exc:
        raise ControlError(f"invalid JSON from {argv[0]}") from exc


def ro_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ControlError(f"read-only database missing: {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def repo_from_url(value: str | None) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ControlError("scheduled project repository must use https://github.com")
    path = parsed.path.strip("/").removesuffix(".git")
    if len(path.split("/")) != 2 or path.split("/", 1)[0] != "SchaffTrade":
        raise ControlError("scheduled project repository must be owned by SchaffTrade")
    return path


def scheduled_projects(db_path: Path) -> list[dict[str, str]]:
    with ro_db(db_path) as db:
        rows = db.execute("""SELECT DISTINCT p.id,p.slug,p.base_dir,p.repo_url FROM scheduler_entries s
            JOIN projects p ON p.id=s.project_id WHERE s.status='scheduled' ORDER BY p.slug""").fetchall()
    return [
        {
            "id": str(r["id"]),
            "slug": str(r["slug"]),
            "base_dir": str(r["base_dir"]),
            "repository": repo_from_url(r["repo_url"]),
        }
        for r in rows
    ]


def merge_modes(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"featurePrAutoMergeMode", "autoMergeFeaturePrWhenGreen"}:
                yield key, child
            yield from merge_modes(child)
    elif isinstance(value, list):
        for child in value:
            yield from merge_modes(child)


def assert_safe_settings(path: Path, project_ids: set[str]) -> None:
    found = 0
    with ro_db(path) as db:
        sources: list[tuple[str, str]] = [
            ("system", r["payload"])
            for r in db.execute("SELECT payload FROM system_settings")
        ]
        if project_ids:
            marks = ",".join("?" for _ in project_ids)
            params = tuple(sorted(project_ids))
            sources += [
                ("project", r["payload"])
                for r in db.execute(
                    f"SELECT payload FROM project_settings WHERE project_id IN ({marks})",
                    params,
                )
            ]
            # settings.db deliberately has no sprint-to-project mapping. Checking every
            # sprint override is the conservative boundary: an unsafe override cannot
            # become effective later merely because its sprint is resumed.
            sources += [
                ("sprint", r["payload"])
                for r in db.execute("SELECT payload FROM sprint_settings")
            ]
        for scope, raw in sources:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ControlError(f"invalid {scope} settings JSON") from exc
            for key, mode in merge_modes(payload):
                found += 1
                safe = (
                    mode in SAFE_MERGE_MODES
                    if key == "featurePrAutoMergeMode" and isinstance(mode, str)
                    else mode is False
                )
                if not safe:
                    raise ControlError(
                        f"unsafe featurePrAutoMergeMode in {scope} settings"
                    )
    if not found:
        raise ControlError("featurePrAutoMergeMode safety setting is absent")


def startup(runner: Runner, db_path: Path, settings_path: Path) -> list[dict[str, str]]:
    projects = scheduled_projects(db_path)
    if not projects:
        raise ControlError("no active scheduled projects")
    for project in projects:
        name = project["repository"].rsplit("/", 1)[-1].casefold()
        if name in PROTECTED:
            raise ControlError(f"protected repository actively scheduled: {name}")
        base = Path(project["base_dir"])
        root = Path(
            checked(
                runner, ["git", "-C", str(base), "rev-parse", "--show-toplevel"]
            ).strip()
        ).resolve()
        if root != base.resolve():
            raise ControlError(
                f"scheduled clone is not an exact git root: {project['slug']}"
            )
        remote = checked(
            runner, ["git", "-C", str(base), "remote", "get-url", "origin"]
        ).strip()
        if repo_from_url(remote).casefold() != project["repository"].casefold():
            raise ControlError(
                f"scheduled clone origin does not match repo_url: {project['slug']}"
            )
        shallow = checked(
            runner, ["git", "-C", str(base), "rev-parse", "--is-shallow-repository"]
        ).strip()
        if shallow != "false":
            raise ControlError(f"scheduled clone is shallow: {project['slug']}")
        partial = runner.run(
            [
                "git",
                "-C",
                str(base),
                "config",
                "--get-regexp",
                r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$",
            ]
        )
        if partial.returncode not in (0, 1):
            raise ControlError(
                f"cannot inspect partial clone configuration: {project['slug']}"
            )
        if partial.returncode == 0:
            raise ControlError(
                f"scheduled clone has partial/promisor configuration: {project['slug']}"
            )
    assert_safe_settings(settings_path, {p["id"] for p in projects})
    return projects


def load_manifest(path: Path) -> list[IssueSpec]:
    try:
        doc = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlError(f"cannot read reviewed manifest: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != {
        "version",
        "reviewed_by",
        "reviewed_at",
        "issues",
    }:
        raise ControlError("manifest must use the exact version/reviewer/issues schema")
    if (
        doc["version"] != 1
        or not isinstance(doc["reviewed_by"], str)
        or not doc["reviewed_by"].strip()
    ):
        raise ControlError("manifest requires version 1 and reviewer")
    try:
        datetime.fromisoformat(str(doc["reviewed_at"]))
    except ValueError as exc:
        raise ControlError("manifest reviewed_at must be ISO-8601") from exc
    if not isinstance(doc["issues"], list):
        raise ControlError("manifest issues must be a list")
    answer: list[IssueSpec] = []
    seen: set[tuple[str, int]] = set()
    keys = {
        "repository",
        "number",
        "files",
        "test_command",
        "content_sha256",
        "offline_safe_approved",
    }
    for item in doc["issues"]:
        if not isinstance(item, dict) or set(item) != keys:
            raise ControlError("manifest issue does not match exact schema")
        repo, number, files = item["repository"], item["number"], item["files"]
        digest, command = item["content_sha256"], item["test_command"]
        if (
            not isinstance(repo, str)
            or len(repo.split("/")) != 2
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or item["offline_safe_approved"] is not True
            or not isinstance(files, list)
            or not 1 <= len(files) <= 3
            or not isinstance(command, str)
            or not command.strip()
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ControlError(
                "manifest issue is not explicitly offline-safe and bounded"
            )
        normalized: list[str] = []
        for value in files:
            candidate = Path(value) if isinstance(value, str) else Path("/")
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or str(candidate) in {"", "."}
            ):
                raise ControlError("manifest files must be relative paths")
            normalized.append(candidate.as_posix())
        key = (repo.casefold(), number)
        if key in seen or len(set(normalized)) != len(normalized):
            raise ControlError("manifest has duplicate issue or file")
        seen.add(key)
        answer.append(
            IssueSpec(repo, number, tuple(normalized), command.strip(), digest)
        )
    return sorted(answer, key=lambda x: (x.repository.casefold(), x.number))


def issue_view(runner: Runner, spec: IssueSpec) -> dict[str, Any]:
    data = json_command(
        runner,
        [
            "gh",
            "issue",
            "view",
            str(spec.number),
            "--repo",
            spec.repository,
            "--json",
            "number,state,url,title,body,assignees,labels,closedByPullRequestsReferences",
        ],
    )
    if not isinstance(data, dict):
        raise ControlError("GitHub issue view returned non-object")
    return data


def current_digest(view: dict[str, Any]) -> str:
    title, body = view.get("title"), view.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        raise ControlError("GitHub issue title/body missing")
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()


def ready_issues(runner: Runner, repos: Iterable[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for repo in sorted(repos, key=str.casefold):
        rows = json_command(
            runner,
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--label",
                READY[0],
                "--label",
                READY[1],
                "--limit",
                str(MAX_READY + 1),
                "--json",
                "number,url,labels",
            ],
        )
        if not isinstance(rows, list):
            raise ControlError("GitHub issue list returned non-list")
        if len(rows) > MAX_READY:
            raise ControlError(
                "ready issue query exceeds cap; refusing potentially truncated result"
            )
        found.extend({"repository": repo, **row} for row in rows)
        if len(found) > MAX_READY:
            raise ControlError("existing ready issue count exceeds global cap")
    return found


def previously_dispatched(db_path: Path, spec: IssueSpec) -> bool:
    with ro_db(db_path) as db:
        row = db.execute(
            """SELECT 1 FROM github_intake_items WHERE lower(repository)=lower(?) AND external_number=?
            AND (dispatch_id IS NOT NULL OR sprint_id IS NOT NULL OR task_id IS NOT NULL
            OR status IN ('claimed','dispatched','completed','failed','held')) LIMIT 1""",
            (spec.repository, spec.number),
        ).fetchone()
    return row is not None


def issue_has_linked_pr(runner: Runner, spec: IssueSpec) -> bool:
    events = json_command(
        runner,
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{spec.repository}/issues/{spec.number}/timeline",
            "--paginate",
        ],
    )
    if not isinstance(events, list):
        raise ControlError("GitHub issue timeline returned non-list")
    for event in events:
        source = event.get("source", {}) if isinstance(event, dict) else {}
        linked = source.get("issue", {}) if isinstance(source, dict) else {}
        if isinstance(linked, dict) and linked.get("pull_request"):
            return True
    return False


def admissible(
    spec: IssueSpec, view: dict[str, Any], linked_pr: bool = False
) -> tuple[bool, str]:
    if view.get("state") != "OPEN" or view.get("number") != spec.number:
        return False, "issue is not open or identity mismatched"
    if current_digest(view) != spec.content_sha256:
        return False, "issue title/body changed since review"
    if view.get("assignees"):
        return False, "issue is assigned"
    if view.get("closedByPullRequestsReferences") or linked_pr:
        return False, "issue has linked pull request"
    names = [
        str(x.get("name", "")).casefold()
        for x in view.get("labels", [])
        if isinstance(x, dict)
    ]
    if any(part in name for name in names for part in DENIED_LABEL_PARTS):
        return False, "issue has denied ownership/hold/provider label"
    return True, "accepted"


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="ascii") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def label(
    runner: Runner, db: Path, settings: Path, manifest: Path, lock: Path, *, apply: bool
) -> dict[str, Any]:
    with locked(lock):
        projects = startup(runner, db, settings)
        scheduled = {p["repository"].casefold(): p["repository"] for p in projects}
        specs = load_manifest(manifest)
        manifest_keys = {(s.repository.casefold(), s.number) for s in specs}
        if any(s.repository.casefold() not in scheduled for s in specs):
            raise ControlError(
                "manifest repository does not map to an active scheduled project repo_url"
            )
        existing = ready_issues(runner, scheduled.values())
        existing_keys = {
            (x["repository"].casefold(), int(x["number"])) for x in existing
        }
        outside = existing_keys - manifest_keys
        if outside:
            rendered = ", ".join(f"{repo}#{number}" for repo, number in sorted(outside))
            raise ControlError(
                f"ready issue exists outside reviewed manifest: {rendered}"
            )
        views = {
            (s.repository.casefold(), s.number): issue_view(runner, s) for s in specs
        }
        linked = {
            (s.repository.casefold(), s.number): issue_has_linked_pr(runner, s)
            for s in specs
        }
        accepted: list[IssueSpec] = []
        preserved: list[IssueSpec] = []
        revoke: list[tuple[IssueSpec, str]] = []
        rejected: list[dict[str, Any]] = []
        for spec in specs:
            key = (spec.repository.casefold(), spec.number)
            reason = None
            if key in existing_keys:
                if previously_dispatched(db, spec):
                    reason = "already labelled and dispatched; readiness preserved"
                else:
                    ok, eligibility_reason = admissible(spec, views[key], linked[key])
                    if ok:
                        preserved.append(spec)
                        reason = "already labelled and still eligible"
                    else:
                        revoke.append((spec, eligibility_reason))
                        continue
            elif previously_dispatched(db, spec):
                reason = "local intake ledger records prior dispatch"
            else:
                ok, reason = admissible(spec, views[key], linked[key])
                effective_existing = len(existing) - len(revoke)
                if ok and effective_existing + len(accepted) < MAX_READY:
                    accepted.append(spec)
                    continue
                if ok:
                    reason = "global ready issue cap reached"
            rejected.append(
                {"repository": spec.repository, "number": spec.number, "reason": reason}
            )
        if apply:
            for spec, _reason in revoke:
                checked(
                    runner,
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(spec.number),
                        "--repo",
                        spec.repository,
                        "--remove-label",
                        ",".join(READY),
                    ],
                )
            for spec in accepted:
                checked(
                    runner,
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(spec.number),
                        "--repo",
                        spec.repository,
                        "--add-label",
                        RESERVED,
                    ],
                )
            # Reservation is phase one. Re-read every predicate and the exact
            # global count before readiness. RESERVED itself is intentionally
            # not a denied label so a stopped run resumes deterministically.
            fresh = {
                (s.repository.casefold(), s.number): issue_view(runner, s)
                for s in accepted
            }
            fresh_linked = {
                (s.repository.casefold(), s.number): issue_has_linked_pr(runner, s)
                for s in accepted
            }
            recounted = ready_issues(runner, scheduled.values())
            recounted_keys = {
                (x["repository"].casefold(), int(x["number"])) for x in recounted
            }
            outside = recounted_keys - manifest_keys
            if outside:
                raise ControlError(
                    "ready issue appeared outside reviewed manifest after reservation"
                )
            to_ready: list[IssueSpec] = []
            for spec in accepted:
                key = (spec.repository.casefold(), spec.number)
                if previously_dispatched(db, spec):
                    raise ControlError(
                        f"post-reservation ledger recheck failed for {spec.repository}#{spec.number}"
                    )
                ok, reason = admissible(spec, fresh[key], fresh_linked[key])
                if not ok:
                    raise ControlError(
                        f"post-reservation recheck failed for {spec.repository}#{spec.number}: {reason}"
                    )
                if key not in recounted_keys:
                    to_ready.append(spec)
            if len(recounted) + len(to_ready) > MAX_READY:
                raise ControlError("global ready issue cap changed after reservation")
            newly_ready: list[IssueSpec] = []
            for spec in to_ready:
                checked(
                    runner,
                    [
                        "gh",
                        "issue",
                        "edit",
                        str(spec.number),
                        "--repo",
                        spec.repository,
                        "--add-label",
                        ",".join(READY),
                    ],
                )
                newly_ready.append(spec)
            try:
                post_views = {
                    (s.repository.casefold(), s.number): issue_view(runner, s)
                    for s in newly_ready
                }
                post_linked = {
                    (s.repository.casefold(), s.number): issue_has_linked_pr(runner, s)
                    for s in newly_ready
                }
                post_ready = ready_issues(runner, scheduled.values())
                post_keys = {
                    (x["repository"].casefold(), int(x["number"])) for x in post_ready
                }
                if post_keys - manifest_keys or len(post_ready) > MAX_READY:
                    raise ControlError(
                        "post-readiness global count or manifest scope mismatch"
                    )
                for spec in newly_ready:
                    key = (spec.repository.casefold(), spec.number)
                    ok, reason = admissible(spec, post_views[key], post_linked[key])
                    if previously_dispatched(db, spec):
                        raise ControlError(
                            f"post-readiness ledger recheck failed for {spec.repository}#{spec.number}"
                        )
                    if not ok or key not in post_keys:
                        raise ControlError(
                            f"post-readiness predicate failed for {spec.repository}#{spec.number}: {reason}"
                        )
            except ControlError as exc:
                cleanup_errors: list[str] = []
                for spec in newly_ready:
                    try:
                        checked(
                            runner,
                            [
                                "gh",
                                "issue",
                                "edit",
                                str(spec.number),
                                "--repo",
                                spec.repository,
                                "--remove-label",
                                ",".join(READY),
                            ],
                        )
                    except ControlError as cleanup_exc:
                        cleanup_errors.append(str(cleanup_exc))
                suffix = (
                    f"; readiness cleanup failed: {'; '.join(cleanup_errors)}"
                    if cleanup_errors
                    else "; newly added readiness labels removed"
                )
                raise ControlError(str(exc) + suffix) from exc
        return {
            "mode": "apply" if apply else "dry-run",
            "existing_ready": len(existing),
            "accepted": [
                {
                    "repository": s.repository,
                    "number": s.number,
                    "files": list(s.files),
                    "test_command": s.test_command,
                }
                for s in accepted
            ],
            "preserved_ready": [
                {"repository": s.repository, "number": s.number} for s in preserved
            ],
            "revoked_ready": [
                {"repository": s.repository, "number": s.number, "reason": reason}
                for s, reason in revoke
            ],
            "rejected": rejected,
        }


def admitted_24h(db_path: Path, now: datetime) -> int:
    cutoff = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    total = 0
    with ro_db(db_path) as db:
        rows = db.execute(
            """SELECT n.output_json FROM node_flow_node_runs n JOIN node_flow_runs r ON r.id=n.run_id
            JOIN scheduler_entries s ON s.project_id=r.project_id AND s.status='scheduled'
            WHERE n.status='succeeded' AND n.finished_at>=?""",
            (cutoff,),
        ).fetchall()
    for row in rows:
        try:
            count = json.loads(row["output_json"] or "{}").get("admitted", 0)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                total += count
        except (json.JSONDecodeError, AttributeError):
            continue
    return total


def failed_schedules(path: Path) -> list[str]:
    with ro_db(path) as db:
        rows = db.execute("""SELECT p.slug FROM scheduler_entries s JOIN projects p ON p.id=s.project_id
            WHERE s.status='failed' OR (s.status='scheduled' AND trim(COALESCE(s.last_error,''))<>'') ORDER BY p.slug""").fetchall()
    return [str(r["slug"]) for r in rows]


def latest_runtime_failures(path: Path, now: datetime) -> list[str]:
    cutoff = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    failures: list[str] = []
    with ro_db(path) as db:
        run_rows = db.execute(
            """WITH ranked AS (
              SELECT p.slug, r.status, ROW_NUMBER() OVER (
                PARTITION BY r.project_id ORDER BY r.updated_at DESC, r.created_at DESC, r.id DESC
              ) AS position
              FROM node_flow_runs r JOIN projects p ON p.id=r.project_id
              JOIN scheduler_entries s ON s.project_id=p.id AND s.status='scheduled'
            ) SELECT slug,status FROM ranked WHERE position=1 AND status='failed' ORDER BY slug"""
        ).fetchall()
        node_rows = db.execute(
            """WITH ranked AS (
              SELECT p.slug, n.status, ROW_NUMBER() OVER (
                PARTITION BY n.project_id ORDER BY n.updated_at DESC, n.created_at DESC, n.id DESC
              ) AS position
              FROM node_flow_node_runs n JOIN projects p ON p.id=n.project_id
              JOIN scheduler_entries s ON s.project_id=p.id AND s.status='scheduled'
            ) SELECT slug,status FROM ranked WHERE position=1 AND status='failed' ORDER BY slug"""
        ).fetchall()
        intake_rows = db.execute(
            """WITH ranked AS (
              SELECT repository, external_number, status, updated_at, ROW_NUMBER() OVER (
                PARTITION BY lower(repository),external_number
                ORDER BY updated_at DESC, created_at DESC, id DESC
              ) AS position
              FROM github_intake_items
            ) SELECT repository,external_number FROM ranked
              WHERE position=1 AND status='failed' AND updated_at>=?
              ORDER BY lower(repository),external_number""",
            (cutoff,),
        ).fetchall()
    failures += [f"node_flow_run:{row['slug']}" for row in run_rows]
    failures += [f"node_flow_node_run:{row['slug']}" for row in node_rows]
    failures += [
        f"intake:{row['repository']}#{row['external_number']}" for row in intake_rows
    ]
    return failures


def linked_prs(runner: Runner, issues: list[dict[str, Any]]) -> list[str]:
    urls: set[str] = set()
    for issue in issues:
        events = json_command(
            runner,
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{issue['repository']}/issues/{issue['number']}/timeline",
                "--paginate",
            ],
        )
        if not isinstance(events, list):
            raise ControlError("GitHub timeline returned non-list")
        for event in events:
            source = event.get("source", {}) if isinstance(event, dict) else {}
            linked = source.get("issue", {}) if isinstance(source, dict) else {}
            if (
                isinstance(linked, dict)
                and linked.get("pull_request")
                and linked.get("html_url")
            ):
                urls.add(str(linked["html_url"]))
    return sorted(urls)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlError(f"invalid health state: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError("invalid health state")
    return value


def write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def health(
    runner: Runner,
    db: Path,
    settings: Path,
    state_path: Path,
    lock: Path,
    service: str,
    wake_target: str,
    *,
    apply: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    with locked(lock):
        # Service health is independent of startup and must still alert when an
        # ExecStartPre assertion prevents the runtime from starting.
        service_result = runner.run(["systemctl", "--user", "is-active", service])
        active = (
            service_result.returncode == 0 and service_result.stdout.strip() == "active"
        )
        preflight_error = ""
        try:
            projects = startup(runner, db, settings)
        except ControlError as exc:
            projects = []
            preflight_error = str(exc)
        gh_error = ""
        try:
            labelled = (
                ready_issues(runner, [p["repository"] for p in projects])
                if projects
                else []
            )
            prs = linked_prs(runner, labelled)
        except ControlError as exc:
            labelled, prs = [], []
            gh_error = str(exc)
        admitted, failures = admitted_24h(db, now), failed_schedules(db)
        runtime_failures = latest_runtime_failures(db, now)
        state = read_state(state_path)
        observation = (
            parse_time(state["observation_started_at"])
            if isinstance(state.get("observation_started_at"), str)
            else now
        )
        reasons = ["failed scheduled rows: " + ", ".join(failures)] if failures else []
        if runtime_failures:
            reasons.append("latest runtime failures: " + ", ".join(runtime_failures))
        if not active:
            reasons.append(f"service inactive: {service}")
        if preflight_error:
            reasons.append("startup assertion failed: " + preflight_error)
        if gh_error:
            reasons.append("GitHub health read failed: " + gh_error)
        if labelled and admitted == 0 and now - observation >= timedelta(hours=24):
            reasons.append("no admissions in 24h while labelled open issues exist")
        fingerprint = (
            hashlib.sha256("\n".join(reasons).encode("ascii")).hexdigest()
            if reasons
            else ""
        )
        sent = False
        wake_status = "not-needed" if not reasons else "dry-run"
        if apply:
            state.setdefault("observation_started_at", now.isoformat())
            if reasons and state.get("alert_fingerprint") != fingerprint:
                pending = state.get("pending_alert")
                recent_pending = False
                if isinstance(pending, dict) and pending.get("key") == fingerprint:
                    attempted = pending.get("last_attempt_at")
                    if isinstance(attempted, str):
                        recent_pending = now - parse_time(attempted) < ALERT_RETRY
                if recent_pending:
                    wake_status = "throttled-pending"
                else:
                    state["pending_alert"] = {
                        "key": fingerprint,
                        "last_attempt_at": now.isoformat(),
                        "status": "pending",
                    }
                    write_state(state_path, state)
                    session = (
                        os.getenv("CODEX_SESSION_ID")
                        or os.getenv("CODEX_THREAD_ID")
                        or "ID unavailable"
                    )
                    origin = (
                        os.getenv("CODEUX_DISPATCH_ORIGIN")
                        or "James chat request 2026-10-24"
                    )
                    message = (
                        "PUMP WAKE C Code UX intake unhealthy: "
                        + "; ".join(reasons)
                        + "\n"
                        + f"ALERT KEY: {fingerprint}\n"
                        + f"DISPATCHED BY: Codex session {session}\n"
                        + f"DISPATCH ORIGIN: {origin}\n"
                    )
                    try:
                        result = runner.run(
                            ["amux", "send", wake_target, "--no-board", "--stdin"],
                            input_text=message,
                        )
                    except ControlError as exc:
                        state["pending_alert"]["status"] = "unknown"
                        write_state(state_path, state)
                        raise ControlError(
                            "AMUX wake outcome unknown; pending alert retained"
                        ) from exc
                    if result.returncode:
                        state["pending_alert"]["status"] = "failed"
                        write_state(state_path, state)
                        raise ControlError("AMUX wake failed; pending alert retained")
                    state.pop("pending_alert", None)
                    state.update(
                        alert_fingerprint=fingerprint, alerted_at=now.isoformat()
                    )
                    sent = True
                    wake_status = "sent"
            elif reasons:
                wake_status = "deduplicated-success"
            elif not reasons:
                state.pop("alert_fingerprint", None)
                state.pop("alerted_at", None)
                state.pop("pending_alert", None)
            write_state(state_path, state)
        return {
            "healthy": not reasons,
            "mode": "apply" if apply else "dry-run",
            "service_active": active,
            "failed_schedules": failures,
            "latest_runtime_failures": runtime_failures,
            "labelled_open_count": len(labelled),
            "admitted_last_24h": admitted,
            "linked_pull_request_urls": prs,
            "linked_pull_requests_are_not_automatically_merge_worthy": True,
            "observation_hours": round((now - observation).total_seconds() / 3600, 2),
            "reasons": reasons,
            "wake_sent": sent,
            "wake_status": wake_status,
            "alert_key": fingerprint or None,
        }


def make_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB)
    common.add_argument("--settings-db", type=Path, default=DEFAULT_SETTINGS)
    common.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("startup", parents=[common])
    labeling = sub.add_parser("label", parents=[common])
    labeling.add_argument("--manifest", type=Path, required=True)
    labeling.add_argument("--apply", action="store_true")
    checking = sub.add_parser("health", parents=[common])
    checking.add_argument("--state", type=Path, default=DEFAULT_STATE)
    checking.add_argument("--service", default=DEFAULT_SERVICE)
    checking.add_argument("--wake-target", default=DEFAULT_WAKE_TARGET)
    checking.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args, runner = make_parser().parse_args(argv), Runner()
    try:
        if args.command == "startup":
            projects = startup(runner, args.db, args.settings_db)
            output: dict[str, Any] = {
                "ok": True,
                "scheduled_project_count": len(projects),
                "repositories": [p["repository"] for p in projects],
                "auto_merge_modes_allowed": sorted(SAFE_MERGE_MODES),
            }
        elif args.command == "label":
            output = label(
                runner,
                args.db,
                args.settings_db,
                args.manifest,
                args.lock,
                apply=args.apply,
            )
        else:
            output = health(
                runner,
                args.db,
                args.settings_db,
                args.state,
                args.lock,
                args.service,
                args.wake_target,
                apply=args.apply,
            )
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output.get("healthy", True) else 2
    except ControlError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, sort_keys=True),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
