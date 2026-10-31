#!/bin/sh
# Fake CLI agent for manual acceptance and end-to-end tests.
#
# Behavior is selected with FAKE_AGENT_BEHAVIOR. When the variable is unset the
# first argument (the rendered prompt file) decides: a prompt that mentions
# "review" approves, anything else reports PR_OPENED. That lets a single runner
# configuration serve both the implementer and the reviewer in one cycle.
#
#   pr_opened             exit 0, prints RESULT: PR_OPENED
#   blocked               exit 0, prints RESULT: BLOCKED <reason>
#   crash                 exit 7 with a message on stderr
#   hang                   sleep FAKE_AGENT_HANG_SECONDS (default 600)
#   approve               exit 0, prints RESULT: APPROVED
#   reject_with_comment   exit 0, prints RESULT: REJECTED (the agent must also
#                         post the ticket comment; a real agent does this through
#                         the OpenProject skill, this script does not)
#   reject_without_comment exit 0, prints nothing
#   no_result             exit 0, prints no RESULT line
#
# The script never reads stdin; set stdin_file only in runner configs that need it.

set -u

behavior="${FAKE_AGENT_BEHAVIOR:-}"
prompt="${1:-}"

if [ -z "$behavior" ]; then
    if [ -n "$prompt" ] && grep -qi 'review' "$prompt" 2>/dev/null; then
        behavior=approve
    else
        behavior=pr_opened
    fi
fi

echo "fake agent: behavior=$behavior prompt=${prompt:-<none>}"

if [ "${FAKE_AGENT_SLEEP_SECONDS:-0}" != "0" ]; then
    sleep "$FAKE_AGENT_SLEEP_SECONDS"
fi

case "$behavior" in
    pr_opened)
        echo "fake agent: committed work and opened the pull request"
        echo "RESULT: PR_OPENED"
        exit 0
        ;;
    blocked)
        echo "fake agent: cannot continue without credentials"
        echo "RESULT: BLOCKED missing credentials"
        exit 0
        ;;
    crash)
        echo "fake agent: simulated crash" >&2
        exit 7
        ;;
    hang)
        echo "fake agent: hanging"
        sleep "${FAKE_AGENT_HANG_SECONDS:-600}"
        echo "RESULT: PR_OPENED"
        exit 0
        ;;
    approve)
        echo "fake agent: reviewed and merged the pull request"
        echo "RESULT: APPROVED"
        exit 0
        ;;
    reject_with_comment)
        echo "fake agent: rejected the pull request (comment must be posted separately)"
        echo "RESULT: REJECTED changes requested"
        exit 0
        ;;
    reject_without_comment)
        echo "fake agent: rejected without posting a ticket comment"
        exit 0
        ;;
    no_result)
        echo "fake agent: finished without a RESULT line"
        exit 0
        ;;
    *)
        echo "fake agent: unknown behavior '$behavior'" >&2
        exit 2
        ;;
esac
