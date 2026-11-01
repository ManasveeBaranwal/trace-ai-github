import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['TRACE_DATA'] = tempfile.mkdtemp(prefix='trace-test-')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as product
from fastapi.testclient import TestClient

class Flow(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(product.app)
        if not product.accounts.has_master():
            product.accounts.create_master('owner@test.example','Test owner','Fixture-password-123')
        response=self.client.post('/auth/login',json={'email':'owner@test.example','password':'Fixture-password-123'})
        assert response.status_code==200,response.text
        self.client.headers['X-CSRF-Token']=response.json()['csrf']
    def fixture(self):
        return self.client.get('/api/sample').content
    def upload(self, blob=None, name='sample.pdf'):
        with patch.object(product, 'launch'):
            return self.client.post('/api/bills', files={'file':(name, blob or self.fixture())})
    def extracted(self):
        r=self.upload().json()
        r['status']='needs_review'
        r['fields']={key:{'value':None,'evidence':None} for key in product.FIELDS}
        product.save(r)
        return r['id']
    def review_body(self):
        return {'fields':dict.fromkeys(product.FIELDS, '') | {'consumption':'1,250','unit':'kWh'},'factor_value':'0.727','factor_source':product.FACTOR['source'],'factor_url':product.FACTOR['url'],'factor_year':'2023-24','factor_region':'India','gas':'CO2','confirmed':True}
    def test_validation(self):
        self.assertEqual(self.upload(b'broken').status_code,422)
        self.assertEqual(self.upload(b'abc','bad.txt').status_code,415)
        self.assertEqual(self.client.post('/api/bills',files={'file':('empty.pdf',b'')}).status_code,422)
        self.assertEqual(self.client.post('/api/bills',data={'page':9},files={'file':('x.pdf',self.fixture())}).status_code,422)
    def test_review_and_exports(self):
        bid=self.extracted()
        self.assertEqual(self.client.get(f'/api/bills/{bid}/export').status_code,409)
        body=self.review_body();body['fields']['supplier']='=1+1'
        r=self.client.post(f'/api/bills/{bid}/review',json=body)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['calculation']['emissions_kg'],'908.750')
        self.assertEqual(len(r.json()['reviews']),1)
        self.assertIn("'=1+1",self.client.get(f'/api/bills/{bid}/export?format=csv').text)
        self.assertEqual(self.client.get(f'/api/bills/{bid}/export').json()['sha256'],product.get(bid)['sha256'])
        body['fields']['unit']='MWh';body['fields']['consumption']='1.25'
        self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).json()['calculation']['emissions_kg'],'908.750')
        self.assertEqual(len(product.get(bid)['reviews']),2)
    def test_reject_bad_calculation(self):
        bid=self.extracted();body=self.review_body()
        for invalid in ['kVAh','units','kW','']:
            body['fields']['unit']=invalid
            self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).status_code,422)
        body['fields']['unit']='kWh'
        for invalid in ['-4','NaN','12 kWh','1,2','1e4']:
            body['fields']['consumption']=invalid
            self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).status_code,422)
        body=self.review_body();body['confirmed']=False
        self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).status_code,422)
    def test_image_and_legacy(self):
        doc=product.pymupdf.open(stream=self.fixture(),filetype='pdf')
        r=self.upload(doc[0].get_pixmap().tobytes('png'),'sample.png')
        self.assertEqual(r.status_code,202,r.text)
        self.assertTrue(r.json()['vision_used'])
        self.assertEqual(self.client.get('/health').status_code,200)
        self.assertIn('/extract',self.client.get('/openapi.json').json()['paths'])
    def test_ai_evidence(self):
        r=self.upload().json()
        fields={key:{'value':None,'evidence':None} for key in product.FIELDS}
        fields['consumption']={'value':'1250','evidence':'Electricity consumption: 1250 kWh'}
        from types import SimpleNamespace
        with patch.object(product.legacy.ai,'generate',return_value=SimpleNamespace(response=json.dumps(fields),done_reason='stop')):
            product.process(r['id'])
        result=product.get(r['id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertTrue(result['fields']['consumption']['quote_matched'])
        self.assertTrue(result['fields']['consumption']['value_in_quote'])
    def test_recent_and_restart(self):
        r=self.upload().json()
        self.assertTrue(any(x['id']==r['id'] for x in self.client.get('/api/bills').json()))
        product.recover_interrupted()
        self.assertEqual(product.get(r['id'])['status'],'failed')

    def test_unsupported_factor_and_limits(self):
        bid=self.extracted(); body=self.review_body()
        body['factor_value']='NaN'
        self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).status_code,422)
        body=self.review_body();body['fields']['consumption']='9999999999999999999999999999999999999999999999'
        self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).status_code,422)
        body=self.review_body();body['factor_url']='javascript:alert(1)'
        self.assertEqual(self.client.post(f'/api/bills/{bid}/review',json=body).status_code,422)

    def test_model_quantity_with_unit_preserves_original(self):
        r=self.upload().json()
        fields={key:{'value':None,'evidence':None} for key in product.FIELDS}
        fields['consumption']={'value':'1250 kWh','evidence':'Electricity consumption: 1250 kWh'}
        fields['unit']={'value':'kWh','evidence':'Electricity consumption: 1250 kWh'}
        from types import SimpleNamespace
        with patch.object(product.legacy.ai,'generate',return_value=SimpleNamespace(response=json.dumps(fields),done_reason='stop')):
            product.process(r['id'])
        result=product.get(r['id'])
        self.assertEqual(result['fields']['consumption']['value'],'1250')
        self.assertEqual(result['raw_extraction']['consumption']['value'],'1250 kWh')
        self.assertEqual(result['fields']['consumption']['raw_value'],'1250 kWh')

    def test_model_failure(self):
        r=self.upload().json()
        with patch.object(product.legacy.ai,'generate',side_effect=RuntimeError('offline')):
            product.process(r['id'])
        self.assertEqual(product.get(r['id'])['status'],'failed')

if __name__=='__main__': unittest.main()
