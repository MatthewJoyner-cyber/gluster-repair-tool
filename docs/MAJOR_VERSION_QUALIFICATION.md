# Qualifying a new Gluster major version

Qualify the exact Gluster package and tool revision on a disposable lab before
adding a compatibility claim. A new major number is a reason to check the
interfaces the tool uses, not to replay every historical Gluster bug. Keep the
existing feature gates closed until their specific commands and responses have
separate live evidence.

1. **Define the claim.** Record the OS release, package source and exact
   Gluster/Python versions, brick filesystem, topology, tool commit and enabled
   features. Decide whether the result is read-only compatibility, one repair
   recipe, or a particular native-command feature. Do not generalize one
   package build to an entire major line or to other distributions.
2. **Review relevant changes.** Read the official release and upgrade notes
   from the last qualified major through the candidate. Prioritize changed or
   deprecated CLI forms, output fields, privilege/package layout, volume type,
   heal behavior, metadata/xattrs, and any safety or data-integrity fix that
   affects a tool assumption. Record what changed and why it matters. A bug
   fixed by native healing needs a scoped observation only when an existing
   fixture matches; it does not require a new repair canary.
3. **Check the contracts.** Run focused offline fixtures for version parsing,
   volume info and brick roles, status/heal-info connection and entry counts,
   resolver responses, and worker metadata as relevant to the claim. Preserve
   fail-closed behavior for missing or unfamiliar output. Add a fixture only
   when the new release actually differs or exposes a gap.
4. **Run a small live pass.** Install the selected source snapshot using the
   normal bootstrap on a controlled lab, or reuse retained guests after
   verifying their identities and installed hashes. Verify healthy peer/brick
   state, then run a read-only health check and zero-action preview on a normal
   replica volume. For a repair-write claim, first let ordinary native healing
   finish on an existing representative case. If it clears the case, record a
   native-heal result and a zero-action tool preview. A separate mechanical
   repair check may use an existing fault builder in an isolated lab with
   healing temporarily held off. Label it operator-seeded, collect fresh tool
   evidence independent of builder state, then review the preview, backups and
   source agreement before one execution. Independently check every affected
   brick, restore normal healing and confirm convergence. Test an additional
   topology, such as replica 2 or an arbiter, only for a stated claim or a
   relevant release-note concern. Do not manufacture new faults merely to
   increase coverage.
5. **Close and report.** Restore heal settings, remove only named fixtures,
   verify all lab volumes are connected and quiet, and follow the lab's
   retention instructions. Keep raw topology and logs private. Record the
   exact command/output evidence, what was independently verified, unresolved
   ambiguity, and whether the result is native-heal observation, read-only
   compatibility, operator-seeded repair, or an enabled feature qualification.
   Update the compatibility table and exact-version feature profile only for
   claims the evidence supports.

A failed or ambiguous safety check blocks the affected write claim; it need
not block publishing read-only findings. Full-heal and native split-brain
resolver execution require their own command/response qualification before
their gates are opened. See [current compatibility](GLUSTER_COMPATIBILITY.md),
[validation evidence](VALIDATION.md), and the
[first-beta plan](FIRST_BETA_RELEASE_PLAN.md) for the present boundary.
