# Deploy-script preview and preflight

`gluster-deploy-heal-tool.sh` updates an installation already created by
bootstrap. It supports the `gluster-repair` service account and the
`/opt/gluster-repair` install root. Unsupported `-s` or `-d` values fail
before volume discovery or host access. Use
[bootstrap](BOOTSTRAP.md) first if the package, helper or install directory is
missing.

`--dry-run` reads local source filenames and `gluster volume info`, then prints
the intended SSH and rsync commands. It does not contact brick hosts, refresh
health/cache state, create keys, or run the printed commands. It may describe
what `--setup-keys` would do without doing it.

`--preflight` requires an existing local private/public keypair and previously
trusted SSH host keys. It uses `StrictHostKeyChecking=yes` and disables SSH
host-key updates. Remote checks run only the installed host helper's help and
`stat` commands and the resolver's help command. There is no probe-directory
creation, removal, rsync, health refresh, or key setup. `--setup-keys` cannot
be combined with `--preflight`. An absent install directory fails preflight;
the read-only check cannot establish whether a future copy will succeed.

Normal deployment also requires known host keys. It never creates a missing
local keypair unless `--setup-keys` is explicitly supplied; a partial pair is
refused. Only after its prechecks does deployment run the planned mkdir/rsync
steps. Health and brick-layout cache refresh happens after successful copying.
Deployment is not transactional, and a failed remote copy may leave partial
files for inspection. No local test here establishes privileged host behavior
or live Gluster acceptance.
