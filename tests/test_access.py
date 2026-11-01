"""Access-control regression tests use only temporary local fixture accounts."""
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
import unittest
import test_flow
product = test_flow.product

class Access(unittest.TestCase):
    setUp = test_flow.Flow.setUp
    fixture = test_flow.Flow.fixture
    upload = test_flow.Flow.upload
    extracted = test_flow.Flow.extracted
    review_body = test_flow.Flow.review_body
    def company(self,name='Fixture company'):
        response=self.client.post('/api/admin/companies',json={'name':name,'country':'India','industry':'Manufacturing'})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()['id']
    def company_user(self,cid):
        email=uuid.uuid4().hex+'@tenant.example'
        r=self.client.post('/api/admin/users',json={'email':email,'name':'Fixture member','role':'company_user','company_id':cid,'password':'Company-fixture-123'})
        self.assertEqual(r.status_code,201,r.text)
        client=TestClient(product.app)
        login=client.post('/auth/login',json={'email':email,'password':'Company-fixture-123'})
        self.assertEqual(login.status_code,200,login.text)
        client.headers['X-CSRF-Token']=login.json()['csrf']
        return client,r.json()['id']
    def test_no_anonymous_access(self):
        anonymous=TestClient(product.app)
        for url in ['/api/bills','/api/admin/overview','/api/status','/docs','/openapi.json']:
            self.assertEqual(anonymous.get(url).status_code,401,url)
        self.assertEqual(anonymous.post('/extract').status_code,401)
        self.assertEqual(anonymous.get('/admin',follow_redirects=False).status_code,303)
        self.assertEqual(anonymous.get('/auth/session').json()['user'],None)
    def test_csrf_and_cross_origin(self):
        company=self.company()
        self.assertEqual(self.client.put('/api/admin/companies/'+company,json={'name':'changed'},headers={'X-CSRF-Token':''}).status_code,403)
        self.assertEqual(self.client.post('/api/admin/companies',json={'name':'bad origin'},headers={'Origin':'https://attacker.example'}).status_code,403)
        self.assertEqual(self.client.post('/auth/login',json={'email':'owner@test.example','password':'Fixture-password-123'},headers={'Sec-Fetch-Site':'cross-site'}).status_code,403)
    def test_tenant_boundaries_all_bill_routes(self):
        a,b=self.company('Company A'),self.company('Company B')
        ca,_=self.company_user(a);cb,_=self.company_user(b)
        with patch.object(product,'launch'):
            r=ca.post('/api/bills',files={'file':('fixture.pdf',self.fixture())})
        self.assertEqual(r.status_code,202,r.text)
        bill=r.json();bid=bill['id'];self.assertEqual(bill['company_id'],a)
        bill['status']='needs_review';product.save(bill)
        self.assertEqual(ca.get('/api/bills/'+bid).status_code,200)
        for suffix in ['', '/preview','/export']:
            self.assertEqual(cb.get('/api/bills/'+bid+suffix).status_code,404)
        self.assertEqual(cb.post('/api/bills/'+bid+'/review',json=self.review_body()).status_code,404)
        self.assertFalse(any(r['id']==bid for r in cb.get('/api/bills').json()))
        self.assertEqual(ca.get('/api/admin/overview').status_code,403)
        self.assertEqual(ca.post('/api/admin/companies',json={'name':'Escalated'}).status_code,403)
        self.assertEqual(ca.post('/extract').status_code,403)
        self.assertEqual(ca.post('/api/bills',data={'company_id':b},files={'file':('fixture.pdf',self.fixture())}).status_code,403)
        self.assertEqual(self.client.get('/api/bills/'+bid).status_code,200)
        self.assertEqual(len(cb.get('/api/companies').json()),1)
    def test_company_suspension_and_password_reset(self):
        cid=self.company();client,uid=self.company_user(cid)
        self.assertEqual(client.get('/api/bills').status_code,200)
        self.assertEqual(self.client.patch('/api/admin/users/'+uid,json={'active':True,'password':'New-fixture-password-123'}).status_code,200)
        self.assertEqual(client.get('/api/bills').status_code,401)
        user=next(u for u in self.client.get('/api/admin/overview').json()['users'] if u['id']==uid)
        self.assertEqual(client.post('/auth/login',json={'email':user['email'],'password':'Company-fixture-123'}).status_code,401)
        self.assertEqual(client.post('/auth/login',json={'email':user['email'],'password':'New-fixture-password-123'}).status_code,200)
        self.client.put('/api/admin/companies/'+cid,json={'name':'Suspended fixture','active':False})
        self.assertEqual(client.get('/api/bills').status_code,401)
        self.assertEqual(client.post('/auth/login',json={'email':user['email'],'password':'New-fixture-password-123'}).status_code,401)
    def test_last_master_and_no_secret_leaks(self):
        uid=self.client.get('/auth/session').json()['user']['id']
        self.assertEqual(self.client.patch('/api/admin/users/'+uid,json={'active':False}).status_code,409)
        overview=self.client.get('/api/admin/overview')
        self.assertNotIn('password_hash',overview.text)
        self.assertNotIn('Fixture-password',overview.text)
        with product.accounts.db() as db:
            token=db.execute('SELECT token_hash FROM sessions LIMIT 1').fetchone()[0]
        self.assertNotEqual(token,self.client.cookies.get('trace_session'))
        self.assertEqual(len(token),64)
    def test_assignment_unassigned_hidden_and_audit(self):
        cid=self.company();client,_=self.company_user(cid)
        bid=self.extracted()
        self.assertEqual(client.get('/api/bills/'+bid).status_code,404)
        response=self.client.put('/api/admin/bills/'+bid+'/company',json={'company_id':cid})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(client.get('/api/bills/'+bid).status_code,200)
        events=self.client.get('/api/admin/overview').json()['audit']
        self.assertTrue(any(e['action']=='bill.assigned' and e['target']==bid for e in events))
        self.assertEqual(self.client.put('/api/admin/bills/'+bid+'/company',json={'company_id':None}).status_code,200)
        self.assertEqual(client.get('/api/bills/'+bid).status_code,404)
    def test_settings_and_session_expiry(self):
        body={**product.FACTOR,'value':'0.5'}
        r=self.client.put('/api/admin/factor',json=body)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/status').json()['factor']['value'],'0.5')
        self.client.put('/api/admin/factor',json=product.FACTOR)
        with product.accounts.db() as db:
            db.execute('UPDATE sessions SET expires_at=0 WHERE token_hash=?',(__import__('hashlib').sha256(self.client.cookies.get('trace_session').encode()).hexdigest(),))
        self.assertEqual(self.client.get('/api/bills').status_code,401)
    def test_logout_revocation(self):
        r=self.client.post('/auth/logout')
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.client.get('/api/admin/overview').status_code,401)
    def test_login_cookie_and_failed_credentials(self):
        client=TestClient(product.app)
        bad=client.post('/auth/login',json={'email':'missing@user.example','password':'Incorrect-fixture'})
        self.assertEqual(bad.status_code,401)
        r=client.post('/auth/login',json={'email':'owner@test.example','password':'Fixture-password-123'})
        cookie=r.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie);self.assertIn('samesite=strict',cookie)
        self.assertEqual(r.json()['user']['role'],'master')
