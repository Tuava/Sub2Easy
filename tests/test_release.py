from pathlib import Path
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.release_check import check_archive, check_source, inspect_bytes
from sub2easy.runtime import default_data_dir, private_lock_file, write_private_launcher


class RuntimeReleaseTests(unittest.TestCase):
    def test_wheel_data_path_is_user_owned_not_site_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);installed=root/'environment'/'site-packages'/'sub2easy'/'runtime.py'
            with patch('sub2easy.runtime.__file__',str(installed)),patch('sub2easy.runtime.Path.home',return_value=root):
                with patch('sub2easy.runtime.sys.platform','darwin'):
                    self.assertEqual(default_data_dir(),root/'Library'/'Application Support'/'Sub2Easy')
                with patch('sub2easy.runtime.sys.platform','linux'),patch.dict(os.environ,{'XDG_DATA_HOME':str(root/'xdg')}):
                    self.assertEqual(default_data_dir(),root/'xdg'/'sub2easy')
                with patch('sub2easy.runtime.sys.platform','linux'),patch.dict(os.environ,{'XDG_DATA_HOME':'relative'}):
                    self.assertEqual(default_data_dir(),root/'.local'/'share'/'sub2easy')

    def test_existing_checkout_vault_never_silently_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir();(root/'data'/'vault.sqlite3').touch();(root/'pyproject.toml').touch()
            with patch('sub2easy.runtime.__file__',str(root/'sub2easy'/'runtime.py')):
                self.assertEqual(default_data_dir(),root.resolve()/'data')

    def test_launcher_write_is_private_and_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);other=root/'other';other.write_text('unchanged')
            launcher=root/'launch-url.txt';launcher.symlink_to(other)
            write_private_launcher(launcher,'synthetic-value')
            self.assertFalse(launcher.is_symlink());self.assertEqual(other.read_text(),'unchanged')
            self.assertEqual(stat.S_IMODE(launcher.stat().st_mode),0o600)
            launcher.chmod(0o644);write_private_launcher(launcher,'replacement')
            self.assertEqual(stat.S_IMODE(launcher.stat().st_mode),0o600)

    def test_lock_rejects_symlink_and_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);other=root/'other';other.write_text('unchanged');lock=root/'.process.lock'
            lock.symlink_to(other)
            with self.assertRaises(OSError):private_lock_file(lock)
            lock.unlink();os.link(other,lock)
            with self.assertRaises(OSError):private_lock_file(lock)
            self.assertEqual(other.read_text(),'unchanged')
            lock.unlink()
            with private_lock_file(lock):self.assertEqual(stat.S_IMODE(lock.stat().st_mode),0o600)


class ReleaseScannerTests(unittest.TestCase):
    def test_source_allowlist_never_selects_working_vault_or_builds(self):
        root=Path(__file__).resolve().parents[1]
        errors,paths=check_source(root)
        self.assertEqual(errors,[])
        self.assertTrue(paths)
        for p in paths:self.assertNotIn(p.relative_to(root).parts[0],{'data','dist','.venv','node_modules'})

    def test_findings_do_not_echo_secret_values(self):
        secret='ghp_'+'A'*40
        result=inspect_bytes('example.txt',secret.encode())
        self.assertTrue(result);self.assertNotIn(secret,' '.join(result))

    def test_rejects_database_exports_and_signed_session(self):
        for name in ['data/vault.sqlite3','docs/export.sub2api.json','launch-url.txt','.env.production']:
            self.assertTrue(inspect_bytes(name,b'{}'))
        signed='eyJ'+'A'*40+'.'+'B'*40+'.'+'C'*40
        self.assertTrue(inspect_bytes('config.txt',signed.encode()))
        self.assertFalse(inspect_bytes('examples/logins.txt',b'demo@example.invalid----SYNTHETIC_PASSWORD'))

    def test_archive_scan_rejects_traversal_or_embedded_runtime_file(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/'release.zip'
            with zipfile.ZipFile(archive,'w') as output:
                output.writestr('../config','{}');output.writestr('release/data/vault.sqlite3',b'{}')
            errors,count=check_archive(archive)
            self.assertEqual(count,2);self.assertEqual(len(errors),2)


if __name__=='__main__':unittest.main()
