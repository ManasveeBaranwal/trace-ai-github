"""Firm hierarchy, scoped provisioning and five role dashboards.

The legacy users.role column is retained for backwards-compatible storage.
access_role is the effective role; existing company users retain admin access.
"""
import json
import os
import sqlite3
import uuid
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from security import password_hash, timestamp

ROLES = ('master', 'consultant_manager', 'consultant', 'company_admin', 'data_entry')
HOMES = dict(zip(ROLES, ('/super-admin', '/consultant-manager', '/consultant', '/company-admin', '/data-entry')))
LABELS = dict(zip(ROLES, ('Super Master', 'Consultant Manager', 'Consultant', 'Company Administrator', 'Data-entry User')))
RANKS = {name: index for index, name in enumerate(ROLES)}


def role(user):
    value = user.get('role') if user else None
    return 'company_admin' if value == 'company_user' else value


def home(user):
    if os.getenv('TRACE_BOUTIQUE_PREVIEW', '0') == '1' and role(user) in {'consultant_manager','consultant'}:
        return '/boutique'
    if os.getenv('TRACE_TWO_ACCESS', '0') == '1' and role(user) == 'company_admin':
        return '/company-manager'
    return HOMES.get(role(user), '/login')


def visible_user(viewer, target):
    """Directory visibility is downward and lateral, never upward or cross-tenant.

    Visibility does not grant account-management permissions.
    """
    viewer_role, target_role = role(viewer), role(target)
    if viewer_role not in RANKS or target_role not in RANKS:
        return False
    if viewer_role == 'master':
        return True
    if RANKS[target_role] < RANKS[viewer_role]:
        return False
    if viewer_role in {'consultant_manager', 'consultant'}:
        return bool(viewer.get('firm_id') and viewer['firm_id'] == target.get('firm_id'))
    return bool(viewer.get('company_id') and viewer['company_id'] == target.get('company_id'))


def visible_record(accounts, viewer, record):
    """Keep full audit identities in storage; hide higher-level identities in responses."""
    if role(viewer) == 'master':
        return record
    with accounts.db() as db:
        visible_ids = {u['id'] for raw in db.execute('SELECT * FROM users')
                       if visible_user(viewer, u := accounts.public_user(raw))}
    visible_ids.add(viewer['id'])
    def project(value):
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, dict):
            return {key: (item if item in visible_ids else None)
                    if key in {'actor_id', 'created_by', 'reviewed_by', 'updated_by'}
                    else project(item) for key, item in value.items()}
        return value
    return project(record)


def migrate(accounts):
    with accounts.db() as db:
        columns = {r['name'] for r in db.execute('PRAGMA table_info(users)')}
        if 'access_role' not in columns:
            backup = accounts.path.with_name('accounts.pre-hierarchy-v1.sqlite3')
            if not backup.exists():
                target = sqlite3.connect(backup)
                try:
                    db.backup(target)
                finally:
                    target.close()
        db.execute('''CREATE TABLE IF NOT EXISTS firms(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            consultant_limit INTEGER NOT NULL DEFAULT 5 CHECK(consultant_limit>=0),
            company_limit INTEGER NOT NULL DEFAULT 10 CHECK(company_limit>=0),
            default_data_entry_limit INTEGER NOT NULL DEFAULT 5 CHECK(default_data_entry_limit>=0),
            created_at TEXT NOT NULL)''')
        for name, definition in [('firm_id', 'TEXT REFERENCES firms(id)'), ('access_role', 'TEXT')]:
            if name not in columns:
                db.execute('ALTER TABLE users ADD COLUMN ' + name + ' ' + definition)
        company_columns = {r['name'] for r in db.execute('PRAGMA table_info(companies)')}
        for name, definition in [('firm_id', 'TEXT REFERENCES firms(id)'), ('data_entry_limit', 'INTEGER NOT NULL DEFAULT 5 CHECK(data_entry_limit>=0)')]:
            if name not in company_columns:
                db.execute('ALTER TABLE companies ADD COLUMN ' + name + ' ' + definition)
        db.execute('''CREATE TABLE IF NOT EXISTS user_facilities(
            user_id TEXT NOT NULL REFERENCES users(id), facility_id TEXT NOT NULL REFERENCES facilities(id),
            PRIMARY KEY(user_id,facility_id))''')
        db.execute('CREATE INDEX IF NOT EXISTS company_firm ON companies(firm_id)')


