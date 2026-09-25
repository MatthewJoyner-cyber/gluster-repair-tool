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

### First access is an administrator step

A bare host is not expected to have an operator key, trusted SSH host keys,
the repair account, or the tool's sudoers policy. Bootstrap currently starts
after an administrator has established the initial login. It uses batch SSH
and noninteractive sudo; it has no password-entry or first-access wizard.

Use the console or an already authorized administrative login to provision
required packages and the initial operator access. Verify the host fingerprint
through a trusted channel before recording it. Create or select the operator
key through the operator's normal key-management workflow; never place private
keys or passwords in the source tree, command history, test reports or support
bundles. A passphrase-protected key must already be usable by the invoking SSH
process, for example through the operator's unlocked agent.

For a non-root installation login, the administrator must separately arrange
permission to execute the remote installer as root. The preflight's
`sudo -n true` probe checks only that command; it does not prove permission for
the later `sudo -n bash -s` installer. Record this prerequisite explicitly and
test the actual installation on disposable hosts. Temporary installation
privileges belong to the bootstrap administrator, not to the repair service
account, and must be removed or reviewed when setup is complete.

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
`visudo` parsing, service login and idempotent reinstall passed on three
disposable Ubuntu 24.04 guests on 2026-09-24. Missing-key/trust/login/sudo
refusals and six directed service-peer connections also passed. At that
checkpoint, additional failure scenarios, other distributions and physical-host
policy remained open.
Follow-up VM tests also refused a changed pinned host key, an encrypted operator
key without an agent, and a second-source SCP failure. The SCP failure left the
installed files, sudoers policy and service key unchanged and removed remote
staging. Invalid package syntax and invalid sudoers output were also refused;
installed files, ownership/modes, sudoers, account keys and remote staging
remained unchanged. These cases do not make bootstrap transactional or qualify
other distributions and physical-host policy.

### Fresh-account and bare-system test sequence

A new operator login on an existing brick host can reveal dependence on that
operator's HOME, keys, groups and cached trust. It cannot establish bare-system
acceptance: installed packages, existing sudoers and the repair account remain.
Do not delete or rename the active service account to manufacture a fresh test.

Use three disposable OS instances with isolated storage and networking for the
full sequence. Run the supported service identity `gluster-repair` at its
normal paths inside each guest. An alternate service name is not currently
supported; changing only the bootstrap username misses fixed peer-transfer
paths and would not qualify the default installation. A fresh `test-operator`
login inside the guests supplies the separate human/admin role. Use newly
generated test credentials, with no reuse of operational service keys.

An official [Ubuntu Server cloud image](https://cloud-images.ubuntu.com/releases/noble/)
is a suitable prebuilt guest for the initial Ubuntu 24.04 test. Pin its build
and verify its signed checksum. Seed only the guest's administrative access;
do not pre-create the repair service user, its keys, tool files or sudoers.
Each guest needs distinct machine and SSH host identities and a private test
disk. Cloud-image testing starts from a preinstalled OS and does not cover
the operating-system installer or a human's initial password-entry workflow.
Keep lab disks independent of the Gluster volumes being investigated.

| Stage | Required evidence |
| --- | --- |
| Bare baseline | Record OS/interpreter and missing packages; prove the repair user, group, home, install directory and sudoers drop-in are absent. Retain a restorable guest baseline. |
| Initial administrator access | Exercise console/password or existing administrative access as applicable. Record the checkpoint outcome without secrets. Establish verified host keys and a usable operator key explicitly. |
| Expected first-access refusals | Missing private/public key, locked key without an agent, unknown/changed host key, failed login, no sudo and insufficient installer sudo permission stop clearly. Record any staging/key side effects and clean only guest-owned test state. |
| Fresh install | Run the real shipped bootstrap, real SSH and real sudo/visudo. Verify user, group, home and shell; home and SSH modes; key ownership; root-owned installed modules; imports outside the checkout; and all six entry-point help commands. |
| Service access | Log in as the service account with the operator and service test keys. Verify required noninteractive helper access and inspect the effective sudo policy. Check that unrelated direct root commands and unprivileged edits to installed code are denied. An allowed helper has its own authority surface and must also be reviewed. |
| Three-peer path | Verify service-key login and trusted peer identity in every directed guest pair. Exercise tool-generated transfers with disposable files and verify ownership, ACLs, trusted/user xattrs and hardlinks. Keep this R6 evidence distinct from an SSH login result. |
| Repeat and failure recovery | Reinstall with existing accounts and keys; check for duplicate authorized keys and ownership drift. Inject transfer, package validation and sudoers failures. Preserve working access and document partial changes; installation is not transactional. |
| Cleanup | Revoke test access, remove temporary installation privileges and destroy only the recorded guests/test storage. Check that original hosts' accounts, keys and tool installations were untouched. |

Interactive first-access checkpoints are part of the test, not a reason to
claim automated success. Record them separately from the repeatable bootstrap
steps, with actual exit results and installed-state checks. VM results qualify
the tested guest environment; different physical-host policy still needs its
own controlled deployment check. Fresh installation alone does not qualify
live Gluster repair correctness.

These guarantees apply to `gluster-bootstrap-host.sh` and
`gluster-bootstrap-volume.sh`. The separate update script has its own
[preview and preflight contract](DEPLOY_PREVIEW.md), tested locally with
stubbed host commands. Its deployed-host behavior remains unqualified.
