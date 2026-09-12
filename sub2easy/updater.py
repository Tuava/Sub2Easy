"""Explicit Git-checkout updates from the fixed project upstream; never auto-install."""

import argparse
import base64
from datetime import datetime, timezone
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib

import httpx

from sub2easy import __version__
from sub2easy.runtime import private_lock_file, write_private_launcher

REPOSITORY = 'Tuava/Sub2Easy'
REMOTE = 'https://github.com/Tuava/Sub2Easy.git'
REPO_URL = 'https://github.com/Tuava/Sub2Easy'
API = 'https://api.github.com/repos/' + REPOSITORY
ROOT = Path(__file__).resolve().parents[1]


class UpdateError(ValueError):
    pass


def version_tuple(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d+\.\d+\.\d+',value):
        raise UpdateError('UPDATE_VERSION_INVALID')
    return tuple(map(int,value.split('.')))


def git(root, *args):
    try:
        result=subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,timeout=120,
                              env={**os.environ,'GIT_TERMINAL_PROMPT':'0'})
    except (OSError,subprocess.TimeoutExpired):raise UpdateError('UPDATE_GIT_UNAVAILABLE') from None
    if result.returncode:raise UpdateError('UPDATE_GIT_FAILED')
    return result.stdout.strip()


def checkout(root=ROOT):
    if not shutil.which('git') or not (root/'.git').exists():return False
    try:return Path(git(root,'rev-parse','--show-toplevel')).resolve()==root.resolve()
    except UpdateError:return False


def read_json(url):
    try:
        with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as client:
            with client.stream('GET',url,headers={'Accept':'application/vnd.github+json',
                                'User-Agent':'Sub2Easy-update/'+__version__}) as response:
                if response.status_code in {403,429}:raise UpdateError('UPDATE_RATE_LIMITED')
                if response.status_code!=200:raise UpdateError('UPDATE_CHECK_FAILED')
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>1024*1024:raise UpdateError('UPDATE_RESPONSE_INVALID')
                value=json.loads(raw)
                if not isinstance(value,dict):raise UpdateError('UPDATE_RESPONSE_INVALID')
                return value
    except (httpx.HTTPError,ValueError) as exc:
        if isinstance(exc,UpdateError):raise
        raise UpdateError('UPDATE_CHECK_FAILED') from None


def raw_metadata(root):
    """GitHub REST rate-limit fallback. Fixed origin + immutable raw commit, no token."""
    listing=git(root,'ls-remote',REMOTE,'refs/heads/main').split()
    if len(listing)!=2 or listing[1]!='refs/heads/main' or not re.fullmatch('[a-f0-9]{40}',listing[0]):
        raise UpdateError('UPDATE_RESPONSE_INVALID')
    sha=listing[0]
    try:
        with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as client:
            with client.stream('GET','https://raw.githubusercontent.com/'+REPOSITORY+'/'+sha+'/pyproject.toml') as response:
                if response.status_code!=200:raise UpdateError('UPDATE_CHECK_FAILED')
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>65536:raise UpdateError('UPDATE_RESPONSE_INVALID')
        return sha,raw.decode()
    except (httpx.HTTPError,UnicodeError):raise UpdateError('UPDATE_CHECK_FAILED') from None


