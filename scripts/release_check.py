#!/usr/bin/env python3
"""Allowlisted source/packaging preflight. Diagnostics never echo a matched secret."""

import argparse
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile

DIRECTORIES = {'sub2easy', 'tests', 'examples', 'docs', 'scripts', '.github'}
FILES = {'README.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
         'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'uv.lock', 'package.json',
         'package-lock.json', 'start.command', '.gitignore', '.gitattributes'}
EXCLUDED_PARTS = {'__pycache__', '.pytest_cache', '.ruff_cache', 'node_modules'}
FORBIDDEN_PARTS = {'data', 'backups', 'exports', 'attachments', '.git', '.venv', 'node_modules'}
FORBIDDEN_SUFFIXES = {'.pyc', '.pyo', '.db', '.pem', '.key', '.p12', '.pfx', '.log', '.bak'}
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'signed-jwt': re.compile(r'eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'),
    'provider-api-key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'personal-filesystem-path': re.compile(r'/(?:Users|home)/[A-Za-z][^/\s"\x27]+/'),
    'literal-session-token': re.compile(r'#token=[A-Za-z0-9_-]{30,}'),
}
EMAIL = re.compile(r'[\w.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')


def source_files(root):
    for item in sorted(FILES):
        path=root/item
        if not path.is_file():raise ValueError(f'Missing release file: {item}')
        if path.is_symlink():raise ValueError(f'Symlink not allowed: {item}')
        yield path
    for folder in sorted(DIRECTORIES):
        base=root/folder
        if not base.is_dir() or base.is_symlink():raise ValueError(f'Missing or linked release folder: {folder}')
        for path in sorted(base.rglob('*')):
            rel=path.relative_to(root)
            if path.is_symlink():raise ValueError(f'Symlink not allowed: {rel}')
            if any(p in EXCLUDED_PARTS for p in rel.parts):continue
            if path.is_file():yield path


def inspect_bytes(name, raw):
    errors=[]
    p=PurePosixPath(name)
    if (p.is_absolute() or '..' in p.parts or any(v in FORBIDDEN_PARTS for v in p.parts)
            or p.name.startswith('.env') or p.name in {'launch-url.txt','.process.lock','.DS_Store'}
            or p.suffix in FORBIDDEN_SUFFIXES or '.sqlite' in p.name
            or p.name.endswith(('.sub2api.json','.reauthorized.json'))):
        errors.append(f'{name}: forbidden runtime/export file')
    try:text=raw.decode('utf-8')
    except UnicodeError:
        return errors+[f'{name}: unexpected binary file; review before publishing']
    for category,pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            line=text.count('\n',0,match.start())+1
            errors.append(f'{name}:{line}: {category}')
    for match in EMAIL.finditer(text):
        domain=match.group(1).lower()
        if not (domain.startswith('example.') or domain.endswith(('.invalid','.test','.example'))):
            errors.append(f'{name}:{text.count(chr(10),0,match.start())+1}: non-example email')
    return errors


def check_source(root):
    errors=[];paths=list(source_files(root))
    for path in paths:errors.extend(inspect_bytes(path.relative_to(root).as_posix(),path.read_bytes()))
    metadata=tomllib.loads((root/'pyproject.toml').read_text())['project']
    version=re.search(r"__version__ = ['\"]([^'\"]+)", (root/'sub2easy/__init__.py').read_text()).group(1)
    if metadata['version']!=version:errors.append('Version mismatch')
    if metadata.get('license')!='MIT':errors.append('Review the configured project license')
    if (root/'.git').exists():
        # Ignore rules do not remove already tracked secrets. Check the index too,
        # but do not initialize a repository or inspect parent repositories.
        tracked=subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True,check=True).stdout
        allowed={p.relative_to(root).as_posix() for p in paths}
        for raw_name in tracked.split(b'\0'):
            if not raw_name:continue
            name=raw_name.decode('utf-8')
            if name not in allowed:
                errors.append(f'{name}: tracked file outside release allowlist')
    return errors,paths


def check_archive(path):
    errors=[];count=0
    if path.suffix in {'.whl','.zip'}:
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if item.is_dir():continue
                count+=1
                if stat.S_ISLNK(item.external_attr>>16):errors.append(f'{item.filename}: symlink')
                errors.extend(inspect_bytes(item.filename,archive.read(item)))
    else:
        with tarfile.open(path,'r:*') as archive:
            for item in archive:
                if item.isdir():continue
                count+=1
                if not item.isfile():errors.append(f'{item.name}: non-regular archive member');continue
                errors.extend(inspect_bytes(item.name,archive.extractfile(item).read()))
    if not count:errors.append('Empty archive')
    return errors,count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--archive',type=Path,action='append',default=[])
    args=parser.parse_args()
    try:
        errors,paths=check_source(args.root.resolve());print(f'Source: {len(paths)} allowlisted files checked')
        for archive in args.archive:
            found,count=check_archive(archive);errors.extend(found);print(f'Artifact {archive.name}: {count} members checked')
    except (ValueError,OSError,tarfile.TarError,zipfile.BadZipFile) as exc:
        print(f'Release check failed: {exc}',file=sys.stderr);return 1
    for error in errors:print(error,file=sys.stderr)
    if errors:return 1
    print('PASS: no findings from configured checks (not a guarantee that all secrets are absent)')
    return 0


if __name__=='__main__':raise SystemExit(main())
