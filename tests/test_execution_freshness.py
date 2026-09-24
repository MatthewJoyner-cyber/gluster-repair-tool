# Copyright 2026 Matthew Joyner
# SPDX-License-Identifier: GPL-2.0-only
"""Saved object evidence must be checked again before repair dispatch."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gluster_heal_tool.apply_binding import BindingError, bind_manifest, bind_plan, bind_apply
from gluster_heal_tool.models import ResolutionObservation, ApplyActionResult, ApplyStep
from gluster_heal_tool.execution_freshness import compare_observations, validate_live_evidence


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.saved = ResolutionObservation(
            host='node-a', raw_entry='/item', backend='/srv/brick/item',
            backend_lexists=True, backend_exists=True, backend_lstat_type='file',
            backend_trusted_gfid='11111111-2222-4333-8444-555555555555',
            backend_size=4, backend_mtime=10, backend_ctime_ns=10000000001,
            backend_mtime_ns=10000000000, backend_inode=101,
        )

    def test_backend_change_is_refused_even_with_same_size_and_second_mtime(self):
        for field, value in [('backend_ctime_ns', 10000000002),
                             ('backend_inode', 102),
                             ('backend_trusted_gfid', 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'),
                             ('backend_acl_access_text', 'different'),
                             ('backend_exists', False)]:
            changed = copy.deepcopy(self.saved)
            setattr(changed, field, value)
            with self.subTest(field=field), self.assertRaises(BindingError):
                compare_observations([self.saved.to_dict()], [changed], 'node-a')

    def test_mount_probe_fields_are_not_backend_freshness(self):
        current = copy.deepcopy(self.saved)
        current.mounted_checked = True
        current.mounted_exists = True
        compare_observations([self.saved.to_dict()], [current], 'node-a')

    def test_missing_duplicate_or_error_reply_is_refused(self):
        error = copy.deepcopy(self.saved)
        error.error = 'connection lost'
        for reply in ([], [self.saved, self.saved], [error]):
            with self.subTest(reply=reply), self.assertRaises(BindingError):
                compare_observations([self.saved.to_dict()], reply, 'node-a')

    def test_older_evidence_without_precise_identity_is_refused(self):
        saved = self.saved.to_dict()
        saved.pop('backend_ctime_ns')
        with self.assertRaises(BindingError):
            compare_observations([saved], [self.saved], 'node-a')

    def test_real_bound_evidence_is_reprobed_and_changed_inode_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = {'objects': {'/item': {'observations': {'node-a': [self.saved.to_dict()]}}}}
            bind_manifest(evidence, volume='example',
                          volume_id='11111111-2222-4333-8444-555555555555',
                          bricks=[{'host': 'node-a', 'path': '/srv/brick', 'role': 'data'}])
            evidence_path = root / 'manifest.json'
            evidence_path.write_text(json.dumps(evidence))
            plan = {'actions': []}
            bind_plan(plan, evidence, evidence_path)
            plan_path = root / 'plan.json'
            plan_path.write_text(json.dumps(plan))
            result = ApplyActionResult(action_id='repair', logical_path='/item',
                action_type='repair_file', execution_mode='dry-run', backup_root='',
                backup_mode='none', batch=False, status='planned',
                steps=[ApplyStep(step_id='write', step_type='restore',
                                 command_preview=['synthetic-write'])])
            payload = {'actions': [result.to_dict()]}
            bind_apply(payload, plan, plan_path)
            with patch('gluster_heal_tool.manager._run_resolve_worker', return_value=[self.saved]) as worker:
                validate_live_evidence(payload, [result], ssh_user='service')
                request = worker.call_args.kwargs['request']
                self.assertFalse(request.probe_mount)
                self.assertEqual(['/item'], request.entries)
                self.assertEqual('/srv/brick', request.brick_path)
                changed = copy.deepcopy(self.saved)
                changed.backend_inode += 1
                worker.return_value = [changed]
                with self.assertRaises(BindingError):
                    validate_live_evidence(payload, [result], ssh_user='service')
                # A selected object must cover each brick. A host-keyed
                # manifest cannot safely distinguish two bricks on one host.
                for extra in ({'host': 'node-b', 'path': '/srv/brick', 'role': 'data'},
                              {'host': 'node-a', 'path': '/srv/second', 'role': 'data'}):
                    bind_manifest(evidence, volume='example',
                        volume_id='11111111-2222-4333-8444-555555555555',
                        bricks=[{'host': 'node-a', 'path': '/srv/brick', 'role': 'data'}, extra])
                    evidence_path.write_text(json.dumps(evidence))
                    bind_plan(plan, evidence, evidence_path)
                    plan_path.write_text(json.dumps(plan))
                    bind_apply(payload, plan, plan_path)
                    worker.reset_mock()
                    with self.subTest(extra=extra), self.assertRaises(BindingError):
                        validate_live_evidence(payload, [result], ssh_user='service')
                    worker.assert_not_called()
