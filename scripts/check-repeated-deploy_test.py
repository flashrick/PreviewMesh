#!/usr/bin/env python3
"""Regression checks for duplicate detection without a Kubernetes runtime."""

import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'repeated_deploy', Path(__file__).with_name('check-repeated-deploy.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DuplicateDetectionTest(unittest.TestCase):
    def setUp(self):
        self.ns = 'pm-r123-pr4'
        self.items = [self.object(kind) for kind in
                      ('Namespace', 'Deployment', 'Service', 'Ingress')]

    def object(self, kind):
        metadata = {'name': self.ns}
        if kind == 'Namespace':
            metadata['labels'] = {'previewmesh.local/repository-id': '123',
                                  'previewmesh.local/pr-number': '4'}
        else:
            metadata['namespace'] = self.ns
        return {'kind': kind, 'metadata': metadata}

    def check(self, items):
        return module.check_duplicates(items, self.ns, '123', '4')

    def test_stable_inventory_and_unrelated_preview(self):
        other = {'kind': 'Service', 'metadata': {'name': 'other', 'namespace': 'other'}}
        items = self.items + [other]
        self.assertEqual(self.check(items), self.check(list(reversed(items))))
        self.assertNotEqual(self.check(items), self.check(self.items))
        self.assertEqual(
            self.check(module.metadata_inventory(items)),
            self.check(items),
        )

    def test_duplicate_namespace_identity(self):
        duplicate = copy.deepcopy(self.items[0])
        duplicate['metadata']['name'] = 'alias'
        with self.assertRaisesRegex(RuntimeError, 'Namespace'):
            self.check(self.items + [duplicate])

    def test_duplicates_with_missing_or_changed_labels(self):
        for kind in ('Deployment', 'Service', 'Ingress'):
            for location in ('inside', 'same-name', 'release-label', 'helm-annotation'):
                with self.subTest(kind=kind, location=location):
                    duplicate = self.object(kind)
                    metadata = duplicate['metadata']
                    if location != 'same-name':
                        metadata['name'] = 'alias'
                    if location != 'inside':
                        metadata['namespace'] = 'other'
                    if location == 'release-label':
                        metadata['labels'] = {'app.kubernetes.io/instance': self.ns}
                    if location == 'helm-annotation':
                        metadata['annotations'] = {'meta.helm.sh/release-name': self.ns}
                    with self.assertRaisesRegex(RuntimeError, kind):
                        self.check(self.items + [duplicate])

    def test_missing_kind(self):
        for item in self.items:
            with self.subTest(kind=item['kind']):
                with self.assertRaisesRegex(RuntimeError, item['kind']):
                    self.check([other for other in self.items if other is not item])


class IdentityEvidenceTest(unittest.TestCase):
    def test_metadata_inventory_excludes_payloads_and_keeps_finalizers(self):
        secret = {
            'kind': 'Secret',
            'metadata': {
                'namespace': 'pm-r123-pr4',
                'name': 'image-pull',
                'uid': 'uid-secret',
                'finalizers': ['example/finalizer'],
                'labels': {'purpose': 'image-pull'},
                'annotations': {'private': 'not-retained', 'previewmesh.local/state': 'ready'},
            },
            'data': {'token': 'must-not-be-retained'},
        }

        inventory = module.metadata_inventory([secret], 'pm-r123-pr4')

        self.assertEqual(inventory[0]['uid'], 'uid-secret')
        self.assertEqual(inventory[0]['finalizers'], ['example/finalizer'])
        self.assertNotIn('data', inventory[0])
        self.assertNotIn('private', inventory[0]['annotations'])

    def test_identity_comparison_allows_only_the_requested_recreated_kind(self):
        namespace = 'pm-r123-pr4'
        before = [
            {'kind': 'Deployment', 'namespace': namespace, 'name': 'preview', 'uid': 'deployment-a'},
            {'kind': 'Service', 'namespace': namespace, 'name': 'preview', 'uid': 'service-a'},
        ]
        after = [dict(item) for item in before]
        after[0]['uid'] = 'deployment-b'

        module.compare_identity_indexes(before, after, 'Deployment')
        with self.assertRaisesRegex(RuntimeError, 'identity change'):
            module.compare_identity_indexes(before, after, 'Service')

    def test_helm_history_is_append_only_and_revision_unique(self):
        before = [{'name': 'sh.helm.release.v1.preview.v1', 'uid': 'helm-1', 'revision': '1'}]
        after = before + [{'name': 'sh.helm.release.v1.preview.v2', 'uid': 'helm-2', 'revision': '2'}]

        comparison = module.compare_helm_history(before, after)

        self.assertEqual(comparison['before_count'], 1)
        self.assertEqual(comparison['added_names'], ['sh.helm.release.v1.preview.v2'])
        with self.assertRaisesRegex(RuntimeError, 'removed'):
            module.compare_helm_history(after, before)
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            module.compare_helm_history(before, before + [dict(before[0])])

if __name__ == '__main__':
    unittest.main()
