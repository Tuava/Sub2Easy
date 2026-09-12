"""Parse login materials without persisting or printing secrets."""

import argparse
import base64
import binascii
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys


class IntakeError(ValueError):
    """Fixed error code only: never include input text."""


def has_login_material(login):
    """A JSON-only OAuth account has an identity, not a password/TOTP login."""
    return isinstance(login, dict) and all(
        isinstance(login.get(key), str) and bool(login[key])
        for key in ('account', 'password', 'totp_secret')
    )


def normalize_email(value):
    if not isinstance(value, str):
        raise IntakeError("INVALID_ACCOUNT")
    # Accept a Markdown-escaped @ in the account field, not in the password.
    value = value.strip().replace("\\@", "@").casefold()
    if (len(value) > 254 or not re.fullmatch(r"[^\s@\\]+@[^\s@\\]+\.[^\s@\\]+", value)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise IntakeError("INVALID_ACCOUNT")
    # OAuth-provider login matching, NOT general RFC email canonicalization.
    # Do not remove dots or +suffixes, or merge aliases across mail providers.
    return value


def normalize_totp_secret(value):
    if not isinstance(value, str):
        raise IntakeError("INVALID_TOTP_SECRET")
    secret = value.strip().upper()
    if not re.fullmatch(r"[A-Z2-7]{16,128}={0,6}", secret):
        raise IntakeError("INVALID_TOTP_SECRET")
    unpadded = secret.rstrip("=")
    padded = unpadded + "=" * ((-len(unpadded)) % 8)
    if "=" in secret and secret != padded:
        raise IntakeError("INVALID_TOTP_SECRET")
    try:
        raw = base64.b32decode(padded)
    except (ValueError, binascii.Error):
        raise IntakeError("INVALID_TOTP_SECRET") from None
    if len(raw) < 10 or base64.b32encode(raw).decode().rstrip("=") != unpadded:
        raise IntakeError("INVALID_TOTP_SECRET")
    return unpadded


@dataclass(frozen=True)
class LoginMaterial:
    line: int
    account: str = field(repr=False)
    password: str = field(repr=False)
    totp_secret: str = field(repr=False)


@dataclass(frozen=True)
class LineIssue:
    line: int
    code: str


@dataclass(frozen=True)
class Batch:
    materials: tuple[LoginMaterial, ...] = field(repr=False)
    duplicates: tuple[int, ...]
    issues: tuple[LineIssue, ...]

    def preview(self):
        return {
            "mode": "local_parse_only",
            "accepted": len(self.materials),
            "accepted_lines": [m.line for m in self.materials],
            "duplicate_lines": list(self.duplicates),
            "errors": [{"line": i.line, "code": i.code} for i in self.issues],
            "secrets_persisted": False,
            "network_requests": 0,
        }


def parse_line(line, number):
    account, sep, remainder = line.partition("----")
    password, second_sep, secret = remainder.rpartition("----")
    if not sep or not second_sep:
        raise IntakeError("EXPECTED_ACCOUNT_PASSWORD_TOTP")
    # Preserve the password verbatim; it may include spaces or the delimiter.
    if (not password or len(password) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in password)):
        raise IntakeError("INVALID_PASSWORD")
    return LoginMaterial(number, normalize_email(account), password, normalize_totp_secret(secret))


def parse_batch(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise IntakeError("BATCH_TOO_LARGE")
    # Split only CRLF/LF, rather than treating arbitrary password characters as newlines.
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    if len(lines) > 10001:
        raise IntakeError("TOO_MANY_LINES")
    candidates, issues = {}, []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            material = parse_line(line, number)
            candidates.setdefault(material.account, []).append(material)
        except IntakeError as exc:
            issues.append(LineIssue(number, str(exc)))
    accepted, duplicates = [], []
    for group in candidates.values():
        first = group[0]
        if any((m.password, m.totp_secret) != (first.password, first.totp_secret) for m in group[1:]):
            # Never silently choose the first/last of conflicting login materials.
            issues.extend(LineIssue(m.line, "CONFLICTING_LOGIN_MATERIAL") for m in group)
        else:
            accepted.append(first)
            duplicates.extend(m.line for m in group[1:])
    return Batch(tuple(sorted(accepted, key=lambda m: m.line)), tuple(sorted(duplicates)),
                 tuple(sorted(issues, key=lambda i: i.line)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="逐行校验 账号----密码----2FA种子；不入库、不联网")
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        with args.file.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise IntakeError("BATCH_TOO_LARGE")
        batch = parse_batch(raw.decode("utf-8"))
        print(json.dumps(batch.preview(), ensure_ascii=False, indent=2))
        return 2 if batch.issues else 0
    except IntakeError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, UnicodeError):
        print("UNREADABLE_UTF8_INPUT", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
