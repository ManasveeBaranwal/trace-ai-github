import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
import test_flow

class LocalDemo(unittest.TestCase):
    def test_direct_master_and_csrf(self):
        with patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'0'}):
            client=TestClient(test_flow.product.app,client=('127.0.0.1',45001))
            entry=client.get('/login')
            self.assertEqual(entry.status_code,200)
            self.assertNotIn('Super Master',entry.text)
            self.assertNotIn('type="password"',entry.text)
            self.assertIsNone(client.get('/auth/session').json()['user'])
            self.assertEqual(client.get('/api/admin/overview').status_code,401)
            self.assertEqual(client.get('/super-admin').status_code,200)
            session=client.get('/auth/session').json()
            self.assertTrue(session['demo_mode'])
            self.assertFalse(session['setup_required'])
            self.assertEqual(session['user']['role'],'master')
            self.assertEqual(client.get('/admin').status_code,200)
            self.assertEqual(client.get('/api/admin/overview').status_code,200)
            self.assertEqual(client.post('/api/admin/companies',json={'name':'Demo CSRF test'}).status_code,403)
            result=client.post('/api/admin/companies',json={'name':'Local demo fixture'},headers={'X-CSRF-Token':session['csrf']})
            self.assertEqual(result.status_code,201,result.text)
            self.assertEqual(client.post('/auth/logout',headers={'X-CSRF-Token':session['csrf']}).status_code,200)
            self.assertIsNone(client.get('/auth/session').json()['user'])
            self.assertEqual(client.get('/api/admin/overview').status_code,401)
    def test_no_remote_bypass_and_login_restorable(self):
        with patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'0'}):
            remote=TestClient(test_flow.product.app,client=('198.51.100.1',45001))
            self.assertEqual(remote.get('/api/admin/overview').status_code,401)
        with patch.dict(os.environ,{'TRACE_REQUIRE_LOGIN':'1'}):
            local=TestClient(test_flow.product.app,client=('127.0.0.1',45001))
            self.assertEqual(local.get('/api/admin/overview').status_code,401)
            self.assertFalse(local.get('/auth/session').json()['demo_mode'])
