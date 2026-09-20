# Bootstrap contract

Bootstrap installs the six runtime entry points and the `gluster_heal_tool/`
package on each selected brick host. It reuses existing service keys and
accounts, and updates tool files and the sudoers drop-in on repeated runs.
The host and volume scripts accept only these runtime defaults:

| Setting | Supported value |
| --- | --- |
| Service account | `gluster-repair` |
| Service home | `/var/lib/gluster-repair` |
| Install root | `/opt/gluster-repair` |

Different `-s`, `-m`, or `-d` values fail before remote access. An existing
service account with a different home also fails before account or installation
changes on that host. Runtime helpers currently depend on the default paths;
custom layouts require a coordinated runtime change.

## Preflight

Supply an existing operator SSH private/public key pair and verify each host's
SSH key in the operator's `known_hosts` before running preflight:

```bash
./gluster-bootstrap-host.sh -H host-a -l operator -i ~/.ssh/id_ed25519 -p ~/.ssh/id_ed25519.pub --preflight
./gluster-bootstrap-volume.sh -v example-volume --preflight
```

Host preflight checks local source/key files, noninteractive SSH access and
`sudo -n true` for a non-root login. It requires an already trusted host key
and disables SSH host-key updates. It does not create operator/service keys,
stage files, or run the remote installer. Volume preflight discovers a pure
Replicate volume, reads existing verified keys for every peer, and performs
the host checks without creating a temporary known-hosts file.

Preflight is an access check: target Python, imports, account compatibility,
file installation and sudoers validation are checked during installation.
A missing operator public key is an error in both modes; bootstrap never
regenerates or overwrites the operator private key.

## Installation and verification

Run the same command without `--preflight` when installation is intended.
Execution may create a shared service keypair locally and accept new SSH host
keys. A partial service keypair is refused. Volume bootstrap installs the
controller's verified peer keys for service-account transfers.

The transport preserves the package directory. Before installing tool files,
the shared installer checks the complete staged tree, requires Python 3.12 or
newer, parses every package module, checks the imported package location, and
runs help for the manager, core CLI, worker, host helper, log helper, and GFID
resolver. It repeats entry-point verification against the installed tree,
outside the checkout and without Python environment or user-site fallback.
Only then does the remote script validate and install sudoers and report ready.
The lab-only `--brick-ops-only` policy uses the helper's actual
`brick-down --volume ... --brick ...` argument order.

Installation is not transactional. A copy or later validation failure may
leave updated files or an account/key setup on the host; it exits unsuccessfully
and must be investigated before use. Repeated installation replaces distributed
files but does not prune unrelated or obsolete files from an existing install.

## Qualification

Local tests exercise the real installer in disposable directories, all six
entry points, repeated upgrades, invalid packages and simulated SSH/SCP
transport. Preflight tests cover failed access, missing peer keys, and absence
of key generation/staging. Actual account creation, privilege ownership,
`visudo` parsing and deployment on a clean disposable host remain to be qualified.

These guarantees apply to `gluster-bootstrap-host.sh` and
`gluster-bootstrap-volume.sh`. The separate update script has its own
[preview and preflight contract](DEPLOY_PREVIEW.md), tested locally with
stubbed host commands. Its deployed-host behavior remains unqualified.
