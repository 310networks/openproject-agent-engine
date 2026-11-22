"""Reconcile trusted local runner health receipts. No ticket, model or dispatch API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode


class Refused(ValueError):
    """Incomplete, stale, ambiguous or ineligible evidence. Never retry on this basis."""


def require(ok: object, message: str) -> None:
    if not ok:
        raise Refused(message)


def instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    require(dt.tzinfo is not None, "timestamp missing offset")
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class Policy:
    repo: str
    workflow: str
    workflow_blob: str
    base: str
    runners: list[str]
    job: str
    step: str
    diagnostic: str
    checkout_job: str
    checkout_step: str
    dependent_verdict: str
    required_success: list[str]

    def __post_init__(self) -> None:
        require(re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo), "invalid repo")
        require(re.fullmatch(r"[\w-]+\.ya?ml", self.workflow), "invalid workflow")
        require(re.fullmatch(r"[0-9a-f]{40}", self.workflow_blob), "unversioned workflow")
        require(
            self.required_success and self.checkout_job in self.required_success,
            "missing required provenance job",
        )


class Provider(Protocol):
    def get(self, path: str) -> Any: ...
    def log(self, job: int) -> str: ...
    def retry(self, job: int) -> None: ...


class GitHubActions:
    def __init__(self, repo: str, token: str) -> None:
        self.root = f"repos/{repo}/"
        # No inherited role/CLI credentials, interactive prompt or custom host.
        self.env = {
            "PATH": os.defpath + ":/usr/local/bin",
            "GH_TOKEN": token,
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "HOME": "/nonexistent",
        }

    def _api(self, path: str, method: str = "GET") -> str:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", method, self.root + path],
            capture_output=True,
            timeout=45,
            env=self.env,
            check=False,
        )
        require(result.returncode == 0, f"provider {method} failed; no fallback dispatch")
        return result.stdout.decode("utf-8", "replace")

    def get(self, path: str) -> Any:
        return json.loads(self._api(path))

    def log(self, job: int) -> str:
        return self._api(f"actions/jobs/{job}/logs")

    def retry(self, job: int) -> None:
        self._api(f"actions/jobs/{job}/rerun", "POST")


def select_page(payload: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    rows = payload[field]
    require(
        isinstance(rows, list) and payload["total_count"] == len(rows),
        "incomplete provider listing",
    )
    return list(rows)


def only(rows: list[dict[str, Any]]) -> dict[str, Any]:
    require(len(rows) == 1, "expected one exact match")
    return rows[0]


def step_lines(api: Provider, job: dict[str, Any], step: dict[str, Any]) -> list[str]:
    start, end = instant(step["started_at"]), instant(step["completed_at"]) + timedelta(seconds=1)
    lines = []
    for line in api.log(job["id"]).splitlines():
        stamp, _, value = line.partition(" ")
        if re.fullmatch(r"[0-9T:.\-]+Z", stamp) and start <= instant(stamp) < end:
            lines.append(value)
    return lines


@dataclass(frozen=True)
class Candidate:
    key: str
    run: int
    job: int


def eligible(api: Provider, p: Policy, receipt: dict[str, Any], rid: int) -> Candidate:
    run = api.get(f"actions/runs/{rid}")
    require(
        run["id"] == rid
        and run["run_attempt"] == 1
        and run["event"] == "pull_request"
        and run["status"] == "completed"
        and run["conclusion"] == "failure"
        and run["path"] == f".github/workflows/{p.workflow}",
        "not a failed first PR attempt",
    )
    ref = only(run["pull_requests"])
    number = int(ref["number"])
    pr = api.get(f"pulls/{number}")
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    require(
        pr["state"] == "open"
        and pr["base"]["ref"] == p.base
        and ref["base"]["sha"] == base
        and run["head_sha"] == ref["head"]["sha"] == head
        and pr["head"]["repo"]["full_name"] == pr["base"]["repo"]["full_name"] == p.repo,
        "stale or external PR",
    )
    merge = pr["merge_commit_sha"]
    require(re.fullmatch(r"[0-9a-f]{40}", merge or ""), "no current merge revision")
    jobs = select_page(api.get(f"actions/runs/{rid}/attempts/1/jobs?per_page=100"), "jobs")
    target = only([j for j in jobs if j["name"] == p.job])
    require(
        target["status"] == "completed"
        and target["conclusion"] == "failure"
        and target["runner_name"] == receipt["runner"],
        "unrelated job failure",
    )
    for name in p.required_success:
        require(
            only([j for j in jobs if j["name"] == name])["conclusion"] == "success",
            "required sibling did not pass",
        )
    for j in jobs:
        if j is not target and j["name"] != p.dependent_verdict:
            require(j["conclusion"] in {"success", "skipped"}, "other failed or unfinished job")
    verdict = only([j for j in jobs if j["name"] == p.dependent_verdict])
    require(verdict["conclusion"] in {"failure", "skipped"}, "unexpected downstream verdict")
    step = only([s for s in target["steps"] if s["conclusion"] == "failure"])
    require(
        step["name"] == p.step
        and instant(receipt["since"]) <= instant(step["started_at"])
        and instant(step["completed_at"]) <= instant(receipt["until"]),
        "wrong incident",
    )
    require(
        p.diagnostic.replace("{runner}", receipt["runner"]) in step_lines(api, target, step),
        "failure signature not proven",
    )
    origin = only([j for j in jobs if j["name"] == p.checkout_job])
    checkout = only(
        [
            s
            for s in origin["steps"]
            if s["name"] == p.checkout_step and s["conclusion"] == "success"
        ]
    )
    hashes = {v for v in step_lines(api, origin, checkout) if re.fullmatch("[a-f0-9]{40}", v)}
    require(hashes == {merge}, "tested merge no longer current")
    commit = api.get(f"git/commits/{merge}")
    require([v["sha"] for v in commit["parents"]] == [base, head], "tested parents differ")
    blob = api.get(f"contents/.github/workflows/{p.workflow}?ref={merge}")
    require(blob["sha"] == p.workflow_blob, "workflow not approved by policy")
    # Compare across event types, not merely PR-triggered jobs. Fail closed on truncation.
    matches = select_page(
        api.get(f"actions/workflows/{p.workflow}/runs?head_sha={head}&per_page=100"),
        "workflow_runs",
    )
    require(
        any(r["id"] == rid for r in matches)
        and all(
            r["id"] <= rid
            and (r["id"] == rid or (r["status"] == "completed" and r["conclusion"] != "success"))
            for r in matches
        ),
        "competing/newer run or prior same-head success",
    )
    key = json.dumps([p.repo, number, p.workflow, head, base, merge, p.job])
    return Candidate(key, rid, int(target["id"]))


def reconcile(
    api: Provider,
    p: Policy,
    receipt: dict[str, Any],
    rid: int,
    db: sqlite3.Connection,
    *,
    apply: bool,
) -> str:
    candidate = eligible(api, p, receipt, rid)
    if not apply:
        return f"observe: would retry job {candidate.job}"
    # Commit BEFORE the one network write. An ambiguous POST spends the budget.
    # Even a process crash or a second collector cannot issue a second POST.
    with db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO retries VALUES (?, ?, ?, 'claimed')",
            (candidate.key, candidate.run, candidate.job),
        )
    if not cursor.rowcount:
        return f"already claimed: {candidate.job}"
    require(eligible(api, p, receipt, rid) == candidate, "identity changed after claim")
    api.retry(candidate.job)
    with db:
        db.execute("UPDATE retries SET state='submitted' WHERE identity=?", (candidate.key,))
    return f"submitted: job {candidate.job} on existing run {rid}"


def read_inbox(inbox: Path) -> list[dict[str, Any]]:
    receipts = []
    for path in sorted(inbox.glob("*.json")):
        require(
            not path.is_symlink() and path.is_file() and path.stat().st_size <= 4096,
            "invalid receipt file",
        )
        receipts.append(json.loads(path.read_text()))
    return receipts


def tick(
    api: Provider,
    policy: Policy,
    inbox: Path | list[dict[str, Any]],
    db: sqlite3.Connection,
    apply: bool,
) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS retries (identity TEXT PRIMARY KEY, "
        "run INTEGER, job INTEGER, state TEXT)"
    )
    # Reconcile uncertain responses read-only, including after a process restart.
    for identity, rid in db.execute(
        "SELECT identity,run FROM retries WHERE state IN ('claimed','submitted')"
    ).fetchall():
        if (
            json.loads(identity)[0] == policy.repo
            and api.get(f"actions/runs/{rid}")["run_attempt"] > 1
        ):
            with db:
                db.execute(
                    "UPDATE retries SET state='observed_attempt' WHERE identity=?", (identity,)
                )
    now = datetime.now(UTC)
    for r in read_inbox(inbox) if isinstance(inbox, Path) else inbox:
        # The inbox or authenticated transport is controlled by the recovery service.
        if (
            r.get("version") != 1
            or r.get("repo") != policy.repo
            or r.get("workflow") != policy.workflow
            or r.get("runner") not in policy.runners
        ):
            continue
        since, until, ready = (instant(r[k]) for k in ("since", "until", "ready_at"))
        if not since <= until <= ready <= now or now - ready > timedelta(days=1):
            continue
        # Include runs created before the refusal window; job timestamps decide eligibility.
        window = (since - timedelta(days=1)).date().isoformat() + ".." + until.date().isoformat()
        query = urlencode(
            {"created": window, "event": "pull_request", "status": "failure", "per_page": 100}
        )
        runs = select_page(
            api.get(f"actions/workflows/{policy.workflow}/runs?{query}"), "workflow_runs"
        )
        for run in runs:
            try:
                print(reconcile(api, policy, r, int(run["id"]), db, apply=apply), flush=True)
            except Refused as exc:
                print(f"skip run {run['id']}: {exc}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--inbox", type=Path)
    source.add_argument("--receipts-stdin", action="store_true", help="authenticated JSON batch")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="default is observe only")
    args = parser.parse_args()
    try:
        policy = Policy(**json.loads(args.policy.read_text()))
        token = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "actions-token").read_text().strip()
        require(token, "missing credential")
        inbox: Path | list[dict[str, Any]]
        if args.receipts_stdin:
            raw = sys.stdin.buffer.read(262145)
            require(len(raw) <= 262144, "oversize receipt batch")
            batch = json.loads(raw)
            require(
                isinstance(batch, list) and all(isinstance(v, dict) for v in batch),
                "invalid receipt batch",
            )
            inbox = batch
        else:
            require(args.inbox.is_dir(), "missing receipt directory")
            inbox = args.inbox
        with closing(sqlite3.connect(args.db, timeout=15)) as db:
            tick(GitHubActions(policy.repo, token), policy, inbox, db, args.apply)
        return 0
    except (
        KeyError,
        ValueError,
        TypeError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(
            f"reconciliation refused ({type(exc).__name__}); check policy/provider", file=sys.stderr
        )
        return 1
