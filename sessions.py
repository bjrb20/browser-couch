"""Remembered devices: a refresh or a reopened tab rejoins the room without the room code or admin PIN.

After a correct room code (or admin PIN) the host gives the browser a random token in an HttpOnly
cookie. The host keeps only an HMAC of each token, keyed with the code that issued it, so:

- the file holds nothing that logs anyone in, nor anything to test a guessed code against;
- a new SOFA_PIN or SOFA_ADMIN_PIN makes every older token fail at once.

Player tokens last 30 days from their last use; admin tokens 7 days from the PIN, however often used.
The file lives in the saves folder, so a restart or a new build keeps everyone logged in.
"""
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import time

PLAYER_FOR = 30*24*3600
ADMIN_FOR = 7*24*3600
KEEP = 500  # records at most; the soonest to expire go first
log = logging.getLogger('stream')


class Sessions:
    def __init__(self, path, pin, admin_pin=None, clock=time.time):
        self.path = Path(path) if path else None
        self.codes = dict(player=pin, admin=admin_pin)
        self.clock = clock
        self.records = {}
        if self.path:
            try:
                records = json.loads(self.path.read_text())
                if isinstance(records, dict):
                    self.records = {key: record for key, record in records.items()
                                    if isinstance(record, dict) and isinstance(record.get('expires'), (int, float))}
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                log.warning('Remembered devices: could not read %s: %s', self.path.name, error)
        self.prune()

    def key(self, kind, token):
        code = self.codes.get(kind)
        if not code or not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        return hmac.new(code.encode(), f'{kind}:{token}'.encode(), hashlib.sha256).hexdigest()

    def issue(self, kind, **fields):
        """A new token, and its record's key. Only the key is kept."""
        token = secrets.token_urlsafe(32)
        key = self.key(kind, token)
        if key is None:
            raise ValueError(f'No code to issue a {kind} session with')
        self.records[key] = dict(fields, kind=kind,
                                 expires=self.clock()+(PLAYER_FOR if kind == 'player' else ADMIN_FOR))
        self.prune()
        self.save()
        return token, key

    def check(self, kind, token):
        """The record's key for a live token of this kind, else None. Player tokens slide on use."""
        key = self.key(kind, token)
        record = self.records.get(key) if key else None
        if not record or record.get('kind') != kind:
            return None
        if record['expires'] <= self.clock():
            self.revoke(key)
            return None
        return key

    def get(self, key):
        return self.records.get(key) if key else None

    def touch(self, key):
        """A player rejoined: 30 more days."""
        record = self.records.get(key)
        if record and record['kind'] == 'player':
            record['expires'] = self.clock()+PLAYER_FOR
            self.save()

    def update(self, key, **fields):
        record = self.records.get(key) if key else None
        if record and any(record.get(name) != value for name, value in fields.items()):
            record.update(fields)
            self.save()

    def revoke(self, *keys):
        if any([self.records.pop(key, None) for key in keys if key]):
            self.save()

    def prune(self):
        now = self.clock()
        self.records = {key: record for key, record in self.records.items() if record['expires'] > now}
        if len(self.records) > KEEP:
            keep = sorted(self.records, key=lambda key: self.records[key]['expires'])[-KEEP:]
            self.records = {key: self.records[key] for key in keep}

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name+'.new')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w') as file:
                json.dump(self.records, file, separators=(',', ':'))
            os.replace(temporary, self.path)
        except OSError as error:
            log.warning('Remembered devices: could not save %s: %s', self.path.name, error)
