# SPDX-License-Identifier: GPL-2.0-only
"""Verify archive fidelity before cleanup and after scoped restoration."""
from __future__ import annotations

from copy import copy
from pathlib import Path
import tarfile
import tempfile

from . import backup_fidelity as fidelity


def _local(host):
    return not host or host.lower() in {"localhost", "127.0.0.1", "::1"}


def inspect_archive(archive, manifest):
    from . import backup_maintenance as backups
    contract = manifest.get("fidelity") or {}
    if manifest.get("schema_version") != 3 or contract.get("contract") != "backup-v1" or contract.get("remote_encoding") != "rsync-fake-super":
        raise ValueError("archive has no supported fidelity contract; retain original backups")
    expected = contract.get("members")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("archive has no captured member inventory")
    artifacts, errors = backups._host_mapped_artifacts(manifest)
    if errors:
        raise ValueError("; ".join(errors))
    owners = {}
    for name in expected:
        if not backups._safe_archive_member(name):
            raise ValueError(f"unsafe archive member: {name}")
        matches = [a for a in artifacts if backups._member_belongs_to_artifact(name, a["member"])]
        if len(matches) != 1:
            raise ValueError(f"archive member must have exactly one destination: {name}")
        owners[name] = "local" if _local(matches[0]["host"]) else matches[0]["host"]
    for artifact in artifacts:
        if artifact["member"] not in expected:
            raise ValueError(f"archived artifact is absent: {artifact['member']}")
        if not _local(artifact["host"]):
            fidelity.safe_host(artifact["host"])
            if artifact["member"] != f"{artifact['host']}/{artifact['destination'].lstrip('/')}":
                raise ValueError("remote archive mapping differs from capture path")
    for name,row in expected.items():
        if any(owners.get(link) != owners[name] for link in row.get("hardlinks", [])):
            raise ValueError("hardlinks cannot cross archive host identities")
    fidelity.verify_tar(archive, expected, backups.ARCHIVE_MANIFEST_NAME)
    return artifacts, expected


def _extract_stage(archive, artifacts, expected, stage):
    from . import backup_maintenance as backups
    report = backups._empty_restore_report()
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != backups.ARCHIVE_MANIFEST_NAME]
        count = backups._restore_members(tar, members, target_root_text=str(stage), display_root=stage,
                    report=report, on_conflict="overwrite", rename_suffix=".kept", prompt_input=None,
                    prompt_output=None, conflict_state={})
    if report["errors"] or report["warnings"] or report["restored_entries"] != count:
        raise ValueError(f"archive staging could not restore all metadata: {report['errors'] + report['warnings']}")
    paths = [(stage / a["member"], a["member"]) for a in artifacts]
    if fidelity.snapshot(paths) != expected:
        raise ValueError("archive staging differs from captured content or metadata")


def verify_cleanup_sources(results, archive):
    from . import backup_maintenance as backups
    manifest = backups._read_archive_manifest(Path(archive))
    artifacts, expected = inspect_archive(archive, manifest)
    selected = {(a.host, a.backup_path) for a in backups.collect_backup_artifacts(results)}
    if selected != {(a["host"], a["destination"]) for a in artifacts}:
        raise ValueError("cleanup selection differs from verified archive destinations")
    locals_ = [a for a in artifacts if _local(a["host"])]
    local_expected = {name:row for name,row in expected.items() if any(backups._member_belongs_to_artifact(name,a["member"]) for a in locals_)}
    if fidelity.snapshot([(Path(a["destination"]), a["member"]) for a in locals_]) != local_expected:
        raise ValueError("local backup changed since capture; originals retained")
    with tempfile.TemporaryDirectory(prefix="gluster-cleanup-verify-") as tmp:
        stage = Path(tmp)
        _extract_stage(archive, artifacts, expected, stage)
        for host in sorted({a["host"] for a in artifacts if not _local(a["host"])}):
            fidelity.verify_remote_group(host, [a["destination"] for a in artifacts if a["host"] == host], stage)


