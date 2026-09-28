# Install on an existing Gluster volume

Bootstrap sets up the restricted `gluster-repair` account and tool files on
each brick host. It does **not** create a Gluster cluster or volume. This guide
starts with the human administrator's access, then runs the installer. The
tested setup is Ubuntu 24.04 LTS with Gluster 11.1 and a pure Replicate volume.
Use [Gluster's volume setup guide](https://docs.gluster.org/en/main/Administrator-Guide/Setting-Up-Volumes/)
and installation instructions appropriate to your package version if you do
not yet have a working volume.

## 1. Choose the controller and check the volume

Work on a Gluster node that can query the intended volume and reach every
brick host by the hostnames shown in `gluster volume info`. The same account
will run the tool after installation. Confirm the volume is `Type: Replicate`,
all intended bricks are online, native healing has been allowed to finish,
and you have a current independent backup. Replace `example-volume` in this
guide with the real volume name.

The controller needs Bash, Git, OpenSSH client tools, Python 3.12 or newer and
the Gluster CLI. Each brick host needs its existing Gluster installation, an
SSH server, sudo, Python 3.12 or newer and the command-line utilities used by
the selected repair operations, including rsync, tar and ACL/xattr tools.
For Ubuntu 24.04, the CLI package is
[`glusterfs-cli`](https://packages.ubuntu.com/noble/glusterfs-cli); use the
package source matching your cluster rather than changing Gluster versions
just to install this tool.

Check the controller's access with one of these commands:

```bash
gluster volume info example-volume
sudo -n gluster volume info example-volume
```

The second is needed only when the first is denied. Volume bootstrap and the
tool can retry this read through `sudo -n`. If both fail, set up the controller
privilege in step 4, then repeat the check. Also inspect `gluster volume status
example-volume` and `gluster volume heal example-volume info` (with `sudo -n`
if needed) before deciding native healing is complete.

## 2. Get the source on the controller

Open the public `gluster-repair-tool` repository named in
[project repositories](../MAINTAINERS.md#project-repositories), choose
**Code → HTTPS**, and copy its clone URL. Replace `REPOSITORY_URL` with that URL:

```bash
git clone REPOSITORY_URL
cd gluster-repair-tool
python3 gluster-manager.py --help
```

Keep this checkout on the controller. Bootstrap copies the matching runtime
files from it to the brick hosts. A signed release tag can be checked out for
a frozen version; keep controller and deployed files on the same revision.

## 3. Set up the administrator SSH key and host trust

On the controller, use an existing `~/.ssh/id_ed25519` keypair or create one
only if neither file exists:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

Choose a passphrase and load the key into your SSH agent before unattended
bootstrap. On a headless terminal, `ssh-agent bash` opens a shell with an
agent; inside it run `ssh-add ~/.ssh/id_ed25519`. Do not put private keys or
passphrases in the repository. If your key has another name, pass both `-i`
and `-p` to the bootstrap commands below.

Verify each brick host's SSH fingerprint through a trusted channel before
accepting its first host-key prompt. Give the **administrator's public key**
to the login account on every brick host. For hosts that allow an initial
password login, repeat this pattern for each host:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub operator@brick-a
ssh operator@brick-a true
```

If password SSH is disabled, use your console, image provisioning or existing
administrator access to install the public key in that account's
`authorized_keys`. Repeat the login check for every brick hostname reported
by the volume. This also records their verified host keys in the controller's
`known_hosts`; the volume installer needs those entries for peer transfers.

## 4. Arrange noninteractive administrator sudo

The administrator login on **each brick host** must run `sudo -n true` during
preflight and `sudo -n bash -s` for the actual installer. If the controller's
plain Gluster CLI is denied, it also needs `sudo -n gluster` locally. The
installer creates a separate, restricted sudoers policy for `gluster-repair`;
that service policy does not provide the initial administrator access.

An already authorized administrator can create a temporary bootstrap rule on
each host with `sudo visudo -f /etc/sudoers.d/gluster-bootstrap-admin` and this
line, replacing `operator` with the actual login:

```text
operator ALL=(root) NOPASSWD: ALL
```

This grants broad root access to that administrator. Use it only under your
site's access policy, and review or remove the bootstrap rule after setup.
Keep whatever noninteractive Gluster CLI access the controller needs for later
health and repair commands. Confirm the remote rule on each host:

```bash
ssh operator@brick-a sudo -n true
ssh operator@brick-a sudo -n id -u
```

The second command should print `0`. A passing `sudo -n true` alone cannot
prove that the installer can run; the real install is the final check.

## 5. Check, install and verify

From the controller checkout, run preflight first, then the same command
without `--preflight`. These commands discover the brick hosts from the named
volume; they do not create the volume.

```bash
./gluster-bootstrap-volume.sh -v example-volume -l operator --preflight
./gluster-bootstrap-volume.sh -v example-volume -l operator
```

Preflight checks the existing local keypair, verified host keys, batch SSH and
`sudo -n true` without creating service keys or staging files. Installation
creates or reuses the `gluster-repair` account and shared service key, copies
the tool, verifies its entry points, and installs its own sudoers drop-in. A
failed or interrupted install can leave partial state; inspect the error and
installed files before retrying.

For every brick host, confirm the service login and installed files, then run
the tool from the controller. For example:

```bash
ssh gluster-repair@brick-a true
ssh operator@brick-a /opt/gluster-repair/gluster-manager.py --help
python3 gluster-manager.py health-check --volume example-volume
python3 gluster-manager.py repair --volume example-volume --preview
```

The first repair run should be a preview. It may contact hosts and trigger
native healing through mount lookups but does not execute repair writes. If a
problem remains after native healing, read the [beta limits](../README.md#beta-scope)
before using `repair --interactive` in a terminal. For setup help, the
optional `gluster-repair-agent` companion can guide these steps; it does not
grant SSH or sudo access.

## Installer contract and limits

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

## Preflight details

For a single host, or to diagnose a volume preflight failure, check one host
directly with the same administrator key:

```bash
./gluster-bootstrap-host.sh -H host-a -l operator -i ~/.ssh/id_ed25519 -p ~/.ssh/id_ed25519.pub --preflight
```

Host preflight checks local source/key files, noninteractive SSH access and
`sudo -n true` for a non-root login. It requires an already trusted host key
and disables SSH host-key updates. It does not create operator/service keys,
stage files, or run the remote installer. Volume preflight discovers a pure
Replicate volume, reads existing verified keys for every peer, and performs
the host checks without creating a temporary known-hosts file.

Preflight is an access check, not an installation check: target Python, imports,
account compatibility, file installation and sudoers validation are checked
during installation. A missing operator public key is an error in both modes;
bootstrap never regenerates or overwrites the operator private key.

## Installation details

Run the same command without `--preflight` when installation is intended.
Execution may create a shared service keypair locally and accept new SSH host
keys. The shared key has no passphrase because brick-to-brick transfers are
noninteractive; protect the controller's copy and each service account home.
A partial service keypair is refused. Volume bootstrap installs the
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
