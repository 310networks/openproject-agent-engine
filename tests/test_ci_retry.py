"""Synthetic provider; these tests never connect to a forge or dispatch CI."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from op_agent_engine.ci_retry import Policy, Refused, eligible, reconcile, tick

HEAD, BASE, MERGE, BLOB = (c * 40 for c in "abcd")
P = Policy(
    "example/widget",
    "verify.yml",
    BLOB,
    "main",
    ["worker-a"],
    "render",
    "preflight",
    "##[error]device unavailable: {runner}",
    "plan",
    "checkout",
    "verdict",
    ["plan", "unit"],
)


class Fake:
    def __init__(self):
        now = datetime.now(UTC) - timedelta(seconds=2)
        self.stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.receipt = dict(
            version=1,
            repo=P.repo,
            workflow=P.workflow,
            runner="worker-a",
            since=(now - timedelta(minutes=5)).isoformat(),
            until=(now + timedelta(seconds=1)).isoformat(),
            ready_at=now.isoformat(),
        )
        self.receipt["ready_at"] = self.receipt["until"]

        def step(name, conclusion):
            return dict(
                name=name, conclusion=conclusion, started_at=self.stamp, completed_at=self.stamp
            )

        self.ref = dict(number=4, base=dict(sha=BASE), head=dict(sha=HEAD))
        self.run = dict(
            id=10,
            status="completed",
            conclusion="failure",
            run_attempt=1,
            event="pull_request",
            path=".github/workflows/verify.yml",
            head_sha=HEAD,
            pull_requests=[self.ref],
        )
        owner = dict(full_name=P.repo)
        self.pr = dict(
            state="open",
            head=dict(repo=owner, sha=HEAD),
            base=dict(repo=owner, sha=BASE, ref="main"),
            merge_commit_sha=MERGE,
        )
        self.jobs = [
            dict(
                id=11,
                name="render",
                status="completed",
                runner_name="worker-a",
                conclusion="failure",
                steps=[step("preflight", "failure")],
            ),
            dict(id=12, name="plan", conclusion="success", steps=[step("checkout", "success")]),
            dict(id=13, name="unit", conclusion="success"),
            dict(id=14, name="verdict", conclusion="failure"),
        ]
        self.same_head = [self.run]
        self.diag = "##[error]device unavailable: worker-a"
        self.posts = []
        self.timeout = False

    def get(self, path):
        if path.startswith("pulls/"):
            return self.pr
        if path.startswith("git/commits/"):
            return dict(parents=[dict(sha=BASE), dict(sha=HEAD)])
        if path.startswith("contents/"):
            return dict(sha=BLOB)
        if "attempts/1/jobs" in path:
            return dict(total_count=len(self.jobs), jobs=self.jobs)
        if "workflows/" in path:
            rows = self.same_head if "head_sha" in path else [self.run]
            return dict(total_count=len(rows), workflow_runs=rows)
        assert path == "actions/runs/10"
        return self.run

    def log(self, job):
        return self.stamp + " " + (self.diag if job == 11 else MERGE)

    def retry(self, job):
        self.posts.append(job)
        if self.timeout:
            raise TimeoutError


def call(fake, tmp_path, apply):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "incident.json").write_text(json.dumps(fake.receipt))
    with closing(sqlite3.connect(tmp_path / "store")) as db:
        tick(fake, P, inbox, db, apply)


def test_observation_does_not_consume_budget_and_restart_reuses_it(tmp_path):
    f = Fake()
    call(f, tmp_path, False)
    assert not f.posts
    call(f, tmp_path, True)
    call(f, tmp_path, True)
    assert f.posts == [11]


def test_lost_response_is_reconciled_without_another_post(tmp_path):
    f = Fake()
    f.timeout = True
    with pytest.raises(TimeoutError):
        call(f, tmp_path, True)
    f.timeout = False
    call(f, tmp_path, True)
    f.run["run_attempt"] = 2
    call(f, tmp_path, True)
    with closing(sqlite3.connect(tmp_path / "store")) as db:
        assert db.execute("SELECT state FROM retries").fetchall() == [("observed_attempt",)]
    assert f.posts == [11]


def test_two_collectors_submit_once(tmp_path):
    f = Fake()
    call(f, tmp_path, False)
    gate = Barrier(2)

    def concurrent():
        with closing(sqlite3.connect(tmp_path / "store")) as db:
            gate.wait()
            return reconcile(f, P, f.receipt, 10, db, apply=True)

    with ThreadPoolExecutor(2) as pool:
        outcome = [x.result() for x in [pool.submit(concurrent), pool.submit(concurrent)]]
    assert any("already claimed" in r for r in outcome)
    assert f.posts == [11]


@pytest.mark.parametrize(
    "reason",
    [
        "pending",
        "newer",
        "stale head",
        "prior success",
        "base changed",
        "attempt",
        "ambiguous PR",
        "test failure",
        "wrong diagnostic",
        "other runner",
        "old failure",
        "wrong tested merge",
        "fork",
    ],
)
def test_ineligible_comparisons(reason):
    f = Fake()
    if reason == "pending":
        f.run["status"] = "in_progress"
    elif reason == "newer":
        f.same_head.append(dict(id=20, status="queued"))
    elif reason == "prior success":
        f.same_head.append(dict(id=9, status="completed", conclusion="success"))
    elif reason == "stale head":
        f.pr["head"]["sha"] = "e" * 40
    elif reason == "base changed":
        f.pr["base"]["sha"] = "e" * 40
    elif reason == "attempt":
        f.run["run_attempt"] = 2
    elif reason == "ambiguous PR":
        f.run["pull_requests"] = []
    elif reason == "test failure":
        f.jobs[2]["conclusion"] = "failure"
    elif reason == "wrong diagnostic":
        f.diag = "##[error]application assertion"
    elif reason == "other runner":
        f.jobs[0]["runner_name"] = "other"
    elif reason == "old failure":
        f.receipt["since"] = datetime.now(UTC).isoformat()
    elif reason == "wrong tested merge":
        f.pr["merge_commit_sha"] = "e" * 40
    elif reason == "fork":
        f.pr["head"]["repo"] = {"full_name": "example/other"}
    with pytest.raises(Refused):
        eligible(f, P, f.receipt, 10)
    assert not f.posts


def test_revalidate_after_claim(tmp_path):
    f = Fake()
    call(f, tmp_path, False)
    original = f.get
    reads = 0

    def moving(path):
        nonlocal reads
        if path == "pulls/4":
            reads += 1
            if reads == 2:
                f.pr["head"]["sha"] = "e" * 40
        return original(path)

    f.get = moving
    call(f, tmp_path, True)
    assert not f.posts
