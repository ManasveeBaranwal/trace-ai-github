"""Isolated coefficient governance, tenancy and Decimal calculation tests."""
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coefficients import CoefficientService, FIELDS, install_coefficients
from security import Accounts, timestamp


def factor(**changes):
    item = {
        'name': 'Purchased electricity — fixture grid', 'category': 'electricity',
        'activity': 'Purchased grid electricity', 'value': '0.727',
        'input_unit': 'kWh', 'output_unit': 'kg CO2', 'kind': 'emission_factor',
        'gas_basis': 'CO2', 'gas_coverage': 'CO2 only', 'geography': 'India',
        'reporting_year': 'FY2023–24', 'publication_year': 2025,
        'accounting_boundary': 'Scope 2 generation only; excludes transmission losses and upstream fuel.',
        'methodology': 'Fixture versioned published grid method; not a production factor.',
        'source_title': 'Fixture published grid table', 'source_url': 'https://example.org/fixture-grid',
        'source_locator': 'Table 1, row 4', 'source_excerpt': 'Fixture reference: purchased grid electricity, CO2 only.',
        'source_version': 'Fixture v1', 'scope': 'platform',
    }
    item.update(changes)
    return item


class CoefficientFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='trace-coefficients-isolated-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.accounts = Accounts(self.root / 'accounts.sqlite3')
        with self.accounts.db() as db:
            for fid in ('firm-a', 'firm-b'):
                db.execute('INSERT INTO firms(id,name,created_at) VALUES(?,?,?)', (fid, fid, timestamp()))
            for cid, fid in [('company-a', 'firm-a'), ('company-a2', 'firm-a'), ('company-b', 'firm-b')]:
                db.execute('INSERT INTO companies(id,name,firm_id,created_at) VALUES(?,?,?,?)', (cid, cid, fid, timestamp()))
        self.master = {'id': 'founder-secret-account', 'role': 'master', 'active': True}
        self.manager = {'id': 'manager-a', 'role': 'consultant_manager', 'firm_id': 'firm-a', 'active': True}
        self.consultant = {'id': 'consultant-a', 'role': 'consultant', 'firm_id': 'firm-a', 'active': True}
        self.admin = {'id': 'admin-a', 'role': 'company_admin', 'company_id': 'company-a', 'firm_id': 'firm-a', 'active': True}
        self.entry = {'id': 'entry-a', 'role': 'data_entry', 'company_id': 'company-a', 'firm_id': 'firm-a', 'active': True}
        self.other = {'id': 'admin-b', 'role': 'company_admin', 'company_id': 'company-b', 'firm_id': 'firm-b', 'active': True}
        self.service = CoefficientService(self.accounts, self.root)

    def published(self, user=None, **changes):
        user = user or self.master
        item = self.service.create(user, factor(**changes))
        return self.service.transition(user, item['id'], item['revision'], 'published')

    def assert_http(self, code, operation):
        with self.assertRaises(HTTPException) as result:
            operation()
        self.assertEqual(result.exception.status_code, code, result.exception.detail)


