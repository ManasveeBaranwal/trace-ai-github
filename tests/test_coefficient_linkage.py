"""Published coefficient versions survive the bill → ledger → export workflow."""
import csv
import io
import json
import os
import unittest
from unittest.mock import patch

from initial_access_ui_server import build_fixture


class CoefficientLinkage(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{},clear=False)
        env.start();self.addCleanup(env.stop)
        self.fx=build_fixture();self.addCleanup(self.fx.storage.cleanup)
        self.client=self.fx.client

    def definition(self,**overrides):
        values=dict(name='Fictional test electricity factor',category='electricity',
                    value='0.4',input_unit='kWh',output_unit='kg CO2e',
                    gas_basis='CO2e',gas_coverage='Fictional test only',
                    geography='Fictional jurisdiction',reporting_year='2026',
                    source_title='Fictional QA method',source_url='https://example.org/qa',
                    source_locator='Test table 1',source_excerpt='Fictional 0.4 kg CO2e/kWh',
                    source_version='QA v1',accounting_boundary='Purchased electricity test only',
                    methodology='Quantity kWh multiplied by QA factor')
        return {**values,**overrides}

    def publish(self,**overrides):
        created=self.client.post('/api/coefficients',json=self.definition(**overrides))
        self.assertEqual(created.status_code,201,created.text)
        item=created.json()['item']
        result=self.client.post('/api/coefficients/'+item['id']+'/publish',json={'revision':item['revision']})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()['item']

    def review(self,item):
        fields={k:'' for k in self.fx.product.FIELDS}
        fields.update(consumption='2',unit='MWh',supplier='Fictional electricity')
        # Submitted fields deliberately differ: linked calculations must use DB truth.
        return self.client.post('/api/bills/'+self.fx.bill+'/review',json={
            'fields':fields,'factor_value':'0.1','factor_source':'Tampered client value',
            'factor_url':'https://example.org/wrong','factor_year':'1900',
            'factor_region':'Wrong','gas':'CO2','confirmed':True,
            'coefficient_version_id':item['id']})

    def test_bill_ledger_and_exports_keep_published_snapshot(self):
        item=self.publish()
        reviewed=self.review(item)
        self.assertEqual(reviewed.status_code,200,reviewed.text)
        calculation=reviewed.json()['calculation']
        self.assertEqual(calculation['emissions_kg'],'800.000')
        self.assertEqual(calculation['gas'],'CO2e')
        self.assertEqual(calculation['factor']['factor_value'],'0.4')
        self.assertEqual(calculation['factor']['factor_source'],'Fictional QA method')
        self.assertEqual(calculation['coefficient_snapshot']['version_id'],item['id'])
        snapshot=calculation['coefficient_snapshot']
        imported=self.client.post('/api/activities/from-bill/'+self.fx.bill,
                                  params={'company_id':self.fx.cid},json={
            'facility_id':self.fx.site,'period_id':self.fx.period})
        self.assertEqual(imported.status_code,201,imported.text)
        activity=imported.json()
        self.assertEqual(activity['coefficient_snapshot'],snapshot)
        result=self.client.post('/api/activities/'+activity['id']+'/calculate',json={'confirmed':True})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['calculation']['emissions_kg'],'800.0')
        self.assertEqual(result.json()['calculation']['coefficient_snapshot'],snapshot)
        revised=self.client.post('/api/coefficients/'+item['id']+'/revise',json={'revision':item['revision']})
        self.assertEqual(revised.status_code,201,revised.text)
        draft=revised.json()['item']
        update=self.client.put('/api/coefficients/'+draft['id'],json={'revision':draft['revision'],'value':'0.8'})
        self.assertEqual(update.status_code,200,update.text)
        next_version=self.client.post('/api/coefficients/'+draft['id']+'/publish',json={'revision':update.json()['item']['revision']})
        self.assertEqual(next_version.status_code,200,next_version.text)
        saved=self.client.get('/api/bills/'+self.fx.bill).json()
        self.assertEqual(saved['calculation'],calculation)
        export=self.client.get('/api/bills/'+self.fx.bill+'/export').json()
        self.assertEqual(export['calculation']['coefficient_snapshot'],snapshot)
        ledger=self.client.get('/api/company/export',params={'company_id':self.fx.cid})
        rows=list(csv.DictReader(io.StringIO(ledger.text)))
        row=next(row for row in rows if row['Coefficient version']==item['id'])
        self.assertEqual(json.loads(row['Coefficient snapshot']),snapshot)

    def test_bill_rejects_loss_factors_and_mismatched_units(self):
        for overrides in [{'category':'electricity_losses'}, {'input_unit':'km'},
                          {'kind':'conversion','output_unit':'GJ'}, {'value':'100'},
                          {'value':'100000000000000000000'}]:
            item=self.publish(**overrides)
            result=self.review(item)
            self.assertEqual(result.status_code,422,result.text)
        self.assertEqual(self.fx.product.get(self.fx.bill)['status'],'needs_review')

    def test_company_factor_cannot_leak_to_other_company_or_unassigned_bill(self):
        item=self.publish(scope='company',scope_id=self.fx.other)
        response=self.review(item)
        self.assertEqual(response.status_code,404,response.text)
        own=self.publish(scope='company',scope_id=self.fx.cid)
        entered=self.client.post('/api/initial/companies/'+self.fx.cid+'/enter',json={})
        self.assertEqual(entered.status_code,200,entered.text)
        session=self.client.get('/auth/session').json()
        self.client.headers['X-CSRF-Token']=session['csrf']
        self.assertEqual(self.client.get('/api/coefficients/'+item['id']).status_code,404)
        self.assertEqual(self.client.get('/api/coefficients/'+own['id']).status_code,200)
        calculated=self.review(own)
        self.assertEqual(calculated.status_code,200,calculated.text)


if __name__=='__main__':unittest.main()
