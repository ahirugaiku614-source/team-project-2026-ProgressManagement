"""成果物の範囲・整合性・既存領域非上書きを合成archiveで検証する。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from build_handoff import allowed,build,ROOT
from unpack_handoff import unpack
from unittest.mock import patch


class HandoffTests(unittest.TestCase):
    def setUp(self):
        directory=ROOT/'frontend'/'artifacts'
        directory.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=directory,prefix='handoff-test-')
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def archive(self,name='backend/app/synthetic.py',body=b'synthetic',digest=None,duplicate=False):
        path=self.root/'synthetic.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr(name,body)
            if duplicate:archive.writestr(name,body)
            archive.writestr('stage10-manifest.json',json.dumps({'version':1,'files':{name:digest or hashlib.sha256(body).hexdigest()}}))
        return path

    def test_source_allowlist_excludes_runtime_credentials_and_outputs(self):
        for path in ('backend/app/main.py','backend/migrations/011_saved_snapshot_sessions.sql','docs/team-development-stage10c.md','frontend/src/style.css','frontend/public/favicon.svg','frontend/src/assets/hero.png'):
            self.assertTrue(allowed(path))
        for path in ('backend/.env','backend/firebase-credentials.json','frontend/dist/index.html','frontend/node_modules/a.js','frontend/artifacts/demo.html','tmp/a.py','backend/app/test.sqlite'):
            self.assertFalse(allowed(path))

    def test_verified_archive_extracts_only_to_new_directory(self):
        dest=self.root/'new'
        self.assertEqual(unpack(self.archive(),dest),1)
        self.assertEqual((dest/'backend/app/synthetic.py').read_bytes(),b'synthetic')

    def test_existing_destination_is_untouched(self):
        dest=self.root/'keep';dest.mkdir();(dest/'evidence').write_bytes(b'must keep')
        with self.assertRaises(ValueError):unpack(self.archive(),dest)
        self.assertEqual((dest/'evidence').read_bytes(),b'must keep')

    def test_traversal_is_refused_before_creating_destination(self):
        dest=self.root/'new'
        with self.assertRaises(ValueError):unpack(self.archive('../outside'),dest)
        self.assertFalse(dest.exists())

    def test_windows_drive_path_is_refused(self):
        dest=self.root/'new'
        with self.assertRaises(ValueError):unpack(self.archive('C:/outside'),dest)
        self.assertFalse(dest.exists())

    def test_mismatch_is_refused_before_creating_destination(self):
        dest=self.root/'new'
        with self.assertRaises(ValueError):unpack(self.archive(digest='0'*64),dest)
        self.assertFalse(dest.exists())

    def test_duplicate_entries_are_refused(self):
        with self.assertRaises(ValueError):unpack(self.archive(duplicate=True),self.root/'new')

    def test_output_outside_artifacts_is_refused(self):
        with self.assertRaises(ValueError):build(ROOT/'must-not-create.zip')
        self.assertFalse((ROOT/'must-not-create.zip').exists())
        with self.assertRaises(ValueError):build(ROOT/'frontend/artifacts/../../must-not-create.zip')

    def test_private_provision_does_not_run_on_windows(self):
        from provision_private import provision
        with patch('provision_private.sys.platform','win32'):
            with self.assertRaises(ValueError):provision(self.root,'tls')
        self.assertFalse(any(self.root.iterdir()))

    def test_namespace_setup_does_not_run_on_windows(self):
        from prepare_layout import prepare
        with patch('prepare_layout.sys.platform','win32'):
            with self.assertRaises(ValueError):prepare(self.root,{})
        self.assertFalse(any(self.root.iterdir()))

    def test_gateway_render_preserves_native_nginx_variables(self):
        from render_gateway import render_file
        a='11111111-1111-4111-8111-111111111111';b='22222222-2222-4222-8222-222222222222'
        output=self.root/'gateway.conf';render_file(a,b,output)
        text=output.read_text(encoding='utf-8')
        self.assertIn('$http_cookie',text);self.assertNotIn('${WORKSPACE_A_ID}',text)
        with self.assertRaises(ValueError):render_file(a,b,output)

    def test_gateway_render_refuses_configuration_injection(self):
        from render_gateway import render_file
        with self.assertRaises(ValueError):render_file('invalid; proxy_pass unsafe','also-invalid',self.root/'gateway.conf')
        self.assertFalse((self.root/'gateway.conf').exists())


if __name__=='__main__':unittest.main()