class Coefficients(CoefficientFixture):
    def test_scopes_and_account_identities_are_enforced_on_every_read(self):
        platform = self.published()
        firm = self.published(self.manager, name='Firm method', scope='firm', scope_id='firm-a')
        company = self.published(self.admin, name='Private company method', scope='company', scope_id='company-a')
        sibling = self.published(self.manager, name='Sibling private', scope='company', scope_id='company-a2')
        foreign = self.published(name='Other firm private', scope='firm', scope_id='firm-b')
        expected = {platform['id'], firm['id'], company['id']}
        for user in (self.admin, self.entry):
            listed = self.service.list(user)
            self.assertEqual({r['id'] for r in listed['items']}, expected)
            for hidden in (sibling, foreign):
                self.assert_http(404, lambda: self.service.get(user, hidden['id']))
                self.assert_http(404, lambda: self.service.resolve_for_calculation(user, hidden['id']))
            self.assert_http(404, lambda: self.service.list(user, company_id='company-b'))
            detail = self.service.get(user, platform['id'])
            self.assertNotIn(self.master['id'], json.dumps(detail))
            self.assertIsNone(detail['history'][0]['actor_id'])
        self.assertEqual(len(self.service.list(self.consultant)['items']), 4)
        applicable = self.service.list(self.master, company_id='company-a')['items']
        self.assertEqual({r['id'] for r in applicable}, expected)
        self.assert_http(404, lambda: self.service.resolve_for_calculation(self.master, foreign['id'], 'company-a'))
        with self.accounts.db() as db:
            self.assertEqual(db.execute('SELECT actor_id FROM coefficient_history WHERE version_id=? LIMIT 1', (platform['id'],)).fetchone()[0], self.master['id'])

    def test_role_permissions_and_explicit_company_publication(self):
        for user in (self.admin, self.manager, self.consultant, self.entry):
            self.assert_http(403, lambda: self.service.create(user, factor()))
        for user in (self.entry, self.consultant):
            self.assert_http(403, lambda: self.service.create(user, factor(scope='company', scope_id='company-a')))
        self.assert_http(403, lambda: self.service.create(self.manager, factor(scope='firm', scope_id='firm-b')))
        self.assert_http(403, lambda: self.service.create(self.admin, factor(scope='company', scope_id='company-a2')))
        draft = self.service.create(self.admin, factor(scope='company', scope_id='company-a'))
        self.assertEqual(draft['status'], 'draft')
        self.assertTrue(draft['permissions']['can_publish'])
        self.assert_http(404, lambda: self.service.get(self.entry, draft['id']))
        self.assert_http(409, lambda: self.service.resolve_for_calculation(self.admin, draft['id']))
        approved = self.service.transition(self.admin, draft['id'], draft['revision'], 'published')
        self.assertEqual(self.service.get(self.entry, approved['id'])['item']['scope'], 'company')
        self.assertFalse(self.service.get(self.entry, approved['id'])['item']['permissions']['can_revise'])

    def test_immutable_publication_revision_checks_and_receipt_survival(self):
        first = self.published()
        receipt = self.service.calculate(self.admin, first['id'], '1000', 'kWh', 'company-a')
        self.assertEqual(receipt['result_value'], '727.000')
        self.assertEqual(receipt['result_unit'], 'kg CO2')
        self.assert_http(409, lambda: self.service.update(self.master, first['id'], {'revision': first['revision'], 'value': '1'}))
        revised = self.service.revise(self.master, first['id'], first['revision'])
        self.assertEqual(revised['family_id'], first['family_id'])
        self.assertEqual(revised['version'], 2)
        self.assert_http(409, lambda: self.service.revise(self.master, first['id']))
        self.assert_http(409, lambda: self.service.update(self.master, revised['id'], {'revision': 99, 'value': '0.5'}))
        changed = self.service.update(self.master, revised['id'], {'revision': revised['revision'], 'value': '0.5', 'source_version': 'Fixture v2'})
        self.assert_http(409, lambda: self.service.transition(self.master, changed['id'], revised['revision'], 'published'))
        newest = self.service.transition(self.master, changed['id'], changed['revision'], 'published')
        self.assertEqual(self.service.list(self.admin)['items'][0]['id'], newest['id'])
        self.assertEqual(self.service.resolve_for_calculation(self.admin, first['id'])['snapshot']['value'], '0.727')
        self.service.transition(self.master, first['id'], first['revision'], 'retired')
        self.assert_http(409, lambda: self.service.resolve_for_calculation(self.admin, first['id']))
        with self.accounts.db() as db:
            stored = json.loads(db.execute('SELECT receipt FROM coefficient_calculations WHERE id=?', (receipt['calculation_id'],)).fetchone()[0])
        self.assertEqual(stored, receipt)
        self.assertEqual(stored['factor_snapshot']['value'], '0.727')
        self.assertEqual(stored['factor_snapshot']['sha256'], receipt['factor_snapshot']['sha256'])

    def test_decimal_units_and_no_gas_relabelling(self):
        item = self.published(value='0.12345678901234567890123456789')
        result = self.service.calculate(self.admin, item['id'], '1.5', 'MWh', record=False)
        self.assertEqual(result['normalized_quantity'], '1500.0')
        self.assertEqual(result['result_value'], '185.185183518518518351851851835000')
        self.assertEqual(result['gas_basis'], 'CO2')
        self.assertEqual(result['result_unit'], 'kg CO2')
        self.assert_http(422, lambda: self.service.calculate(self.admin, item['id'], '10', 'L'))
        cv = self.published(name='Qualified fuel', category='fuel_combustion', input_unit='kWh (Net CV)')
        self.assert_http(422, lambda: self.service.calculate(self.admin, cv['id'], '10', 'kWh'))
        conversion = self.published(name='Exact conversion', kind='conversion', value='1000', input_unit='t', output_unit='kg', gas_basis='', gas_coverage='')
        self.assertEqual(self.service.calculate(self.admin, conversion['id'], '2', 't', record=False)['result_unit'], 'kg')
        for invalid in ('NaN', '-1', 'Infinity', '1e-1000000', '0' * 161):
            self.assert_http(422, lambda: self.service.calculate(self.admin, item['id'], invalid, 'kWh'))

    def test_publication_requires_source_boundary_and_method(self):
        draft = self.service.create(self.master, factor(source_locator='', methodology=''))
        self.assert_http(422, lambda: self.service.transition(self.master, draft['id'], draft['revision'], 'published'))
        self.assertEqual(self.service.list(self.master)['total'], 0)
        self.assert_http(422, lambda: self.service.create(self.master, factor(source_url='javascript:alert(1)')))
        self.assert_http(422, lambda: self.service.create(self.master, factor(valid_from='2026-12-31', valid_to='2026-01-01')))
        mismatch = self.service.create(self.master, factor(output_unit='kg CO2e', gas_basis='CO2'))
        self.assert_http(422, lambda: self.service.transition(self.master, mismatch['id'], mismatch['revision'], 'published'))
        self.assert_http(422, lambda: self.service.update(self.master, draft['id'], {'revision': 1, 'scope': 'company', 'scope_id': 'company-a'}))

    @staticmethod
    def csv_rows(rows):
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()

    def test_csv_preview_and_atomic_commit(self):
        good = factor(name='Imported good')
        bad = factor(name='Imported bad', value='not a number')
        csv_text = self.csv_rows([good, bad])
        preview = self.service.import_csv(self.master, csv_text)
        self.assertFalse(preview['valid'])
        self.assertEqual(preview['errors'][0]['row'], 3)
        result = self.service.import_csv(self.master, csv_text, commit=True)
        self.assertFalse(result['committed'])
        self.assertEqual(self.service.list(self.master, status='all')['total'], 0)
        good_csv = self.csv_rows([good, factor(name='Second good')])
        self.assertTrue(self.service.import_csv(self.master, good_csv)['valid'])
        self.assertEqual(self.service.list(self.master, status='all')['total'], 0)
        imported = self.service.import_csv(self.master, good_csv, commit=True)
        self.assertTrue(imported['committed'])
        self.assertEqual({row['status'] for row in imported['items']}, {'draft'})
        self.assertEqual(self.service.list(self.master, status='all')['total'], 2)
        blocked = self.service.import_csv(self.admin, good_csv, commit=True)
        self.assertFalse(blocked['valid'])
        self.assertEqual(self.service.list(self.master, status='all')['total'], 2)

    def test_seed_installation_is_reviewed_idempotent_and_additive(self):
        (self.root / 'research').mkdir()
        seed = {'version': 'fixture-v1', 'items': [dict(factor(), id='official-1', reviewed=True), dict(factor(name='Unreviewed'), id='official-2', reviewed=False)]}
        (self.root / 'research' / 'coefficient-seed.json').write_text(json.dumps(seed))
        seeded = CoefficientService(self.accounts, self.root)
        self.assertIsNone(seeded.seed_warning)
        self.assertEqual(seeded.list(self.master)['total'], 1)
        self.assertEqual(seeded.list(self.master, status='draft')['total'], 1)
        self.assertEqual(seeded.list(self.master)['items'][0]['provenance']['seed_id'], 'official-1')
        again = CoefficientService(self.accounts, self.root)
        self.assertEqual(again.list(self.master, status='all')['total'], 2)
        self.assertTrue((self.root / 'accounts.pre-coefficients-v1.sqlite3').exists())
        with self.accounts.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM companies').fetchone()[0], 3)

    def test_api_contract_and_static_routes(self):
        app = FastAPI()
        users = {'master': self.master, 'admin': self.admin, 'entry': self.entry}
        @app.middleware('http')
        async def fixture_identity(request, call_next):
            request.state.user = users.get(request.headers.get('x-fixture-user'))
            return await call_next(request)
        install_coefficients(app, self.accounts, self.root)
        client = TestClient(app)
        self.assertEqual(client.get('/api/coefficients').status_code, 401)
        client.headers['x-fixture-user'] = 'master'
        template = client.get('/api/coefficients/template')
        self.assertEqual(template.status_code, 200)
        self.assertIn('source_locator', template.text)
        draft = client.post('/api/coefficients', json=factor()).json()['item']
        published = client.post('/api/coefficients/' + draft['id'] + '/publish', json={'revision': draft['revision']})
        self.assertEqual(published.status_code, 200, published.text)
        client.headers['x-fixture-user'] = 'admin'
        calc = client.post('/api/coefficients/calculate', json={'version_id': draft['id'], 'quantity': '100', 'unit': 'kWh', 'company_id': 'company-a'})
        self.assertEqual(calc.status_code, 200, calc.text)
        self.assertEqual(calc.json()['result_value'], '72.700')
        self.assertEqual(client.get('/api/coefficients?company_id=company-b').status_code, 404)
        self.assertEqual(client.get('/api/knowledge/status').json()['retrieval_backend'], 'sqlite_fts5')
        asked = client.post('/api/knowledge/ask', json={'question': 'Purchased electricity India 2023-24'})
        self.assertEqual(asked.status_code, 200, asked.text)
        self.assertEqual(asked.json()['mode'], 'retrieval_only')
        self.assertEqual(asked.json()['coefficients'][0]['id'], draft['id'])


if __name__ == '__main__':
    unittest.main()
