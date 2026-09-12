"""Offline sub2api JSON intake. No network and no secrets in preview/errors."""

from dataclasses import dataclass, field
import json

from sub2easy.intake import IntakeError, normalize_email
from sub2easy.nvtokens import export_document, parse_response


MAX_BYTES = 2 * 1024 * 1024
MAX_ACCOUNTS = 1000


@dataclass(frozen=True)
class OAuthImportItem:
    index: int
    authorization: dict = field(repr=False)
    document: dict = field(repr=False)


@dataclass(frozen=True)
class Sub2Batch:
    items: tuple[OAuthImportItem, ...] = field(repr=False)
    duplicates: tuple[int, ...]
    errors: tuple[dict, ...]
    total: int
    ignored_proxies: int

    def preview(self):
        return {'mode': 'sub2_json', 'accepted': len(self.items), 'total': self.total,
                'accepted_indices': [i.index for i in self.items],
                'duplicate_indices': list(self.duplicates), 'errors': list(self.errors),
                'ignored_proxies': self.ignored_proxies,
                'secrets_persisted': False, 'network_requests': 0}


def parse_sub2(text, client_id=''):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_BYTES:
        raise IntakeError('SUB2_INPUT_TOO_LARGE')
    try:
        payload = json.loads(text.removeprefix('\ufeff'))
    except (ValueError, RecursionError):
        raise IntakeError('SUB2_INVALID_JSON') from None
    entries, ignored = [], 0

    def collect(value, depth=0):
        nonlocal ignored
        if depth > 6:
            raise IntakeError('SUB2_UNSUPPORTED_FORMAT')
        document = export_document(value)
        if isinstance(document, list):
            for child in document:
                collect(child, depth + 1)
            return
        if not isinstance(document, dict):
            raise IntakeError('SUB2_UNSUPPORTED_FORMAT')
        if 'accounts' in document:
            # Current upstream also exports bundles with omitted type/version.
            if (document.get('type', 'sub2api-data') not in {'sub2api-data', 'sub2api-bundle'}
                    or type(document.get('version', 1)) is not int or document.get('version', 1) != 1
                    or not isinstance(document['accounts'], list)):
                raise IntakeError('SUB2_UNSUPPORTED_FORMAT')
            proxies = document.get('proxies', [])
            if not isinstance(proxies, list):
                raise IntakeError('SUB2_UNSUPPORTED_FORMAT')
            ignored += len(proxies)
            items = document['accounts']
        elif 'credentials' in document:
            items = [document]
        else:
            raise IntakeError('SUB2_UNSUPPORTED_FORMAT')
        # NVT's summary describes one identity, never distribute it over a batch.
        summary = value.get('summary') if isinstance(value, dict) and 'account_json' in value else None
        if summary is not None and len(items) != 1:
            raise IntakeError('SUB2_AMBIGUOUS_SUMMARY')
        for item in items:
            entries.append((item, document.get('exported_at'), summary))
            if len(entries) > MAX_ACCOUNTS:
                raise IntakeError('SUB2_TOO_MANY_ACCOUNTS')

    try:
        collect(payload)
    except RecursionError:
        raise IntakeError('SUB2_UNSUPPORTED_FORMAT') from None
    if not entries:
        raise IntakeError('SUB2_NO_ACCOUNTS')
    errors, grouped = [], {}
    for index, (item, exported_at, summary) in enumerate(entries, 1):
        if not isinstance(item, dict) or item.get('platform') != 'openai' or item.get('type') != 'oauth':
            errors.append({'index': index, 'code': 'SUB2_UNSUPPORTED_ACCOUNT_TYPE'})
            continue
        credentials = item.get('credentials')
        try:
            account_email = normalize_email(credentials.get('email') if isinstance(credentials, dict) else None)
        except IntakeError:
            errors.append({'index': index, 'code': 'SUB2_EMAIL_REQUIRED'})
            continue
        source = {'account_json': {'type': 'sub2api-data', 'version': 1, 'accounts': [item]}}
        if summary is not None:
            source['summary'] = summary
        parsed = parse_response(200, json.dumps(source).encode(), account_email, client_id=client_id)
        if not parsed.authorization:
            errors.append({'index': index, 'code': parsed.review_code})
            continue
        # Keep only normalized credentials + a display name, not passwords,
        # cookies, private keys, proxy payloads, foreign group IDs or file paths.
        clean = {'platform': 'openai', 'type': 'oauth',
                 'name': item.get('name', '')[:200] if isinstance(item.get('name'), str) else '',
                 'credentials': parsed.authorization['credentials']}
        record = {'type': 'sub2api-data', 'version': 1, 'accounts': [clean], 'proxies': []}
        if isinstance(exported_at, str):
            record['exported_at'] = exported_at[:64]
        grouped.setdefault(account_email, []).append(OAuthImportItem(index, parsed.authorization, record))
    accepted, duplicates = [], []
    for items in grouped.values():
        first = items[0]
        # Existing local identity is one login-email key; do not silently choose
        # between different workspaces or credential revisions in the same input.
        if any(i.authorization != first.authorization for i in items[1:]):
            errors.extend({'index': i.index, 'code': 'SUB2_CONFLICTING_ACCOUNT'} for i in items)
        else:
            accepted.append(first)
            duplicates.extend(i.index for i in items[1:])
    return Sub2Batch(tuple(sorted(accepted, key=lambda i: i.index)), tuple(sorted(duplicates)),
                     tuple(sorted(errors, key=lambda e: e['index'])), len(entries), ignored)
