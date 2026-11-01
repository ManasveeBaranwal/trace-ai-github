"""Local account, tenant, session and audit services. No default credentials."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SESSION_SECONDS = 8 * 60 * 60

def timestamp():
    return datetime.now(timezone.utc).isoformat()

def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('Use a password between 12 and 256 characters.')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64*1024*1024)
    return 'scrypt$' + salt.hex() + '$' + digest.hex()

def password_matches(password, encoded):
    try:
        _, salt, expected = encoded.split('$')
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1, maxmem=64*1024*1024)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False

class Accounts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS companies(id TEXT PRIMARY KEY, name TEXT NOT NULL, industry TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('master','company_user')), company_id TEXT REFERENCES companies(id), password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL, target TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS login_failures(key TEXT NOT NULL, at REAL NOT NULL);
            ''')
        from hierarchy import migrate
        migrate(self)
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, actor, action, target='', details=None, db=None):
        if db is None:
            with self.db() as conn:
                return self.audit(actor, action, target, details, conn)
        db.execute('INSERT INTO audit(at,actor_id,action,target,details) VALUES(?,?,?,?,?)', (timestamp(), actor, action, target, json.dumps(details or {})))

    def has_master(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM users WHERE role='master' AND active=1").fetchone())

    def create_master(self, email, name, password):
        encoded = password_hash(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM users WHERE role='master'").fetchone():
                raise ValueError('A master account already exists. Sign in to manage accounts.')
            uid = str(uuid.uuid4())
            db.execute('INSERT INTO users(id,email,name,role,company_id,password_hash,active,created_at) VALUES(?,?,?,?,?,?,?,?)',(uid,email.strip().lower(),name,'master',None,encoded,1,timestamp()))
            self.audit(uid,'master.created',uid,db=db)
        return uid

    @staticmethod
    def public_user(row):
        result = {k:row[k] for k in ('id','email','name','role','company_id','active','created_at')}
        result['role'] = row['access_role'] or ('company_admin' if row['role']=='company_user' else row['role'])
        result['firm_id'] = row['firm_id']
        return result

    def authenticate(self, email, password, ip):
        email = email.strip().lower()
        # Per-address and per-email counters survive restart; failures expire after 15 minutes.
        keys = [hashlib.sha256(('ip:'+ip).encode()).hexdigest(), hashlib.sha256(('email:'+email).encode()).hexdigest()]
        with self.db() as db:
            db.execute('DELETE FROM login_failures WHERE at<?',(time.time()-900,))
            if any(db.execute('SELECT COUNT(*) FROM login_failures WHERE key=?',(key,)).fetchone()[0] >= 8 for key in keys):
                raise ValueError('Too many failed attempts. Try again in 15 minutes.')
            row = db.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        valid = password_matches(password, row['password_hash'] if row else self.dummy_hash)
        with self.db() as db:
            from hierarchy import valid_tenant
            if not valid or not row or not row['active'] or not valid_tenant(self, self.public_user(row), db):
                db.executemany('INSERT INTO login_failures VALUES(?,?)',[(key,time.time()) for key in keys])
                self.audit(None,'login.failed','',db=db)
                return None
            db.execute('DELETE FROM login_failures WHERE key=?',(keys[1],))
            token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires_at<?',(time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],csrf,time.time()+SESSION_SECONDS))
            self.audit(row['id'],'login.succeeded',row['id'],db=db)
        return token, csrf, self.public_user(row)

    def session(self, token):
        if not token:
            return None
        with self.db() as db:
            row = db.execute('SELECT u.*,s.csrf,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
            if not row:
                return None
            from hierarchy import valid_tenant
            if not valid_tenant(self, self.public_user(row), db):
                return None
            return {'user':self.public_user(row),'csrf':row['csrf'],'expires_at':row['expires_at']}

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256((token or '').encode()).hexdigest(),))

    def get_setting(self, key, default):
        with self.db() as db:
            row = db.execute('SELECT payload FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
