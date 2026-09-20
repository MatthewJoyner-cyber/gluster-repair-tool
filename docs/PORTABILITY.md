# Local portability and deployment limits

The controller targets Linux with Python 3.12 or newer. Bootstrap refuses an
older interpreter. The candidate has been exercised locally with CPython
3.12.3 on one Linux host; later Python versions and other distributions have
not yet been qualified. Windows and macOS are outside this candidate's runtime
scope. Source help and offline tests need no Gluster volume, private agent
configuration, or personal mount.

| Environment | Current evidence | Remaining check |
| --- | --- | --- |
| Linux, CPython 3.12.3, unprivileged account | Clean file-only copy, fresh HOME/XDG state, full offline suite and installed entry-point help pass | A separate clean host and distribution |
| Python 3.13 or later | Bootstrap's minimum-version gate does not reject it; no interpreter was available for tests here | Run full suite and install check |
| Gluster client/server and brick hosts | No live qualification from this staging pass | Matching client/server versions, account/ownership, sudoers, helper deployment and repair fixtures |

Work defaults to `$XDG_STATE_HOME/gluster-repair/work` when `XDG_STATE_HOME`
is absolute, otherwise `$HOME/.local/state/gluster-repair/work`. Backup
discovery uses the sibling `backups` directory. Set
`GLUSTER_REPAIR_WORK_ROOT` and `GLUSTER_REPAIR_BACKUP_DIR` to explicit paths
when retaining existing private state. The process reads these settings at
startup; set them before launching the CLI. A missing work directory is
created when a write is requested. An unusable or unwritable location fails
the write rather than silently selecting another root. `status-report` can
read a missing status file as empty without creating state.

Changing a default does **not** move old runs, archives, mounts, or status.
Inventory the old location privately, set overrides if old state must remain
in use, and verify the intended paths before resuming. Unknown prior writes
need focused verification. See [migration](../MIGRATION.md) and
[backup fidelity](BACKUP_FIDELITY.md).

For host deployment, the controller also needs the matching Gluster
client/CLI, SSH access and the authorized remote workers. Individual
operations need Bash, sudo, mount tools, rsync, tar, attr/ACL utilities and
the Gluster service commands they invoke. The
[bootstrap contract](BOOTSTRAP.md) describes the fixed service account and
install paths and what its local installer checks. A local test cannot
establish account creation, privileged ownership, `visudo` acceptance or
behavior on a fresh deployed brick; those remain release gates.
