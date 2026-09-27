# Session starter

Read TODO, the active review/plan, and relevant safety rules. Read HISTORY.md for pre-migration design history and Git commits for later changes. This is a sanitized candidate with
unresolved repair safety findings; no live cluster state is implied by it.

For first publication, use [the first-beta plan](../docs/FIRST_BETA_RELEASE_PLAN.md)
to distinguish acceptance work from optional post-beta qualification. Historical
review findings require reconciliation with later evidence, not automatic reruns.

For a task on the user's environment, consult
`$GLUSTER_REPAIR_PRIVATE_ROOT/steering/START_HERE.md`, defaulting to
`$HOME/.gluster-repair-private/steering/START_HERE.md`, if available and relevant.
Read only the named task record. Never copy it into public outputs.

The core uses its own CLI and standard test commands. Optional installed
bounded helpers are configured privately; no fixed account, checkout path,
lab inventory, or agent runtime is required here.