def allowed_company(accounts, user, cid, db=None):
    if not user or not cid:
        return False
    if db is None:
        with accounts.db() as conn:
            return allowed_company(accounts, user, cid, conn)
    company = db.execute('SELECT c.*,f.active AS firm_active FROM companies c LEFT JOIN firms f ON f.id=c.firm_id WHERE c.id=?', (cid,)).fetchone()
    if not company:
        return False
    if role(user) == 'master':
        return True
    if not company['active'] or (company['firm_id'] and not company['firm_active']):
        return False
    if role(user) in {'consultant_manager', 'consultant'}:
        return bool(user.get('firm_id') and user['firm_id'] == company['firm_id'])
    return user.get('company_id') == cid


def allowed_site(accounts, user, cid, fid, db=None):
    if db is None:
        with accounts.db() as conn:
            return allowed_site(accounts, user, cid, fid, conn)
    if not allowed_company(accounts, user, cid, db):
        return False
    if role(user) != 'data_entry':
        return True
    return bool(db.execute('SELECT 1 FROM user_facilities uf JOIN facilities f ON f.id=uf.facility_id WHERE uf.user_id=? AND f.id=? AND f.company_id=?', (user['id'], fid, cid)).fetchone())


def allowed_bill(accounts, user, bill):
    if role(user) == 'master':
        return True
    return allowed_company(accounts, user, bill.get('company_id')) and (role(user) != 'data_entry' or bill.get('created_by') == user['id'])


def valid_tenant(accounts, user, db):
    if role(user) == 'master':
        return True
    if role(user) in {'consultant_manager', 'consultant'}:
        return bool(db.execute('SELECT 1 FROM firms WHERE id=? AND active=1', (user.get('firm_id'),)).fetchone())
    return allowed_company(accounts, user, user.get('company_id'), db)


def reviewer(user):
    if role(user) == 'data_entry':
        raise HTTPException(403, 'Review permission is required for this action')


def administrator(user):
    if role(user) not in {'master', 'consultant_manager', 'company_admin'}:
        raise HTTPException(403, 'Administrator access required')


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class FirmInput(Input):
    name: str = Field(min_length=2, max_length=160)
    active: bool = True
    consultant_limit: int = Field(default=5, ge=0, le=10000)
    company_limit: int = Field(default=10, ge=0, le=10000)
    default_data_entry_limit: int = Field(default=5, ge=0, le=10000)


class CompanyInput(Input):
    name: str = Field(min_length=2, max_length=160)
    industry: str = Field(default='', max_length=120)
    country: str = Field(default='', max_length=100)
    firm_id: str | None = None
    data_entry_limit: int | None = Field(default=None, ge=0, le=10000)
    active: bool = True


class UserInput(Input):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    role: Literal['master', 'consultant_manager', 'consultant', 'company_admin', 'data_entry', 'company_user']
    company_id: str | None = None
    firm_id: str | None = None
    password: str = Field(min_length=12, max_length=256)
    facility_ids: list[str] = Field(default_factory=list, max_length=1000)

    @field_validator('email')
    @classmethod
    def email_valid(cls, value):
        import re
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Enter a valid email address')
        return value.lower()


class UserUpdate(Input):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    active: bool
    password: str | None = Field(default=None, min_length=12, max_length=256)
    facility_ids: list[str] | None = Field(default=None, max_length=1000)


def check_seats(db, selected_role, cid=None, fid=None):
    if selected_role == 'data_entry':
        company = db.execute('SELECT data_entry_limit FROM companies WHERE id=?', (cid,)).fetchone()
        used = db.execute("SELECT COUNT(*) FROM users WHERE access_role='data_entry' AND company_id=? AND active=1", (cid,)).fetchone()[0]
        if not company or used >= company[0]:
            raise HTTPException(409, 'Data-entry account limit reached. Contact support to request an increase.')
    if selected_role == 'consultant':
        firm = db.execute('SELECT consultant_limit FROM firms WHERE id=?', (fid,)).fetchone()
        used = db.execute("SELECT COUNT(*) FROM users WHERE access_role='consultant' AND firm_id=? AND active=1", (fid,)).fetchone()[0]
        if not firm or used >= firm[0]:
            raise HTTPException(409, 'Consultant account limit reached. Contact support to request an increase.')


