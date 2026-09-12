#!/usr/bin/env python3
"""Build a clean, deterministic GitHub-ready source ZIP from the explicit allowlist."""
import argparse
import hashlib
from pathlib import Path
import tomllib
import zipfile

from release_check import check_archive, check_source


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    errors,files=check_source(root)
    if errors:raise SystemExit('\n'.join(errors))
    version=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
    output=args.output or root/'dist'/f'sub2easy-{version}-github-source.zip'
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise SystemExit('Output already exists; choose another --output or remove the old artifact explicitly')
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            rel=path.relative_to(root).as_posix()
            info=zipfile.ZipInfo(f'Sub2Easy-{version}/{rel}',date_time=(2026,1,1,0,0,0))
            info.external_attr=(0o100755 if rel=='start.command' else 0o100644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,path.read_bytes())
    errors,count=check_archive(output)
    if errors:output.unlink();raise SystemExit('\n'.join(errors))
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(f'{digest}  {output.name}\n')
    print(f'{output.resolve()}\n{count} files; SHA256 {digest}')


if __name__=='__main__':main()
