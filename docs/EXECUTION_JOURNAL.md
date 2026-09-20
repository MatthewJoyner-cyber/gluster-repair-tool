# Durable execution attempts

Each execution receives a new attempt ID and immutable JSON events under
`RUN/attempts/ATTEMPT/`. Events record the selected plan, step intent, each
executor command's exact argv, return code and both output streams, step results,
and the final outcome. A step intent also covers local filesystem operations
such as creating a staging directory. Remote helper internals are represented by
the helper invocation; this is not a trace of every remote system call.

An intent is written and fsynced before dispatch. Its result is written and
fsynced immediately when known, before another step starts. File creation is
exclusive, directories are fsynced, and concurrent workers serialize event
creation. Execution fails closed if the journal cannot be written. Another
process cannot execute against the same run directory while its lock is held.
Already-dispatched parallel commands may finish when a peer fails.

Exceptions and handled interruption preserve earlier results and record an
unknown in-flight outcome. Process death can leave a start event without a
finish; that is unresolved, not evidence that no write occurred. The executor
refuses reuse of a run with an incomplete, unreadable or unfinished attempt.
It never repairs the journal or replays such a command automatically.

The CLI records the saved apply fingerprint and run directory before dispatch.
Choosing another run directory does not bypass an unresolved attempt for that
same saved apply artifact. Inspect possible effects and refresh evidence and
planning before another authorized run. A new artifact does not itself prove
that the previous write was harmless; the existing planner and binding gates
still apply.

Summary files such as `execute.json`, per-action reports and `repair.log` remain
convenience views. A later successful attempt may replace those views, but its
new attempt directory preserves the earlier events. Summary-write failures
block CLI completion, post-run healing and backup cleanup. Managed heal
restoration still runs, and diagnostics identify the retained attempt directory.

Public CLI calls use their configured persistent run directory. Direct Python
calls without `run_dir` create and report a retained temporary directory; callers
requiring retention across reboot or temporary-file cleanup must supply a durable
location. Filesystem fsync guarantees, functioning storage and retention of the
run directory are prerequisites. This is not protection against hardware failure,
manual deletion, journal tampering or a remote operation continuing after a lost
connection. A recorded return code still requires domain-specific verification.

Tests exercise process exit during a step, success followed by failure,
interruption, command-level records, result/report-write failures, parallel
workers, immutable successive attempts, and CLI refusal of an unresolved saved
artifact. They use disposable local artifacts and mocked repair commands.
