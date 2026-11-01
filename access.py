import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from security import SESSION_SECONDS, password_hash, timestamp
from hierarchy import allowed_bill, allowed_company, home, create_account, update_account, UserInput, UserUpdate
from local_founder import LocalFounderSessions, RETURN_SECONDS, safe_return_to

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)

class Company(Strict):
    name: str = Field(min_length=2,max_length=160)
    industry: str = Field(default='',max_length=120)
    country: str = Field(default='',max_length=100)
    active: bool = True

class NewUser(Strict):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    email: str = Field(max_length=254)
    name: str = Field(min_length=1,max_length=120)
    role: Literal['master','company_user'] = 'company_user'
    company_id: str | None = None
    password: str = Field(min_length=12,max_length=256)
    @field_validator('email')
    @classmethod
    def email_valid(cls, value):
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):
            raise ValueError('Enter a valid email address')
        return value.lower()

class UserChange(Strict):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    active: bool
    password: str | None = Field(default=None,min_length=12,max_length=256)

class Assignment(Strict):
    company_id: str | None

class Factor(Strict):
    value: Decimal = Field(ge=0,le=10,allow_inf_nan=False)
    source: str = Field(min_length=3,max_length=1000)
    url: str = Field(min_length=8,max_length=2000)
    year: str = Field(min_length=2,max_length=100)
    region: str = Field(min_length=2,max_length=100)
    gas: Literal['CO2','CO2e'] = 'CO2'
    @field_validator('url')
    @classmethod
    def url_valid(cls, value):
        if urlsplit(value).scheme not in {'https','http'} or not urlsplit(value).netloc:
            raise ValueError('Use an HTTP source URL')
        return value

