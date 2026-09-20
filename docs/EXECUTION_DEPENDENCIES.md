# Execution dependencies

An action may start only when every named prerequisite completed in an earlier
wave of the current execution. The executor checks actual outcomes before
dispatching a wave. A previous run's `completed` status is not fresh authority.
Actions must enter execution in `planned` or `proposed` state; other states
require fresh planning. Missing/duplicate identities, self-dependencies and
same/later-wave prerequisite references are rejected. The executor does not
repair the graph or silently reorder an approved plan.

With `--keep-going`, independent actions may continue after a failure. Children
and transitive descendants of failed, unknown or unmet prerequisites become
`blocked`, with the prerequisite and its state recorded in their notes. Their
planned steps become `not-run`; blocked actions are excluded from the executed
count. Without keep-going, remaining independent work is also left unstarted.
Already-dispatched parallel work can finish; interruption prevents new waves.

Only a fully `completed` action satisfies a prerequisite. The conservative rule
also blocks descendants of `completed-with-nonblocking-skips`, including
staging-only work or a successful native resolver whose fallback steps were
skipped. This does not mean the native operation failed. It means the dependent
action needs fresh evidence and a new plan rather than inferred completion.

Unexpected step outcomes and caught worker exceptions become `unknown` and do
not permit dependent writes. Handled interrupts retain an execution report and
are propagated to the CLI's interruption handler. Summaries distinguish
`failed_actions`, `blocked_actions`, `unknown_actions` and `not_run_actions`.
Per-action records explain blocked and unstarted work as well as executed work.

The public CLI returns 2 for an incomplete run and marks completion blocked.
It stops before post-run healing, verification or backup cleanup. Managed heal
restoration still runs. Unknown outcomes remain unknown when status and execution
history are merged. Inspect the records, verify possible writes and collect fresh
evidence before planning another attempt.

[Native resolver outcome checks](EXECUTION_OUTCOMES.md) now stop unknown results
before intra-action fallback. [Durable attempt records](EXECUTION_JOURNAL.md)
preserve intent and results independently of wave-summary writes. Validation
uses mocked commands and disposable local artifacts; no live repair
qualification is implied.
