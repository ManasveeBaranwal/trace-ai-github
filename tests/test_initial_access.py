"""Two-role local sessions: real tenant scope, no founder fallback or fake users."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from access import install_access
from brsr import install_brsr
from hierarchy import install_hierarchy
from initial_access import install_initial_access
from portal import install_portal
from security import Accounts


class InitialAccess(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TRACE_TWO_ACCESS': '1', 'TRACE_REQUIRE_LOGIN': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='trace-initial-access-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'static').mkdir()
        for filename in ['dashboards.html', 'initial-access.html', 'entry.html', 'login.html', 'admin.html', 'index.html']:
            (self.root/'static'/filename).write_text('<html>Temporary access test</html>')
        (self.root/'research').mkdir()
        shutil.copyfile(ROOT/'research/brsr-disclosures.json', self.root/'research/brsr-disclosures.json')
        self.accounts = Accounts(self.root/'accounts.sqlite3')
        self.owner_id = self.accounts.create_master('owner@fixture.example', 'Fixture Owner', 'Fixture-password-123')
        self.app = FastAPI()
        self.bills = {}

        @contextmanager
        def bill_connection():
            db = sqlite3.connect(self.root/'bills.sqlite3')
            try:
                db.execute('CREATE TABLE IF NOT EXISTS bills(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                with db:
                    yield db
            finally:
                db.close()

        self.bill_connection = bill_connection

        def get_bill(bid):
            if bid not in self.bills:
                raise HTTPException(404, 'Bill not found')
            return self.bills[bid]

        factor = {'value': '0.5', 'source': 'Fixture factor', 'url': 'https://example.org/factor', 'year': '2024-25', 'region': 'Fixture', 'gas': 'CO2'}
        install_access(self.app, self.accounts, get_bill, lambda b: None, bill_connection, self.root, factor, lambda bid: None)
        install_portal(self.app, self.accounts, self.root, bill_connection, get_bill, factor)
        install_hierarchy(self.app, self.accounts, self.root, bill_connection)
        install_brsr(self.app, self.accounts, self.root, get_bill)
        install_initial_access(self.app, self.accounts, self.root, bill_connection, get_bill)

        @self.app.get('/api/bills/{bid}')
        def bill(bid: str):
            return get_bill(bid)

        self.master = self.local_client()
        self.master.get('/super-admin')
        self.csrf(self.master)
        self.company_a = self.create_company('Company A')
        self.company_b = self.create_company('Company B')
        for bid, cid in [('bill-a', self.company_a), ('bill-b', self.company_b)]:
            item = {'id': bid, 'company_id': cid, 'filename': bid+'.pdf', 'status': 'needs_review', 'created_by': self.owner_id, 'created_at': '2026-11-01T00:00:00Z'}
            self.bills[bid] = item
            with bill_connection() as db:
                db.execute('INSERT INTO bills VALUES(?,?)', (bid, json.dumps(item)))

    def local_client(self):
        return TestClient(self.app, base_url='http://127.0.0.1:8002', client=('127.0.0.1', 41001))

    def csrf(self, client):
        session = client.get('/auth/session').json()
        client.headers['X-CSRF-Token'] = session['csrf'] or ''
        return session

    def create_company(self, name):
        response = self.master.post('/api/access/companies', json={'name': name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def enter(self, cid=None):
        response = self.master.post('/api/initial/companies/'+(cid or self.company_a)+'/enter')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['home'], '/company-manager')
        session = self.csrf(self.master)
        self.assertEqual(session['user']['role'], 'company_admin')
        return self.master

    def test_scoped_session_has_no_fake_account_or_higher_directory(self):
        manager = self.enter()
        payload = manager.get('/api/initial/overview').json()
        self.assertEqual([c['id'] for c in payload['companies']], [self.company_a])
        self.assertEqual([d['id'] for d in payload['documents']], ['bill-a'])
        self.assertEqual(payload['stats']['documents'], 1)
        self.assertEqual(payload['stats']['manager_profiles'], 1)
        self.assertNotIn(self.company_b, json.dumps(payload))
        self.assertNotIn(self.owner_id, json.dumps(payload))
        self.assertEqual({u['role'] for u in payload['users']}, {'company_admin'})
        self.assertEqual(manager.get('/api/access/overview').json()['users'], [])
        with self.accounts.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)
            stored = db.execute('SELECT token_hash,csrf FROM initial_manager_sessions').fetchone()
            token = manager.cookies.get('trace_company_manager')
            self.assertNotEqual(stored['token_hash'], token)
            self.assertEqual(stored['token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.assertTrue(manager.get('/auth/session').json()['local_company_mode'])
        self.assertFalse(manager.get('/auth/session').json()['demo_mode'])

    def test_company_scope_enforced_on_existing_workflows(self):
        manager = self.enter()
        self.assertEqual([c['id'] for c in manager.get('/api/companies').json()], [self.company_a])
        self.assertEqual(manager.get('/api/bills/bill-a').status_code, 200)
        self.assertEqual(manager.get('/api/bills/bill-b').status_code, 404)
        self.assertIn(manager.get('/api/company/context?company_id='+self.company_b).status_code, {403, 404})
        self.assertEqual(manager.get('/api/brsr/reports?company_id='+self.company_b).status_code, 404)
        self.assertEqual(manager.get('/api/admin/overview').status_code, 403)
        self.assertEqual(manager.put('/api/admin/factor', json={}).status_code, 403)
        self.assertEqual(manager.post('/api/initial/companies/'+self.company_b+'/enter').status_code, 403)
        self.assertEqual(manager.put('/api/initial/companies/'+self.company_b+'/manager', json={'name': 'Attempt'}).status_code, 403)
        own = manager.post('/api/facilities', json={'name': 'Company A meter room'})
        self.assertEqual(own.status_code, 201, own.text)
        foreign = manager.post('/api/facilities?company_id='+self.company_b, json={'name': 'Other site'})
        self.assertIn(foreign.status_code, {403, 404})

    def test_entry_requires_master_csrf_and_same_origin(self):
        url = '/api/initial/companies/'+self.company_a+'/enter'
        denied = self.master.post(url, headers={'X-CSRF-Token': ''})
        self.assertEqual(denied.status_code, 403)
        denied = self.master.post(url, headers={'Origin': 'https://elsewhere.example'})
        self.assertEqual(denied.status_code, 403)
        anonymous = self.local_client()
        self.assertEqual(anonymous.post(url).status_code, 401)
        self.assertIsNone(anonymous.get('/auth/session').json()['user'])
        with self.accounts.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM initial_manager_profiles').fetchone()[0], 0)

    def test_local_only_entry_and_real_account_precedence(self):
        remote = TestClient(self.app, client=('198.51.100.5', 41002))
        remote.get('/super-admin')
        self.assertIsNone(remote.get('/auth/session').json()['user'])
        login = remote.post('/auth/login', json={'email': 'owner@fixture.example', 'password': 'Fixture-password-123'})
        self.assertEqual(login.status_code, 200)
        remote.headers['X-CSRF-Token'] = login.json()['csrf']
        self.assertEqual(remote.post('/api/initial/companies/'+self.company_a+'/enter').status_code, 403)
        with patch.dict(os.environ, {'TRACE_REQUIRE_LOGIN': '1'}):
            local = self.local_client()
            login = local.post('/auth/login', json={'email': 'owner@fixture.example', 'password': 'Fixture-password-123'})
            self.assertEqual(login.status_code, 200)
            local.headers['X-CSRF-Token'] = login.json()['csrf']
            self.assertEqual(local.post('/api/initial/companies/'+self.company_a+'/enter').status_code, 403)
        manager = self.enter()
        company_token = manager.cookies.get('trace_company_manager')
        anonymous_remote = TestClient(self.app, base_url='http://127.0.0.1:8002', client=('198.51.100.6', 41003))
        anonymous_remote.cookies.set('trace_company_manager', company_token, domain='127.0.0.1', path='/')
        self.assertIsNone(anonymous_remote.get('/auth/session').json()['user'])
        self.assertEqual(anonymous_remote.get('/api/initial/overview').status_code, 401)
        login = manager.post('/auth/login', json={'email': 'owner@fixture.example', 'password': 'Fixture-password-123'})
        self.assertEqual(login.status_code, 200)
        # Even a copied local manager cookie cannot override the real account.
        manager.cookies.set('trace_company_manager', company_token, domain='127.0.0.1', path='/')
        self.assertEqual(manager.get('/auth/session').json()['user']['role'], 'master')
        manager.cookies.set('trace_session', 'invalid-real-session', domain='127.0.0.1', path='/')
        self.assertIsNone(manager.get('/auth/session').json()['user'])

    def test_expired_cookie_blocks_founder_and_has_explicit_recovery(self):
        founder_cookie = self.master.cookies.get('trace_local_master')
        manager = self.enter()
        manager.cookies.set('trace_local_master', founder_cookie, domain='127.0.0.1', path='/')
        with self.accounts.db() as db:
            db.execute('UPDATE initial_manager_sessions SET expires_at=0')
        self.assertIsNone(manager.get('/auth/session').json()['user'])
        self.assertTrue(manager.get('/auth/session').json()['recovery_available'])
        self.assertEqual(manager.get('/super-admin', follow_redirects=False).headers['location'], '/login?next=%2Fsuper-admin')
        self.assertEqual(manager.post('/auth/logout', headers={'Origin': 'https://elsewhere.example'}).status_code, 403)
        recovered = manager.post('/auth/logout', headers={'Origin': 'http://127.0.0.1:8002'})
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertTrue(recovered.json()['recovered'])
        self.assertIsNone(manager.get('/auth/session').json()['user'])
        manager.get('/super-admin')
        self.assertEqual(manager.get('/auth/session').json()['user']['role'], 'master')

    def test_company_cannot_elevate_and_logout_revokes_session(self):
        manager = self.enter()
        self.assertEqual(manager.get('/super-admin', follow_redirects=False).headers['location'], '/company-manager')
        self.assertEqual(manager.get('/auth/session').json()['user']['company_id'], self.company_a)
        token = manager.cookies.get('trace_company_manager')
        self.assertEqual(manager.post('/auth/logout', headers={'X-CSRF-Token': ''}).status_code, 403)
        self.assertEqual(manager.post('/auth/logout').status_code, 200)
        self.assertIsNone(manager.get('/auth/session').json()['user'])
        manager.cookies.set('trace_company_manager', token, domain='127.0.0.1', path='/')
        self.assertIsNone(manager.get('/auth/session').json()['user'])
        self.assertEqual(manager.get('/api/initial/overview').status_code, 401)

    def test_entering_company_revokes_founder_auth_and_return_context(self):
        token = self.master.cookies.get('trace_local_master')
        hint = self.master.cookies.get('trace_local_return')
        manager = self.enter()
        self.assertIsNone(manager.cookies.get('trace_local_master'))
        self.assertIsNone(manager.cookies.get('trace_local_return'))
        self.assertFalse(manager.get('/auth/session').json()['local_entry_available'])
        replay = self.local_client()
        replay.cookies.set('trace_local_master', token, domain='127.0.0.1', path='/')
        replay.cookies.set('trace_local_return', hint, domain='127.0.0.1', path='/')
        session = replay.get('/auth/session').json()
        self.assertIsNone(session['user'])
        self.assertFalse(session['local_entry_available'])
        self.assertEqual(replay.get('/api/admin/overview').status_code, 401)

    def test_disable_company_invalidates_manager_session(self):
        manager = self.enter()
        with self.accounts.db() as db:
            db.execute('UPDATE companies SET active=0 WHERE id=?', (self.company_a,))
        self.assertIsNone(manager.get('/auth/session').json()['user'])
        self.assertEqual(manager.get('/api/initial/overview').status_code, 401)

    def test_disabling_firm_or_manager_profile_revokes_effective_access(self):
        firm = self.master.post('/api/access/firms', json={'name': 'Fixture parent firm'})
        self.assertEqual(firm.status_code, 201, firm.text)
        firm_id = firm.json()['id']
        with self.accounts.db() as db:
            db.execute('UPDATE companies SET firm_id=? WHERE id=?', (firm_id, self.company_a))
        manager = self.enter()
        with self.accounts.db() as db:
            db.execute('UPDATE firms SET active=0 WHERE id=?', (firm_id,))
        self.assertIsNone(manager.get('/auth/session').json()['user'])
        founder = self.local_client()
        founder.get('/super-admin')
        self.csrf(founder)
        self.assertEqual(founder.post('/api/initial/companies/'+self.company_a+'/enter').status_code, 409)
        with self.accounts.db() as db:
            db.execute('UPDATE firms SET active=1 WHERE id=?', (firm_id,))
        self.assertEqual(founder.post('/api/initial/companies/'+self.company_a+'/enter').status_code, 200)
        self.csrf(founder)
        with self.accounts.db() as db:
            db.execute('UPDATE initial_manager_profiles SET active=0 WHERE company_id=?', (self.company_a,))
        self.assertIsNone(founder.get('/auth/session').json()['user'])

    def test_two_role_provisioning_and_profile_audit(self):
        result = self.master.put('/api/initial/companies/'+self.company_a+'/manager', json={'name': 'Named local manager'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue((self.root/'accounts.pre-initial-access-v1.sqlite3').exists())
        self.assertEqual(self.master.post('/api/access/users', json={'role': 'consultant'}).status_code, 403)
        manager = self.enter()
        self.assertEqual(manager.get('/auth/session').json()['user']['name'], 'Named local manager')
        self.assertEqual(manager.post('/api/access/users', json={'role': 'data_entry'}).status_code, 403)
        self.accounts.audit(manager.get('/auth/session').json()['user']['id'], 'fixture.local_action', self.company_a)
        founder = self.local_client()
        founder.get('/super-admin')
        self.csrf(founder)
        events = founder.get('/api/admin/overview').json()['audit']
        event = next(e for e in events if e['action'] == 'fixture.local_action')
        self.assertEqual(event['actor_email'], 'Named local manager')
        # Profile update revokes prior sessions, just as account changes do.
        renamed = founder.put('/api/initial/companies/'+self.company_a+'/manager', json={'name': 'Updated manager'})
        self.assertEqual(renamed.status_code, 200)
        self.assertIsNone(manager.get('/auth/session').json()['user'])


if __name__ == '__main__':
    unittest.main()
