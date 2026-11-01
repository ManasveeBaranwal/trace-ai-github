"""Local founder sessions survive restart without becoming role bypasses."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from access import install_access
from security import Accounts


class LocalFounder(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'TRACE_REQUIRE_LOGIN':'0', 'TRACE_TWO_ACCESS':'1'})
        env.start(); self.addCleanup(env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='trace-founder-test-')
        self.addCleanup(self.temp.cleanup)
        self.accounts = Accounts(Path(self.temp.name)/'accounts.sqlite3')
        self.accounts.create_master('owner@fixture.example','Owner','Fixture-password-123')
        self.app = self.build_app()
        self.client = self.client_for(self.app)

    def build_app(self):
        app = FastAPI()
        @contextmanager
        def unused_connection():
            yield None
        install_access(app,self.accounts,lambda bid:None,lambda b:None,
                       unused_connection,ROOT,{},lambda bid:None)
        @app.get('/super-admin')
        def founder(request:Request):
            if not request.state.user:return RedirectResponse('/login',303)
            return {'role':request.state.user['role']}
        @app.get('/api/probe')
        def probe(request:Request):return {'role':request.state.user['role']}
        @app.get('/coefficients')
        def coefficients(request:Request):
            if not request.state.user:return RedirectResponse('/login',303)
            return {'ok':True}
        return app

    def client_for(self,app,host='127.0.0.1'):
        return TestClient(app,base_url='http://127.0.0.1:8002',client=(host,41001))

    def enter(self):
        response=self.client.get('/super-admin')
        self.assertEqual(response.status_code,200,response.text)
        session=self.client.get('/auth/session').json()
        self.client.headers['X-CSRF-Token']=session['csrf']
        return session

    def test_sessions_persist_across_reinstall_and_tokens_are_hashed(self):
        session=self.enter()
        token=self.client.cookies.get('trace_local_master')
        hint=self.client.cookies.get('trace_local_return')
        with self.accounts.db() as db:
            auth=db.execute('SELECT * FROM local_founder_sessions').fetchone()
            returning=db.execute('SELECT * FROM local_founder_returns').fetchone()
        self.assertEqual(auth['token_hash'],hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(returning['token_hash'],hashlib.sha256(hint.encode()).hexdigest())
        self.assertNotEqual(token,auth['token_hash'])
        restarted=self.client_for(self.build_app())
        restarted.cookies.update(self.client.cookies)
        result=restarted.get('/auth/session').json()
        self.assertEqual(result['user']['role'],'master')
        self.assertEqual(result['csrf'],session['csrf'])
        self.assertEqual(result['user']['created_at'],session['user']['created_at'])
        self.assertEqual(restarted.get('/api/probe').status_code,200)

    def test_logout_revokes_auth_retains_only_non_auth_return_context(self):
        self.enter()
        token=self.client.cookies.get('trace_local_master')
        response=self.client.post('/auth/logout')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['next'],'/login')
        session=self.client.get('/auth/session').json()
        self.assertIsNone(session['user'])
        self.assertTrue(session['local_entry_available'])
        self.assertEqual(session['local_entry_url'],'/super-admin')
        self.assertEqual(self.client.get('/api/probe').status_code,401)
        replay=self.client_for(self.app)
        replay.cookies.set('trace_local_master',token,domain='127.0.0.1',path='/')
        self.assertIsNone(replay.get('/auth/session').json()['user'])
        self.assertFalse(replay.get('/auth/session').json()['local_entry_available'])
        self.assertEqual(replay.get('/api/probe').status_code,401)

    def test_expired_known_session_and_hint_have_actionable_explicit_recovery(self):
        self.enter()
        with self.accounts.db() as db:db.execute('UPDATE local_founder_sessions SET expires_at=0')
        result=self.client.get('/auth/session').json()
        self.assertIsNone(result['user'])
        self.assertTrue(result['local_entry_available'])
        denied=self.client.post('/auth/logout',headers={'Origin':'https://elsewhere.example'})
        self.assertEqual(denied.status_code,403)
        recovered=self.client.post('/auth/logout',headers={'Origin':'http://127.0.0.1:8002'})
        self.assertEqual(recovered.status_code,200,recovered.text)
        self.assertEqual(recovered.json()['next'],'/super-admin')
        self.assertIsNone(self.client.get('/auth/session').json()['user'])
        opened=self.client.get('/super-admin?return_to=%2Fcoefficients%23knowledge',follow_redirects=False)
        self.assertEqual(opened.status_code,303)
        self.assertEqual(opened.headers['location'],'/coefficients#knowledge')
        self.assertEqual(self.client.get('/auth/session').json()['user']['role'],'master')

    def test_clean_company_and_real_account_cookies_never_show_owner_recovery(self):
        clean=self.client.get('/auth/session').json()
        self.assertFalse(clean['local_entry_available'])
        self.assertIsNone(clean['local_entry_url'])
        self.enter()
        owner_cookies=self.client.cookies
        for cookie in ['trace_company_manager','trace_session']:
            blocked=self.client_for(self.app)
            blocked.cookies.update(owner_cookies)
            blocked.cookies.set(cookie,'invalid-but-present',domain='127.0.0.1',path='/')
            session=blocked.get('/auth/session').json()
            self.assertIsNone(session['user'])
            self.assertFalse(session['local_entry_available'])
            self.assertIsNone(session['local_entry_url'])
            blocked.get('/super-admin?return_to=/coefficients')
            self.assertIsNone(blocked.get('/auth/session').json()['user'])

    def test_known_expired_token_is_recovery_context_without_a_hint(self):
        self.enter()
        self.client.cookies.delete('trace_local_return',domain='127.0.0.1',path='/')
        with self.accounts.db() as db:
            db.execute('UPDATE local_founder_sessions SET expires_at=?',(time.time()-1,))
        session=self.client.get('/auth/session').json()
        self.assertIsNone(session['user'])
        self.assertTrue(session['local_entry_available'])
        self.assertEqual(self.client.get('/api/probe').status_code,401)

    def test_remote_and_required_login_never_accept_local_credentials(self):
        self.enter()
        remote=self.client_for(self.app,'198.51.100.2')
        remote.cookies.update(self.client.cookies)
        self.assertIsNone(remote.get('/auth/session').json()['user'])
        self.assertFalse(remote.get('/auth/session').json()['local_entry_available'])
        self.assertEqual(remote.get('/api/probe').status_code,401)
        with patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'1'}):
            session=self.client.get('/auth/session').json()
            self.assertIsNone(session['user'])
            self.assertFalse(session['local_entry_available'])
            self.assertEqual(self.client.get('/api/probe').status_code,401)

    def test_real_login_revokes_both_local_tokens(self):
        self.enter()
        token=self.client.cookies.get('trace_local_master')
        hint=self.client.cookies.get('trace_local_return')
        logged=self.client.post('/auth/login',json={'email':'owner@fixture.example','password':'Fixture-password-123'})
        self.assertEqual(logged.status_code,200,logged.text)
        self.assertIsNone(self.client.cookies.get('trace_local_return'))
        replay=self.client_for(self.app)
        replay.cookies.set('trace_local_master',token,domain='127.0.0.1',path='/')
        replay.cookies.set('trace_local_return',hint,domain='127.0.0.1',path='/')
        session=replay.get('/auth/session').json()
        self.assertIsNone(session['user'])
        self.assertFalse(session['local_entry_available'])

    def test_return_to_is_internal_and_only_applied_after_founder_auth(self):
        for target in ['https://elsewhere.example/x','//elsewhere.example','/auth/logout','/api/admin/overview','/static/entry.html','/login','/coefficients\\elsewhere','/coefficients\r\nX-Test:yes']:
            response=self.client.get('/super-admin',params={'return_to':target},follow_redirects=False)
            self.assertEqual(response.status_code,200,(target,response.text))
            self.assertNotIn('location',response.headers)
        valid=self.client.get('/super-admin',params={'return_to':'/brsr?company=fixture#answers'},follow_redirects=False)
        self.assertEqual(valid.status_code,303)
        self.assertEqual(valid.headers['location'],'/brsr?company=fixture#answers')

    def test_html_sign_in_redirect_preserves_only_allowed_workspace_destination(self):
        result=self.client.get('/coefficients?company=fixture',follow_redirects=False)
        self.assertEqual(result.status_code,303)
        target=urlsplit(result.headers['location'])
        self.assertEqual(target.path,'/login')
        self.assertEqual(parse_qs(target.query)['next'],['/coefficients?company=fixture'])
        self.assertEqual(result.headers['cache-control'],'no-store')
        self.assertEqual(self.client.get('/api/probe',follow_redirects=False).status_code,401)
        clean=self.client.get('/login')
        self.assertEqual(clean.status_code,200)


if __name__=='__main__':unittest.main()
