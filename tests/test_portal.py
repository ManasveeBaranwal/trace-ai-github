import json
from decimal import Decimal
import unittest
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
import test_flow
import test_access
product=test_flow.product

class Portal(unittest.TestCase):
    setUp=test_flow.Flow.setUp
    fixture=test_flow.Flow.fixture
    upload=test_flow.Flow.upload
    extracted=test_flow.Flow.extracted
    review_body=test_flow.Flow.review_body
    company=test_access.Access.company
    company_user=test_access.Access.company_user
    def setup_workspace(self):
        cid=self.company('Portal fixture '+uuid.uuid4().hex[:5])
        site=self.client.post('/api/facilities',params={'company_id':cid},json={'name':'Factory one','meter':'METER-01'}).json()['id']
        period=self.client.post('/api/reporting-periods',params={'company_id':cid},json={'name':'August 2024','start':'2024-08-01','end':'2024-08-31'}).json()['id']
        return cid,site,period
    def activity_body(self,site,period):
        return {'facility_id':site,'period_id':period,'title':'Electricity fixture','category':'electricity','scope':'2','quantity':'1250','unit':'kWh','factor_value':'0.727','gas':'CO2','factor_source':'Historical factor fixture','factor_url':'https://example.org/source','factor_year':'2023-24'}
    def create(self,cid,body):
        r=self.client.post('/api/activities',params={'company_id':cid},json=body)
        self.assertEqual(r.status_code,201,r.text)
        return r.json()['id']
    def test_draft_review_revision_and_totals(self):
        cid,site,pid=self.setup_workspace();body=self.activity_body(site,pid);aid=self.create(cid,body)
        dash=lambda:self.client.get('/api/company/dashboard',params={'company_id':cid}).json()
        self.assertEqual(dash()['drafts'],1);self.assertEqual(dash()['totals_kg']['CO2'],'0')
        self.assertEqual(self.client.post('/api/activities/'+aid+'/calculate',json={'confirmed':False}).status_code,422)
        r=self.client.post('/api/activities/'+aid+'/calculate',json={'confirmed':True})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['calculation']['emissions_kg'],'908.750')
        self.assertEqual(Decimal(dash()['totals_kg']['CO2']),Decimal('908.750'))
        body['quantity']='1.25';body['unit']='MWh'
        changed=self.client.put('/api/activities/'+aid,json=body)
        self.assertEqual(changed.status_code,200,changed.text);self.assertEqual(changed.json()['status'],'draft')
        self.assertEqual(len(changed.json()['history']),1);self.assertEqual(dash()['totals_kg']['CO2'],'0')
        self.client.post('/api/activities/'+aid+'/calculate',json={'confirmed':True})
        self.assertEqual(Decimal(dash()['totals_kg']['CO2']),Decimal('908.750'))
    def test_period_locks_overlap_and_cross_tenant(self):
        cid,site,pid=self.setup_workspace();aid=self.create(cid,self.activity_body(site,pid))
        period={'name':'August 2024','start':'2024-08-01','end':'2024-08-31','status':'closed'}
        url='/api/reporting-periods/'+pid+'?company_id='+cid
        self.assertEqual(self.client.put(url,json=period).status_code,409)
        self.client.post('/api/activities/'+aid+'/calculate',json={'confirmed':True})
        self.assertEqual(self.client.put(url,json=period).status_code,200)
        self.assertEqual(self.client.put('/api/activities/'+aid,json=self.activity_body(site,pid)).status_code,409)
        self.assertEqual(self.client.post('/api/activities/'+aid+'/calculate',json={'confirmed':True}).status_code,409)
        self.assertEqual(self.client.post('/api/reporting-periods',params={'company_id':cid},json=period).status_code,409)
        member,_=self.company_user(cid);period['status']='open'
        self.assertEqual(member.put(url,json=period).status_code,403)
        self.assertEqual(self.client.put(url,json=period).status_code,200)
        other=self.company('Other company');outsider,_=self.company_user(other)
        self.assertEqual(outsider.get('/api/company/dashboard',params={'company_id':cid}).status_code,403)
        self.assertEqual(outsider.put('/api/activities/'+aid,json=self.activity_body(site,pid)).status_code,404)
        self.assertEqual(outsider.post('/api/activities/'+aid+'/calculate',json={'confirmed':True}).status_code,404)
        self.assertEqual(outsider.get('/api/company/export',params={'company_id':cid}).status_code,403)
        self.assertEqual(outsider.post('/api/activities',json=self.activity_body(site,pid)).status_code,422)
    def test_bill_import_duplicate_and_source_ownership(self):
        cid,site,pid=self.setup_workspace();bid=self.extracted()
        self.client.put('/api/admin/bills/'+bid+'/company',json={'company_id':cid})
        self.client.post('/api/bills/'+bid+'/review',json=self.review_body())
        endpoint='/api/activities/from-bill/'+bid+'?company_id='+cid;placement={'facility_id':site,'period_id':pid}
        r=self.client.post(endpoint,json=placement)
        self.assertEqual(r.status_code,201,r.text);self.assertEqual(r.json()['status'],'draft')
        self.assertEqual(self.client.post(endpoint,json=placement).status_code,409)
        same=product.get(bid).copy();same['id']=str(uuid.uuid4());product.save(same)
        self.assertEqual(self.client.post('/api/activities/from-bill/'+same['id'],params={'company_id':cid},json=placement).status_code,409)
        self.assertEqual(self.client.put('/api/admin/bills/'+bid+'/company',json={'company_id':None}).status_code,409)
    def test_gas_separation_filters_custom_factor_and_csv(self):
        cid,site,pid=self.setup_workspace();a=self.activity_body(site,pid)
        aid=self.create(cid,a);self.client.post('/api/activities/'+aid+'/calculate',json={'confirmed':True})
        a.update(title='=Potential formula',category='fuel',scope='1',quantity='100',unit='L',factor_value='2.5',gas='CO2e')
        bid=self.create(cid,a);self.client.post('/api/activities/'+bid+'/calculate',json={'confirmed':True})
        data=self.client.get('/api/company/dashboard',params={'company_id':cid,'period_id':pid}).json()
        self.assertEqual(data['totals_kg'],{'CO2':'908.750','CO2e':'250.0'})
        self.assertEqual(data['by_scope']['1']['CO2e'],'250.0')
        output=self.client.get('/api/company/export',params={'company_id':cid}).text
        self.assertIn("'=Potential formula",output)
        self.assertIn('250.0',output)
        none=self.client.get('/api/company/dashboard',params={'company_id':cid,'facility_id':'not-a-site'}).json()
        self.assertEqual(none['reviewed'],0)
    def test_input_validation_and_data_persistence(self):
        cid,site,pid=self.setup_workspace();body=self.activity_body(site,pid)
        for key,value in [('quantity','-1'),('quantity','NaN'),('factor_value','Infinity'),('unit','L'),('scope','1'),('factor_url','javascript:alert(1)')]:
            invalid={**body,key:value}
            self.assertEqual(self.client.post('/api/activities',params={'company_id':cid},json=invalid).status_code,422,(key,value))
        aid=self.create(cid,body)
        with product.accounts.db() as db:
            payload=json.loads(db.execute('SELECT payload FROM activities WHERE id=?',(aid,)).fetchone()[0])
            self.assertEqual(payload['company_id'],cid)
            self.assertEqual(db.execute('SELECT version FROM portal_schema').fetchone()[0],1)
        member,_=self.company_user(cid)
        self.assertEqual(member.get('/company').status_code,200)
        self.assertEqual(member.get('/api/company/context').json()['company']['id'],cid)
