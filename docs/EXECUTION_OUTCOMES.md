# Native resolver outcomes and missing targets

Native split-brain commands are writes. Losing their response does not establish
that nothing changed. An unrecognized response or operational failure sets the
step and action to `unknown`, stops all remaining steps in that action, and
blocks dependent actions. Keep-going may still run independent actions.
The current [compatibility profile](GLUSTER_COMPATIBILITY.md) has no live-qualified
native resolver write path. Public apply-run and direct executor calls refuse
such a plan before any action dispatch; the response rules below describe the
behavior to retain when a version is qualified with a controlled live canary.
Inspect possible effects, refresh evidence and build a new plan before another
attempt. There is no automatic resolver retry or fallback after an unknown result.

The response classifier recognizes complete English messages for the exact
volume-relative file argument, along with the command mode and exit code:

| Response | Behavior |
| --- | --- |
| Recognized successful heal, exit zero | Skip the remaining fallback steps. |
| Recognized file-not-in-split-brain response, exit zero or one | Skip fallback; subsequent verification still determines completion. |
| Recognized mtime/size tie for the matching selection mode, exit zero or one | Continue only the follow-up already present in the reviewed plan. |
| Permission/transport failure, signal, exception, empty or unfamiliar output | Stop with unknown outcome; do not run source-brick or destructive fallback. |

Every nonblank line from stdout and stderr participates in classification.
Additional diagnostics, a different file, conflicting outcomes, unexpected exit
codes, and a tie from source-brick selection all stop the action. Filenames
containing newlines cannot be classified through this text interface.

Gluster's GFID-heal path can emit an I/O lookup diagnostic followed by a specific
GFID result. The candidate accepts only the exact two-line shape for the same
file, exit zero, and a recognized GFID success or selection-mode tie. A standalone
lookup error, different path, or extra diagnostic does not qualify. This behavior
is based on the upstream [glfsheal response implementation](https://github.com/gluster/glusterfs/blob/devel/heal/src/glfs-heal.c).
It is a narrow parser contract, not proof of correct file contents. Additional
Gluster versions, translated messages and live outcomes require qualification;
unrecognized variants stop safely rather than granting fallback authority.

Missing-target tolerance is restricted to an explicitly optional removal with
exit one, empty stdout, and a complete `rm` ENOENT diagnostic naming exactly its
target. General `not found` or `cannot stat` text is insufficient. ENOTCONN, EIO,
stale handles, permission errors and transport failures cannot count as absence.

Failed directory creation, backup, staging, restore, metadata attachment or
quarantine stops execution even if an older plan sets `tolerate_missing`.
A missing backup or quarantine source requires refreshed evidence before any
later deletion. A failed backup-parent mkdir never permits the copy or removal.
These restrictions intentionally tighten older best-effort plans.

The shared CLI reports incomplete execution and stops before post-run healing,
verification and backup cleanup; managed heal settings are still restored.
See [dependency behavior](EXECUTION_DEPENDENCIES.md) and
[durable per-command attempt records](EXECUTION_JOURNAL.md) for the execution
record and refusal behavior after process loss.
Local fault tests exercise the real executor with mocked commands. No live
resolver, repair, or cluster operation was used to qualify this change.
