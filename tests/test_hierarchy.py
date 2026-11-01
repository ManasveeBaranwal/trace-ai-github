"""Real sessions, tenant isolation, seat enforcement and contributor permissions."""
import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
import test_flow
from security import Accounts, password_hash, timestamp

product = test_flow.product


class Hierarchy(unittest.TestCase):
    def setUp(self):
        test_flow.Flow.setUp(self)
        self.firm = self.post(self.client, '/api/access/firms', {'name': 'Fixture Firm '+uuid.uuid4().hex[:6], 'consultant_limit': 1, 'company_limit': 2, 'default_data_entry_limit': 1})
        self.other_firm = self.post(self.client, '/api/access/firms', {'name': 'Other Fixture Firm '+uuid.uuid4().hex[:6]})
        self.company = self.post(self.client, '/api/access/companies', {'name': 'Fixture Client', 'firm_id': self.firm})
        self.other_company = self.post(self.client, '/api/access/companies', {'name': 'Other Fixture Client', 'firm_id': self.other_firm})
        self.site = self.post(self.client, '/api/facilities?company_id='+self.company, {'name': 'Assigned site'})
        self.unassigned = self.post(self.client, '/api/facilities?company_id='+self.company, {'name': 'Unassigned site'})
        self.period = self.post(self.client, '/api/reporting-periods?company_id='+self.company, {'name': 'Fixture period', 'start': '2026-01-01', 'end': '2026-12-31'})
        self.manager, _ = self.account('consultant_manager', firm_id=self.firm)
        self.consultant, _ = self.account('consultant', firm_id=self.firm)
        self.admin, _ = self.account('company_admin', company_id=self.company)
        self.entry, self.entry_id = self.account('data_entry', company_id=self.company, facility_ids=[self.site])

    def post(self, client, path, body):
        r = client.post(path, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()['id']

    def account(self, selected_role, **assignment):
        email = uuid.uuid4().hex+'@fixture.example'
        uid = self.post(self.client, '/api/access/users', {'name': 'Fixture '+selected_role, 'email': email, 'password': 'Fixture-password-123', 'role': selected_role, **assignment})
        client = TestClient(product.app)
        login = client.post('/auth/login', json={'email': email, 'password': 'Fixture-password-123'})
        self.assertEqual(login.status_code, 200, login.text)
        client.headers['X-CSRF-Token'] = login.json()['csrf']
        return client, uid

    def activity(self, site=None):
        return {'title': 'Fixture electricity', 'facility_id': site or self.site, 'period_id': self.period, 'category': 'electricity', 'scope': '2', 'quantity': '100', 'unit': 'kWh', 'factor_value': '0.5', 'gas': 'CO2', 'factor_source': 'Fixture source', 'factor_url': 'https://example.org/fixture-factor', 'factor_year': '2026'}

    def test_five_dashboards_and_role_redirects(self):
        for client, role, path in [(self.client, 'master', '/super-admin'), (self.manager, 'consultant_manager', '/consultant-manager'), (self.consultant, 'consultant', '/consultant'), (self.admin, 'company_admin', '/company-admin'), (self.entry, 'data_entry', '/data-entry')]:
            self.assertEqual(client.get('/auth/session').json()['user']['role'], role)
            self.assertEqual(client.get('/', follow_redirects=False).headers['location'], path)
            self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.get('/api/access/overview').json()['role'], role)
            if role != 'master':
                self.assertEqual(client.get('/super-admin', follow_redirects=False).headers['location'], path)
        self.assertEqual(self.manager.get('/preview/master').status_code, 403)
        self.assertEqual(self.manager.get('/api/access/overview?preview_role=master').status_code, 403)
        for role in ['master', 'consultant_manager', 'consultant', 'company_admin', 'data_entry']:
            self.assertEqual(self.client.get('/preview/'+role).status_code, 200)
            self.assertTrue(self.client.get('/api/access/overview?preview_role='+role).json()['preview'])

    def test_firm_and_company_isolation(self):
        for client in [self.manager, self.consultant, self.admin, self.entry]:
            self.assertEqual([c['id'] for c in client.get('/api/companies').json()], [self.company])
            self.assertEqual(client.get('/api/company/dashboard?company_id='+self.other_company).status_code in {403, 404}, True)
            overview = client.get('/api/access/overview')
            self.assertNotIn(self.other_company, overview.text)
            self.assertNotIn(self.other_firm, overview.text)
            self.assertNotIn('password_hash', overview.text)
            self.assertEqual(client.put('/api/admin/factor', json=product.FACTOR).status_code, 403)
        self.assertEqual(self.consultant.post('/api/access/users', json={'name': 'Escalation', 'email': 'escalate@fixture.example', 'password': 'Fixture-password-123', 'role': 'master'}).status_code, 403)

    def test_directory_is_same_level_and_downward_only(self):
        # Peers stay visible, but no role can discover higher-level accounts.
        peer_manager, _ = self.account('consultant_manager', firm_id=self.firm)
        self.account('company_admin', company_id=self.company)
        foreign_manager, foreign_id = self.account('consultant_manager', firm_id=self.other_firm)
        expected = [(self.client, {'master','consultant_manager','consultant','company_admin','data_entry'}),
                    (self.manager, {'consultant_manager','consultant','company_admin','data_entry'}),
                    (self.consultant, {'consultant','company_admin','data_entry'}),
                    (self.admin, {'company_admin','data_entry'}),
                    (self.entry, {'data_entry'})]
        owner_id = self.client.get('/auth/session').json()['user']['id']
        manager_id = self.manager.get('/auth/session').json()['user']['id']
        consultant_id = self.consultant.get('/auth/session').json()['user']['id']
        for client, allowed in expected:
            people = client.get('/api/access/overview').json()['users']
            self.assertEqual({u['role'] for u in people}, allowed)
            if client is not self.client:
                self.assertNotIn(owner_id, [u['id'] for u in people])
                self.assertNotIn(foreign_id, [u['id'] for u in people])
        self.assertEqual(sum(u['role']=='consultant_manager' for u in peer_manager.get('/api/access/overview').json()['users']), 2)
        self.assertEqual(self.consultant.patch('/api/access/users/'+manager_id,json={'active':False}).status_code,403)
        self.assertEqual(self.admin.patch('/api/access/users/'+consultant_id,json={'active':False}).status_code,404)
        # Seeing peers never grants permission to manage them.
        self.assertEqual(self.manager.patch('/api/access/users/'+manager_id,json={'active':False}).status_code,403)

    def test_local_mode_never_overrides_a_lower_role(self):
        import os
        with patch.dict(os.environ, {'TRACE_REQUIRE_LOGIN':'0'}):
            client=TestClient(product.app,client=('127.0.0.1',45003))
            client.get('/super-admin')
            self.assertEqual(client.get('/auth/session').json()['user']['role'],'master')
            with product.accounts.db() as db:
                email=db.execute('SELECT email FROM users WHERE id=?',(self.entry_id,)).fetchone()[0]
            result=client.post('/auth/login',json={'email':email,'password':'Fixture-password-123'})
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(client.get('/auth/session').json()['user']['role'],'data_entry')
            self.assertFalse(client.get('/auth/session').json()['demo_mode'])
            self.assertEqual(client.get('/super-admin',follow_redirects=False).headers['location'],'/data-entry')
            self.assertEqual(client.get('/api/admin/overview').status_code,403)
            self.assertEqual(client.get('/preview/master').status_code,403)
            client.post('/auth/logout',headers={'X-CSRF-Token':result.json()['csrf']})
            self.assertIsNone(client.get('/auth/session').json()['user'])
            self.assertEqual(client.get('/api/admin/overview').status_code,401)

    def test_shared_records_hide_higher_account_ids_but_preserve_audit_storage(self):
        aid=self.post(self.client,'/api/activities?company_id='+self.company,self.activity())
        owner_id=self.client.get('/auth/session').json()['user']['id']
        consultant_id=self.consultant.get('/auth/session').json()['user']['id']
        self.consultant.put('/api/activities/'+aid,json={**self.activity(),'quantity':'200'})
        self.consultant.post('/api/activities/'+aid+'/calculate',json={'confirmed':True})
        for client in [self.admin,self.entry]:
            for url in ['/api/activities','/api/company/dashboard','/api/access/overview']:
                result=client.get(url)
                self.assertEqual(result.status_code,200,result.text)
                self.assertNotIn(owner_id,result.text)
                self.assertNotIn(consultant_id,result.text)
        with product.accounts.db() as db:
            raw=db.execute('SELECT payload FROM activities WHERE id=?',(aid,)).fetchone()[0]
        self.assertIn(owner_id,raw)
        self.assertIn(consultant_id,raw)

    def test_manager_provisioning_and_founder_limits(self):
        response = self.manager.post('/api/access/companies', json={'name': 'Second fixture client'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.manager.post('/api/access/companies', json={'name': 'Over quota'}).status_code, 409)
        self.assertEqual(self.manager.post('/api/access/companies', json={'name': 'Higher allowance', 'data_entry_limit': 999}).status_code, 403)
        self.assertEqual(self.manager.post('/api/access/companies', json={'name': 'Another firm', 'firm_id': self.other_firm}).status_code, 403)
        self.assertEqual(self.manager.put('/api/access/firms/'+self.firm, json={'name': 'Escalated quota', 'consultant_limit': 99}).status_code, 403)
        payload = {'name': 'Over quota consultant', 'email': uuid.uuid4().hex+'@fixture.example', 'password': 'Fixture-password-123', 'role': 'consultant', 'firm_id': self.firm}
        self.assertEqual(self.manager.post('/api/access/users', json=payload).status_code, 409)
        payload['firm_id'] = self.other_firm
        self.assertEqual(self.manager.post('/api/access/users', json=payload).status_code, 403)
        self.assertEqual(self.manager.put('/api/access/companies/'+self.company, json={'name': 'Fixture Client', 'active': False}).status_code, 200)
        self.assertEqual(self.admin.get('/api/access/overview').status_code, 401)
        self.assertIn(self.company, self.manager.get('/api/access/overview').text)
        self.assertEqual(self.manager.put('/api/access/companies/'+self.company, json={'name': 'Fixture Client', 'active': True}).status_code, 200)

    def test_data_entry_seat_release_reactivation_and_role_escalation(self):
        body = {'name': 'New contributor', 'email': uuid.uuid4().hex+'@fixture.example', 'password': 'Fixture-password-123', 'role': 'data_entry', 'company_id': self.company, 'facility_ids': [self.site]}
        self.assertEqual(self.admin.post('/api/access/users', json=body).status_code, 409)
        self.assertEqual(self.admin.patch('/api/access/users/'+self.entry_id, json={'active': False}).status_code, 200)
        self.assertEqual(self.entry.get('/api/activities').status_code, 401)
        self.post(self.admin, '/api/access/users', body)
        self.assertEqual(self.admin.patch('/api/access/users/'+self.entry_id, json={'active': True}).status_code, 409)
        body.update(role='company_admin', email=uuid.uuid4().hex+'@fixture.example', facility_ids=[])
        self.assertEqual(self.admin.post('/api/access/users', json=body).status_code, 403)
        self.assertEqual(self.entry.post('/api/access/users', json=body).status_code, 401)

    def test_concurrent_provisioning_cannot_overbook_last_seat(self):
        from concurrent.futures import ThreadPoolExecutor
        from fastapi import HTTPException
        from hierarchy import create_account, UserInput
        self.admin.patch('/api/access/users/'+self.entry_id, json={'active': False})
        actor = self.admin.get('/auth/session').json()['user']
        def attempt(index):
            try:
                create_account(product.accounts, actor, UserInput(name='Concurrent fixture', email=uuid.uuid4().hex+'@fixture.example', password='Fixture-password-123', role='data_entry', company_id=self.company))
                return 201
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, [1, 2])), [201, 409])

    def test_assigned_facility_drafts_and_review_permissions(self):
        aid = self.post(self.entry, '/api/activities', self.activity())
        hidden = self.post(self.client, '/api/activities?company_id='+self.company, self.activity(self.unassigned))
        self.assertEqual(self.entry.post('/api/activities', json=self.activity(self.unassigned)).status_code, 403)
        facilities = self.entry.get('/api/facilities').json()
        self.assertEqual([f['id'] for f in facilities], [self.site])
        self.assertNotIn(hidden, self.entry.get('/api/activities').text)
        self.assertNotIn(hidden, self.entry.get('/api/access/overview').text)
        self.assertEqual(self.entry.put('/api/activities/'+hidden, json=self.activity()).status_code, 404)
        self.assertEqual(self.entry.post('/api/activities/'+aid+'/calculate', json={'confirmed': True}).status_code, 403)
        self.assertEqual(self.entry.get('/api/company/export').status_code, 403)
        self.assertEqual(self.entry.post('/api/facilities', json={'name': 'Escalated facility'}).status_code, 403)
        self.assertEqual(self.entry.put('/api/activities/'+aid, json={**self.activity(), 'quantity': '200'}).status_code, 200)
        result = self.consultant.post('/api/activities/'+aid+'/calculate', json={'confirmed': True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['calculation']['emissions_kg'], '100.0')
        self.assertEqual(self.entry.put('/api/activities/'+aid, json=self.activity()).status_code, 403)
        self.assertEqual(self.admin.get('/api/company/export').status_code, 200)

    def test_bill_draft_submission_then_consultant_review(self):
        with patch.object(product, 'launch'):
            response = self.entry.post('/api/bills', files={'file': ('fixture.pdf', self.client.get('/api/sample').content)})
        self.assertEqual(response.status_code, 202, response.text)
        bill = response.json()
        bill.update(status='needs_review', fields={key: {'value': None, 'evidence': None} for key in product.FIELDS})
        product.save(bill)
        bid = bill['id']
        body = test_flow.Flow.review_body(self)
        response = self.entry.post('/api/bills/'+bid+'/draft', json={'fields': body['fields']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('calculation', response.json())
        self.assertEqual(self.entry.post('/api/bills/'+bid+'/review', json=body).status_code, 403)
        self.assertEqual(self.entry.get('/api/bills/'+bid+'/export').status_code, 403)
        self.assertEqual(self.manager.get('/api/bills/'+bid).status_code, 200)
        self.assertEqual(self.consultant.get('/api/bills/'+bid).json()['draft_fields'], body['fields'])
        other, _ = self.account('consultant_manager', firm_id=self.other_firm)
        self.assertEqual(other.get('/api/bills/'+bid).status_code, 404)
        self.assertNotIn(bid, other.get('/api/bills').text)
        self.assertEqual(self.consultant.post('/api/bills/'+bid+'/review', json=body).status_code, 200)
        self.assertEqual(self.entry.post('/api/bills/'+bid+'/draft', json={'fields': body['fields']}).status_code, 409)

    def test_suspending_firm_ends_client_and_consultant_access(self):
        response = self.client.put('/api/access/firms/'+self.firm, json={'name': 'Suspended Fixture Firm', 'active': False, 'consultant_limit': 1, 'company_limit': 2})
        self.assertEqual(response.status_code, 200, response.text)
        for client in [self.manager, self.consultant, self.admin, self.entry]:
            self.assertEqual(client.get('/api/access/overview').status_code, 401)


class Migration(unittest.TestCase):
    def test_legacy_accounts_and_credentials_survive(self):
        path = Path(tempfile.mkdtemp())/'accounts.sqlite3'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE companies(id TEXT PRIMARY KEY,name TEXT NOT NULL,industry TEXT NOT NULL,country TEXT NOT NULL,active INTEGER NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('master','company_user')),company_id TEXT REFERENCES companies(id),password_hash TEXT NOT NULL,active INTEGER NOT NULL,created_at TEXT NOT NULL);''')
            db.execute('INSERT INTO companies VALUES(?,?,?,?,?,?)', ('company', 'Retained company', '', 'India', 1, timestamp()))
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?)', ('old-user', 'retained@fixture.example', 'Retained user', 'company_user', 'company', password_hash('Fixture-password-123'), 1, timestamp()))
        accounts = Accounts(path)
        self.assertTrue(path.with_name('accounts.pre-hierarchy-v1.sqlite3').exists())
        result = accounts.authenticate('retained@fixture.example', 'Fixture-password-123', 'testclient')
        self.assertIsNotNone(result)
        self.assertEqual(result[2]['role'], 'company_admin')
        self.assertEqual(result[2]['company_id'], 'company')
        # Startup is idempotent and does not replace retained records.
        Accounts(path)
        with accounts.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