def restore_verified_archive(archive, manifest, report, *, restore_root, cleanup_archive,
                             on_conflict, rename_suffix, prompt_input, prompt_output, preview):
    from . import backup_maintenance as backups
    report.update(restore_complete=False, archive_removed=False, fidelity_verified=False)
    try:
        if restore_root:
            raise ValueError("host-mapped archives restore only to their recorded destinations; --restore-root is for legacy archives")
        artifacts, expected = inspect_archive(archive, manifest)
        for artifact in artifacts:
            operation = {**artifact, "operation": "local-extract" if _local(artifact["host"]) else "remote-rsync"}
            if not _local(artifact["host"]):
                operation["command"] = backups._remote_restore_command(artifact["host"], Path("<archive-stage>") / artifact["member"], artifact["destination"])
            report["planned_restores"].append(operation)
        if preview:
            if cleanup_archive:
                report["warnings"].append("archive retained because preview does not restore entries")
            return report
        with tempfile.TemporaryDirectory(prefix="gluster-backup-restore-") as tmp:
            stage = Path(tmp)
            _extract_stage(archive, artifacts, expected, stage)
            local_artifacts = [a for a in artifacts if _local(a["host"])]
            if local_artifacts:
                names = {}
                for name in expected:
                    for artifact in local_artifacts:
                        if backups._member_belongs_to_artifact(name, artifact["member"]):
                            names[name] = artifact["destination"].lstrip("/") + name[len(artifact["member"]):]
                if len(set(names.values())) != len(names):
                    raise ValueError("local archive destinations overlap")
                with tarfile.open(archive, "r:gz") as tar:
                    rebased = []
                    for member in tar.getmembers():
                        if member.name not in names:
                            continue
                        rewritten = copy(member)
                        rewritten.name = names[member.name]
                        if member.islnk():
                            if member.linkname not in names:
                                raise ValueError("hardlink leaves the authorized local destinations")
                            rewritten.linkname = names[member.linkname]
                        rebased.append(rewritten)
                    count = backups._restore_members(tar, rebased, target_root_text="/", display_root=Path("/"),
                                report=report, on_conflict=on_conflict, rename_suffix=rename_suffix,
                                prompt_input=prompt_input, prompt_output=prompt_output, conflict_state={})
                    report["expected_restore_entries"] += count
                local_expected = {name:row for name,row in expected.items() if name in names}
                if (not report["skipped_conflicts"] and not report["skipped_newer_paths"]
                        and fidelity.snapshot([(Path(a["destination"]), a["member"]) for a in local_artifacts]) != local_expected):
                    raise ValueError("restored local backup differs from captured content or metadata")
            for host in sorted({a["host"] for a in artifacts if not _local(a["host"])}):
                if on_conflict != "overwrite":
                    raise ValueError(f"remote artifact requires --on-conflict overwrite: {host}")
                group = [a for a in artifacts if a["host"] == host]
                paths = [a["destination"] for a in group]
                command = fidelity.transfer_group_command(host, paths, stage, stage / f".{host}.restore-files", push=True)
                for operation in report["planned_restores"]:
                    if operation["host"] == host:
                        operation["command"] = command
                try:
                    fidelity.run_transfer(command)
                    fidelity.verify_remote_group(host, paths, stage, push=True)
                except (OSError, ValueError) as exc:
                    raise ValueError(f"remote restore failed for {host}:{paths[0]}: {exc}") from exc
                count = sum(any(backups._member_belongs_to_artifact(name,a["member"]) for a in group) for name in expected)
                report["expected_restore_entries"] += count
                report["restored_entries"] += count
                report["restored_paths"].extend(f"{host}:{path}" for path in paths)
        complete = not any(report[key] for key in ("errors", "warnings", "skipped_conflicts", "skipped_newer_paths"))
        report.update(restore_complete=complete, fidelity_verified=complete)
        if cleanup_archive and complete:
            archive.unlink()
            report["archive_removed"] = True
    except (OSError, ValueError, tarfile.TarError) as exc:
        report["errors"].append(str(exc))
    if cleanup_archive and not report["archive_removed"]:
        report["warnings"].append("archive retained because restoration or fidelity verification was incomplete")
    return report
