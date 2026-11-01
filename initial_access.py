"""Initial Super Access / Company Manager workspace and local role sessions.

Local manager profiles are explicit company-scoped identities, not invented email
accounts. They share the existing company_admin permissions and use separately
revocable hashed sessions. Passwordless entry is restricted to the local founder
workflow; it is unavailable when account login is required or from remote hosts.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hierarchy import allowed_bill, allowed_company, role, visible_record
from security import SESSION_SECONDS, timestamp


def two_access_enabled():
    return os.getenv('TRACE_TWO_ACCESS', '0') == '1'


class ManagerProfile(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    active: bool = True


def install_initial_access(app, accounts, root, bill_connection, get_bill=None):
    with accounts.db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='initial_manager_profiles'").fetchone():
            backup = accounts.path.with_name('accounts.pre-initial-access-v1.sqlite3')
            if not backup.exists():
                target = sqlite3.connect(backup)
                try:
                    db.backup(target)
                finally:
                    target.close()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS initial_manager_profiles(
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL UNIQUE REFERENCES companies(id),
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS initial_manager_sessions(
            token_hash TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES initial_manager_profiles(id),
            csrf TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS initial_manager_session_profile
            ON initial_manager_sessions(profile_id);
        ''')

    def manager_user(profile, company):
        return {'id': profile['id'], 'email': '', 'name': profile['name'],
                'role': 'company_admin', 'company_id': company['id'],
                'firm_id': company.get('firm_id'), 'active': bool(profile['active']),
                'created_at': profile['created_at'], 'local_only': True}

    def resolve_session(token):
        if not token or not two_access_enabled():
            return None
        with accounts.db() as db:
            found = db.execute('''
                SELECT p.*,s.csrf,s.expires_at FROM initial_manager_sessions s
                JOIN initial_manager_profiles p ON p.id=s.profile_id
                WHERE s.token_hash=? AND s.expires_at>? AND p.active=1
            ''', (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
            if not found:
                return None
            profile = dict(found)
            company = db.execute('SELECT * FROM companies WHERE id=?', (profile['company_id'],)).fetchone()
            if not company:
                return None
            company = dict(company)
            user = manager_user(profile, company)
            if not allowed_company(accounts, user, company['id'], db):
                return None
            return {'user': user, 'csrf': profile['csrf'], 'expires_at': profile['expires_at']}

    def revoke_session(token):
        if token:
            with accounts.db() as db:
                db.execute('DELETE FROM initial_manager_sessions WHERE token_hash=?',
                           (hashlib.sha256(token.encode()).hexdigest(),))

    app.state.resolve_company_manager_session = resolve_session
    app.state.revoke_company_manager_session = revoke_session

    def actor(request):
        user = request.state.user
        if not user:
            raise HTTPException(401, 'Sign in required')
        if not two_access_enabled():
            raise HTTPException(404, 'Initial two-access workspace is not enabled')
        if role(user) not in {'master', 'company_admin'}:
            raise HTTPException(403, 'This workspace is for Super Access and Company Manager')
        return user

    def super_access(request):
        user = actor(request)
        if role(user) != 'master':
            raise HTTPException(403, 'Super Access required')
        return user

    def company_for_master(db, cid):
        found = db.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()
        if not found:
            raise HTTPException(404, 'Company not found')
        return dict(found)

    def profile_for_company(db, company, name=None):
        existing = db.execute('SELECT * FROM initial_manager_profiles WHERE company_id=?',
                              (company['id'],)).fetchone()
        if existing:
            return dict(existing)
        now = timestamp()
        value = {'id': str(uuid.uuid4()), 'company_id': company['id'],
                 'name': name or company['name']+' manager', 'active': 1,
                 'created_at': now, 'updated_at': now}
        db.execute('INSERT INTO initial_manager_profiles VALUES(?,?,?,?,?,?)',
                   (value['id'], value['company_id'], value['name'], 1, now, now))
        return value

    @app.get('/api/initial/overview')
    def overview(request: Request):
        user = actor(request)
        is_master = role(user) == 'master'
        with accounts.db() as db:
            companies = [dict(row) for row in db.execute('SELECT * FROM companies ORDER BY name')
                         if allowed_company(accounts, user, row['id'], db)]
            company_ids = {c['id'] for c in companies}
            profiles = [dict(row) for row in db.execute('SELECT * FROM initial_manager_profiles ORDER BY name')
                        if row['company_id'] in company_ids]
            profiles_by_company = {p['company_id']: p for p in profiles}
            users = []
            for row in db.execute('SELECT * FROM users ORDER BY created_at DESC'):
                item = accounts.public_user(row)
                if role(item) not in {'master', 'company_admin'}:
                    continue
                if is_master or (role(item) == 'company_admin' and item['company_id'] in company_ids):
                    users.append(item)
            for profile in profiles:
                company = next(c for c in companies if c['id'] == profile['company_id'])
                users.append(manager_user(profile, company))
            facilities = [dict(row) for row in db.execute('SELECT * FROM facilities ORDER BY name')
                          if row['company_id'] in company_ids]
            activities = [json.loads(row['payload']) for row in db.execute('SELECT * FROM activities ORDER BY created_at DESC')
                          if row['company_id'] in company_ids]
            reports = []
            for row in db.execute('SELECT * FROM brsr_reports ORDER BY financial_year DESC,created_at DESC'):
                if row['company_id'] not in company_ids:
                    continue
                profile = json.loads(row['payload'])
                answers = [json.loads(a[0]) for a in db.execute('SELECT payload FROM brsr_answers WHERE report_id=?', (row['id'],))]
                catalogue = db.execute('SELECT payload FROM brsr_catalog_versions WHERE version=?',
                                       (profile['catalog_version'],)).fetchone()
                questions = json.loads(catalogue[0])['items'] if catalogue else []
                required_ids = {q['id'] for q in questions if q.get('required', False)}
                reviewed_ids = {a['question_id'] for a in answers if a['status'] == 'reviewed' and a.get('applicability') != 'missing'}
                profile['progress'] = {'answers': len(answers),
                    'drafts': sum(a['status'] == 'draft' for a in answers),
                    'submitted': sum(a['status'] == 'submitted' for a in answers),
                    'reviewed': len(reviewed_ids), 'required': len(required_ids),
                    'required_reviewed': len(required_ids & reviewed_ids),
                    'completion_percent': round(100*len(required_ids & reviewed_ids)/len(required_ids), 1) if required_ids else 0}
                reports.append(profile)
        with bill_connection() as db:
            bills = [json.loads(row[0]) for row in db.execute('SELECT payload FROM bills ORDER BY rowid DESC')]
        documents = [{k: bill.get(k) for k in ('id', 'company_id', 'filename', 'status', 'created_at', 'created_by')}
                     for bill in bills if allowed_bill(accounts, user, bill)]
        for company in companies:
            cid = company['id']
            company['manager_profile'] = profiles_by_company.get(cid)
            company.update(document_count=sum(d['company_id'] == cid for d in documents),
                           facility_count=sum(f['company_id'] == cid for f in facilities),
                           activity_count=sum(a['company_id'] == cid for a in activities),
                           report_count=sum(r['company_id'] == cid for r in reports),
                           needs_review=sum(d['company_id'] == cid and d['status'] == 'needs_review' for d in documents))
            if not is_master:
                company.pop('firm_id', None)
        stats = {'companies': len(companies), 'users': len(users), 'facilities': len(facilities),
                 'documents': len(documents), 'activities': len(activities), 'reports': len(reports),
                 'needs_review': sum(d['status'] == 'needs_review' for d in documents),
                 'drafts': sum(a['status'] == 'draft' for a in activities),
                 'reviewed': sum(a['status'] == 'reviewed' for a in activities),
                 'manager_profiles': len(profiles)}
        stats.update({key+'_count': stats[key] for key in ('companies', 'users', 'facilities', 'documents', 'activities', 'reports')})
        return visible_record(accounts, user, {'access_stage': 'two', 'role': role(user),
            'label': 'Super Access' if is_master else 'Company Manager',
            'home': '/super-admin' if is_master else '/company-manager',
            'local_access': bool(request.state.local_enabled), 'companies': companies,
            'users': users, 'facilities': facilities, 'documents': documents,
            'activities': activities, 'reports': reports, 'manager_profiles': profiles,
            'stats': stats})

    @app.put('/api/initial/companies/{cid}/manager')
    @app.post('/api/initial/companies/{cid}/manager')
    def update_manager(cid: str, body: ManagerProfile, request: Request):
        user = super_access(request)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            company = company_for_master(db, cid)
            profile = profile_for_company(db, company, body.name)
            db.execute('UPDATE initial_manager_profiles SET name=?,active=?,updated_at=? WHERE id=?',
                       (body.name, int(body.active), timestamp(), profile['id']))
            db.execute('DELETE FROM initial_manager_sessions WHERE profile_id=?', (profile['id'],))
            accounts.audit(user['id'], 'company_manager.updated', profile['id'],
                           {'company_id': cid, 'name': body.name, 'active': body.active}, db)
            result = dict(db.execute('SELECT * FROM initial_manager_profiles WHERE id=?', (profile['id'],)).fetchone())
        return result

    @app.post('/api/initial/companies/{cid}/enter')
    def enter_company(cid: str, request: Request):
        user = super_access(request)
        if not request.state.local_enabled:
            raise HTTPException(403, 'Passwordless company entry is available only on the local development server')
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            company = company_for_master(db, cid)
            profile = profile_for_company(db, company)
            if not profile['active']:
                raise HTTPException(409, 'Company Manager access is disabled')
            manager = manager_user(profile, company)
            if not allowed_company(accounts, manager, cid, db):
                raise HTTPException(409, 'This company or its consulting firm is inactive')
            token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
            db.execute('DELETE FROM initial_manager_sessions WHERE expires_at<=?', (time.time(),))
            db.execute('INSERT INTO initial_manager_sessions VALUES(?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), profile['id'], csrf, time.time()+SESSION_SECONDS))
            accounts.audit(user['id'], 'company_manager.entered', profile['id'], {'company_id': cid}, db)
        accounts.logout(request.cookies.get('trace_session'))
        revoke_session(request.cookies.get('trace_company_manager'))
        revoke_founder = getattr(app.state, 'revoke_local_founder_context', None)
        if revoke_founder:
            revoke_founder(request.cookies.get('trace_local_master'), request.cookies.get('trace_local_return'))
        response = JSONResponse({'home': '/company-manager', 'company': {'id': cid, 'name': company['name']}, 'user': manager})
        response.set_cookie('trace_company_manager', token, max_age=SESSION_SECONDS,
                            httponly=True, samesite='strict', secure=request.url.scheme == 'https', path='/')
        response.delete_cookie('trace_session', path='/')
        response.delete_cookie('trace_local_master', path='/')
        response.delete_cookie('trace_local_return', path='/')
        return response
