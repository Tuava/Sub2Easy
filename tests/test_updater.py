from contextlib import closing
import base64
import fcntl
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sub2easy import updater
from sub2easy.gui import create_app
from sub2easy.runtime import private_lock_file


class UpdateCheckTests(unittest.TestCase):
    def test_read_only_check_uses_exact_commit_and_no_credentials(self):
        sha='a'*40
        document=b'[project]\nname="sub2easy"\nversion="0.8.0"\n'
        with patch.object(updater,'read_json',side_effect=[{'sha':sha},{'encoding':'base64','content':base64.b64encode(document).decode()}]) as read,patch.object(updater,'checkout',return_value=False):
            result=updater.check()
        self.assertTrue(result['available']);self.assertFalse(result['automatic_install'])
        self.assertEqual(result['install_mode'],'package_or_source_zip')
        self.assertEqual(read.call_args_list[1].args[0],updater.API+'/contents/pyproject.toml?ref='+sha)
    def test_invalid_commit_and_wrong_project_rejected(self):
        with patch.object(updater,'read_json',return_value={'sha':'bad'}):
            with self.assertRaisesRegex(updater.UpdateError,'UPDATE_RESPONSE_INVALID'):updater.check()
        document=b'[project]\nname="other"\nversion="0.8.0"\n'
        with patch.object(updater,'read_json',side_effect=[{'sha':'a'*40},{'encoding':'base64','content':base64.b64encode(document).decode()}]):
            with self.assertRaisesRegex(updater.UpdateError,'UPDATE_RESPONSE_INVALID'):updater.check()
    def test_version_strictly_numeric(self):
        self.assertGreater(updater.version_tuple('0.10.0'),updater.version_tuple('0.9.9'))
        for v in ['latest','0.7.0;rm',None,'1.0.0-rc1']:
            with self.assertRaises(updater.UpdateError):updater.version_tuple(v)
    def test_github_rate_limit_uses_token_free_fallback(self):
        with patch.object(updater,'read_json',side_effect=updater.UpdateError('UPDATE_RATE_LIMITED')),\
             patch.object(updater.shutil,'which',return_value='/synthetic/git'),\
             patch.object(updater,'raw_metadata',return_value=('a'*40,'[project]\nname="sub2easy"\nversion="0.8.0"\n')) as fallback,\
             patch.object(updater,'checkout',return_value=False):
            result=updater.check()
        self.assertTrue(result['available']);fallback.assert_called_once()
    def test_gui_check_is_cached_even_while_vault_locked_and_never_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(directory,token='TEST',start_worker=False)
            with TestClient(app,base_url='http://127.0.0.1:8765',headers={'x-local-token':'TEST'}) as c:
                info={'commit':'a'*40,'current_version':'0.7.0','latest_version':'0.8.0','available':True,'install_mode':'git'}
                with patch.object(updater,'check',return_value=info) as check,patch.object(updater,'apply') as apply:
                    first=c.post('/api/updates/check',json={});second=c.post('/api/updates/check',json={})
                    self.assertEqual(first.status_code,200,first.text);self.assertEqual(second.status_code,200)
                    check.assert_called_once();apply.assert_not_called()
                    self.assertIn('--data-dir',first.json()['update_command'])
                self.assertEqual(c.post('/api/updates/check',json={},headers={'x-local-token':'bad'}).status_code,401)


class UpdateApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'source';self.root.mkdir()
        self.data=Path(self.tmp.name)/'vault';self.data.mkdir()
        self.remote=Path(self.tmp.name)/'upstream';self.remote.mkdir()
        for root in [self.root,self.remote]:
            self.git(root,'init','-b','main');self.git(root,'config','user.email','test@example.invalid');self.git(root,'config','user.name','Synthetic')
        (self.remote/'version.txt').write_text('old');self.git(self.remote,'add','.');self.git(self.remote,'commit','-m','old')
        self.git(self.root,'fetch',str(self.remote),'main');self.git(self.root,'reset','--hard','FETCH_HEAD')
        self.git(self.root,'remote','add','origin',updater.REMOTE)
        self.old=self.git(self.root,'rev-parse','HEAD')
        (self.remote/'version.txt').write_text('new');self.git(self.remote,'commit','-am','new');self.new=self.git(self.remote,'rev-parse','HEAD')
        with closing(sqlite3.connect(self.data/'vault.sqlite3')) as db:
            db.execute('CREATE TABLE sample (value TEXT)');db.execute("INSERT INTO sample VALUES ('opaque-encrypted-material')");db.commit()
        self.real_git=updater.git
    def tearDown(self):self.tmp.cleanup()
    @staticmethod
    def git(root,*args):return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()
    def local_git(self,root,*args):
        if args[0]=='fetch':args=('fetch','--no-tags',str(self.remote),self.new)
        return self.real_git(root,*args)
    def apply(self,**kw):
        with patch.object(updater,'check',return_value={'commit':self.new,'latest_version':'0.8.0'}),patch.object(updater,'git',side_effect=self.local_git),patch.object(updater.shutil,'which',return_value='/synthetic/tool'):
            return updater.apply(self.root,self.data,**kw)
    def test_fast_forward_backs_up_database_and_preserves_values(self):
        with patch.object(updater,'sync') as sync:r=self.apply(expected_commit=self.new)
        self.assertEqual(r['state'],'updated');self.assertEqual(self.git(self.root,'rev-parse','HEAD'),self.new)
        sync.assert_called_once()
        with closing(sqlite3.connect(r['backup'])) as db:self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],'opaque-encrypted-material')
        self.assertEqual((self.root/'version.txt').read_text(),'new')
        self.assertTrue((self.data/'last-update.json').exists())
    def test_active_service_lock_blocks_before_network(self):
        with private_lock_file(self.data/'.process.lock') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(updater,'check') as check:
                with self.assertRaisesRegex(updater.UpdateError,'UPDATE_STOP_SERVICE_FIRST'):updater.apply(self.root,self.data)
                check.assert_not_called()
    def test_dirty_source_not_overwritten(self):
        (self.root/'version.txt').write_text('local edit')
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_DIRTY_CHECKOUT'):self.apply()
        self.assertEqual((self.root/'version.txt').read_text(),'local edit')
    def test_wrong_origin_and_branch_refused(self):
        self.git(self.root,'remote','set-url','origin','https://example.invalid/fork.git')
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_WRONG_ORIGIN'):self.apply()
        self.git(self.root,'remote','set-url','origin',updater.REMOTE);self.git(self.root,'checkout','-b','custom')
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_REQUIRES_MAIN_BRANCH'):self.apply()
    def test_changed_confirmed_target_does_not_merge(self):
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_TARGET_CHANGED'):self.apply(expected_commit='b'*40)
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),self.old)
    def test_dependency_failure_rolls_back_without_deleting_database(self):
        with patch.object(updater,'sync',side_effect=[updater.UpdateError('UPDATE_DEPENDENCIES_FAILED'),None]):
            with self.assertRaisesRegex(updater.UpdateError,'UPDATE_ROLLED_BACK'):self.apply()
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),self.old)
        self.assertTrue((self.data/'vault.sqlite3').exists())
        self.assertEqual(len(list((self.data/'backups').rglob('vault.sqlite3'))),1)
    def test_diverged_local_commit_refused(self):
        (self.root/'mine.txt').write_text('keep');self.git(self.root,'add','.');self.git(self.root,'commit','-m','local commit')
        head=self.git(self.root,'rev-parse','HEAD')
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_DIVERGED'):self.apply()
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),head)
    def test_source_zip_no_git_refused(self):
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_REQUIRES_GIT_CHECKOUT'):updater.apply(self.data,self.data)
    def test_backup_symlink_refused_without_code_change(self):
        (self.data/'backups').symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(updater.UpdateError,'UPDATE_INVALID_DATA_PATH'):self.apply()
        self.assertEqual(self.git(self.root,'rev-parse','HEAD'),self.old)


if __name__=='__main__':unittest.main()
