"""Connected company workspace: tenant-scoped organisation, periods and ledger."""
import csv
import io
import json
import sqlite3
import uuid
from datetime import date
from decimal import Decimal, localcontext
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from security import timestamp
from hierarchy import role, allowed_company, allowed_site, reviewer, visible_record
from coefficient_usage import resolve_emission_factor

class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)

class Site(Input):
    name:str=Field(min_length=2,max_length=160)
    location:str=Field(default='',max_length=160)
    meter:str=Field(default='',max_length=100)
    active:bool=True

class Period(Input):
    name:str=Field(min_length=2,max_length=100)
    start:date
    end:date
    status:Literal['open','closed']='open'
    @model_validator(mode='after')
    def valid_dates(self):
        if self.start>self.end:raise ValueError('End date must follow start date')
        return self

class Placement(Input):
    facility_id:str
    period_id:str

class Activity(Placement):
    title:str=Field(min_length=2,max_length=160)
    category:Literal['electricity','fuel','refrigerant','travel','other']
    scope:Literal['1','2','3']
    quantity:Decimal=Field(ge=0,le=Decimal('1000000000000'),allow_inf_nan=False,max_digits=24,decimal_places=9)
    unit:Literal['kWh','MWh','L','kg','t','km','passenger-km','unit']
    factor_value:Decimal=Field(ge=0,le=Decimal('1000000000'),allow_inf_nan=False,max_digits=24,decimal_places=9)
    gas:Literal['CO2','CO2e']='CO2'
    factor_source:str=Field(min_length=3,max_length=1000)
    factor_url:str=Field(min_length=8,max_length=2000)
    factor_year:str=Field(min_length=2,max_length=100)
    coefficient_version_id:str|None=Field(default=None,min_length=1,max_length=200)
    notes:str=Field(default='',max_length=2000)
    @model_validator(mode='after')
    def valid_method(self):
        url=urlsplit(self.factor_url)
        if url.scheme not in {'http','https'} or not url.netloc:raise ValueError('Use an HTTP source URL')
        if self.category=='electricity' and (self.scope!='2' or self.unit not in {'kWh','MWh'}):raise ValueError('Electricity requires Scope 2 and kWh/MWh')
        return self

class Confirmation(Input):
    confirmed:bool

