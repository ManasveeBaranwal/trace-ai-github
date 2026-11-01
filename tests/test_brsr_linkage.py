"""Regression: evidence used by BRSR must retain its company ownership."""
import unittest
from unittest.mock import patch
import test_flow

product = test_flow.product


class BrsrEvidenceLink(unittest.TestCase):
    setUp = test_flow.Flow.setUp
    fixture = test_flow.Flow.fixture

    def test_bill_in_current_or_historical_disclosure_cannot_be_reassigned(self):
        a = self.client.post('/api/admin/companies', json={'name':'BRSR evidence fixture A'}).json()['id']
        b = self.client.post('/api/admin/companies', json={'name':'BRSR evidence fixture B'}).json()['id']
        with patch.object(product, 'launch'):
            response = self.client.post('/api/bills', data={'company_id':a}, files={'file':('fixture.pdf', self.fixture())})
        self.assertEqual(response.status_code, 202, response.text)
        record = response.json();record['status']='needs_review';product.save(record)
        report = self.client.post('/api/brsr/reports?company_id='+a, json={'name':'Fictional BRSR evidence test','financial_year':'2026-27','previous_financial_year':'2025-26','reporting_start':'2026-04-01','reporting_end':'2027-03-31','boundary':'standalone','listed':True})
        self.assertEqual(report.status_code, 201, report.text)
        url='/api/brsr/reports/'+report.json()['id']+'/answers/A-1'
        body={'values':{'response':'Fictional CIN'},'narrative':'Test fixture only','evidence':[{'type':'bill','reference':record['id'],'page':1}],'applicability':'applicable','status':'draft','expected_revision':0}
        response=self.client.put(url,json=body)
        self.assertEqual(response.status_code,200,response.text)
        assignment='/api/admin/bills/'+record['id']+'/company'
        self.assertEqual(self.client.put(assignment,json={'company_id':b}).status_code,409)
        response=self.client.put(url,json={**body,'evidence':[],'expected_revision':1})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.put(assignment,json={'company_id':b}).status_code,409)
        self.assertEqual(self.client.put(assignment,json={'company_id':a}).status_code,200)