def validate_assignment(db, cid, ids):
    for fid in set(ids):
        if not db.execute('SELECT 1 FROM facilities WHERE id=? AND company_id=? AND active=1', (fid, cid)).fetchone():
            raise HTTPException(422, 'Assign only active facilities belonging to this company')


def create_account(accounts, actor, body):
    administrator(actor)
    selected_role = role({'role': body.role})
    encoded = password_hash(body.password)
    uid = str(uuid.uuid4())
    with accounts.db() as db:
        db.execute('BEGIN IMMEDIATE')
        cid, fid = body.company_id, body.firm_id
        if selected_role in {'company_admin', 'data_entry'}:
            if not allowed_company(accounts, actor, cid, db):
                raise HTTPException(403, 'Company access denied')
            company = db.execute('SELECT * FROM companies WHERE id=? AND active=1', (cid,)).fetchone()
            if not company:
                raise HTTPException(422, 'Choose an active company')
            if fid and fid != company['firm_id']:
                raise HTTPException(422, 'Company and firm do not match')
            fid = company['firm_id']
        elif selected_role in {'consultant_manager', 'consultant'}:
            if cid:
                raise HTTPException(422, 'Consulting users belong to a firm, not a single company')
            if not db.execute('SELECT 1 FROM firms WHERE id=? AND active=1', (fid,)).fetchone():
                raise HTTPException(422, 'Choose an active consulting firm')
        elif cid or fid:
            raise HTTPException(422, 'Super Master must not belong to a company or consulting firm')
        if role(actor) == 'consultant_manager':
            if selected_role not in {'consultant', 'company_admin', 'data_entry'} or fid != actor.get('firm_id'):
                raise HTTPException(403, 'You can provision consultants and company users only within your firm')
        if role(actor) == 'company_admin' and (selected_role != 'data_entry' or cid != actor.get('company_id')):
            raise HTTPException(403, 'Company administrators can create only their own data-entry users')
        if body.facility_ids and selected_role != 'data_entry':
            raise HTTPException(422, 'Facility assignments apply to data-entry users')
        validate_assignment(db, cid, body.facility_ids)
        check_seats(db, selected_role, cid, fid)
        try:
            db.execute('INSERT INTO users(id,email,name,role,company_id,password_hash,active,created_at,firm_id,access_role) VALUES(?,?,?,?,?,?,1,?,?,?)',
                       (uid, body.email, body.name, 'master' if selected_role == 'master' else 'company_user', cid, encoded, timestamp(), fid, selected_role))
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'An account with that email already exists')
        db.executemany('INSERT INTO user_facilities VALUES(?,?)', [(uid, f) for f in set(body.facility_ids)])
        accounts.audit(actor['id'], 'user.created', uid, {'role': selected_role, 'company_id': cid, 'firm_id': fid, 'facility_ids': body.facility_ids}, db)
    return {'id': uid, 'email': body.email, 'role': selected_role}


