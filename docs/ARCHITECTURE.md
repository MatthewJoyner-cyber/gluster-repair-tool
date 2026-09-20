# Architecture

| Responsibility | Modules |
| --- | --- |
| Public entry points | gluster-manager.py, gluster-heal-tool.py, gluster-worker.py |
| Discovery and transport | manager, resolver, worker, protocol, volume |
| Evidence and identity | manifest, evidence_provenance, role_safety |
| Planning | planner, planner_graph, planner_payload, execution_plan |
| Apply construction | apply, apply_planning and family modules |
| Execution and reporting | executor, apply_reporting, controller_cycle_report |
| Operator interaction | simple_mode, simple_interactive, simple_assistants |
| Preservation | backup_maintenance, remote_ops |
| Health and verification | health, heal_guard, temp_mount, heal_performance |
| Disposable fixtures | canary and family modules |
| Local storage | controller_paths, status, shared_io |

The manager coordinates evidence and plans. Workers collect bounded host-side
facts or perform named operations. SSH is transport; repair reasoning belongs
in the planner. Apply artifacts materialize proposed commands. Execution must
not infer a new repair when a command or prerequisite fails.

This describes the intended division and existing modules. The open review
documents places where implementation does not yet satisfy that contract.
The agent companion delegates to this CLI and never implements another healer.
