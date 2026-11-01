"""Fictional records for local boutique layout review, never the main store."""
import hashlib
import json
from pathlib import Path
import uuid

from fastapi.testclient import TestClient
from security import timestamp


def seed_boutique_sample(product, metadata):
    folder = Path(product.DATA)
    if not (folder/'.boutique-preview.json').is_file():
        raise RuntimeError('Sample data can only be created in the isolated boutique store')
    fid = metadata['firm_id']
    uid = metadata['profile_id']
    user = {'id':uid,'role':'consultant_manager','firm_id':fid,'company_id':None,
            'name':'Aarav · Sample firm manager','email':'','active':True,'created_at':metadata['created_at']}
    if product.accounts.get_setting('boutique_sample_v1', None):
        if not product.accounts.get_setting('boutique_sample_missing_fix_v2', None):
            from boutique import DatasetInput
            context = product.app.state.boutique.context(user)
            for row in context['datasets']:
                if (row['name'] in {'September electricity','September waste generated'}
                    and row['revision']==1 and row['status']=='draft' and not row.get('value')
                    and row.get('notes')=='Fictional sample data, prepared for interface review.'):
                    body={k:row[k] for k in DatasetInput.model_fields if k in row}
                    body.update(status='missing',expected_revision=row['revision'])
                    product.app.state.boutique._write(user,'dataset',DatasetInput,body,row['id'])
            with product.accounts.db() as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('boutique_sample_missing_fix_v2','true'))
        return
    stable = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, fid+':'+name))
    clients = [('cedar','Cedar Manufacturing','Manufacturing','Pune plant'),
               ('harbor','Harbor Services','Professional services','Bengaluru office'),
               ('willow','Willow Foods','Food processing','Nashik unit')]
    now = timestamp()
    with product.accounts.db() as db:
        db.execute('INSERT OR IGNORE INTO firms VALUES(?,?,?,?,?,?,?)',
                   (fid,'Canopy Sustainability · Sample firm',1,5,12,5,now))
        for slug,name,industry,site in clients:
            cid,sid,pid = (stable(slug+suffix) for suffix in ('',':site',':year'))
            db.execute('INSERT OR IGNORE INTO companies(id,name,industry,country,active,created_at,firm_id,data_entry_limit) VALUES(?,?,?,?,?,?,?,?)',
                       (cid,name,industry,'India',1,now,fid,5))
            db.execute('INSERT OR IGNORE INTO facilities VALUES(?,?,?,?,?,?,?)',
                       (sid,cid,site,'Fictional sample location','SAMPLE-'+slug.upper(),1,now))
            db.execute('INSERT OR IGNORE INTO reporting_periods VALUES(?,?,?,?,?,?,?)',
                       (pid,cid,'FY 2026-27','2026-04-01','2027-03-31','open',now))
    service = product.app.state.boutique
    context = service.context(user)
    owners = {}
    for slug,name,email,department in [
        ('cedar','Priya Shah','priya.finance@cedar.example','Finance'),
        ('cedar','Neel Rao','neel.facilities@cedar.example','Facilities'),
        ('harbor','Sara Mehta','sara.hr@harbor.example','People operations'),
        ('willow','Rohan Patil','rohan.operations@willow.example','Operations')]:
        owner = next((o for o in context['owners'] if o['company_id']==stable(slug) and o['name']==name), None)
        owners[name] = owner or service.create_owner(user,{'company_id':stable(slug),'name':name,
            'email':email,'department':department,'notes':'Fictional source contact for layout review; no message is sent.'})
    samples = [
        ('cedar','September electricity','Priya Shah','Electricity bill','bill','kWh','electricity','', 'missing','2026-10-10'),
        ('cedar','September water withdrawal','Neel Rao','Water meter register','spreadsheet','m3','water','820','submitted',None),
        ('harbor','Quarterly workforce headcount','Sara Mehta','Workforce schedule','spreadsheet','people','workforce','84','draft',None),
        ('willow','September waste generated','Rohan Patil','Waste transfer records','manual','t','waste','', 'missing','2026-10-24')]
    dataset_by_name = {}
    for slug,name,owner,source_name,source_type,unit,category,value,status,due in samples:
        cid,pid,sid = stable(slug),stable(slug+':year'),stable(slug+':site')
        context=service.context(user)
        source = next((s for s in context['sources'] if s['company_id']==cid and s['name']==source_name),None)
        source = source or service.create_source(user,{'company_id':cid,'name':source_name,
            'owner_id':owners[owner]['id'],'facility_id':sid,'source_type':source_type,
            'frequency':'monthly' if category!='workforce' else 'quarterly','unit':unit,
            'description':'Fictional sample source. Confirm against supporting evidence before review.'})
        dataset = next((d for d in context['datasets'] if d['company_id']==cid and d['name']==name),None)
        dataset = dataset or service.create_dataset(user,{'company_id':cid,'period_id':pid,'name':name,
            'source_id':source['id'],'owner_id':owners[owner]['id'],'facility_id':sid,'category':category,
            'unit':unit,'value':value,'status':status,'notes':'Fictional sample data, prepared for interface review.'})
        dataset_by_name[name] = dataset
        if due and not any(r.get('dataset_id')==dataset['id'] for r in context['requests']):
            service.create_request(user,{'company_id':cid,'period_id':pid,'dataset_id':dataset['id'],
                'owner_id':owners[owner]['id'],'title':name,'due_date':due,
                'instructions':'Please provide the September 2026 source record and the reported quantity/unit. Fictional sample request.'})
    context=service.context(user)
    for name,blob,filename in [
        ('September water withdrawal',b'Fictional sample only\nPeriod,Water withdrawal,Unit\nSeptember 2026,820,m3\n','Fictional water register.csv'),
        ('Quarterly workforce headcount',b'Fictional sample only\nPeriod,Headcount,Unit\nSeptember 2026,84,people\n','Fictional workforce schedule.csv')]:
        dataset=dataset_by_name[name]
        if not any(e.get('dataset_id')==dataset['id'] for e in context['evidence']):
            service.upload_evidence(user,dataset['company_id'],blob,filename,
                period_id=dataset['period_id'],dataset_id=dataset['id'],title=filename)

    # A prepared fictional PDF exercises source viewing without claiming AI ran.
    bid = stable('sample-bill')
    with product.connection() as db:
        exists = db.execute('SELECT 1 FROM bills WHERE id=?',(bid,)).fetchone()
    if not exists:
        doc=product.pymupdf.open();page=doc.new_page(width=595,height=842)
        lines=['FICTIONAL ELECTRICITY BILL | LOCAL SAMPLE','Supplier: Sample Grid Energy',
               'Account number: SAMPLE-CEDAR-01','Invoice number: SAMPLE-2026-09',
               'Billing period: 01 Sep 2026 - 30 Sep 2026','Electricity consumption: 1250 kWh',
               'Amount due: INR 10250.00','Prepared sample data. No AI extraction has been run.']
        for index,line in enumerate(lines):page.insert_text((45,65+index*40),line,fontsize=15 if index==0 else 12)
        blob=doc.tobytes();text=page.get_text();preview=page.get_pixmap(matrix=product.pymupdf.Matrix(1.5,1.5)).tobytes('png');doc.close()
        (folder/(bid+'.source')).write_bytes(blob);(folder/(bid+'.png')).write_bytes(preview)
        values={'supplier':'Sample Grid Energy','account_number':'SAMPLE-CEDAR-01','invoice_number':'SAMPLE-2026-09',
                'billing_period':'01 Sep 2026 - 30 Sep 2026','consumption':'1250','unit':'kWh','amount_due':'10250.00','currency':'INR'}
        fields={k:{'value':v,'evidence':next(line for line in lines if v in line),
                   'quote_matched':True,'value_in_quote':True,'source_page':1} for k,v in values.items()}
        product.save({'id':bid,'company_id':stable('cedar'),'created_by':uid,
            'filename':'Fictional September electricity bill.pdf','sha256':hashlib.sha256(blob).hexdigest(),
            'page':1,'page_count':1,'vision_used':False,'source_text':text,'model':None,
            'created_at':now,'status':'needs_review','fields':fields,'reviews':[],
            'sample_prepared':True,'preparation_note':'Manually prepared fictional sample, not AI output.'})
    aid = stable('sample-activity')
    bill = product.get(bid)
    with product.accounts.db() as db:
        if not db.execute('SELECT 1 FROM activities WHERE id=?',(aid,)).fetchone():
            payload={'id':aid,'company_id':stable('cedar'),'facility_id':stable('cedar:site'),'period_id':stable('cedar:year'),
                'title':'September electricity · Fictional draft','category':'electricity','scope':'2','quantity':'1250','unit':'kWh',
                'factor_value':'0.5','gas':'CO2e','factor_source':'Fictional demonstration coefficient — do not use for reporting',
                'factor_url':'https://example.org/fictional-layout-sample','factor_year':'2026','notes':'Fictional layout sample. Quantity, source and factor require review.',
                'status':'draft','calculation':None,'history':[],'created_at':now,'updated_at':now,'created_by':uid,
                'source':{'id':bid,'filename':bill['filename'],'sha256':bill['sha256'],'page':1,'reviewed_at':None},'source_bill_id':bid}
            db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?)',(aid,payload['company_id'],payload['facility_id'],payload['period_id'],bid,bill['sha256']+':page:1',json.dumps(payload),now))

    client=TestClient(product.app,base_url='https://localhost:8004',client=('127.0.0.1',45001))
    client.get('/boutique').raise_for_status()
    session=client.get('/auth/session').json();client.headers['X-CSRF-Token']=session['csrf']
    for slug,name,_,_ in clients:
        cid=stable(slug)
        reports=client.get('/api/brsr/reports',params={'company_id':cid}).json()
        report=next((r for r in reports if r['financial_year']=='2026-27'),None)
        if report is None:
            response=client.post('/api/brsr/reports',params={'company_id':cid},json={
                'name':'BRSR FY 2026-27 · '+name,'financial_year':'2026-27','boundary':'standalone',
                'reporting_start':'2026-04-01','reporting_end':'2027-03-31','listed':True,
                'notes':'Fictional company and sample data for local layout review. This is an incomplete reporting draft.'})
            response.raise_for_status();report=response.json()
        if slug=='cedar':
            detail=client.get('/api/brsr/reports/'+report['id']).json()
            if not any(a['question_id']=='P1-E-8' for a in detail['answers']):
                response=client.put('/api/brsr/reports/'+report['id']+'/answers/P1-E-8',json={
                    'values':{'accounts_payable_current':'250','accounts_payable_previous':'200',
                        'cost_procured_current':'1000','cost_procured_previous':'800','payables_basis':'Fictional schedules; all monetary amounts in INR'},
                    'narrative':'Fictional procurement example for review.',
                    'evidence':[{'type':'reference','reference':'Fictional procurement schedule, sample page 1'}],
                    'applicability':'applicable','status':'submitted','expected_revision':0})
                response.raise_for_status()
    client.close()
    with product.accounts.db() as db:
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('boutique_sample_v1',json.dumps({'firm_id':fid,'created_at':now})))
