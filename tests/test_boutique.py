"""Boutique workflow tests use isolated, explicitly fictional firm stores."""
from contextlib import contextmanager
from datetime import date
import json
import hashlib
import re
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pymupdf

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from security import Accounts, timestamp
from portal import install_portal
from boutique import install_boutique


class Boutique(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='boutique-unit-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'research').mkdir()
        (self.root/'research/brsr-disclosures.json').write_text(json.dumps({'version':'fixture','items':[]}))
        self.accounts=Accounts(self.root/'accounts.sqlite3')
        self.app=FastAPI()
        self.users={
            'manager':{'id':'local-firm-manager','name':'Fictional manager','email':'','role':'consultant_manager','firm_id':'firm-a','active':True},
            'consultant':{'id':'local-consultant','name':'Fictional consultant','email':'','role':'consultant','firm_id':'firm-a','active':True},
            'foreign':{'id':'foreign-manager','name':'Other manager','role':'consultant_manager','firm_id':'firm-b','active':True},
            'company':{'id':'company-manager','name':'Company manager','role':'company_admin','firm_id':'firm-a','company_id':'company-a','active':True},
            'master':{'id':'founder-hidden','name':'Founder not visible','role':'master','active':True},
        }
        @self.app.middleware('http')
        async def session(request,call_next):
            request.state.user=self.users.get(request.headers.get('X-Fixture-User'))
            return await call_next(request)
        @contextmanager
        def bill_connection():
            db=sqlite3.connect(self.root/'bills.sqlite3')
            try:
                db.execute('CREATE TABLE IF NOT EXISTS bills(id TEXT PRIMARY KEY,payload TEXT)')
                with db:yield db
            finally:db.close()
        self.bills=bill_connection
        install_portal(self.app,self.accounts,self.root,bill_connection,lambda _:None,{})
        with self.accounts.db() as db:
            db.executescript('''
                CREATE TABLE brsr_reports(id TEXT PRIMARY KEY,company_id TEXT,payload TEXT,financial_year TEXT,created_at TEXT);
                CREATE TABLE brsr_answers(report_id TEXT,payload TEXT);
                CREATE TABLE brsr_catalog_versions(version TEXT,payload TEXT);
            ''')
            for fid in ['firm-a','firm-b']:
                db.execute('INSERT INTO firms VALUES(?,?,?,?,?,?,?)',(fid,fid,1,3,4,5,timestamp()))
            for cid,fid in [('company-a','firm-a'),('company-a2','firm-a'),('company-b','firm-b')]:
                db.execute('INSERT INTO companies(id,name,firm_id,created_at) VALUES(?,?,?,?)',(cid,cid,fid,timestamp()))
                db.execute('INSERT INTO facilities VALUES(?,?,?,?,?,?,?)',('site-'+cid,cid,'Fictional site','','',1,timestamp()))
                db.execute('INSERT INTO reporting_periods VALUES(?,?,?,?,?,?,?)',('period-'+cid,cid,'FY 2026-27','2026-04-01','2027-03-31','open',timestamp()))
            db.execute('INSERT INTO reporting_periods VALUES(?,?,?,?,?,?,?)',('period-prior','company-a','FY 2025-26','2025-04-01','2026-03-31','open',timestamp()))
        self.service=install_boutique(self.app,self.accounts,self.root,bill_connection,lambda _:None)
        self.client=TestClient(self.app)
        self.client.headers['X-Fixture-User']='manager'
        self.manager=self.users['manager']

    def create(self,path,body):
        response=self.client.post('/api/boutique/'+path,json=body)
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def dataset(self,**overrides):
        return self.create('datasets',{'company_id':'company-a','period_id':'period-company-a',
            'name':'Fictional electricity','category':'electricity','unit':'kWh',**overrides})

    def evidence(self,dataset_id=None,company='company-a',period='period-company-a'):
        doc=pymupdf.open();p=doc.new_page();p.insert_text((50,50),'Fictional evidence: Electricity 1250 kWh');blob=doc.tobytes();doc.close()
        data={'company_id':company,'period_id':period,'title':'Fictional electricity evidence'}
        if dataset_id:data['dataset_id']=dataset_id
        return self.client.post('/api/boutique/evidence',data=data,files={'file':('electricity.pdf',blob)})

    def test_every_read_is_firm_scoped_and_lower_roles_cannot_enter(self):
        dataset=self.dataset()
        context=self.client.get('/api/boutique/overview').json()
        self.assertEqual({c['id'] for c in context['companies']},{'company-a','company-a2'})
        self.assertEqual(context['users'][0]['id'],self.manager['id'])
        self.assertNotIn('founder-hidden',json.dumps(context))
        self.assertEqual(self.client.get('/api/boutique/company?company_id=company-b').status_code,404)
        self.assertEqual(self.client.get('/api/boutique/company?company_id=company-a&period_id=period-company-b').status_code,404)
        self.client.headers['X-Fixture-User']='foreign'
        self.assertEqual(self.client.put('/api/boutique/datasets/'+dataset['id'],json={
            'company_id':'company-a','period_id':'period-company-a','name':'Forbidden update','expected_revision':1}).status_code,404)
        self.client.headers['X-Fixture-User']='company'
        self.assertEqual(self.client.get('/api/boutique/overview').status_code,403)
        self.client.headers.pop('X-Fixture-User')
        self.assertEqual(self.client.get('/api/boutique/overview').status_code,401)
        self.client.headers['X-Fixture-User']='master'
        self.assertEqual(self.client.get('/api/boutique/overview').status_code,422)
        self.assertEqual(self.client.get('/api/boutique/overview?firm_id=firm-a').status_code,200)

    def test_source_owner_and_period_links_cannot_cross_company(self):
        owner=self.create('owners',{'company_id':'company-a2','name':'Fictional source owner'})
        invalid=self.client.post('/api/boutique/sources',json={'company_id':'company-a','name':'Bad owner link','owner_id':owner['id']})
        self.assertEqual(invalid.status_code,422,invalid.text)
        invalid=self.client.post('/api/boutique/datasets',json={'company_id':'company-a','period_id':'period-company-b','name':'Bad period'})
        self.assertEqual(invalid.status_code,422)
        invalid=self.client.post('/api/boutique/datasets',json={'company_id':'company-a','period_id':'period-company-a','name':'Bad evidence','evidence_ids':['not-a-real-file']})
        self.assertEqual(invalid.status_code,404)
        with self.accounts.db() as db:db.execute("UPDATE reporting_periods SET status='closed' WHERE id='period-company-a'")
        invalid=self.client.post('/api/boutique/datasets',json={'company_id':'company-a','period_id':'period-company-a','name':'Closed period'})
        self.assertEqual(invalid.status_code,409)

    def test_dataset_review_requires_submission_and_preserves_revisions(self):
        dataset=self.dataset(value='1250',status='draft')
        url='/api/boutique/datasets/'+dataset['id']
        self.assertEqual(self.client.post(url+'/review',json={'expected_revision':1,'confirmed':True}).status_code,409)
        submitted=self.client.put(url,json={'company_id':'company-a','period_id':'period-company-a','name':dataset['name'],
            'value':'1250','unit':'kWh','status':'submitted','expected_revision':1})
        self.assertEqual(submitted.status_code,200,submitted.text)
        self.assertEqual(submitted.json()['revision'],2)
        self.assertEqual(self.client.put(url,json={'company_id':'company-a','period_id':'period-company-a','name':dataset['name'],'expected_revision':1}).status_code,409)
        self.assertEqual(self.client.post(url+'/review',json={'expected_revision':2,'confirmed':True}).status_code,422)
        reviewed=self.client.post(url+'/review',json={'expected_revision':2,'confirmed':True,'reason':'Manually checked fictional test ledger'})
        self.assertEqual(reviewed.status_code,200,reviewed.text)
        self.assertEqual(reviewed.json()['status'],'reviewed')
        changed=self.client.put(url,json={'company_id':'company-a','period_id':'period-company-a','name':dataset['name'],
            'value':'1300','unit':'kWh','status':'draft','expected_revision':3})
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertEqual(changed.json()['status'],'draft')
        with self.accounts.db() as db:
            history=[json.loads(r[0]) for r in db.execute('SELECT payload FROM boutique_history WHERE record_id=?',(dataset['id'],))]
        self.assertTrue(any(h.get('status')=='reviewed' and h['value']=='1250' for h in history))
        self.assertEqual(self.client.post(url+'/review',json={'expected_revision':4,'confirmed':False}).status_code,422)

    def test_uploaded_evidence_is_hashed_scoped_and_demotes_reviewed_data(self):
        dataset=self.dataset(value='1250',status='submitted')
        self.service.review_dataset(self.manager,dataset['id'],{'expected_revision':1,'confirmed':True,'reason':'Manual test ledger'})
        upload=self.evidence(dataset['id']);self.assertEqual(upload.status_code,201,upload.text)
        evidence=upload.json()
        self.assertEqual(len(evidence['sha256']),64)
        self.assertTrue(evidence['has_preview'])
        self.assertEqual(self.client.get(evidence['preview_url']).headers['content-type'],'image/png')
        downloaded=self.client.get(evidence['download_url']);self.assertEqual(downloaded.status_code,200)
        self.assertIn('attachment',downloaded.headers['content-disposition'])
        data=self.client.get('/api/boutique/company?company_id=company-a').json()['datasets'][0]
        self.assertEqual(data['status'],'draft');self.assertIn(evidence['id'],data['evidence_ids'])
        self.client.headers['X-Fixture-User']='foreign'
        self.assertEqual(self.client.get(evidence['download_url']).status_code,404)
        self.assertEqual(self.client.get(evidence['preview_url']).status_code,404)
        self.client.headers['X-Fixture-User']='manager'
        bad=self.client.post('/api/boutique/evidence',data={'company_id':'company-a'},files={'file':('x.html',b'<script>alert(1)</script>')})
        self.assertEqual(bad.status_code,415)

    def test_reminders_are_drafts_and_missing_is_distinct_from_zero(self):
        owner=self.create('owners',{'company_id':'company-a','name':'Fictional finance','email':'finance@example.org'})
        missing=self.dataset(owner_id=owner['id'])
        zero=self.dataset(name='Verified zero field',value='0',status='submitted')
        req=self.create('requests',{'company_id':'company-a','period_id':'period-company-a','dataset_id':missing['id'],
            'owner_id':owner['id'],'title':'September electricity evidence','due_date':'2026-01-01'})
        context=self.client.get('/api/boutique/company?company_id=company-a').json()
        self.assertEqual(context['stats']['missing'],1)
        self.assertEqual(context['stats']['submitted'],1)
        self.assertTrue(context['requests'][0]['overdue'])
        draft=self.client.post('/api/boutique/requests/'+req['id']+'/reminder',json={'channel':'email'})
        self.assertEqual(draft.status_code,201,draft.text)
        self.assertEqual(draft.json()['delivery'],'not_sent')
        self.assertEqual(draft.json()['status'],'draft')
        self.assertIsNone(draft.json()['submission_url'])
        self.assertEqual(draft.json()['to'],'finance@example.org')
        self.assertEqual(self.client.get('/api/boutique/company?company_id=company-a').json()['requests'][0]['status'],'open')
        self.assertEqual(zero['value'],'0')

    def test_context_notes_do_not_count_as_a_provided_dataset_value(self):
        missing=self.dataset(notes='Please upload the September electricity bill. This is collection context, not an answer.')
        self.assertEqual(missing['status'],'missing')
        self.assertEqual(missing['value'],'')
        context=self.service.context(self.manager,company_id='company-a')
        self.assertEqual(context['stats']['missing'],1)
        zero=self.dataset(name='Explicit zero value',value='0',notes='Confirmed zero consumption')
        self.assertEqual(zero['status'],'draft')
        self.assertEqual(zero['value'],'0')
        context=self.service.context(self.manager,company_id='company-a')
        self.assertEqual(context['stats']['missing'],1)

    def test_year_filters_keep_contacts_but_not_other_year_datasets(self):
        self.dataset()
        self.dataset(name='Prior year fictional record',period_id='period-prior')
        owner=self.create('owners',{'company_id':'company-a','name':'Fictional HR'})
        context=self.client.get('/api/boutique/company?company_id=company-a&year=2026-27').json()
        self.assertEqual(len(context['datasets']),1)
        self.assertEqual(context['owners'][0]['id'],owner['id'])
        inferred=self.service.context(self.manager,period_id='period-company-a')
        self.assertEqual(inferred['company']['id'],'company-a')
        self.assertEqual(len(inferred['datasets']),1)
        self.assertEqual(self.client.get('/api/boutique/company?company_id=company-a&period_id=period-company-a&year=2025-26').status_code,422)

    def test_definition_scope_publication_and_immutable_revisions(self):
        created=self.create('definitions',{'name':'Monthly test meter data','description':'Fictional collection definition',
            'scope':'firm','source_title':'Fictional internal method v1'})
        url='/api/boutique/definitions/'+created['id']
        published=self.client.post(url+'/publish',json={'expected_revision':1,'confirmed':True})
        self.assertEqual(published.status_code,200,published.text)
        changed=self.client.put(url,json={'name':'Changed official-like wording','description':'Never overwrite published source','scope':'firm','expected_revision':2})
        self.assertEqual(changed.status_code,409)
        revised=self.client.post(url+'/revise',json={'expected_revision':2,'confirmed':True})
        self.assertEqual(revised.status_code,201,revised.text)
        self.assertEqual(revised.json()['version'],2)
        self.assertEqual(revised.json()['family_id'],created['family_id'])
        self.client.headers['X-Fixture-User']='consultant'
        self.assertEqual(self.client.post('/api/boutique/definitions',json={'name':'Forbidden publish','description':'Consultant has no publication authority'}).status_code,403)
        self.client.headers['X-Fixture-User']='manager'
        other=self.client.post('/api/boutique/definitions',json={'name':'Foreign custom field','description':'Another company','scope':'company','company_id':'company-b'})
        self.assertEqual(other.status_code,404)

    def collection_link(self,dataset=None):
        dataset=dataset or self.dataset()
        req=self.create('requests',{'company_id':'company-a','period_id':'period-company-a',
            'dataset_id':dataset['id'],'title':'Submit September electricity','instructions':'Supply the requested electricity record only.'})
        created=self.client.post('/api/boutique/requests/'+req['id']+'/submission-link',json={'expected_revision':1,'expires_days':7})
        self.assertEqual(created.status_code,201,created.text)
        return req,dataset,created.json()

    def test_collection_link_is_scoped_hashed_and_single_submission(self):
        self.dataset(name='Unrelated private workforce record',value='sensitive-other-record')
        req,dataset,created=self.collection_link()
        url=created['url'];token=url.rsplit('/',1)[1]
        with self.accounts.db() as db:
            stored=dict(db.execute('SELECT * FROM boutique_submission_links WHERE request_id=?',(req['id'],)).fetchone())
        self.assertEqual(stored['token_hash'],hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token,json.dumps(stored))
        anon=TestClient(self.app)
        form=anon.get(url)
        self.assertEqual(form.status_code,200,form.text)
        self.assertIn('Submit September electricity',form.text)
        for forbidden in ['sensitive-other-record','Unrelated private workforce','company-b','Super Access','consultant_manager','founder-hidden']:
            self.assertNotIn(forbidden,form.text)
        nonce=re.search(r'name="csrf" value="([a-f0-9]+)"',form.text).group(1)
        rejected=anon.post(url,data={'value':'1250','csrf':nonce},headers={'Origin':'https://other.example'})
        self.assertEqual(rejected.status_code,403)
        self.assertEqual(anon.post(url,data={'value':'1250','csrf':'wrong'},headers={'Origin':'http://testserver'}).status_code,403)
        response=anon.post(url,data={'value':'0','notes':'Fictional zero use confirmed','csrf':nonce},headers={'Origin':'http://testserver'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Submission received',response.text)
        self.assertEqual(anon.get(url).status_code,409)
        context=self.client.get('/api/boutique/company?company_id=company-a').json()
        updated=next(d for d in context['datasets'] if d['id']==dataset['id'])
        self.assertEqual(updated['value'],'0');self.assertEqual(updated['status'],'submitted')
        self.assertNotIn(token,json.dumps(context))
        self.assertEqual(next(r for r in context['requests'] if r['id']==req['id'])['status'],'received')
        self.assertEqual(self.client.post('/api/boutique/requests/'+req['id']+'/submission-link',json={'expected_revision':1}).status_code,409)

    def test_collection_links_expire_and_do_not_survive_closed_or_changed_records(self):
        req,dataset,created=self.collection_link()
        with self.accounts.db() as db:db.execute('UPDATE boutique_submission_links SET expires_at=0')
        self.assertEqual(TestClient(self.app).get(created['url']).status_code,410)
        fresh=self.client.post('/api/boutique/requests/'+req['id']+'/submission-link',json={'expected_revision':1}).json()
        with self.accounts.db() as db:db.execute("UPDATE reporting_periods SET status='closed' WHERE id='period-company-a'")
        self.assertEqual(TestClient(self.app).get(fresh['url']).status_code,410)
        with self.accounts.db() as db:db.execute("UPDATE reporting_periods SET status='open' WHERE id='period-company-a'")
        edit=self.client.put('/api/boutique/datasets/'+dataset['id'],json={'company_id':'company-a','period_id':'period-company-a','name':'Changed request dataset','expected_revision':1})
        self.assertEqual(edit.status_code,200,edit.text)
        self.assertEqual(TestClient(self.app).get(fresh['url']).status_code,409)
        self.client.headers['X-Fixture-User']='foreign'
        self.assertEqual(self.client.post('/api/boutique/requests/'+req['id']+'/submission-link',json={'expected_revision':1}).status_code,404)
        self.client.headers['X-Fixture-User']='consultant'
        self.assertEqual(self.client.post('/api/boutique/requests/'+req['id']+'/submission-link',json={'expected_revision':1}).status_code,403)

    def test_reminder_links_are_returned_once_and_not_stored_in_plaintext(self):
        req,_,created=self.collection_link()
        response=self.client.post('/api/boutique/requests/'+req['id']+'/reminder',json={'channel':'email'})
        self.assertEqual(response.status_code,201,response.text)
        reminder=response.json()
        self.assertEqual(reminder['delivery'],'not_sent')
        self.assertTrue(reminder['submission_url'].startswith('/collect/'))
        token=reminder['submission_url'].rsplit('/',1)[1]
        self.assertIn(reminder['submission_url'],reminder['body'])
        with self.accounts.db() as db:
            stored=db.execute('SELECT payload FROM boutique_records WHERE id=?',(reminder['id'],)).fetchone()[0]
        self.assertNotIn(token,stored)
        self.assertNotIn(token,json.dumps(self.service.context(self.manager)))
        self.assertEqual(TestClient(self.app).get(reminder['submission_url']).status_code,200)


if __name__=='__main__':unittest.main()