def install_portal(app,accounts,root,bill_connection,get_bill,default_factor):
    with accounts.db() as db:
        existing=db.execute("SELECT 1 FROM sqlite_master WHERE name='portal_schema'").fetchone()
        backup=accounts.path.with_name('accounts.pre-portal-v1.sqlite3')
        if not existing and not backup.exists():
            target=sqlite3.connect(backup)
            try:db.backup(target)
            finally:target.close()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS portal_schema(version INTEGER PRIMARY KEY,installed_at TEXT NOT NULL);
        INSERT OR IGNORE INTO portal_schema VALUES(1,CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS facilities(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),name TEXT NOT NULL,location TEXT NOT NULL,meter TEXT NOT NULL,active INTEGER NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reporting_periods(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),name TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('open','closed')),created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),facility_id TEXT NOT NULL REFERENCES facilities(id),period_id TEXT NOT NULL REFERENCES reporting_periods(id),source_bill_id TEXT,source_key TEXT,payload TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(company_id,source_key));
        CREATE INDEX IF NOT EXISTS activity_company_period ON activities(company_id,period_id);
        CREATE INDEX IF NOT EXISTS facilities_company ON facilities(company_id);
        ''')

    def company(request,company_id=None,db=None):
        user=request.state.user
        if not user:raise HTTPException(401,'Sign in required')
        if role(user) in {'company_admin','data_entry'}:
            if company_id and company_id!=user['company_id']:raise HTTPException(403,'Company access denied')
            company_id=user['company_id']
        if not company_id:raise HTTPException(422,'Choose a company workspace first')
        if db is None:
            with accounts.db() as conn:return company(request,company_id,conn)
        row=db.execute('SELECT * FROM companies WHERE id=?',(company_id,)).fetchone()
        if not row or not allowed_company(accounts,user,company_id,db):raise HTTPException(404,'Company not found')
        if not row['active']:raise HTTPException(409,'This company is suspended')
        return dict(row)

    def placement(db,cid,facility_id,period_id):
        facility=db.execute('SELECT * FROM facilities WHERE id=? AND company_id=?',(facility_id,cid)).fetchone()
        period=db.execute('SELECT * FROM reporting_periods WHERE id=? AND company_id=?',(period_id,cid)).fetchone()
        if not facility or not period:raise HTTPException(422,'Choose a facility and reporting period belonging to this company')
        if not facility['active']:raise HTTPException(409,'The facility is inactive')
        if period['status']!='open':raise HTTPException(409,'This reporting period is closed')
        return facility,period

    def owned_activity(db,request,aid):
        row=db.execute('SELECT * FROM activities WHERE id=?',(aid,)).fetchone()
        if not row:raise HTTPException(404,'Activity not found')
        if not allowed_site(accounts,request.state.user,row['company_id'],row['facility_id'],db):raise HTTPException(404,'Activity not found')
        company(request,row['company_id'],db)
        return row,json.loads(row['payload'])

    def linked_factor(user,cid,body):
        if not body.coefficient_version_id:
            return body,None
        canonical=resolve_emission_factor(app,user,body.coefficient_version_id,cid,body.category,body.unit)
        fields={k:canonical[k] for k in ['factor_value','factor_source','factor_url','factor_year','gas']}
        return body.model_copy(update=fields),canonical['coefficient_snapshot']

    def write_activity(db,actor,cid,body,source=None,snapshot=None):
        placement(db,cid,body.facility_id,body.period_id)
        aid=str(uuid.uuid4())
        payload={'id':aid,'company_id':cid,**body.model_dump(mode='json'),'status':'draft','calculation':None,'history':[],'source':source,'created_at':timestamp(),'updated_at':timestamp(),'created_by':actor}
        if snapshot is not None:payload['coefficient_snapshot']=snapshot
        key=(source['sha256']+':page:'+str(source['page'])) if source else None
        try:db.execute('INSERT INTO activities VALUES(?,?,?,?,?,?,?,?)',(aid,cid,body.facility_id,body.period_id,source['id'] if source else None,key,json.dumps(payload),payload['created_at']))
        except sqlite3.IntegrityError:raise HTTPException(409,'This bill page is already in the company inventory')
        accounts.audit(actor,'activity.created',aid,{'company_id':cid,'source_bill_id':source['id'] if source else None},db)
        return payload

    @app.get('/company',include_in_schema=False)
    def portal_page(request:Request):
        if not request.state.user:return RedirectResponse('/login',303)
        return FileResponse(root/'static/company.html',headers={'Cache-Control':'no-store'})

    @app.get('/api/company/context')
    def context(request:Request,company_id:str|None=None):
        selected=company(request,company_id)
        with accounts.db() as db:
            facilities=[dict(r) for r in db.execute('SELECT * FROM facilities WHERE company_id=? ORDER BY name',(selected['id'],)) if allowed_site(accounts,request.state.user,selected['id'],r['id'],db)]
            periods=[dict(r) for r in db.execute('SELECT * FROM reporting_periods WHERE company_id=? ORDER BY start DESC',(selected['id'],))]
        return {'company':selected,'facilities':facilities,'periods':periods,'master_context':request.state.user['role']=='master','factor':accounts.get_setting('factor',default_factor)}

    @app.get('/api/facilities')
    def list_sites(request:Request,company_id:str|None=None):
        return context(request,company_id)['facilities']

    @app.post('/api/facilities',status_code=201)
    def create_site(body:Site,request:Request,company_id:str|None=None):
        reviewer(request.state.user)
        with accounts.db() as db:
            cid=company(request,company_id,db)['id'];fid=str(uuid.uuid4())
            db.execute('INSERT INTO facilities VALUES(?,?,?,?,?,?,?)',(fid,cid,body.name,body.location,body.meter,int(body.active),timestamp()))
            accounts.audit(request.state.user['id'],'facility.created',fid,{'company_id':cid},db)
        return {'id':fid,**body.model_dump()}

    @app.put('/api/facilities/{fid}')
    def edit_site(fid:str,body:Site,request:Request,company_id:str|None=None):
        reviewer(request.state.user)
        with accounts.db() as db:
            cid=company(request,company_id,db)['id']
            if not db.execute('SELECT id FROM facilities WHERE id=? AND company_id=?',(fid,cid)).fetchone():raise HTTPException(404,'Facility not found')
            db.execute('UPDATE facilities SET name=?,location=?,meter=?,active=? WHERE id=?',(body.name,body.location,body.meter,int(body.active),fid))
            accounts.audit(request.state.user['id'],'facility.updated',fid,body.model_dump(),db)
        return {'ok':True}

    @app.get('/api/reporting-periods')
    def list_periods(request:Request,company_id:str|None=None):return context(request,company_id)['periods']

    @app.post('/api/reporting-periods',status_code=201)
    def create_period(body:Period,request:Request,company_id:str|None=None):
        reviewer(request.state.user)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cid=company(request,company_id,db)['id'];pid=str(uuid.uuid4())
            if db.execute('SELECT id FROM reporting_periods WHERE company_id=? AND start<=? AND end>=?',(cid,body.end.isoformat(),body.start.isoformat())).fetchone():raise HTTPException(409,'Reporting periods must not overlap within a company')
            db.execute('INSERT INTO reporting_periods VALUES(?,?,?,?,?,?,?)',(pid,cid,body.name,body.start.isoformat(),body.end.isoformat(),body.status,timestamp()))
            accounts.audit(request.state.user['id'],'period.created',pid,body.model_dump(mode='json'),db)
        return {'id':pid,**body.model_dump(mode='json')}

    @app.put('/api/reporting-periods/{pid}')
    def edit_period(pid:str,body:Period,request:Request,company_id:str|None=None):
        reviewer(request.state.user)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cid=company(request,company_id,db)['id']
            old=db.execute('SELECT * FROM reporting_periods WHERE id=? AND company_id=?',(pid,cid)).fetchone()
            if not old:raise HTTPException(404,'Reporting period not found')
            rows=db.execute('SELECT payload FROM activities WHERE period_id=?',(pid,)).fetchall()
            if rows and (old['start']!=body.start.isoformat() or old['end']!=body.end.isoformat()):raise HTTPException(409,'Dates cannot change after activities have been assigned')
            if body.status=='closed' and any(json.loads(r[0])['status']!='reviewed' for r in rows):raise HTTPException(409,'Review all draft activities before closing this period')
            if db.execute('SELECT id FROM reporting_periods WHERE company_id=? AND id!=? AND start<=? AND end>=?',(cid,pid,body.end.isoformat(),body.start.isoformat())).fetchone():raise HTTPException(409,'Reporting periods must not overlap')
            if old['status']=='closed' and body.status=='open' and request.state.user['role']!='master':raise HTTPException(403,'A master must reopen a closed reporting period')
            db.execute('UPDATE reporting_periods SET name=?,start=?,end=?,status=? WHERE id=?',(body.name,body.start.isoformat(),body.end.isoformat(),body.status,pid))
            accounts.audit(request.state.user['id'],'period.updated',pid,{'before':dict(old),'after':body.model_dump(mode='json')},db)
        return {'ok':True}

    @app.get('/api/activities')
    def activities(request:Request,company_id:str|None=None,facility_id:str|None=None,period_id:str|None=None):
        cid=company(request,company_id)['id']
        with accounts.db() as db:
            rows=db.execute('SELECT payload FROM activities WHERE company_id=? ORDER BY created_at DESC',(cid,)).fetchall()
        return [visible_record(accounts,request.state.user,a) for a in map(lambda r:json.loads(r[0]),rows) if allowed_site(accounts,request.state.user,cid,a['facility_id']) and (not facility_id or a['facility_id']==facility_id) and (not period_id or a['period_id']==period_id)]

    @app.post('/api/activities',status_code=201)
    def create_activity(body:Activity,request:Request,company_id:str|None=None):
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cid=company(request,company_id,db)['id']
            if not allowed_site(accounts,request.state.user,cid,body.facility_id,db):raise HTTPException(403,'Facility access denied')
            body,snapshot=linked_factor(request.state.user,cid,body)
            return write_activity(db,request.state.user['id'],cid,body,snapshot=snapshot)

    @app.put('/api/activities/{aid}')
    def edit_activity(aid:str,body:Activity,request:Request):
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row,old=owned_activity(db,request,aid)
            if role(request.state.user)=='data_entry' and (old['created_by']!=request.state.user['id'] or old['status']!='draft'):raise HTTPException(403,'You can edit only your own draft activities')
            if not allowed_site(accounts,request.state.user,row['company_id'],body.facility_id,db):raise HTTPException(403,'Facility access denied')
            placement(db,row['company_id'],row['facility_id'],row['period_id'])
            placement(db,row['company_id'],body.facility_id,body.period_id)
            body,snapshot=linked_factor(request.state.user,row['company_id'],body)
            history=old['history']+[{'at':timestamp(),'actor_id':request.state.user['id'],'previous':{k:v for k,v in old.items() if k!='history'}}]
            changed={**old,**body.model_dump(mode='json'),'status':'draft','calculation':None,'history':history,'updated_at':timestamp()}
            changed.pop('coefficient_snapshot',None)
            if snapshot is not None:changed['coefficient_snapshot']=snapshot
            db.execute('UPDATE activities SET facility_id=?,period_id=?,payload=? WHERE id=?',(body.facility_id,body.period_id,json.dumps(changed),aid))
            accounts.audit(request.state.user['id'],'activity.revised',aid,{},db)
        return visible_record(accounts,request.state.user,changed)

    @app.post('/api/activities/{aid}/calculate')
    def calculate(aid:str,body:Confirmation,request:Request):
        reviewer(request.state.user)
        if not body.confirmed:raise HTTPException(422,'Confirm the quantity, unit, source and reporting allocation')
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row,a=owned_activity(db,request,aid)
            placement(db,row['company_id'],row['facility_id'],row['period_id'])
            data=Activity.model_validate({k:a[k] for k in Activity.model_fields if k in a})
            data,snapshot=linked_factor(request.state.user,row['company_id'],data)
            if snapshot is not None:
                a.update(data.model_dump(mode='json'))
                a['coefficient_snapshot']=snapshot
            with localcontext() as dec:
                dec.prec=60
                quantity=data.quantity*(1000 if data.category=='electricity' and data.unit=='MWh' else 1)
                result=quantity*data.factor_value
            basis='kWh' if data.category=='electricity' else data.unit
            a['calculation']={'quantity_basis':str(quantity),'factor_unit':'kg '+data.gas+'/'+basis,'factor_value':str(data.factor_value),'emissions_kg':str(result),'gas':data.gas,'method_version':'electricity-location-v1' if data.category=='electricity' else 'custom-factor-v1','at':timestamp(),'actor_id':request.state.user['id']}
            if snapshot is not None:
                a['calculation'].update(coefficient_version_id=data.coefficient_version_id,coefficient_snapshot=snapshot,method_version='published-coefficient-v1')
            a.update(status='reviewed',updated_at=timestamp())
            db.execute('UPDATE activities SET payload=? WHERE id=?',(json.dumps(a),aid))
            accounts.audit(request.state.user['id'],'activity.calculated',aid,{'gas':data.gas,'emissions_kg':str(result)},db)
        return visible_record(accounts,request.state.user,a)

    @app.post('/api/activities/from-bill/{bid}',status_code=201)
    def import_bill(bid:str,body:Placement,request:Request,company_id:str|None=None):
        reviewer(request.state.user)
        cid=company(request,company_id)['id'];bill=get_bill(bid)
        if bill.get('company_id')!=cid:raise HTTPException(404,'Bill not found in this company')
        if bill['status']!='reviewed':raise HTTPException(409,'Review and calculate the bill before adding it to the inventory')
        f=bill['reviewed_fields'];factor=bill['calculation']['factor']
        activity=Activity(**body.model_dump(),title=f.get('supplier') or bill['filename'],category='electricity',scope='2',quantity=f['consumption'].replace(',',''),unit='MWh' if f['unit'].strip().lower()=='mwh' else 'kWh',factor_value=factor['factor_value'],gas=bill['calculation']['gas'],factor_source=factor['factor_source'],factor_url=factor['factor_url'],factor_year=factor['factor_year'],coefficient_version_id=factor.get('coefficient_version_id'),notes='Reporting period allocation requires review. Printed billing period: '+f.get('billing_period',''))
        source={k:bill.get(k) for k in ['id','filename','sha256','page','reviewed_at']}
        source['evidence']=bill['fields'].get('consumption',{}).get('evidence')
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Copy the reviewed bill's immutable version, including a retired
            # historical version; a new ledger calculation rechecks publication.
            return write_activity(db,request.state.user['id'],cid,activity,source,snapshot=factor.get('coefficient_snapshot'))

    @app.get('/api/company/dashboard')
    def dashboard(request:Request,company_id:str|None=None,facility_id:str|None=None,period_id:str|None=None):
        context_data=context(request,company_id)
        records=activities(request,context_data['company']['id'],facility_id,period_id)
        totals={'CO2':Decimal(0),'CO2e':Decimal(0)};scopes={s:{g:Decimal(0) for g in totals} for s in ['1','2','3']}
        with localcontext() as dec:
            dec.prec=60
            for a in records:
                if a['status']=='reviewed' and a['calculation']:
                    value=Decimal(a['calculation']['emissions_kg']);gas=a['calculation']['gas']
                    totals[gas]+=value;scopes[a['scope']][gas]+=value
        with bill_connection() as db:raw=db.execute('SELECT payload FROM bills ORDER BY rowid DESC').fetchall()
        bills=[b for b in map(lambda r:json.loads(r[0]),raw) if b.get('company_id')==context_data['company']['id'] and (role(request.state.user)!='data_entry' or b.get('created_by')==request.state.user['id'])]
        with accounts.db() as db:
            imported={r[0] for r in db.execute('SELECT source_bill_id FROM activities WHERE company_id=? AND source_bill_id IS NOT NULL',(context_data['company']['id'],))}
        documents=[{k:b.get(k) for k in ['id','filename','status','created_at','page','sha256']} | {'in_inventory':b['id'] in imported} for b in bills]
        return {**context_data,'activities':records,'documents':documents,'totals_kg':{g:str(v) for g,v in totals.items()},'by_scope':{s:{g:str(v) for g,v in values.items()} for s,values in scopes.items()},'reviewed':sum(a['status']=='reviewed' for a in records),'drafts':sum(a['status']=='draft' for a in records)}

    @app.get('/api/company/export')
    def export_inventory(request:Request,company_id:str|None=None,facility_id:str|None=None,period_id:str|None=None):
        reviewer(request.state.user)
        cid=company(request,company_id)['id'];records=activities(request,cid,facility_id,period_id)
        output=io.StringIO();writer=csv.writer(output)
        writer.writerow(['Activity','Facility ID','Period ID','Scope','Category','Quantity','Unit','Gas','Emissions kg','Factor','Source','Source URL','Source bill','Status','Coefficient version','Coefficient snapshot'])
        def safe(value):
            value=str(value or '')
            return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
        for a in records:
            c=a['calculation'] or {}
            writer.writerow([safe(v) for v in [a['title'],a['facility_id'],a['period_id'],a['scope'],a['category'],a['quantity'],a['unit'],a['gas'],c.get('emissions_kg',''),a['factor_value'],a['factor_source'],a['factor_url'],(a['source'] or {}).get('id',''),a['status'],a.get('coefficient_version_id',''),json.dumps(c.get('coefficient_snapshot',a.get('coefficient_snapshot'))) if a.get('coefficient_snapshot') else '']])
        accounts.audit(request.state.user['id'],'inventory.exported',cid,{'rows':len(records),'drafts_included':True})
        return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="trace-inventory.csv"','Cache-Control':'no-store'})