def update_account(accounts, actor, uid, body):
    administrator(actor)
    encoded = password_hash(body.password) if body.password else None
    with accounts.db() as db:
        db.execute('BEGIN IMMEDIATE')
        raw = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not raw:
            raise HTTPException(404, 'Account not found')
        user = accounts.public_user(raw)
        target_role = role(user)
        if not visible_user(actor, user):
            raise HTTPException(404, 'Account not found')
        if role(actor) == 'consultant_manager' and (user.get('firm_id') != actor.get('firm_id') or target_role not in {'consultant', 'company_admin', 'data_entry'}):
            raise HTTPException(403, 'Account access denied')
        if role(actor) == 'company_admin' and (user.get('company_id') != actor.get('company_id') or target_role != 'data_entry'):
            raise HTTPException(403, 'Account access denied')
        if not body.active and uid == actor['id']:
            raise HTTPException(409, 'You cannot deactivate your own account')
        if not body.active and target_role == 'master':
            if db.execute("SELECT COUNT(*) FROM users WHERE role='master' AND active=1").fetchone()[0] <= 1:
                raise HTTPException(409, 'The last active Super Master cannot be disabled')
        if body.active and not raw['active']:
            if not valid_tenant(accounts, user, db):
                raise HTTPException(409, 'Activate the company or consulting firm first')
            check_seats(db, target_role, user.get('company_id'), user.get('firm_id'))
        if body.facility_ids is not None:
            if target_role != 'data_entry':
                raise HTTPException(422, 'Facility assignments apply to data-entry users')
            validate_assignment(db, user['company_id'], body.facility_ids)
            db.execute('DELETE FROM user_facilities WHERE user_id=?', (uid,))
            db.executemany('INSERT INTO user_facilities VALUES(?,?)', [(uid, f) for f in set(body.facility_ids)])
        db.execute('UPDATE users SET active=? WHERE id=?', (int(body.active), uid))
        if encoded:
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (encoded, uid))
        db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        accounts.audit(actor['id'], 'user.updated', uid, {'active': body.active, 'password_reset': bool(encoded), 'facility_ids': body.facility_ids}, db)
    return {'ok': True}


