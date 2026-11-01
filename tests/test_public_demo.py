"""A public sample can be explored remotely but can never change firm records."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from public_demo_server import PUBLIC_KIND, load_public_demo, validate_public_store


class PublicDemo(unittest.TestCase):
    def test_public_launcher_requires_explicit_mode(self):
        with patch.dict(os.environ, {'TRACE_PUBLIC_DEMO':'0'}):
            with self.assertRaises(RuntimeError):
                load_public_demo()

    def test_unmarked_or_non_temporary_data_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='trace-public-safety-') as temp:
            folder=Path(temp)
            with self.assertRaises(RuntimeError):
                validate_public_store(folder, {'kind':'trace-boutique-fictional-preview-v1'})
        with self.assertRaises(RuntimeError):
            validate_public_store(ROOT/'data')

    def test_marked_real_accounts_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='trace-public-safety-') as temp:
            folder=Path(temp).resolve()
            (folder/'.public-demo.json').write_text(json.dumps({'kind':PUBLIC_KIND, 'directory':str(folder)}))
            with sqlite3.connect(folder/'accounts.sqlite3') as db:
                db.execute('CREATE TABLE users(id TEXT)')
                db.execute("INSERT INTO users VALUES('real-owner')")
            with self.assertRaisesRegex(RuntimeError,'real user accounts'):
                validate_public_store(folder, {'kind':'trace-boutique-fictional-preview-v1'})

    def test_remote_read_only_workflow_and_source_only_assistance(self):
        # A fresh process is part of the public launch boundary. The ordinary
        # unittest suite has already imported its own separate fixture app.
        script=r'''
import json, os
from fastapi.testclient import TestClient
from public_demo_server import load_public_demo, NoHostedModel
app=load_public_demo()
import app as product
c=TestClient(app,base_url='https://localhost',client=('203.0.113.4',45000))
assert c.get('/auth/session').json()['public_demo_mode'] is True
assert c.get('/api/boutique/overview').status_code==401
cross=c.get('/boutique',headers={'Sec-Fetch-Site':'cross-site'},follow_redirects=False)
assert cross.status_code==303 and 'trace_boutique_session' not in cross.headers.get('set-cookie','')
assert c.get('/boutique').status_code==200
s=c.get('/auth/session').json()
assert s['user']['role']=='consultant_manager' and s['read_only'] is True
assert s['local_entry_available'] is False and s['demo_mode'] is False
assert s['user']['local_only'] is False
cookie=c.cookies.get('trace_boutique_session')
assert cookie and 'trace_local_master' not in c.cookies
c.headers['X-CSRF-Token']=s['csrf']
context=c.get('/api/boutique/overview')
assert context.status_code==200,context.text
assert len(context.json()['companies'])==3
assert c.get('/api/admin/overview').status_code==403
assert c.get('/super-admin',follow_redirects=False).status_code!=200
assert c.get('/preview/master').status_code==403
assert c.get('/api/boutique/company?company_id=foreign-client').status_code in {403,404}
with product.accounts.db() as db:
 assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0
 assert db.execute('SELECT COUNT(*) FROM firms').fetchone()[0]==1
before=product.app.state.boutique.context(s['user'])
for method,url in [
 ('post','/api/boutique/datasets'),('put','/api/boutique/datasets/anything'),
 ('post','/api/bills'),('post','/api/access/users'),('put','/api/admin/factor'),
 ('post','/api/coefficients'),('post','/api/coefficients/calculate'),
 ('post','/api/boutique/requests/anything/submission-link'),
 ('post','/api/brsr/reports/anything/snapshots'),('post','/auth/login'),
 ('post','/extract'),('post','/collect/anything')]:
 response=getattr(c,method)(url,json={})
 assert response.status_code==403,(url,response.status_code,response.text)
assert c.get('/collect/anything').status_code==403
assert c.post('/api/boutique/ai/ask',json={'question':'What data is missing?'},headers={'X-CSRF-Token':''}).status_code==403
assert c.post('/api/boutique/ai/ask',json={'question':'What data is missing?'},headers={'Origin':'https://attacker.example'}).status_code==403
assert c.get('/api/boutique/ai/status').json()['model_available'] is False
answer=c.post('/api/boutique/ai/ask',json={'question':'What data is missing?'})
assert answer.status_code==200,answer.text
assert answer.json()['mode']=='retrieval_only' and answer.json()['sources']
assert answer.json()['model'] is None
assert c.post('/api/boutique/ai/ask',json={'question':'Remind the missing data owners'}).json()['proposed_actions']==[]
knowledge=c.post('/api/knowledge/ask',json={'question':'India electricity emission factor'})
assert knowledge.status_code==200,knowledge.text
assert knowledge.json()['mode']!='local_generation'
assert product.app.state.boutique_ai.ai is None
assert product.app.state.coefficient_knowledge.ai is None
assert isinstance(product.legacy.ai,NoHostedModel)
after=product.app.state.boutique.context(s['user'])
for key in ('companies','owners','sources','datasets','requests','evidence','reports','activities'):
 assert before[key]==after[key],key
blocked=TestClient(app,base_url='https://localhost',client=('203.0.113.4',45000))
blocked.cookies.update(c.cookies)
blocked.cookies.set('trace_session','expired-real-account',domain='localhost.local',path='/')
assert blocked.get('/auth/session').json()['user'] is None
assert blocked.get('/api/boutique/overview').status_code==401
assert blocked.get('/boutique',follow_redirects=False).status_code==303
assert c.post('/auth/logout').status_code==200
assert c.get('/api/boutique/overview').status_code==401
print(json.dumps({'companies':3,'mode':'read_only_public_demo','inference':'disabled','users':0}))
'''
        with tempfile.TemporaryDirectory(prefix='trace-public-integration-') as temp:
            # The launcher must make its own initially empty child directory.
            folder=Path(temp)/'sample'
            env={**os.environ,'TRACE_PUBLIC_DEMO':'1','TRACE_DATA':str(folder),
                 'TRACE_COOKIE_SECURE':'1','TRACE_REQUIRE_LOGIN':'1',
                 'TRACE_ALLOWED_HOSTS':'localhost,127.0.0.1,testserver',
                 'SUSTIVIO_BACKEND':str(ROOT/'legacy_backend.py')}
            result=subprocess.run([sys.executable,'-c',script],cwd=ROOT,env=env,
                                  capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            self.assertIn('read_only_public_demo',result.stdout)


if __name__=='__main__':
    unittest.main()
