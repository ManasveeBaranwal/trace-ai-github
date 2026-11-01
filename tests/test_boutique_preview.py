"""The boutique preview is an isolated firm identity, never a founder bypass."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from access import install_access
from boutique_preview import install_boutique_preview
from security import Accounts, timestamp


class BoutiquePreview(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'0','TRACE_BOUTIQUE_PREVIEW':'1','TRACE_TWO_ACCESS':'0'})
        env.start();self.addCleanup(env.stop)
        temp=tempfile.TemporaryDirectory(prefix='trace-boutique-auth-')
        self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        (self.root/'static').mkdir()
        for name in ['boutique.html','boutique-entry.html','entry.html','login.html']:
            (self.root/'static'/name).write_text('<html>Boutique fixture</html>')
        self.accounts=Accounts(self.root/'accounts.sqlite3')
        self.metadata={'kind':'trace-boutique-fictional-preview-v1','firm_id':'sample-firm',
                       'profile_id':'sample-manager','created_at':timestamp()}
        (self.root/'.boutique-preview.json').write_text(json.dumps(self.metadata))
        with self.accounts.db() as db:
            db.execute('INSERT INTO firms VALUES(?,?,?,?,?,?,?)',('sample-firm','Fictional firm',1,5,10,5,timestamp()))
        self.app=self.build()
        self.client=TestClient(self.app,base_url='http://localhost',client=('127.0.0.1',45001))

    def build(self):
        app=FastAPI()
        @contextmanager
        def bills():
            db=sqlite3.connect(':memory:')
            db.execute('CREATE TABLE bills(id TEXT,payload TEXT)')
            try:yield db
            finally:db.close()
        install_access(app,self.accounts,lambda bid:{},lambda item:None,bills,self.root,{},lambda bid:None)
        install_boutique_preview(app,self.accounts,self.root)
        @app.get('/super-admin')
        def founder(request:Request):
            return {'role':'master'} if request.state.user and request.state.user['role']=='master' else RedirectResponse('/login',303)
        @app.get('/api/probe')
        @app.post('/api/probe')
        def probe(request:Request):return {'user':request.state.user}
        return app

    def enter(self):
        self.assertEqual(self.client.get('/boutique').status_code,200)
        session=self.client.get('/auth/session').json()
        self.client.headers['X-CSRF-Token']=session['csrf']
        return session

    def test_entry_is_real_firm_scope_and_csrf_protected(self):
        session=self.enter()
        self.assertEqual(session['user']['role'],'consultant_manager')
        self.assertEqual(session['user']['firm_id'],'sample-firm')
        self.assertEqual(session['home'],'/boutique')
        self.assertTrue(session['local_boutique_mode'])
        self.assertFalse(session['demo_mode'])
        self.assertFalse(session['local_entry_available'])
        self.assertEqual(self.client.get('/api/admin/overview').status_code,403)
        self.assertEqual(self.client.post('/api/probe',headers={'X-CSRF-Token':''}).status_code,403)
        self.assertEqual(self.client.post('/api/probe').status_code,200)
        token=self.client.cookies.get('trace_boutique_session')
        with self.accounts.db() as db:
            saved=db.execute('SELECT token_hash FROM boutique_preview_sessions').fetchone()[0]
            self.assertEqual(saved,hashlib.sha256(token.encode()).hexdigest())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)

    def test_real_or_company_cookie_blocks_local_firm_entry(self):
        self.enter()
        for name in ['trace_session','trace_company_manager']:
            client=TestClient(self.app,base_url='http://localhost',client=('127.0.0.1',45001))
            client.cookies.update(self.client.cookies)
            client.cookies.set(name,'expired-other-identity',domain='localhost.local',path='/')
            self.assertIsNone(client.get('/auth/session').json()['user'])
            self.assertEqual(client.get('/api/probe').status_code,401)
            self.assertEqual(client.get('/boutique',follow_redirects=False).status_code,303)

    def test_preview_cannot_enter_founder_before_or_after_logout(self):
        self.assertNotEqual(self.client.get('/super-admin',follow_redirects=False).status_code,200)
        self.enter()
        self.assertEqual(self.client.get('/api/probe').json()['user']['role'],'consultant_manager')
        token=self.client.cookies.get('trace_boutique_session')
        self.assertEqual(self.client.post('/auth/logout').status_code,200)
        self.assertIsNone(self.client.get('/auth/session').json()['user'])
        self.assertNotEqual(self.client.get('/super-admin',follow_redirects=False).status_code,200)
        replay=TestClient(self.app,base_url='http://localhost',client=('127.0.0.1',45001))
        replay.cookies.set('trace_boutique_session',token,domain='localhost.local',path='/')
        self.assertEqual(replay.get('/api/probe').status_code,401)

    def test_persistent_cookie_survives_restart_but_not_expiry(self):
        self.enter()
        second=TestClient(self.build(),base_url='http://localhost',client=('127.0.0.1',45001))
        second.cookies.update(self.client.cookies)
        self.assertEqual(second.get('/auth/session').json()['user']['firm_id'],'sample-firm')
        with self.accounts.db() as db:db.execute('UPDATE boutique_preview_sessions SET expires_at=0')
        self.assertIsNone(second.get('/auth/session').json()['user'])
        self.assertTrue(second.get('/auth/session').json()['recovery_available'])

    def test_remote_and_login_required_reject_same_cookie(self):
        self.enter()
        remote=TestClient(self.app,base_url='http://localhost',client=('198.51.100.2',45001))
        remote.cookies.update(self.client.cookies)
        self.assertIsNone(remote.get('/auth/session').json()['user'])
        self.assertEqual(remote.get('/api/probe').status_code,401)
        with patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'1'}):
            self.assertIsNone(self.client.get('/auth/session').json()['user'])
            self.assertEqual(self.client.get('/api/probe').status_code,401)

    def test_marker_required_and_cross_site_entry_denied(self):
        self.assertEqual(self.client.get('/boutique',headers={'Sec-Fetch-Site':'cross-site'},follow_redirects=False).status_code,303)
        (self.root/'.boutique-preview.json').unlink()
        with self.assertRaises(RuntimeError):self.build()


if __name__=='__main__':unittest.main()
