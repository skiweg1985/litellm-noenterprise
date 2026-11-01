"""Durable non-secret subscription metadata in the existing token volume."""
import time
import uuid
from pathlib import Path

from . import tokens


class Registry:
    def __init__(self, root=None):
        self.root = Path(root or tokens.auth_path().parent)
        self.path = self.root / '.subscriptions.json'
        self.lock_path = self.root / '.subscriptions'

    def records(self):
        value = tokens.read(self.path)
        if value is None:
            if self.path.exists():
                raise ValueError('Subscription registry is unreadable.')
            return {}
        if value.get('version') != 1 or not isinstance(value.get('accounts'), dict):
            raise ValueError('Unsupported subscription registry.')
        return value['accounts']

    def write(self, records):
        tokens.write({'version': 1, 'accounts': records}, self.path)

    def seed(self, names):
        with tokens.locked(self.lock_path):
            records = self.records()
            changed = False
            for name in names:
                if name not in records:
                    records[name] = self.entry(name)
                    changed = True
            if changed:
                self.write(records)
        return records

    @staticmethod
    def entry(display_name):
        return {'subscription_id': str(uuid.uuid4()), 'display_name': display_name,
                'retired': False, 'created_at': time.time()}

    @staticmethod
    def validate_name(display_name):
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 100:
            raise ValueError('Use a subscription name between 1 and 100 characters.')
        if any(ord(c) < 32 or ord(c) == 127 for c in display_name):
            raise ValueError('Subscription names cannot contain control characters.')
        return display_name.strip()

    def create(self, display_name):
        display_name = self.validate_name(display_name)
        with tokens.locked(self.lock_path):
            records = self.records()
            alias = 'sub_' + uuid.uuid4().hex
            records[alias] = self.entry(display_name)
            self.write(records)
        return alias

    def rename(self, alias, display_name):
        display_name = self.validate_name(display_name)
        with tokens.locked(self.lock_path):
            records = self.records()
            if alias not in records:
                raise KeyError(alias)
            if records[alias]['retired']:
                raise ValueError('Retired subscriptions cannot be changed.')
            records[alias]['display_name'] = display_name
            self.write(records)

    def retired(self, alias):
        return bool(self.records().get(alias, {}).get('retired'))

    def snapshot(self, alias, state_path=None):
        record = self.records().get(alias)
        if record is None:
            return None
        state = tokens.read(state_path or tokens.auth_path(alias).with_name(tokens.auth_path(alias).name + '.state.json')) or {}
        return {key: record[key] for key in ('subscription_id', 'display_name', 'retired')} | {
            'connection_revision': state.get('connection_revision')}


def subscription_snapshot(alias):
    """Capture safe attribution at dispatch; never includes provider identity/tokens."""
    return Registry().snapshot(alias)
