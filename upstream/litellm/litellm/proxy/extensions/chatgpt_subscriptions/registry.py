"""Durable non-secret subscription metadata in the existing token volume."""
import time
import uuid
from pathlib import Path

from . import tokens
from .settings import Access, Factors, FairUse


class Registry:
    def __init__(self, root=None):
        self.root = Path(root or tokens.auth_path().parent)
        self.path = self.root / '.subscriptions.json'
        self.lock_path = self.root / '.subscriptions'

    def read(self):
        value = tokens.read(self.path)
        if value is None:
            if self.path.exists():
                raise ValueError('Subscription registry is unreadable.')
            value = {'version': 2, 'accounts': {}}
        if type(value.get('version')) is not int or value['version'] not in (1, 2) or not isinstance(value.get('accounts'), dict):
            raise ValueError('Unsupported subscription registry.')
        try:
            for record in value['accounts'].values():
                record['access'] = Access.model_validate(record.get('access', {})).model_dump()
                record['fair_use'] = FairUse.model_validate(record.get('fair_use', {})).model_dump()
            value['fair_use_factors'] = Factors.model_validate(value.get('fair_use_factors', {})).model_dump()
        except (ValueError, AttributeError, TypeError):
            raise ValueError('Invalid subscription settings.') from None
        value['version'] = 2
        return value

    def records(self):
        return self.read()['accounts']

    def write(self, records):
        # All writers hold lock_path. Preserve unrelated settings on lifecycle writes.
        tokens.write({'version': 2, 'accounts': records,
                      'fair_use_factors': self.read()['fair_use_factors']}, self.path)

    def update_settings(self, field, body, alias=None):
        model = {'access': Access, 'fair_use': FairUse, 'fair_use_factors': Factors}[field]
        saved = model.model_validate(body).model_dump()
        with tokens.locked(self.lock_path):
            value = self.read()
            target = value if field == 'fair_use_factors' else value['accounts'][alias]
            if target.get('retired'):
                raise ValueError('Retired subscriptions cannot be changed.')
            previous = target[field]
            target[field] = saved
            tokens.write(value, self.path)
        return previous, saved

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
                'retired': False, 'created_at': time.time(),
                'access': Access().model_dump(), 'fair_use': FairUse().model_dump()}

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
