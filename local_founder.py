"""Persistent local founder sessions and non-authenticating return hints.

Only the private loopback entry route may mint these values. A return hint
identifies an already used entry flow; it never resolves into an account.
"""
import hashlib
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from security import SESSION_SECONDS, timestamp


RETURN_SECONDS = 30 * 24 * 60 * 60
RETURN_PATHS = frozenset({
    '/boutique',
    '/super-admin', '/coefficients', '/brsr', '/app', '/company',
    '/company-manager', '/admin', '/consultant-manager', '/consultant',
    '/company-admin', '/data-entry',
})


def safe_return_to(value):
    """Allow only explicit internal workspace routes, never API/auth URLs."""
    if not isinstance(value, str) or len(value) > 3000 or not value.startswith('/'):
        return None
    if '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.path not in RETURN_PATHS:
        return None
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if value else ''


class LocalFounderSessions:
    def __init__(self, accounts):
        self.accounts = accounts
        with accounts.db() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_founder_sessions'").fetchone()
            backup = accounts.path.with_name('accounts.pre-local-founder-v1.sqlite3')
            if not exists and not backup.exists():
                target = sqlite3.connect(backup)
                try:
                    db.backup(target)
                finally:
                    target.close()
            db.executescript('''
                CREATE TABLE IF NOT EXISTS local_founder_sessions(
                    token_hash TEXT PRIMARY KEY,
                    csrf TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_founder_returns(
                    token_hash TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
            ''')

    def issue(self):
        token, csrf, hint = (secrets.token_urlsafe(length) for length in (40, 32, 40))
        now = time.time()
        created = timestamp()
        with self.accounts.db() as db:
            # Retain recently expired session hashes only as non-authenticating
            # recovery context. Logged-out sessions are deleted immediately.
            db.execute('DELETE FROM local_founder_sessions WHERE expires_at<?', (now - RETURN_SECONDS,))
            db.execute('DELETE FROM local_founder_returns WHERE expires_at<=?', (now,))
            db.execute('INSERT INTO local_founder_sessions VALUES(?,?,?,?)',
                       (digest(token), csrf, created, now + SESSION_SECONDS))
            db.execute('INSERT INTO local_founder_returns VALUES(?,?,?)',
                       (digest(hint), created, now + RETURN_SECONDS))
        return token, hint, {'csrf': csrf, 'created_at': created, 'expires_at': now + SESSION_SECONDS}

    def resolve(self, token):
        if not token:
            return None
        with self.accounts.db() as db:
            row = db.execute('SELECT csrf,created_at,expires_at FROM local_founder_sessions WHERE token_hash=? AND expires_at>?',
                             (digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def recognized(self, token=None, hint=None):
        now = time.time()
        with self.accounts.db() as db:
            if hint and db.execute('SELECT 1 FROM local_founder_returns WHERE token_hash=? AND expires_at>?',
                                   (digest(hint), now)).fetchone():
                return True
            return bool(token and db.execute('SELECT 1 FROM local_founder_sessions WHERE token_hash=? AND expires_at>?',
                                             (digest(token), now - RETURN_SECONDS)).fetchone())

    def revoke(self, token=None, hint=None, keep_return=False):
        with self.accounts.db() as db:
            if token:
                db.execute('DELETE FROM local_founder_sessions WHERE token_hash=?', (digest(token),))
            if hint and not keep_return:
                db.execute('DELETE FROM local_founder_returns WHERE token_hash=?', (digest(hint),))