def check(root=ROOT):
    try:
        info=read_json(API+'/commits/main');sha=info.get('sha')
        if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{40}',sha):raise UpdateError('UPDATE_RESPONSE_INVALID')
        content=read_json(API+'/contents/pyproject.toml?ref='+sha)
        if content.get('encoding')!='base64':raise UpdateError('UPDATE_RESPONSE_INVALID')
        try:metadata=base64.b64decode(content['content'],validate=False).decode()
        except (KeyError,TypeError,ValueError,UnicodeError):raise UpdateError('UPDATE_RESPONSE_INVALID') from None
    except UpdateError as exc:
        if str(exc) not in {'UPDATE_RATE_LIMITED','UPDATE_CHECK_FAILED'} or not shutil.which('git'):raise
        sha,metadata=raw_metadata(root)
    try:
        document=tomllib.loads(metadata)
        version=document['project']['version'];remote_version=version_tuple(version)
        if document['project']['name']!='sub2easy':raise ValueError()
    except (KeyError,TypeError,ValueError,UnicodeError):raise UpdateError('UPDATE_RESPONSE_INVALID') from None
    source=checkout(root);head=git(root,'rev-parse','HEAD') if source else None
    return {'current_version':__version__,'latest_version':version,'commit':sha,'current_commit':head,
            'available':remote_version>version_tuple(__version__) or (source and head!=sha and remote_version>=version_tuple(__version__)),
            'install_mode':'git' if source else 'package_or_source_zip','channel':'main',
            'repository':REPO_URL,'commit_url':REPO_URL+'/commit/'+sha,
            'checked_at':time.time(),'automatic_install':False}


def clean_checkout(root):
    if not checkout(root):raise UpdateError('UPDATE_REQUIRES_GIT_CHECKOUT')
    if git(root,'symbolic-ref','--short','HEAD')!='main':raise UpdateError('UPDATE_REQUIRES_MAIN_BRANCH')
    origin=git(root,'remote','get-url','origin')
    if origin not in {REMOTE,REPO_URL,'git@github.com:Tuava/Sub2Easy.git'}:
        raise UpdateError('UPDATE_WRONG_ORIGIN')
    if git(root,'status','--porcelain','--untracked-files=normal'):
        raise UpdateError('UPDATE_DIRTY_CHECKOUT')


def sync(root):
    try:
        result=subprocess.run(['uv','sync','--locked'],cwd=root,capture_output=True,text=True,timeout=300)
    except (OSError,subprocess.TimeoutExpired):raise UpdateError('UPDATE_DEPENDENCIES_FAILED') from None
    if result.returncode:raise UpdateError('UPDATE_DEPENDENCIES_FAILED')