def install_access(app, accounts, get_bill, save_bill, bill_connection, root, default_factor, launch):
    founder = LocalFounderSessions(accounts)
    app.state.revoke_local_founder_context = founder.revoke
    demo_user = {'id':'local-demo-master','email':'','name':'Local master','role':'master','company_id':None,'active':True,'created_at':timestamp()}
    allowed_hosts = [host.strip() for host in os.getenv('TRACE_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver,[::1]').split(',') if host.strip()]
    render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()
    if render_hostname:
        if not re.fullmatch(r'[A-Za-z0-9.-]+', render_hostname):
            raise RuntimeError('RENDER_EXTERNAL_HOSTNAME must be a hostname')
        allowed_hosts.append(render_hostname)
    if not allowed_hosts or '*' in allowed_hosts:
        raise RuntimeError('TRACE_ALLOWED_HOSTS requires explicit hostnames, not a wildcard')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    def master(request):
        if request.state.user['role'] != 'master':
            raise HTTPException(403,'Master access required')
        return request.state.user

    @app.middleware('http')
    async def access_control(request, call_next):
        path = request.url.path
        public_demo = bool(getattr(app.state, 'public_demo_active', False))
        local_enabled = os.getenv('TRACE_REQUIRE_LOGIN','1') == '0' and request.client and request.client.host in {'127.0.0.1','::1'}
        account_cookie = request.cookies.get('trace_session')
        # Public demo identity comes only from the isolated fictional store.
        # A normal account cookie never becomes a demo or founder credential.
        session = None if public_demo else accounts.session(account_cookie)
        company_cookie = request.cookies.get('trace_company_manager')
        boutique_cookie = request.cookies.get('trace_boutique_session')
        local_boutique_mode = False
        local_company_mode = False
        resolver = getattr(app.state, 'resolve_company_manager_session', None)
        if local_enabled and not account_cookie and not session and company_cookie and resolver:
            session = resolver(company_cookie)
            local_company_mode = bool(session)
        boutique_resolver = getattr(app.state, 'resolve_boutique_session', None)
        if (local_enabled or public_demo) and not account_cookie and not company_cookie and not session and boutique_cookie and boutique_resolver:
            session = boutique_resolver(boutique_cookie)
            local_boutique_mode = bool(session)
        # A real account always keeps its role, even on the founder's local Mac.
        # Passwordless founder access starts only at the separate private entry URL.
        local_cookie = request.cookies.get('trace_local_master','')
        return_hint = request.cookies.get('trace_local_return','')
        local_credentials_allowed = bool(local_enabled and not public_demo and not account_cookie and not company_cookie and not boutique_cookie and not session
                                         and not getattr(app.state, 'boutique_preview_active', False))
        founder_session = founder.resolve(local_cookie) if local_credentials_allowed else None
        enter_local = bool(local_credentials_allowed
                           and not founder_session
                           and path == '/super-admin' and request.method == 'GET'
                           and request.headers.get('sec-fetch-site') != 'cross-site'
                           and request.headers.get('sec-fetch-dest','document') == 'document')
        issued_token = issued_hint = None
        if enter_local:
            issued_token, issued_hint, founder_session = founder.issue()
        demo_mode = bool(local_credentials_allowed and founder_session)
        if demo_mode:
            session = {'user':{**demo_user, 'created_at':founder_session['created_at']},
                       'csrf':founder_session['csrf'], 'expires_at':founder_session['expires_at']}
        request.state.demo_mode = demo_mode
        request.state.local_company_mode = local_company_mode
        request.state.local_boutique_mode = local_boutique_mode
        request.state.local_enabled = bool(local_enabled)
        request.state.public_demo_mode = public_demo
        request.state.user = session['user'] if session else None
        request.state.session = session
        request.state.local_entry_available = bool(local_credentials_allowed and
            (enter_local or founder.recognized(local_cookie, return_hint)))
        changing = request.method not in {'GET','HEAD','OPTIONS'}
        if changing:
            origin = request.headers.get('origin')
            expected = str(request.base_url).rstrip('/')
            if request.headers.get('sec-fetch-site') == 'cross-site' or (origin and origin != expected):
                return JSONResponse({'detail':'Cross-origin request rejected'},403)
        if public_demo:
            # These POSTs only retrieve scoped source records or end a session.
            # Everything that could save data, extract documents, create a
            # collection capability, review a report or grant access is denied.
            read_only_posts = {'/api/boutique/ai/ask', '/api/knowledge/ask', '/auth/logout'}
            if path.startswith('/collect/') or (changing and not (request.method == 'POST' and path in read_only_posts)):
                return JSONResponse({'detail':'This public demo is read-only. Use the local workspace to change data.',
                                     'public_demo_mode':True,'read_only':True},403,
                                    headers={'Cache-Control':'no-store'})
        protected = path.startswith('/api/') or path in {'/extract','/docs','/redoc','/openapi.json','/docs/oauth2-redirect'}
        if protected:
            if not session:
                return JSONResponse({'detail':'Sign in required'},401,headers={'Cache-Control':'no-store'})
            if changing and not hmac.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
                return JSONResponse({'detail':'Session verification failed. Reload and try again.'},403)
            if (path.startswith('/api/admin/') or path in {'/extract','/docs','/redoc','/openapi.json','/docs/oauth2-redirect'}) and session['user']['role'] != 'master':
                return JSONResponse({'detail':'Master access required'},403)
            if os.getenv('TRACE_TWO_ACCESS','0') == '1' and changing and path in {'/api/access/users','/api/admin/users'}:
                # Retain existing future-role accounts, while provisioning only
                # the initial two roles through either account-creation API.
                try:
                    selected_role = (await request.json()).get('role')
                except (ValueError, AttributeError):
                    selected_role = None
                if session['user']['role'] != 'master' or (selected_role is not None and selected_role not in {'master','company_admin','company_user'}):
                    return JSONResponse({'detail':'Only Super Access and Company Manager can be provisioned in this initial version'},403)
            match = re.match(r'^/api/bills/([^/]+)',path)
            if match:
                try:
                    record = get_bill(match[1])
                except HTTPException:
                    return JSONResponse({'detail':'Bill not found'},404)
                if not allowed_bill(accounts, session['user'], record):
                    return JSONResponse({'detail':'Bill not found'},404)
        response = await call_next(request)
        destination = safe_return_to(request.query_params.get('return_to'))
        if demo_mode and path == '/super-admin' and request.method == 'GET' and destination:
            response = RedirectResponse(destination,303)
        elif (not session and request.method == 'GET' and safe_return_to(path)
              and response.status_code in {302,303,307,308}
              and response.headers.get('location') == '/login'):
            # HTML routes redirect before session.js can retain their target.
            # Preserve only allowlisted internal workspaces, never API/auth URLs.
            requested = path + ('?' + request.url.query if request.url.query else '')
            response.headers['Location'] = '/login?' + urlencode({'next':safe_return_to(requested) or path})
            response.headers['Cache-Control'] = 'no-store'
        if enter_local:
            response.set_cookie('trace_local_master',issued_token,max_age=SESSION_SECONDS,httponly=True,samesite='strict',secure=request.url.scheme=='https',path='/')
            response.set_cookie('trace_local_return',issued_hint,max_age=RETURN_SECONDS,httponly=True,samesite='strict',secure=request.url.scheme=='https',path='/')
        if path.startswith(('/auth/','/api/')) or path in {'/','/admin','/app','/login'}:
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Keep the form's Origin header while stripping capability tokens from
        # Referer URLs. A no-referrer form POST can produce an opaque Origin.
        response.headers['Referrer-Policy'] = 'origin' if path.startswith('/collect/') else 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.get('/login',include_in_schema=False)
    def login_page(request:Request):
        if request.state.user:
            return RedirectResponse(home(request.state.user),303)
        if request.state.public_demo_mode:
            return FileResponse(root/'static/public-demo-entry.html',headers={'Cache-Control':'no-store'})
        if request.state.local_enabled and request.query_params.get('account') != '1':
            if (getattr(app.state, 'boutique_preview_active', False)
                    and not request.cookies.get('trace_session') and not request.cookies.get('trace_company_manager')):
                return FileResponse(root/'static/boutique-entry.html',headers={'Cache-Control':'no-store'})
            return FileResponse(root/'static/entry.html',headers={'Cache-Control':'no-store'})
        return FileResponse(root/'static/login.html')

    @app.get('/admin',include_in_schema=False)
    def admin_page(request:Request):
        if not request.state.user:
            return RedirectResponse('/login',303)
        master(request)
        return FileResponse(root/'static/admin.html')

    @app.get('/app',include_in_schema=False)
    def workbench(request:Request):
        if not request.state.user:
            return RedirectResponse('/login',303)
        return FileResponse(root/'static/index.html')

    @app.get('/auth/session')
    def current_session(request:Request):
        return {'user':request.state.user,'csrf':request.state.session['csrf'] if request.state.session else None,'setup_required':False,'demo_mode':request.state.demo_mode,'home':home(request.state.user),
                'access_stage':'two' if os.getenv('TRACE_TWO_ACCESS','0') == '1' else 'hierarchy',
                'local_company_mode':request.state.local_company_mode,
                'local_boutique_mode':request.state.local_boutique_mode,
                'public_demo_mode':request.state.public_demo_mode,
                'read_only':request.state.public_demo_mode,
                'local_entry_available':request.state.local_entry_available,
                'local_entry_url':'/super-admin' if request.state.local_entry_available else None,
                'recovery_available':bool(not request.state.session and request.state.local_enabled and any(request.cookies.get(k) for k in ('trace_session','trace_local_master','trace_company_manager','trace_boutique_session')))}

    @app.post('/auth/login')
    def login(body:Login,request:Request):
        if request.headers.get('content-type','').split(';')[0] != 'application/json':
            raise HTTPException(415,'Use JSON')
        try:
            result = accounts.authenticate(body.email,body.password,request.client.host)
        except ValueError as exc:
            raise HTTPException(429,str(exc))
        if not result:
            raise HTTPException(401,'Email or password is incorrect, or this account is unavailable.')
        token,csrf,user = result
        founder.revoke(request.cookies.get('trace_local_master'), request.cookies.get('trace_local_return'))
        response = JSONResponse({'user':user,'csrf':csrf})
        response.set_cookie('trace_session',token,max_age=SESSION_SECONDS,httponly=True,samesite='strict',secure=request.url.scheme=='https' or os.getenv('TRACE_COOKIE_SECURE')=='1',path='/')
        response.delete_cookie('trace_local_master',path='/')
        response.delete_cookie('trace_local_return',path='/')
        revoke_local = getattr(app.state, 'revoke_company_manager_session', None)
        if revoke_local:
            revoke_local(request.cookies.get('trace_company_manager'))
        response.delete_cookie('trace_company_manager',path='/')
        boutique_revoke = getattr(app.state, 'revoke_boutique_session', None)
        if boutique_revoke:
            boutique_revoke(request.cookies.get('trace_boutique_session'))
        response.delete_cookie('trace_boutique_session',path='/')
        return response

    @app.post('/auth/logout')
    def logout(request:Request):
        session = request.state.session
        if not session:
            # Explicitly clear a stale local credential without granting a role.
            # Active sessions still require CSRF verification below.
            same_origin = request.headers.get('origin') == str(request.base_url).rstrip('/') or request.headers.get('sec-fetch-site') == 'same-origin'
            if not (request.state.local_enabled and same_origin and (request.state.local_entry_available or any(request.cookies.get(k) for k in ('trace_session','trace_local_master','trace_company_manager','trace_boutique_session')))):
                raise HTTPException(403,'Session verification required')
        elif not hmac.compare_digest(request.headers.get('x-csrf-token',''),session['csrf']):
            raise HTTPException(403,'Session verification required')
        accounts.logout(request.cookies.get('trace_session'))
        retain_return = bool(request.state.local_entry_available)
        founder.revoke(request.cookies.get('trace_local_master'), request.cookies.get('trace_local_return'),
                       keep_return=retain_return)
        revoke_local = getattr(app.state, 'revoke_company_manager_session', None)
        if revoke_local:
            revoke_local(request.cookies.get('trace_company_manager'))
        boutique_revoke = getattr(app.state, 'revoke_boutique_session', None)
        if boutique_revoke:
            boutique_revoke(request.cookies.get('trace_boutique_session'))
        if session:
            accounts.audit(session['user']['id'],'logout',session['user']['id'])
        response = JSONResponse({'ok':True,'recovered':not bool(session),
                                 'next':'/super-admin' if retain_return and not session else '/login'})
        response.delete_cookie('trace_session',path='/')
        response.delete_cookie('trace_local_master',path='/')
        response.delete_cookie('trace_company_manager',path='/')
        response.delete_cookie('trace_boutique_session',path='/')
        if not retain_return:
            response.delete_cookie('trace_local_return',path='/')
        return response

    @app.get('/api/companies')
    def available_companies(request:Request):
        with accounts.db() as db:
            rows = [r for r in db.execute('SELECT * FROM companies ORDER BY name') if allowed_company(accounts,request.state.user,r['id'],db)]
        return [dict(r) for r in rows]

    @app.get('/api/admin/overview')
    def overview(request:Request):
        master(request)
        with accounts.db() as db:
            users = [accounts.public_user(r) for r in db.execute('SELECT * FROM users ORDER BY created_at DESC')]
            companies = [dict(r) for r in db.execute('SELECT * FROM companies ORDER BY created_at DESC')]
            events = [dict(r) for r in db.execute('SELECT a.*,u.email AS actor_email FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 100')]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='initial_manager_profiles'").fetchone():
                local_names = {r['id']:r['name'] for r in db.execute('SELECT id,name FROM initial_manager_profiles')}
                for event in events:
                    if not event['actor_email'] and event['actor_id'] in local_names:
                        event['actor_email'] = local_names[event['actor_id']]
            workspace_counts={c['id']:{'facilities':0,'activities':0} for c in companies}
            for table_name in ['facilities','activities']:
                for row in db.execute('SELECT company_id,COUNT(*) AS total FROM '+table_name+' GROUP BY company_id'):
                    if row['company_id'] in workspace_counts:workspace_counts[row['company_id']][table_name]=row['total']
            sessions = db.execute('SELECT COUNT(*) FROM sessions WHERE expires_at>strftime(\'%s\',\'now\')').fetchone()[0]
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='initial_manager_sessions'").fetchone():
                sessions += db.execute('SELECT COUNT(*) FROM initial_manager_sessions WHERE expires_at>strftime(\'%s\',\'now\')').fetchone()[0]
        with bill_connection() as db:
            bills = [json.loads(row[0]) for row in db.execute('SELECT payload FROM bills ORDER BY rowid DESC')]
        records = [{k:b.get(k) for k in ('id','filename','status','company_id','created_at','created_by','model','page_count','reviewed_at')} for b in bills]
        return {'workspace_counts':workspace_counts,'companies':companies,'users':users,'bills':records,'audit':events,'sessions':sessions,'factor':accounts.get_setting('factor',default_factor)}

    @app.post('/api/admin/companies',status_code=201)
    def create_company(body:Company,request:Request):
        actor = master(request)
        cid = str(uuid.uuid4())
        with accounts.db() as db:
            db.execute('INSERT INTO companies(id,name,industry,country,active,created_at) VALUES(?,?,?,?,?,?)',(cid,body.name,body.industry,body.country,int(body.active),timestamp()))
            accounts.audit(actor['id'],'company.created',cid,body.model_dump(),db)
        return {'id':cid,**body.model_dump()}

    @app.put('/api/admin/companies/{company_id}')
    def update_company(company_id:str,body:Company,request:Request):
        actor = master(request)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM companies WHERE id=?',(company_id,)).fetchone():
                raise HTTPException(404,'Company not found')
            previous=db.execute('SELECT * FROM companies WHERE id=?',(company_id,)).fetchone()
            if body.active and not previous['active'] and previous['firm_id']:
                firm=db.execute('SELECT company_limit FROM firms WHERE id=?',(previous['firm_id'],)).fetchone()
                used=db.execute('SELECT COUNT(*) FROM companies WHERE firm_id=? AND active=1',(previous['firm_id'],)).fetchone()[0]
                if used>=firm[0]:raise HTTPException(409,'Company allowance reached')
            db.execute('UPDATE companies SET name=?,industry=?,country=?,active=? WHERE id=?',(body.name,body.industry,body.country,int(body.active),company_id))
            if not body.active:
                db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE company_id=?)',(company_id,))
            accounts.audit(actor['id'],'company.updated',company_id,body.model_dump(),db)
        return {'ok':True}

    @app.post('/api/admin/users',status_code=201)
    def create_user(body:NewUser,request:Request):
        actor = master(request)
        return create_account(accounts,actor,UserInput(**body.model_dump()))

    @app.patch('/api/admin/users/{user_id}')
    def update_user(user_id:str,body:UserChange,request:Request):
        actor = master(request)
        return update_account(accounts,actor,user_id,UserUpdate(**body.model_dump()))

    @app.post('/api/admin/users/{user_id}/revoke-sessions')
    def revoke(user_id:str,request:Request):
        actor = master(request)
        with accounts.db() as db:
            if not db.execute('SELECT id FROM users WHERE id=?',(user_id,)).fetchone():
                raise HTTPException(404,'User not found')
            db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
            accounts.audit(actor['id'],'sessions.revoked',user_id,db=db)
        return {'ok':True}

    @app.put('/api/admin/bills/{bill_id}/company')
    def assign(bill_id:str,body:Assignment,request:Request):
        actor = master(request)
        record = get_bill(bill_id)
        if record['status'] in {'queued','extracting'}:
            raise HTTPException(409,'Wait for extraction to finish before reassigning')
        if body.company_id:
            with accounts.db() as db:
                row = db.execute('SELECT active FROM companies WHERE id=?',(body.company_id,)).fetchone()
            if not row or not row['active']:
                raise HTTPException(422,'Choose an active company')
        with accounts.db() as db:
            linked=db.execute('SELECT id FROM activities WHERE source_bill_id=?',(bill_id,)).fetchone()
        if linked and record.get('company_id')!=body.company_id:
            raise HTTPException(409,'This evidence is linked to an inventory activity and cannot be reassigned')
        if record.get('company_id')!=body.company_id:
            # Both current and historical evidence links must remain resolvable.
            with accounts.db() as db:
                for table in ['brsr_answers','brsr_answer_versions']:
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                        continue
                    for row in db.execute('SELECT payload FROM '+table):
                        answer=json.loads(row[0])
                        if any(e.get('type')=='bill' and e.get('reference')==bill_id for e in answer.get('evidence',[])):
                            raise HTTPException(409,'This evidence supports a BRSR disclosure and cannot be reassigned')
        previous = record.get('company_id')
        record['company_id'] = body.company_id
        save_bill(record)
        accounts.audit(actor['id'],'bill.assigned',bill_id,{'from':previous,'to':body.company_id})
        return {'ok':True}

    @app.post('/api/admin/bills/{bill_id}/retry')
    def retry(bill_id:str,request:Request):
        actor = master(request)
        record = get_bill(bill_id)
        if record['status'] != 'failed':
            raise HTTPException(409,'Only failed extractions can be retried')
        record.setdefault('extraction_attempts',[]).append({'at':timestamp(),'previous_error':record.get('error'),'previous_error_type':record.get('error_type')})
        record.update(status='queued')
        record.pop('error',None)
        record.pop('error_type',None)
        save_bill(record)
        launch(bill_id)
        accounts.audit(actor['id'],'bill.retried',bill_id)
        return {'ok':True}

    @app.put('/api/admin/factor')
    def set_factor(body:Factor,request:Request):
        actor = master(request)
        payload = body.model_dump(mode='json')
        with accounts.db() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('factor',json.dumps(payload)))
            accounts.audit(actor['id'],'factor.updated','default_factor',payload,db)
        return payload