def install_hierarchy(app, accounts, root, bill_connection):
    def super_master(user):
        if role(user) != 'master':
            raise HTTPException(403, 'Super Master access required')

    @app.get('/api/access/overview')
    def overview(request: Request, preview_role: str | None = None):
        actor = request.state.user
        user = actor
        preview = bool(preview_role)
        with accounts.db() as db:
            all_companies = [dict(r) for r in db.execute('SELECT * FROM companies ORDER BY name')]
            all_firms = [dict(r) for r in db.execute('SELECT * FROM firms ORDER BY name')]
            if preview:
                super_master(actor)
                if preview_role not in ROLES:
                    raise HTTPException(422, 'Unknown role')
                firm = next((f for f in all_firms if f['active']), None)
                company = next((c for c in all_companies if c['active'] and (not firm or c['firm_id'] == firm['id'])), None)
                user = {'id': 'preview-only', 'role': preview_role, 'firm_id': firm['id'] if firm else None, 'company_id': company['id'] if company else None}
            companies = [c for c in all_companies if allowed_company(accounts, user, c['id'], db) or (role(user) in {'consultant_manager','consultant'} and user.get('firm_id') and c['firm_id']==user['firm_id'])]
            cids = {c['id'] for c in companies}
            firms = all_firms if role(user) == 'master' else ([f for f in all_firms if f['id'] == user.get('firm_id')] if role(user) in {'consultant_manager','consultant'} else [])
            users = []
            for raw in db.execute('SELECT * FROM users ORDER BY created_at DESC'):
                u = accounts.public_user(raw)
                if visible_user(user, u):
                    if role(user) in {'master', 'consultant_manager', 'company_admin'} or u['id'] == user['id']:
                        u['facility_ids'] = [r[0] for r in db.execute('SELECT facility_id FROM user_facilities WHERE user_id=?', (u['id'],))]
                    users.append(u)
            facilities = [dict(r) for r in db.execute('SELECT * FROM facilities ORDER BY name') if r['company_id'] in cids and allowed_site(accounts, user, r['company_id'], r['id'], db)]
            activities = [json.loads(r['payload']) for r in db.execute('SELECT * FROM activities ORDER BY created_at DESC') if r['company_id'] in cids and allowed_site(accounts, user, r['company_id'], r['facility_id'], db)]
            for c in companies:
                c['data_entry_used'] = db.execute("SELECT COUNT(*) FROM users WHERE company_id=? AND access_role='data_entry' AND active=1", (c['id'],)).fetchone()[0]
                ca = [a for a in activities if a['company_id'] == c['id']]
                c.update(activities=len(ca), drafts=sum(a['status'] == 'draft' for a in ca), reviewed=sum(a['status'] == 'reviewed' for a in ca))
            for f in firms:
                f['consultant_used'] = db.execute("SELECT COUNT(*) FROM users WHERE firm_id=? AND access_role='consultant' AND active=1", (f['id'],)).fetchone()[0]
                f['company_used'] = db.execute('SELECT COUNT(*) FROM companies WHERE firm_id=? AND active=1', (f['id'],)).fetchone()[0]
        with bill_connection() as db:
            bills = [json.loads(r[0]) for r in db.execute('SELECT payload FROM bills ORDER BY rowid DESC')]
        bills = [b for b in bills if allowed_bill(accounts, user, b)]
        documents = [{k: b.get(k) for k in ('id', 'company_id', 'filename', 'status', 'created_at', 'created_by')} for b in bills]
        return visible_record(accounts, user, {'role': role(user), 'label': LABELS[role(user)], 'home': home(user), 'preview': preview, 'firms': firms, 'companies': companies,
                'users': users, 'facilities': facilities, 'documents': documents, 'activities': activities,
                'stats': {'firms': len(firms), 'companies': len(companies), 'users': len(users), 'documents': len(documents), 'drafts': sum(a['status'] == 'draft' for a in activities), 'reviewed': sum(a['status'] == 'reviewed' for a in activities), 'needs_review': sum(b['status'] == 'needs_review' for b in bills)}})

    @app.post('/api/access/firms', status_code=201)
    def create_firm(body: FirmInput, request: Request):
        actor = request.state.user
        super_master(actor)
        fid = str(uuid.uuid4())
        with accounts.db() as db:
            db.execute('INSERT INTO firms VALUES(?,?,?,?,?,?,?)', (fid, body.name, int(body.active), body.consultant_limit, body.company_limit, body.default_data_entry_limit, timestamp()))
            accounts.audit(actor['id'], 'firm.created', fid, body.model_dump(), db)
        return {'id': fid}

    @app.put('/api/access/firms/{fid}')
    def update_firm(fid: str, body: FirmInput, request: Request):
        super_master(request.state.user)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM firms WHERE id=?', (fid,)).fetchone():
                raise HTTPException(404, 'Consulting firm not found')
            consultant_used = db.execute("SELECT COUNT(*) FROM users WHERE firm_id=? AND access_role='consultant' AND active=1", (fid,)).fetchone()[0]
            company_used = db.execute('SELECT COUNT(*) FROM companies WHERE firm_id=? AND active=1', (fid,)).fetchone()[0]
            if body.consultant_limit < consultant_used or body.company_limit < company_used:
                raise HTTPException(409, 'Deactivate accounts or companies before reducing limits below current usage')
            db.execute('UPDATE firms SET name=?,active=?,consultant_limit=?,company_limit=?,default_data_entry_limit=? WHERE id=?', (body.name, int(body.active), body.consultant_limit, body.company_limit, body.default_data_entry_limit, fid))
            accounts.audit(request.state.user['id'], 'firm.updated', fid, body.model_dump(), db)
        return {'ok': True}

    @app.post('/api/access/companies', status_code=201)
    def create_company(body: CompanyInput, request: Request):
        actor = request.state.user
        if role(actor) not in {'master', 'consultant_manager'}:
            raise HTTPException(403, 'Super Master or Consultant Manager access required')
        fid = body.firm_id if role(actor) == 'master' else actor.get('firm_id')
        if role(actor) != 'master' and (body.data_entry_limit is not None or (body.firm_id and body.firm_id != fid)):
            raise HTTPException(403, 'Only Super Master can change allowances or choose another firm')
        cid = str(uuid.uuid4())
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            limit = 5
            if fid:
                firm = db.execute('SELECT * FROM firms WHERE id=? AND active=1', (fid,)).fetchone()
                if not firm:
                    raise HTTPException(422, 'Choose an active consulting firm')
                used = db.execute('SELECT COUNT(*) FROM companies WHERE firm_id=? AND active=1', (fid,)).fetchone()[0]
                if body.active and used >= firm['company_limit']:
                    raise HTTPException(409, 'Company allowance reached. Ask Super Master to increase it.')
                limit = firm['default_data_entry_limit']
            if body.data_entry_limit is not None:
                limit = body.data_entry_limit
            db.execute('INSERT INTO companies(id,name,industry,country,active,created_at,firm_id,data_entry_limit) VALUES(?,?,?,?,?,?,?,?)', (cid, body.name, body.industry, body.country, int(body.active), timestamp(), fid, limit))
            accounts.audit(actor['id'], 'company.created', cid, {'firm_id': fid, 'data_entry_limit': limit}, db)
        return {'id': cid}

    @app.put('/api/access/companies/{cid}')
    def update_company(cid: str, body: CompanyInput, request: Request):
        actor = request.state.user
        if role(actor) not in {'master', 'consultant_manager'}:
            raise HTTPException(403, 'Company management access denied')
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()
            if not old or (role(actor)!='master' and old['firm_id']!=actor.get('firm_id')):
                raise HTTPException(404,'Company not found')
            fid = old['firm_id']
            if role(actor) != 'master' and (body.data_entry_limit is not None or (body.firm_id and body.firm_id != fid)):
                raise HTTPException(403, 'Only Super Master can change company allowances')
            if body.firm_id != fid and body.firm_id is not None:
                if old['firm_id']:
                    raise HTTPException(409, 'Moving companies between firms needs a separate reviewed migration')
                if db.execute('SELECT 1 FROM users WHERE company_id=? AND firm_id IS NOT NULL', (cid,)).fetchone():
                    raise HTTPException(409, 'Review existing firm memberships before assigning this company')
                fid = body.firm_id
            if fid:
                firm = db.execute('SELECT * FROM firms WHERE id=? AND active=1', (fid,)).fetchone()
                if not firm:
                    raise HTTPException(422, 'Choose an active firm')
                if body.active and (not old['active'] or fid != old['firm_id']):
                    if db.execute('SELECT COUNT(*) FROM companies WHERE firm_id=? AND active=1', (fid,)).fetchone()[0] >= firm['company_limit']:
                        raise HTTPException(409, 'Company allowance reached')
            limit = old['data_entry_limit'] if body.data_entry_limit is None else body.data_entry_limit
            used = db.execute("SELECT COUNT(*) FROM users WHERE company_id=? AND access_role='data_entry' AND active=1", (cid,)).fetchone()[0]
            if limit < used:
                raise HTTPException(409, 'Deactivate data-entry accounts before reducing their allowance below usage')
            db.execute('UPDATE companies SET name=?,industry=?,country=?,active=?,firm_id=?,data_entry_limit=? WHERE id=?', (body.name, body.industry, body.country, int(body.active), fid, limit, cid))
            db.execute('UPDATE users SET firm_id=? WHERE company_id=?', (fid, cid))
            db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE company_id=?)', (cid,))
            accounts.audit(actor['id'], 'company.updated', cid, {'firm_id': fid, 'data_entry_limit': limit, 'active': body.active}, db)
        return {'ok': True}

    @app.post('/api/access/users', status_code=201)
    def add_user(body: UserInput, request: Request):
        return create_account(accounts, request.state.user, body)

    @app.patch('/api/access/users/{uid}')
    def change_user(uid: str, body: UserUpdate, request: Request):
        return update_account(accounts, request.state.user, uid, body)

    for selected_role, path in HOMES.items():
        def page_factory(expected):
            def page(request: Request):
                if not request.state.user:
                    return RedirectResponse('/login', 303)
                if role(request.state.user) != expected:
                    return RedirectResponse(home(request.state.user), 303)
                if expected in {'consultant_manager','consultant'}:
                    return RedirectResponse('/boutique',303)
                if os.getenv('TRACE_TWO_ACCESS', '0') == '1' and expected in {'master', 'company_admin'}:
                    if expected == 'company_admin' and request.url.path == '/company-admin':
                        return RedirectResponse('/company-manager', 303)
                    return FileResponse(root / 'static/initial-access.html', headers={'Cache-Control': 'no-store'})
                return FileResponse(root / 'static/dashboards.html', headers={'Cache-Control': 'no-store'})
            return page
        app.add_api_route(path, page_factory(selected_role), methods=['GET'], include_in_schema=False)

    app.add_api_route('/company-manager', page_factory('company_admin'), methods=['GET'], include_in_schema=False)

    @app.get('/preview/{selected_role}', include_in_schema=False)
    def preview_page(selected_role: str, request: Request):
        if not request.state.user:
            return RedirectResponse('/login', 303)
        super_master(request.state.user)
        if selected_role not in ROLES:
            raise HTTPException(404, 'Unknown role preview')
        return FileResponse(root / 'static/dashboards.html', headers={'Cache-Control': 'no-store'})