def backup_vault(data):
    path=data/'vault.sqlite3'
    if not path.exists():return None
    if path.is_symlink():raise UpdateError('UPDATE_INVALID_DATA_PATH')
    if (data/'backups').is_symlink():raise UpdateError('UPDATE_INVALID_DATA_PATH')
    (data/'backups').mkdir(exist_ok=True,mode=0o700)
    (data/'backups').chmod(0o700)
    destination=data/'backups'/('before-update-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    destination.mkdir(parents=True,mode=0o700)
    backup=destination/'vault.sqlite3'
    # Use SQLite backup, not a raw copy that could omit WAL transactions. No decryption.
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
        with closing(sqlite3.connect(backup)) as target:source.backup(target)
    backup.chmod(0o600)
    return str(backup)


def apply(root, data, expected_commit=None):
    root=root.resolve();data=data.expanduser().absolute()
    clean_checkout(root)
    if not shutil.which('uv'):raise UpdateError('UPDATE_UV_REQUIRED')
    data.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:lock=private_lock_file(data/'.process.lock')
    except OSError:raise UpdateError('UPDATE_INVALID_DATA_PATH') from None
    with lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise UpdateError('UPDATE_STOP_SERVICE_FIRST') from None
        # A second check after acquiring the process lock closes the normal startup race.
        clean_checkout(root)
        latest=check(root)
        if expected_commit and expected_commit!=latest['commit']:raise UpdateError('UPDATE_TARGET_CHANGED')
        old=git(root,'rev-parse','HEAD')
        if old==latest['commit']:return {'state':'up_to_date','version':latest['latest_version'],'commit':old}
        if version_tuple(latest['latest_version'])<version_tuple(__version__):raise UpdateError('UPDATE_DOWNGRADE_BLOCKED')
        git(root,'fetch','--no-tags',REMOTE,latest['commit'])
        fetched=git(root,'rev-parse','FETCH_HEAD')
        if fetched!=latest['commit']:raise UpdateError('UPDATE_TARGET_CHANGED')
        try:git(root,'merge-base','--is-ancestor',old,fetched)
        except UpdateError:raise UpdateError('UPDATE_DIVERGED') from None
        backup=backup_vault(data)
        git(root,'merge','--ff-only',fetched)
        try:sync(root)
        except UpdateError:
            # --keep refuses rather than overwriting concurrent local edits. Never reset --hard.
            try:
                if git(root,'rev-parse','HEAD')!=fetched:raise UpdateError('UPDATE_ROLLBACK_NEEDS_REVIEW')
                git(root,'reset','--keep',old)
                sync(root)
            except UpdateError:raise UpdateError('UPDATE_ROLLBACK_NEEDS_REVIEW') from None
            raise UpdateError('UPDATE_ROLLED_BACK') from None
        result={'state':'updated','version':latest['latest_version'],'commit':fetched,
                'previous_commit':old,'backup':backup,'restart_required':True}
        receipt=data/'last-update.json'
        write_private_launcher(receipt,json.dumps(result,ensure_ascii=False,indent=2))
        return result


MESSAGES={
    'UPDATE_STOP_SERVICE_FIRST':'先停止使用这个数据目录的 Sub2Easy 后端；关闭网页不算停止服务。',
    'UPDATE_DIRTY_CHECKOUT':'源码有本地修改或未跟踪文件，请先自行提交/备份；更新器不会覆盖或 stash。',
    'UPDATE_REQUIRES_MAIN_BRANCH':'仅支持 main 分支快进更新；自定义分支请自行合并。',
    'UPDATE_WRONG_ORIGIN':'origin 不是 Tuava/Sub2Easy，请自行管理该 fork 的更新。',
    'UPDATE_REQUIRES_GIT_CHECKOUT':'当前不是 Git clone 安装。请备份数据后安装新 wheel，或另建 Git clone 并继续指定原数据目录。',
    'UPDATE_DIVERGED':'本地提交已分叉，拒绝重置或覆盖。请自行合并。',
    'UPDATE_ROLLED_BACK':'新依赖同步失败，源码与旧依赖已回滚；数据库备份已保留。',
    'UPDATE_ROLLBACK_NEEDS_REVIEW':'更新失败且自动回滚未完成，请查看 Git 状态和数据目录 backups；不要重复强制更新。',
    'UPDATE_UV_REQUIRED':'请先安装 uv。',
    'UPDATE_RATE_LIMITED':'GitHub 版本接口限流；稍后重试，不需要填写 GitHub Token。',
    'UPDATE_CHECK_FAILED':'无法读取 GitHub 版本信息，请检查网络。',
}


def main():
    parser=argparse.ArgumentParser(description='Sub2Easy 检查/更新（固定 Tuava/Sub2Easy main，只快进、不删除数据）')
    parser.add_argument('--apply',action='store_true',help='停服务后执行更新；默认只检查版本')
    parser.add_argument('--yes',action='store_true',help='确认备份、快进更新源码并同步依赖')
    parser.add_argument('--data-dir',type=Path,help='执行更新时必填，必须与服务启动参数一致')
    parser.add_argument('--expected-commit',help='只允许安装本次检查确认的提交SHA')
    args=parser.parse_args()
    if args.apply and (not args.yes or args.data_dir is None):
        parser.error('--apply 必须同时提供 --yes 和与服务一致的 --data-dir')
    os.umask(0o077)
    try:
        result=apply(ROOT,args.data_dir,args.expected_commit) if args.apply else check()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (UpdateError,OSError,sqlite3.Error) as exc:
        code=str(exc) if isinstance(exc,UpdateError) else 'UPDATE_LOCAL_IO_FAILED'
        print(code+': '+MESSAGES.get(code,'更新未完成；保留原始数据，检查安装方式/网络后重试。'),file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
