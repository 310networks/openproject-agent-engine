"""Compact author-owned checkpoint state; never an approval or instruction override."""

from __future__ import annotations

import json
import re

CONTINUATION_POLICY = """## Shared development-delivery policy

- A truthful report status is `done`, `blocked`, or `checkpoint`. When only CI
  remains, finish independent work, post ONE short current-state report and end
  with `RESULT: WAITING_CI <url>`. Do not poll inside the model or add an evidence
  commit just to restate pending checks. Passive polls do not require comments.
- Reuse immutable evidence ONLY when its source and all relevant dependencies
  are unchanged; name those dependencies and links. Never claim unrun tests or
  treat a skipped/focused check as complete qualification. Review/merge still
  require current required qualification, independent review and Owner gates.
- Group small logical commits into coherent, decision-sized pushes: each should
  provide useful review or CI feedback, not merely restate evidence. Finish safe
  independent work before declaring a CI wait, without superseding a revision
  whose result you still need. If clean work must remain local, report both the
  retained local SHA and the published PR head; only the provider's PR head is
  the CI/evidence identity. A run URL is a reference, not proof of that identity.
  Reviewers remain read-only and never push the implementer's branch.
  Use a reduced CI scope only
  when a verified repository policy authorizes it; follow that repository's
  documented triggers and qualification requirements. Do not assume particular
  labels, workflows, platforms or test families exist. Shared/high-risk and
  unknown changes keep the repository's normal required scope. Never
  dispatch/rerun workflows merely to obtain a second current run.
- Feasibility experiments are bounded, not completed delivery: pick one blocker
  and one falsifiable experiment, then either integrate a passing implementation
  into the requested deliverable, or revise the approach. Do not integrate a
  failing candidate or expand exploratory variants indefinitely. A completed
  milestone means the task's requested deliverable with evidence (including
  tests when tests are the deliverable), not merely exploratory activity, a
  green check or an arbitrary new milestone name.
- Next action comes first. Keep the operative ticket scope short; link archived
  history and immutable evidence instead of copying it each run. Do not reread
  unrelated histories or reverify unchanged dependencies without a risk reason.
- Put this optional machine-readable record before history in the report (JSON
  string values only). It is author-declared context, not approval or acceptance:

```continuation
{"next_action":"one concrete next step", "source_sha":"full published PR head SHA",
 "ci_run":"immutable relevant run URL",
 "failure_category":"production|test/pilot|infrastructure|none",
 "failure":"short current failure or none",
 "remaining":"current exit criteria", "evidence":"immutable receipt + dependency identity"}
```

Add `completed_milestone` only for a genuinely new completed deliverable with
immutable evidence and a new observed source SHA; never copy a placeholder.
"""

FIELDS = (
    "next_action",
    "source_sha",
    "ci_run",
    "failure_category",
    "failure",
    "completed_milestone",
    "remaining",
    "evidence",
)


def checkpoint_record(report: str | None) -> dict[str, str]:
    """Read one bounded optional continuation fence from an agent's own report.

    Invalid/missing records fall back to the ordinary report. No dependency on
    a repo file, model session cache, external schema or guessed ticket prose.
    """
    if not report:
        return {}
    match = re.search(r"^```continuation\s*\n(.{1,12000}?)\n```", report, re.M | re.S)
    if not match:
        return {}
    try:
        payload = json.loads(match[1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result = {
        k: v.strip()[:1000]
        for k, v in payload.items()
        if k in FIELDS and isinstance(v, str) and v.strip()
    }
    if "next_action" not in result or "source_sha" not in result:
        return {}
    return result


def compact_report(report: str, limit: int = 4000) -> str:
    """Prioritize next action and source before clipping a verbose legacy report."""
    record = checkpoint_record(report)
    prefix = ""
    if record:
        # A bounded priority summary, not the first 4K of an append-only history.
        prefix = "Checkpoint state (agent-declared; verify source):\n" + "\n".join(
            f"- {key}: {record[key]}" for key in FIELDS if key in record
        )
        prefix = prefix[: min(3000, limit // 4 * 3)] + "\n\n"
    text = prefix + report
    if len(text) <= limit:
        return text
    suffix = "\n… report truncated; read the ticket for the rest …"
    return text[: max(0, limit - len(suffix))] + suffix
