"""Cross-module workflow checks for the two-access local development stage."""
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from initial_access_ui_server import build_fixture


class InitialAccessIntegration(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.fixture = build_fixture()
        self.addCleanup(self.fixture.storage.cleanup)
        self.client = self.fixture.client
        response = self.client.post('/api/initial/companies/'+self.fixture.cid+'/enter',
                                    json={'name':'Cedar manager · Fictional QA'})
        self.assertEqual(response.status_code, 200, response.text)
        session = self.client.get('/auth/session').json()
        self.assertEqual(session['user']['role'], 'company_admin')
        self.assertEqual(session['user']['company_id'], self.fixture.cid)
        self.client.headers['X-CSRF-Token'] = session['csrf']

    def test_manager_collects_calculates_reviews_and_exports_own_company(self):
        context = self.client.get('/api/company/context')
        self.assertEqual(context.status_code, 200, context.text)
        self.assertEqual(context.json()['company']['id'], self.fixture.cid)
        self.assertEqual(self.client.get('/api/company/context?company_id='+self.fixture.other).status_code, 403)

        site = self.client.post('/api/facilities', json={
            'name':'Manager-created fictional site', 'location':'Fictional location',
            'meter':'FICTIONAL-METER-02'})
        self.assertEqual(site.status_code, 201, site.text)
        period = self.client.post('/api/reporting-periods', json={
            'name':'November 2026 · Fictional QA', 'start':'2026-11-01', 'end':'2026-11-30'})
        self.assertEqual(period.status_code, 201, period.text)
        activity = self.client.post('/api/activities', json={
            'title':'Manager-created fictional electricity record',
            'facility_id':site.json()['id'], 'period_id':period.json()['id'],
            'category':'electricity', 'scope':'2', 'quantity':'2', 'unit':'MWh',
            'factor_value':'0.5', 'gas':'CO2',
            'factor_source':'Fictional QA only, not an official factor',
            'factor_url':'https://example.org/fictional-qa', 'factor_year':'2026'})
        self.assertEqual(activity.status_code, 201, activity.text)
        calculated = self.client.post('/api/activities/'+activity.json()['id']+'/calculate',
                                      json={'confirmed':True})
        self.assertEqual(calculated.status_code, 200, calculated.text)
        self.assertEqual(calculated.json()['status'], 'reviewed')
        self.assertEqual(Decimal(calculated.json()['calculation']['emissions_kg']), Decimal('1000'))

        report = self.fixture.report
        detail = self.client.get('/api/brsr/reports/'+report)
        self.assertEqual(detail.status_code, 200, detail.text)
        answer = next(a for a in detail.json()['answers'] if a['question_id']=='P1-E-8')
        self.assertEqual(Decimal(answer['calculations'][0]['value']), Decimal('91.25'))
        reviewed = self.client.post('/api/brsr/reports/'+report+'/answers/P1-E-8/review',
                                    json={'confirmed':True,'expected_revision':answer['revision']})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        snapshot = self.client.post('/api/brsr/reports/'+report+'/snapshots', json={'confirmed':True})
        self.assertEqual(snapshot.status_code, 201, snapshot.text)
        exported = self.client.get('/api/brsr/snapshots/'+snapshot.json()['id']+'/export?format=json')
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.json()['payload']['company']['id'], self.fixture.cid)
        self.assertFalse(exported.json()['complete'])

        # A newly scoped manager can also establish its own new reporting year.
        profile = {'name':'FY 2025-26 · Fictional QA', 'financial_year':'2025-26',
                   'previous_financial_year':'2024-25', 'boundary':'standalone',
                   'reporting_start':'2025-04-01', 'reporting_end':'2026-03-31', 'listed':True}
        own = self.client.post('/api/brsr/reports', json=profile)
        self.assertEqual(own.status_code, 201, own.text)
        self.assertEqual(self.client.post('/api/brsr/reports?company_id='+self.fixture.other,
                                         json=profile).status_code, 404)
        self.assertEqual(self.client.post('/api/facilities?company_id='+self.fixture.other,
                                         json={'name':'Forbidden foreign site'}).status_code, 403)
        self.assertEqual(self.client.get('/api/admin/overview').status_code, 403)
        self.assertEqual(self.client.get('/api/bills/'+self.fixture.bill).status_code, 200)


if __name__ == '__main__':
    unittest.main()
