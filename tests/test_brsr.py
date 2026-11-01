"""BRSR preparation tests use an isolated DB and explicitly fictional catalogue.

These exercise data integrity and access rules, not regulatory conformance.
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from access import install_access
from brsr import install_brsr
from hierarchy import UserInput, create_account
from security import Accounts, timestamp


FIXTURE_CATALOG = {'version': 'fixture-brsr-v1', 'coverage_note': 'FICTIONAL TEST CATALOGUE: not a reporting standard.', 'items': [
    {'id': 'A-1', 'section': 'A', 'principle': None, 'title': 'Fixture company disclosure', 'required': True, 'core': False, 'source_url': 'https://example.org/fixture',
     'fields': [{'id': 'company_name', 'label': 'Company name', 'type': 'text', 'required': True}]},
    {'id': 'P6-E-1', 'section': 'C', 'principle': 6, 'title': 'Fixture energy disclosure', 'required': True, 'core': True, 'source_url': 'https://example.org/fixture',
     'fields': [{'id': 'renewable', 'label': 'Renewable quantity', 'type': 'number', 'min': 0, 'required': True}, {'id': 'total', 'label': 'Total quantity', 'type': 'number', 'min': 0, 'required': True}],
     'calculations': [{'id': 'share', 'label': 'Fixture renewable share', 'operator': 'percent', 'operation': 'percentage', 'inputs': ['renewable'], 'numerator': 'renewable', 'denominator': 'total', 'unit': '%'}]},
    {'id': 'P3-E-1', 'section': 'C', 'principle': 3, 'title': 'Fixture workforce table', 'required': True, 'core': False, 'source_url': 'https://example.org/fixture',
     'fields': [{'id': 'people', 'label': 'Fixture workforce', 'type': 'table', 'required': True, 'columns': [{'id': 'group', 'label': 'Group', 'type': 'text', 'required': True}, {'id': 'count', 'label': 'People', 'type': 'integer', 'min': 0, 'required': True}, {'id': 'note', 'label': 'Optional note', 'type': 'text'}]}]}
]}


class Brsr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='trace-brsr-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'research').mkdir()
        (self.root/'research/brsr-disclosures.json').write_text(json.dumps(FIXTURE_CATALOG))
        self.accounts = Accounts(self.root/'accounts.sqlite3')
        owner_id = self.accounts.create_master('owner@brsr-fixture.example', 'Fixture owner', 'Fixture-password-123')
        with self.accounts.db() as db:
            db.execute('CREATE TABLE facilities(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),name TEXT NOT NULL,location TEXT,meter TEXT,active INTEGER,created_at TEXT)')
            self.owner = self.accounts.public_user(db.execute('SELECT * FROM users WHERE id=?', (owner_id,)).fetchone())
            for fid in ['firm-a', 'firm-b']:
                db.execute('INSERT INTO firms VALUES(?,?,?,?,?,?,?)', (fid, 'Fixture '+fid, 1, 5, 5, 5, timestamp()))
            for cid, fid in [('client-a', 'firm-a'), ('client-b', 'firm-b')]:
                db.execute('INSERT INTO companies(id,name,industry,country,active,created_at,firm_id,data_entry_limit) VALUES(?,?,?,?,?,?,?,?)', (cid, 'Fixture '+cid, '', 'India', 1, timestamp(), fid, 5))
        self.bills = {
            'bill-a': {'id': 'bill-a', 'company_id': 'client-a', 'created_by': owner_id, 'filename': 'Fixture A.pdf', 'sha256': 'a'*64, 'page_count': 2},
            'bill-b': {'id': 'bill-b', 'company_id': 'client-b', 'created_by': owner_id, 'filename': 'Fixture B.pdf', 'sha256': 'b'*64, 'page_count': 1}}
        def get_bill(bid):
            if bid not in self.bills:
                raise HTTPException(404, 'Bill not found')
            return self.bills[bid]
        @contextmanager
        def bill_connection():
            db = sqlite3.connect(self.root/'bills.sqlite3')
            db.execute('CREATE TABLE IF NOT EXISTS bills(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            try:
                with db:
                    yield db
            finally:
                db.close()
        self.app = FastAPI()
        install_access(self.app, self.accounts, get_bill, lambda b: None, bill_connection, self.root, {}, lambda bid: None)
        install_brsr(self.app, self.accounts, self.root, get_bill=get_bill)
        self.client = self.login('owner@brsr-fixture.example')
        self.admin, self.admin_id = self.user('company_admin', company_id='client-a')
        self.entry, self.entry_id = self.user('data_entry', company_id='client-a')
        self.consultant, self.consultant_id = self.user('consultant', firm_id='firm-a')
        self.foreign, _ = self.user('consultant_manager', firm_id='firm-b')
        self.rid = self.make_report()

    def login(self, email):
        client = TestClient(self.app)
        result = client.post('/auth/login', json={'email': email, 'password': 'Fixture-password-123'})
        self.assertEqual(result.status_code, 200, result.text)
        client.headers['X-CSRF-Token'] = result.json()['csrf']
        return client

    def user(self, selected_role, **assignment):
        email = selected_role+'@brsr-fixture.example'
        result = create_account(self.accounts, self.owner, UserInput(name='Fixture '+selected_role, email=email, role=selected_role, password='Fixture-password-123', **assignment))
        return self.login(email), result['id']

    def profile(self):
        return {'name': 'Fixture BRSR preparation', 'financial_year': '2025-26', 'previous_financial_year': '2024-25', 'reporting_start': '2025-04-01', 'reporting_end': '2026-03-31', 'boundary': 'standalone', 'listed': True}

    def make_report(self, client=None, cid='client-a'):
        response = (client or self.client).post('/api/brsr/reports?company_id='+cid, json=self.profile())
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def url(self, qid='A-1'):
        return '/api/brsr/reports/'+self.rid+'/answers/'+qid

    def answer(self, values=None, **changes):
        return {'values': values if values is not None else {'company_name': 'Fixture client'}, 'narrative': '', 'evidence': [{'type': 'reference', 'reference': 'Fixture annual report, page 2'}], 'applicability': 'applicable', 'status': 'submitted', 'expected_revision': 0, **changes}

    def snapshot(self):
        response = self.admin.post('/api/brsr/reports/'+self.rid+'/snapshots', json={'confirmed': True})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def test_tenant_scope_and_anonymous_protection(self):
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get('/api/brsr/catalog').status_code, 401)
        for suffix in ['', '/snapshots']:
            self.assertEqual(self.foreign.get('/api/brsr/reports/'+self.rid+suffix).status_code, 404)
        self.assertEqual(self.foreign.put(self.url(), json=self.answer()).status_code, 404)
        self.assertEqual(self.foreign.get('/api/brsr/reports?company_id=client-a').status_code, 404)
        self.assertEqual(self.admin.get('/api/brsr/reports?company_id=client-b').status_code, 404)
        self.assertEqual(self.admin.get('/api/brsr/reports/'+self.rid).status_code, 200)
        self.assertEqual(self.consultant.get('/api/brsr/reports/'+self.rid).status_code, 200)

    def test_assignment_boundaries_and_draft_only_contributor(self):
        detail = self.entry.get('/api/brsr/reports/'+self.rid).json()
        self.assertEqual(detail['catalog']['items'], [])
        self.assertFalse(detail['permissions']['can_review'])
        self.assertEqual(self.entry.put(self.url(), json=self.answer()).status_code, 404)
        assign = '/api/brsr/reports/'+self.rid+'/assignments/'+self.entry_id
        self.assertEqual(self.consultant.put(assign, json={'question_ids': ['A-1']}).status_code, 403)
        self.assertEqual(self.admin.put(assign, json={'question_ids': ['A-1']}).status_code, 200)
        response = self.entry.put(self.url(), json=self.answer())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'submitted')
        self.assertEqual(self.entry.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 1}).status_code, 403)
        self.assertEqual(self.entry.post('/api/brsr/reports/'+self.rid+'/snapshots', json={'confirmed': True}).status_code, 403)
        self.assertEqual(self.entry.put(self.url('P6-E-1'), json=self.answer({'renewable': '1', 'total': '2'})).status_code, 404)
        reviewed = self.admin.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 1})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(self.entry.put(self.url(), json=self.answer(expected_revision=2)).status_code, 403)
        # Revoking the assignment immediately removes both values and editing access.
        self.admin.put(assign, json={'question_ids': []})
        self.assertEqual(self.entry.get('/api/brsr/reports/'+self.rid).json()['answers'], [])

    def test_review_validation_revisions_and_no_missing_as_zero(self):
        saved = self.admin.put(self.url(), json=self.answer(values={}, applicability='missing'))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.admin.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 1}).status_code, 422)
        self.assertEqual(self.admin.put(self.url(), json=self.answer(expected_revision=0)).status_code, 409)
        self.assertEqual(self.admin.put(self.url(), json=self.answer(expected_revision=1, evidence=[])).status_code, 200)
        self.assertEqual(self.admin.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 2}).status_code, 422)
        self.assertEqual(self.admin.put(self.url(), json=self.answer(expected_revision=2)).status_code, 200)
        self.assertEqual(self.admin.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 3}).status_code, 200)
        # Further edits return a reviewed answer to submitted/draft and retain old versions.
        self.assertEqual(self.consultant.put(self.url(), json=self.answer(expected_revision=4, status='draft')).status_code, 200)
        with self.accounts.db() as db:
            versions = db.execute('SELECT COUNT(*) FROM brsr_answer_versions WHERE report_id=? AND question_id=?', (self.rid, 'A-1')).fetchone()[0]
        self.assertEqual(versions, 4)

    def test_numeric_table_validation_and_deterministic_calculations(self):
        for value in ['NaN', 'Infinity', '-1']:
            response = self.admin.put(self.url('P6-E-1'), json=self.answer({'renewable': value, 'total': '100'}))
            self.assertEqual(response.status_code, 422, response.text)
        result = self.admin.put(self.url('P6-E-1'), json=self.answer({'renewable': '25', 'total': '100'}))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(Decimal(result.json()['calculations'][0]['value']), Decimal('25'))
        zero = self.admin.put(self.url('P6-E-1'), json=self.answer({'renewable': '0', 'total': '0'}, expected_revision=1))
        self.assertIsNone(zero.json()['calculations'][0]['value'])
        bad = self.admin.put(self.url('P3-E-1'), json=self.answer({'people': [{'group': 'Workers', 'count': '3.5'}]}))
        self.assertEqual(bad.status_code, 422)
        unknown = self.admin.put(self.url('P3-E-1'), json=self.answer({'people': [{'group': 'Workers', 'count': '3', 'injected': 'x'}]}))
        self.assertEqual(unknown.status_code, 422)
        good = self.admin.put(self.url('P3-E-1'), json=self.answer({'people': [{'group': 'Workers', 'count': '3'}]}))
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(self.admin.post(self.url('P3-E-1')+'/review', json={'confirmed': True, 'expected_revision': 1}).status_code, 200)

    def test_bill_evidence_ownership_and_page_bounds(self):
        foreign = self.admin.put(self.url(), json=self.answer(evidence=[{'type': 'bill', 'reference': 'bill-b'}]))
        self.assertEqual(foreign.status_code, 422)
        page = self.admin.put(self.url(), json=self.answer(evidence=[{'type': 'bill', 'reference': 'bill-a', 'page': 3}]))
        self.assertEqual(page.status_code, 422)
        good = self.admin.put(self.url(), json=self.answer(evidence=[{'type': 'bill', 'reference': 'bill-a', 'page': 2}]))
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.json()['evidence'][0]['sha256'], 'a'*64)
        self.bills['bill-a']['company_id'] = 'client-b'
        self.assertEqual(self.admin.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 1}).status_code, 422)

    def test_snapshot_is_immutable_escaped_and_hashed(self):
        original = '<script>alert(1)</script>'
        self.admin.put(self.url(), json=self.answer({'company_name': original}))
        self.admin.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 1})
        sid = self.snapshot()
        endpoint = '/api/brsr/snapshots/'+sid+'/export'
        before = self.admin.get(endpoint+'?format=json').json()
        self.assertFalse(before['complete'])
        self.assertEqual(before['payload']['progress']['missing'], 2)
        canonical = json.dumps(before['payload'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        self.assertEqual(before['sha256'], hashlib.sha256(canonical.encode()).hexdigest())
        html = self.admin.get(endpoint).text
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('DATA NOT PROVIDED', html)
        self.assertIn('not an exchange filing', html)
        self.admin.put(self.url(), json=self.answer({'company_name': '=1+1'}, expected_revision=2))
        after = self.admin.get(endpoint+'?format=json').json()
        self.assertEqual(before, after)
        sid2 = self.snapshot()
        self.assertIn("'=1+1", self.admin.get('/api/brsr/snapshots/'+sid2+'/export?format=csv').text)
        self.assertEqual(self.foreign.get(endpoint).status_code, 404)
        self.assertEqual(self.entry.get(endpoint).status_code, 403)

    def test_catalogue_is_pinned_and_profile_years_validated(self):
        changed = dict(FIXTURE_CATALOG, version='fixture-brsr-v2', items=[*FIXTURE_CATALOG['items'], {'id': 'NEW', 'section': 'C', 'title': 'New fixture', 'required': True, 'fields': []}])
        (self.root/'research/brsr-disclosures.json').write_text(json.dumps(changed))
        detail = self.admin.get('/api/brsr/reports/'+self.rid).json()
        self.assertEqual(detail['catalog']['version'], 'fixture-brsr-v1')
        self.assertEqual(len(detail['catalog']['items']), 3)
        duplicate = self.client.post('/api/brsr/reports?company_id=client-a', json=self.profile())
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self.client.post('/api/brsr/reports?company_id=client-a', json={**self.profile(), 'financial_year': '2025-28'}).status_code, 422)
        self.assertEqual(self.client.post('/api/brsr/reports?company_id=client-a', json={**self.profile(), 'previous_financial_year': '2024-x'}).status_code, 422)
        self.assertEqual(self.admin.put('/api/brsr/reports/'+self.rid, json={**self.profile(), 'expected_revision': 999}).status_code, 409)
        self.assertTrue((self.root/'accounts.pre-brsr-v1.sqlite3').exists())
        with self.accounts.db() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_higher_identities_hidden_and_raw_audit_kept(self):
        self.client.put(self.url(), json=self.answer())
        self.consultant.post(self.url()+'/review', json={'confirmed': True, 'expected_revision': 1})
        detail = self.admin.get('/api/brsr/reports/'+self.rid)
        self.assertNotIn(self.owner['id'], detail.text)
        self.assertNotIn(self.consultant_id, detail.text)
        with self.accounts.db() as db:
            raw = db.execute('SELECT payload FROM brsr_answers WHERE report_id=?', (self.rid,)).fetchone()[0]
        self.assertIn(self.owner['id'], raw)
        self.assertIn(self.consultant_id, raw)


if __name__ == '__main__':
    unittest.main()
