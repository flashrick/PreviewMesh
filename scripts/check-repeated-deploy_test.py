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


if __name__ == '__main__':
    unittest.main()
