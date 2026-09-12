"""POSIX desktop data paths and private launcher files (macOS/Linux)."""

import os
from pathlib import Path
import stat
import sys
import tempfile


def default_data_dir():
    # Preserve a source checkout's existing vault; never move or copy it implicitly.
    checkout = Path(__file__).resolve().parents[1]
    legacy = checkout / 'data'
    if (checkout / 'pyproject.toml').is_file() and (legacy / 'vault.sqlite3').is_file():
        return legacy
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Sub2Easy'
    xdg = Path(os.environ.get('XDG_DATA_HOME', ''))
    return (xdg if xdg.is_absolute() else Path.home() / '.local' / 'share') / 'sub2easy'


def private_lock_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise OSError('Invalid process lock file')
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a+')
    except BaseException:
        os.close(fd)
        raise


def write_private_launcher(path, url):
    """Atomic replacement never follows an existing symlink or retains loose modes."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.launch-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(url)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
