"""Run against the isolated passwordless QA server on port 8003, not customer data."""
import time
import httpx

client=httpx.Client(base_url='http://127.0.0.1:8003',timeout=30)
session=client.get('/auth/session').json()
assert session.get('demo_mode'),'Use an isolated local demo server'
client.headers['X-CSRF-Token']=session['csrf']
cid=next(c['id'] for c in client.get('/api/companies').json() if c['name']=='Demo Manufacturing · QA')
context=client.get('/api/company/context',params={'company_id':cid}).json()
site=context['facilities'][0]['id'];period=next(p['id'] for p in context['periods'] if p['status']=='open')
fixture=client.get('/api/sample').content
r=client.post('/api/bills',data={'company_id':cid},files={'file':('live-portal-demo.pdf',fixture)})
r.raise_for_status();record=r.json();start=time.monotonic()
while record['status'] in {'queued','extracting'}:
 assert time.monotonic()-start<330
 time.sleep(2)
 record=client.get('/api/bills/'+record['id']).json()
assert record['status']=='needs_review',record
fields={k:v['value'] or '' for k,v in record['fields'].items()}
assert fields['consumption']=='1250',fields
factor=context['factor']
review={'fields':fields,'confirmed':True,'gas':factor['gas'],**{'factor_'+k:factor[k] for k in ['value','source','url','year','region']}}
r=client.post('/api/bills/'+record['id']+'/review',json=review);r.raise_for_status()
r=client.post('/api/activities/from-bill/'+record['id'],params={'company_id':cid},json={'facility_id':site,'period_id':period});r.raise_for_status();aid=r.json()['id']
assert r.json()['status']=='draft'
r=client.post('/api/activities/'+aid+'/calculate',json={'confirmed':True});r.raise_for_status()
assert r.json()['calculation']['emissions_kg']=='908.750'
exported=client.get('/api/company/export',params={'company_id':cid});exported.raise_for_status();assert record['id'] in exported.text
summary=client.get('/api/company/dashboard',params={'company_id':cid}).json()
assert any(d['id']==record['id'] and d['in_inventory'] for d in summary['documents'])
assert client.get('/api/admin/overview').json()['workspace_counts'][cid]['activities']>=1
print({'status':'passed','flow':'real PDF -> Ollama -> bill review -> company inventory -> calculation -> CSV -> master counts','seconds':round(time.monotonic()-start,1),'activity_emissions_kg':r.json()['calculation']['emissions_kg']})
