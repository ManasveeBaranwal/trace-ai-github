"""Opt-in real Ollama test. Upload only generated fictional bill fixtures."""
import json
import time
import httpx
import pymupdf
from pathlib import Path

BASE='http://127.0.0.1:8002'
client=httpx.Client(base_url=BASE,timeout=30)
session=client.get('/auth/session').json()
if not session.get('user'):
    import getpass
    email=input('Trace AI master email: ')
    login=client.post('/auth/login',json={'email':email,'password':getpass.getpass('Password: ')})
    login.raise_for_status()
    session=login.json()
client.headers['X-CSRF-Token']=session['csrf']
assert client.get('/api/status').json()['ready']
blob=client.get('/api/sample').content
report=[]
for kind in ['pdf','png']:
    if kind=='png':
        with pymupdf.open(stream=blob,filetype='pdf') as doc:
            fixture=doc[0].get_pixmap(matrix=pymupdf.Matrix(1.5,1.5)).tobytes('png')
    else: fixture=blob
    response=client.post('/api/bills',files={'file':('fictional-smoke.'+kind,fixture)})
    response.raise_for_status();record=response.json(); start=time.monotonic()
    while record['status'] in {'queued','extracting'}:
        assert time.monotonic()-start<330,'Extraction timed out'
        time.sleep(2)
        record=client.get('/api/bills/'+record['id']).json()
    assert record['status']=='needs_review',record
    assert record['fields']['consumption']['value']=='1250',record['fields']
    assert record['fields']['unit']['value'].lower()=='kwh',record['fields']
    fields={key:value['value'] or '' for key,value in record['fields'].items()}
    factor=client.get('/api/status').json()['factor']
    review={'fields':fields,'confirmed':True,'gas':'CO2',**{'factor_'+k:factor[k] for k in ['value','source','url','year','region']}}
    r=client.post('/api/bills/'+record['id']+'/review',json=review)
    r.raise_for_status();reviewed=r.json()
    assert reviewed['calculation']['emissions_kg']=='908.750',reviewed
    for fmt in ['json','csv']:
        exported=client.get('/api/bills/'+record['id']+'/export',params={'format':fmt})
        assert exported.status_code==200
        assert '908.750' in exported.text
    report.append({'type':kind,'id':record['id'],'status':'passed','seconds':round(time.monotonic()-start,1),'consumption':fields['consumption'],'emissions_kg':reviewed['calculation']['emissions_kg'],'vision_used':record['vision_used']})
    print(json.dumps(report[-1]),flush=True)
Path(__file__).resolve().parents[1].joinpath('data/live-smoke-report.json').write_text(json.dumps(report,indent=2))
