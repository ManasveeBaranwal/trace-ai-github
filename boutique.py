"""Firm-scoped boutique workflow: collection records, evidence and draft reminders.

This is an additive local product module. Owners are business contacts, not
accounts; reminder preparation does not send a message or grant upload access.
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import re
import secrets
import sqlite3
import time
import uuid
import zipfile
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import pymupdf
from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hierarchy import allowed_company, role, visible_record, visible_user
from security import timestamp

MAX_UPLOAD = 20 * 1024 * 1024


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int | None = Field(default=None, ge=1)


class OwnerInput(Input):
    company_id: str
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(default='', max_length=254)
    phone: str = Field(default='', max_length=80)
    department: str = Field(default='', max_length=160)
    notes: str = Field(default='', max_length=2000)

    @field_validator('email')
    @classmethod
    def email_address(cls, value):
        if value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise ValueError('Enter a valid contact email, or leave it blank')
        return value


class SourceInput(Input):
    company_id: str
    name: str = Field(min_length=2, max_length=180)
    owner_id: str | None = None
    facility_id: str | None = None
    source_type: Literal['manual', 'bill', 'spreadsheet', 'sensor', 'api'] = 'manual'
    frequency: str = Field(default='monthly', min_length=1, max_length=80)
    unit: str = Field(default='', max_length=100)
    description: str = Field(default='', max_length=4000)


class DatasetInput(Input):
    company_id: str
    period_id: str
    name: str = Field(min_length=2, max_length=180)
    source_id: str | None = None
    owner_id: str | None = None
    facility_id: str | None = None
    category: str = Field(default='other', min_length=1, max_length=100)
    unit: str = Field(default='', max_length=100)
    value: str = Field(default='', max_length=20000)
    notes: str = Field(default='', max_length=6000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    status: Literal['missing', 'draft', 'submitted'] = 'missing'
    question_id: str = Field(default='', max_length=100)


class RequestInput(Input):
    company_id: str
    period_id: str
    dataset_id: str | None = None
    owner_id: str | None = None
    title: str = Field(min_length=2, max_length=180)
    due_date: date | None = None
    instructions: str = Field(default='', max_length=6000)


class DatasetReview(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    confirmed: bool
    decision: Literal['reviewed', 'returned'] = 'reviewed'
    reason: str = Field(default='', max_length=4000)


class ReminderInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    channel: Literal['email'] = 'email'


class DefinitionInput(Input):
    name: str = Field(min_length=2, max_length=180)
    description: str = Field(min_length=3, max_length=6000)
    unit: str = Field(default='', max_length=100)
    frequency: str = Field(default='annual', max_length=80)
    source_title: str = Field(default='', max_length=500)
    source_url: str = Field(default='', max_length=2000)
    scope: Literal['firm', 'company'] = 'firm'
    company_id: str | None = None

    @field_validator('source_url')
    @classmethod
    def source_link(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('Use a source URL without embedded credentials')
        return value


class Publication(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    confirmed: bool


class SubmissionLinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    expires_days: int = Field(default=7, ge=1, le=14)


class SubmissionInput(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    value: str = Field(min_length=1,max_length=20000)
    notes: str = Field(default='',max_length=6000)


class BoutiqueService:
    def __init__(self, accounts, root, bill_connection, get_bill):
        self.accounts, self.root = accounts, Path(root)
        self.bill_connection, self.get_bill = bill_connection, get_bill
        self.storage = accounts.path.parent / 'boutique-evidence'
        self.storage.mkdir(exist_ok=True)
        with accounts.db() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='boutique_records'").fetchone():
                backup = accounts.path.with_name('accounts.pre-boutique-v1.sqlite3')
                if not backup.exists():
                    target = sqlite3.connect(backup)
                    try: db.backup(target)
                    finally: target.close()
            db.executescript('''
                CREATE TABLE IF NOT EXISTS boutique_records(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,firm_id TEXT NOT NULL REFERENCES firms(id),
                    company_id TEXT REFERENCES companies(id),period_id TEXT REFERENCES reporting_periods(id),
                    revision INTEGER NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,created_by TEXT);
                CREATE INDEX IF NOT EXISTS boutique_scope ON boutique_records(firm_id,company_id,period_id,kind);
                CREATE TABLE IF NOT EXISTS boutique_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,record_id TEXT NOT NULL REFERENCES boutique_records(id),
                    at TEXT NOT NULL,actor_id TEXT,action TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS boutique_submission_links(
                    id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,
                    request_id TEXT NOT NULL REFERENCES boutique_records(id),
                    company_id TEXT NOT NULL,period_id TEXT NOT NULL,dataset_id TEXT NOT NULL,
                    request_revision INTEGER NOT NULL,dataset_revision INTEGER NOT NULL,
                    expires_at REAL NOT NULL,created_at TEXT NOT NULL,created_by TEXT,
                    used_at TEXT);
                CREATE INDEX IF NOT EXISTS boutique_link_request ON boutique_submission_links(request_id,expires_at);
            ''')
        path = self.root / 'research/brsr-disclosures.json'
        self.catalog = json.loads(path.read_text()) if path.exists() else {'version': '', 'items': []}

    @staticmethod
    def _validate(model, payload):
        try: return model.model_validate(payload).model_dump(mode='json')
        except Exception as exc: raise HTTPException(422, str(exc))

    def _scope(self, user, db, firm_id=None):
        if not user or not user.get('active', True): raise HTTPException(401, 'Sign in required')
        r = role(user)
        if r == 'master':
            if not firm_id: raise HTTPException(422, 'Choose a consulting firm')
        elif r in {'consultant_manager', 'consultant'}:
            if firm_id and firm_id != user.get('firm_id'): raise HTTPException(404, 'Firm not found')
            firm_id = user.get('firm_id')
        else: raise HTTPException(403, 'A consulting firm workspace is required')
        firm = db.execute('SELECT * FROM firms WHERE id=? AND active=1', (firm_id,)).fetchone()
        if not firm: raise HTTPException(404, 'Firm not found')
        return dict(firm)

    def _company(self, user, db, cid, fid):
        company = db.execute('SELECT * FROM companies WHERE id=? AND firm_id=?', (cid, fid)).fetchone()
        if not company or not allowed_company(self.accounts, user, cid, db): raise HTTPException(404, 'Company not found')
        if not company['active']: raise HTTPException(409, 'Company is inactive')
        return dict(company)

    def _period(self, db, cid, pid, writable=False):
        period = db.execute('SELECT * FROM reporting_periods WHERE id=? AND company_id=?', (pid, cid)).fetchone()
        if not period: raise HTTPException(422, 'Choose a reporting period belonging to this company')
        if writable and period['status'] != 'open': raise HTTPException(409, 'This reporting period is closed')
        return dict(period)

    def _record(self, user, db, rid, kind, fid):
        row = db.execute('SELECT * FROM boutique_records WHERE id=? AND kind=? AND firm_id=?', (rid, kind, fid)).fetchone()
        if not row: raise HTTPException(404, kind.capitalize()+' not found')
        if row['company_id']: self._company(user, db, row['company_id'], fid)
        return row, json.loads(row['payload'])

    @staticmethod
    def _expected(row, revision):
        if revision is None or revision != row['revision']: raise HTTPException(409, 'This record changed. Reload before saving.')

    def _event(self, db, user, record, action):
        db.execute('INSERT INTO boutique_history(record_id,at,actor_id,action,payload) VALUES(?,?,?,?,?)',
                   (record['id'], timestamp(), user['id'], action, json.dumps(record)))
        self.accounts.audit(user['id'], 'boutique.'+action, record['id'],
                            {'company_id': record.get('company_id'), 'revision': record['revision']}, db)

    def _save(self, db, user, kind, fid, payload, old=None, action='created'):
        at = timestamp()
        record = {**payload, 'id': old['id'] if old else str(uuid.uuid4()), 'kind': kind,
                  'firm_id': fid, 'revision': old['revision']+1 if old else 1,
                  'created_at': old['created_at'] if old else at, 'updated_at': at,
                  'created_by': old.get('created_by') if old else user['id'], 'updated_by': user['id']}
        record.pop('expected_revision', None)
        if old:
            self._event(db, user, old, 'before_'+action)
            db.execute('UPDATE boutique_records SET revision=?,status=?,payload=?,updated_at=? WHERE id=?',
                       (record['revision'], record.get('status', 'active'), json.dumps(record), at, record['id']))
        else:
            db.execute('INSERT INTO boutique_records VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (record['id'], kind, fid, record.get('company_id'), record.get('period_id'), record['revision'],
                        record.get('status', 'active'), json.dumps(record), at, at, user['id']))
        self._event(db, user, record, kind+'_'+action)
        return record

    def _relations(self, user, db, data, fid, writable=True):
        cid = data['company_id']
        self._company(user, db, cid, fid)
        if data.get('period_id'): self._period(db, cid, data['period_id'], writable)
        if data.get('facility_id'):
            found = db.execute('SELECT active FROM facilities WHERE id=? AND company_id=?', (data['facility_id'], cid)).fetchone()
            if not found or not found['active']: raise HTTPException(422, 'Choose an active facility belonging to this company')
        for field, kind in [('owner_id', 'owner'), ('source_id', 'source'), ('dataset_id', 'dataset')]:
            if data.get(field):
                _, linked = self._record(user, db, data[field], kind, fid)
                if linked['company_id'] != cid: raise HTTPException(422, 'Linked record belongs to another company')
                if kind == 'dataset' and linked.get('period_id') != data.get('period_id'):
                    raise HTTPException(422, 'Dataset belongs to another reporting period')
        for eid in data.get('evidence_ids', []):
            _, evidence = self._record(user, db, eid, 'evidence', fid)
            if evidence['company_id'] != cid or (evidence.get('period_id') and evidence['period_id'] != data.get('period_id')):
                raise HTTPException(422, 'Evidence must belong to this company and reporting period')

    def _write(self, user, kind, model, payload, rid=None, firm_id=None):
        data = self._validate(model, payload)
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fid = self._scope(user, db, firm_id)['id']
            old = None
            if rid:
                row, old = self._record(user, db, rid, kind, fid)
                self._expected(row, data.get('expected_revision'))
                if data.get('company_id') != old.get('company_id') or data.get('period_id') != old.get('period_id'):
                    raise HTTPException(422, 'Company and reporting period cannot be changed on an existing record')
            self._relations(user, db, data, fid)
            if kind == 'dataset':
                if not data.get('owner_id') and data.get('source_id'):
                    _, source = self._record(user, db, data['source_id'], 'source', fid)
                    data['owner_id'] = source.get('owner_id')
                if data['status'] == 'submitted' and not data['value']:
                    raise HTTPException(422, 'Enter a value or narrative before submitting')
                if data['value'] and data['status'] == 'missing': data['status'] = 'draft'
                data['evidence_ids'] = list(dict.fromkeys(data['evidence_ids']))
            elif kind == 'request':
                if not data.get('owner_id') and data.get('dataset_id'):
                    _, dataset = self._record(user, db, data['dataset_id'], 'dataset', fid)
                    data['owner_id'] = dataset.get('owner_id')
                data['status'] = 'open'
            else: data['status'] = 'active'
            result = self._save(db, user, kind, fid, data, old, 'updated' if old else 'created')
        return visible_record(self.accounts, user, result)

    def create_owner(self, user, payload, firm_id=None): return self._write(user, 'owner', OwnerInput, payload, firm_id=firm_id)
    def create_source(self, user, payload, firm_id=None): return self._write(user, 'source', SourceInput, payload, firm_id=firm_id)
    def create_dataset(self, user, payload, firm_id=None): return self._write(user, 'dataset', DatasetInput, payload, firm_id=firm_id)
    def create_request(self, user, payload, firm_id=None): return self._write(user, 'request', RequestInput, payload, firm_id=firm_id)

    def review_dataset(self, user, rid, payload, firm_id=None):
        data = self._validate(DatasetReview, payload)
        if not data['confirmed']: raise HTTPException(422, 'Confirm that you checked the submitted value and source')
        if data['decision'] == 'returned' and not data['reason']: raise HTTPException(422, 'Explain the correction required')
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fid = self._scope(user, db, firm_id)['id']
            row, old = self._record(user, db, rid, 'dataset', fid)
            self._expected(row, data['expected_revision'])
            self._period(db, old['company_id'], old['period_id'], True)
            if old['status'] != 'submitted': raise HTTPException(409, 'Submit the dataset before review')
            if data['decision'] == 'reviewed' and not old.get('evidence_ids') and not data['reason']:
                raise HTTPException(422, 'Attach supporting evidence or record the manual verification basis in the review reason')
            changed = {**old, 'status': data['decision'], 'review_reason': data['reason'], 'reviewed_at': timestamp(), 'reviewed_by': user['id']}
            result = self._save(db, user, 'dataset', fid, changed, old, data['decision'])
        return visible_record(self.accounts, user, result)

    @staticmethod
    def _expiry(value):
        return datetime.fromtimestamp(value,timezone.utc).isoformat()

    def _active_links(self,db,request,dataset):
        if not dataset:return []
        return db.execute('''SELECT id,expires_at FROM boutique_submission_links
            WHERE request_id=? AND used_at IS NULL AND expires_at>?
            AND request_revision=? AND dataset_revision=? ORDER BY expires_at DESC''',
            (request['id'],time.time(),request['revision'],dataset['revision'])).fetchall()

    def _issue_link(self,db,user,request,dataset,expires_days=7):
        if dataset['status'] in {'submitted','reviewed'}:
            raise HTTPException(409,'The requested submission has already been received')
        if len(self._active_links(db,request,dataset)) >= 20:
            raise HTTPException(429,'This request already has enough active collection links')
        token=secrets.token_urlsafe(32);link_id=str(uuid.uuid4())
        expires_at=time.time()+expires_days*86400
        db.execute('INSERT INTO boutique_submission_links VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
            (link_id,hashlib.sha256(token.encode()).hexdigest(),request['id'],request['company_id'],
             request['period_id'],dataset['id'],request['revision'],dataset['revision'],expires_at,timestamp(),user['id']))
        self.accounts.audit(user['id'],'boutique.submission_link_created',link_id,
            {'request_id':request['id'],'company_id':request['company_id'],'expires_at':self._expiry(expires_at)},db)
        return {'url':'/collect/'+token,'submission_url':'/collect/'+token,
                'expires_at':self._expiry(expires_at),'scope':'single_request','request_id':request['id']}

    def create_submission_link(self,user,rid,payload,firm_id=None):
        body=self._validate(SubmissionLinkInput,payload)
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fid=self._scope(user,db,firm_id)['id']
            if role(user) not in {'master','consultant_manager'}:
                raise HTTPException(403,'Only the firm manager can create collection links')
            row,request=self._record(user,db,rid,'request',fid)
            self._expected(row,body['expected_revision'])
            self._period(db,request['company_id'],request['period_id'],True)
            if not request.get('dataset_id'):raise HTTPException(422,'Assign a dataset before creating a collection link')
            _,dataset=self._record(user,db,request['dataset_id'],'dataset',fid)
            if dataset['company_id']!=request['company_id'] or dataset['period_id']!=request['period_id']:
                raise HTTPException(422,'The request and dataset must use the same company and period')
            return self._issue_link(db,user,request,dataset,body['expires_days'])

    def _collection(self,db,token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,100}',token):raise HTTPException(404,'Collection link not found')
        link=db.execute('SELECT * FROM boutique_submission_links WHERE token_hash=?',
                        (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not link:raise HTTPException(404,'Collection link not found')
        if link['expires_at']<=time.time():raise HTTPException(410,'This collection link has expired. Ask your consulting team for a new link.')
        if link['used_at']:raise HTTPException(409,'This request has already been submitted')
        company=db.execute('''SELECT c.* FROM companies c JOIN firms f ON f.id=c.firm_id
            WHERE c.id=? AND c.active=1 AND f.active=1''',(link['company_id'],)).fetchone()
        period=db.execute('SELECT * FROM reporting_periods WHERE id=? AND company_id=? AND status=?',
                          (link['period_id'],link['company_id'],'open')).fetchone()
        if not company or not period:raise HTTPException(410,'This request is no longer open')
        reqrow=db.execute("SELECT * FROM boutique_records WHERE id=? AND kind='request' AND company_id=? AND period_id=? AND firm_id=?",
                          (link['request_id'],link['company_id'],link['period_id'],company['firm_id'])).fetchone()
        dsrow=db.execute("SELECT * FROM boutique_records WHERE id=? AND kind='dataset' AND company_id=? AND period_id=? AND firm_id=?",
                         (link['dataset_id'],link['company_id'],link['period_id'],company['firm_id'])).fetchone()
        if not reqrow or not dsrow:raise HTTPException(410,'This request is no longer available')
        request,dataset=json.loads(reqrow['payload']),json.loads(dsrow['payload'])
        if request.get('dataset_id')!=dataset['id'] or reqrow['revision']!=link['request_revision'] or dsrow['revision']!=link['dataset_revision']:
            raise HTTPException(409,'This request changed. Ask your consulting team for an updated collection link.')
        if dataset['status'] in {'submitted','reviewed'}:raise HTTPException(409,'This request has already been received')
        return dict(link),request,dataset,dict(company),dict(period)

    def collection_form(self,token):
        with self.accounts.db() as db:link,request,dataset,company,period=self._collection(db,token)
        nonce=hashlib.sha256((token+':submission-form').encode()).hexdigest()
        def e(value):return escape(str(value or ''),quote=True)
        return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Submit requested data · Trace AI</title><style>
body{margin:0;background:#f3f5ec;color:#193d31;font:16px/1.6 system-ui,sans-serif}main{max-width:620px;margin:50px auto;background:white;border:1px solid #dae4d2;border-radius:18px;padding:32px}label{display:block;margin:20px 0 6px;font-weight:600}input,textarea{box-sizing:border-box;width:100%;padding:12px;border:1px solid #ccd9c7;border-radius:7px;font:inherit}button{margin-top:24px;padding:14px 20px;border:0;border-radius:7px;background:#214e39;color:white;font:inherit;cursor:pointer}.small{font-size:13px;color:#6c7c69}.note{white-space:pre-wrap}h1{line-height:1.2}@media(max-width:700px){main{margin:18px;padding:24px}}</style></head><body><main>
<strong>↗ trace AI</strong><p class="small">REQUESTED DATA SUBMISSION</p><h1>'''+e(request['title'])+'''</h1><p>'''+e(company['name'])+' · '+e(period['name'])+'''</p><p class="note">'''+e(request.get('instructions'))+'''</p>
<p class="small">Due: '''+e(request.get('due_date') or 'Not specified')+' · Link expires: '+e(self._expiry(link['expires_at']))+'''</p>
<form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf" value="'''+nonce+'''">
<label for="value">Requested value or narrative'''+(' ('+e(dataset['unit'])+')' if dataset.get('unit') else '')+'''</label><textarea id="value" name="value" rows="4" maxlength="20000" required></textarea>
<label for="notes">Notes about your source or method</label><textarea id="notes" name="notes" rows="3" maxlength="6000"></textarea>
<label for="evidence">Supporting evidence (optional)</label><input id="evidence" name="file" type="file" accept=".pdf,.png,.jpg,.jpeg,.csv,.xlsx"><p class="small">PDF, PNG, JPG, CSV or XLSX · 20 MB maximum. Supply only information requested here.</p>
<button type="submit">Submit for review →</button></form><p class="small">This link permits this request only. Your consulting team will review the submitted information.</p></main></body></html>'''

    def submit_collection(self,token,value,notes='',blob=None,filename=''):
        body=self._validate(SubmissionInput,{'value':value,'notes':notes})
        with self.accounts.db() as db:self._collection(db,token)
        file_info=self._file_info(filename,blob) if blob else None
        paths=[]
        try:
            with self.accounts.db() as db:
                db.execute('BEGIN IMMEDIATE')
                link,request,dataset,company,period=self._collection(db,token)
                actor={'id':'submission:'+link['id']}
                evidence_ids=list(dataset.get('evidence_ids',[]))
                if file_info:
                    extension,mime,preview=file_info
                    record=self._save(db,actor,'evidence',company['firm_id'],{
                        'company_id':company['id'],'period_id':period['id'],'dataset_id':dataset['id'],
                        'title':request['title']+' — submitted evidence','filename':Path(filename).name[:255],
                        'mime':mime,'size':len(blob),'sha256':hashlib.sha256(blob).hexdigest(),'extension':extension,
                        'status':'stored','has_preview':preview is not None})
                    path=self.storage/(record['id']+extension);path.write_bytes(blob);paths.append(path)
                    if preview is not None:
                        path=self.storage/(record['id']+'.preview.png');path.write_bytes(preview);paths.append(path)
                    record.update(download_url='/api/boutique/evidence/'+record['id']+'/download',
                                  preview_url='/api/boutique/evidence/'+record['id']+'/preview' if preview is not None else None)
                    db.execute('UPDATE boutique_records SET payload=? WHERE id=?',(json.dumps(record),record['id']))
                    evidence_ids.append(record['id'])
                changed={**dataset,**body,'evidence_ids':evidence_ids,'status':'submitted',
                         'submitted_at':timestamp(),'submitted_via':'scoped_collection_link'}
                for field in ['review_reason','reviewed_at','reviewed_by']:changed.pop(field,None)
                result=self._save(db,actor,'dataset',company['firm_id'],changed,dataset,'owner_submitted')
                db.execute('UPDATE boutique_submission_links SET used_at=? WHERE request_id=? AND used_at IS NULL',
                           (timestamp(),request['id']))
            return {'ok':True,'status':'submitted','message':'Your submission has been received for review.'}
        except Exception:
            for path in paths:path.unlink(missing_ok=True)
            raise

    def prepare_reminder(self, user, rid, channel='email', firm_id=None):
        if channel != 'email': raise HTTPException(422, 'Only email draft preparation is available')
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            firm = self._scope(user, db, firm_id)
            _, req = self._record(user, db, rid, 'request', firm['id'])
            self._period(db, req['company_id'], req['period_id'], True)
            if req.get('dataset_id'):
                _, dataset = self._record(user, db, req['dataset_id'], 'dataset', firm['id'])
                if dataset['status'] in {'submitted','reviewed'}:
                    raise HTTPException(409, 'The submission has already been received. Review or return it before preparing another reminder.')
            company = self._company(user, db, req['company_id'], firm['id'])
            fresh_link=None
            if req.get('dataset_id') and role(user) in {'master','consultant_manager'}:
                _, dataset = self._record(user, db, req['dataset_id'], 'dataset', firm['id'])
                if self._active_links(db,req,dataset):fresh_link=self._issue_link(db,user,req,dataset)
            owner = None
            if req.get('owner_id'): _, owner = self._record(user, db, req['owner_id'], 'owner', firm['id'])
            subject = 'Data request reminder: '+req['title']
            text = 'Hello '+(owner['name'] if owner else 'data owner')+',\n\nPlease provide '+req['title']+' for '+company['name']+'.'
            if req.get('due_date'): text += ' The requested due date is '+req['due_date']+'.'
            if req.get('instructions'): text += '\n\n'+req['instructions']
            text += '\n\nPlease contact your consulting team to submit the requested evidence.\n\n'+firm['name']
            draft = {'company_id': req['company_id'], 'period_id': req['period_id'], 'request_id': rid,
                     'channel': 'email', 'to': owner.get('email') if owner else None,
                     'subject': subject, 'body': text, 'status': 'draft', 'delivery': 'not_sent',
                     'submission_url': None, 'submission_link_status': 'available' if fresh_link else 'not_enabled',
                     'missing_contact': not bool(owner and owner.get('email'))}
            if fresh_link:draft['submission_link_expires_at']=fresh_link['expires_at']
            result = self._save(db, user, 'reminder', firm['id'], draft)
            if fresh_link:
                # Return the fresh bearer link once. Persist only a redacted
                # draft, never a recoverable raw collection credential.
                result={**result,'body':text+'\n\nSubmission link: '+fresh_link['url'],
                        'submission_url':fresh_link['url'],'submission_link_status':'created_for_draft',
                        'submission_link_expires_at':fresh_link['expires_at']}
        return visible_record(self.accounts, user, result)

    @staticmethod
    def _financial_year(period):
        try:
            start = date.fromisoformat(period['start']); year = start.year if start.month >= 4 else start.year-1
            return f'{year}-{str(year+1)[-2:]}'
        except (ValueError, KeyError): return ''

    def context(self, user, company_id=None, period_id=None, year=None, firm_id=None):
        with self.accounts.db() as db:
            firm = self._scope(user, db, firm_id)
            if period_id:
                period = db.execute('SELECT * FROM reporting_periods WHERE id=?', (period_id,)).fetchone()
                if not period: raise HTTPException(404, 'Reporting period not found')
                if company_id and period['company_id'] != company_id: raise HTTPException(404, 'Reporting period not found')
                company_id = period['company_id']
            selected = self._company(user, db, company_id, firm['id']) if company_id else None
            companies = [dict(row) for row in db.execute('SELECT * FROM companies WHERE firm_id=? ORDER BY name', (firm['id'],))]
            if selected: companies = [selected]
            cids = {c['id'] for c in companies if allowed_company(self.accounts, user, c['id'], db)}
            facilities = [dict(row) for row in db.execute('SELECT * FROM facilities ORDER BY name') if row['company_id'] in cids]
            periods = [dict(row) for row in db.execute('SELECT * FROM reporting_periods ORDER BY start DESC') if row['company_id'] in cids]
            for period in periods: period['financial_year'] = self._financial_year(period)
            eligible = {p['id'] for p in periods if (not period_id or p['id'] == period_id) and
                        (not year or year == p['financial_year'] or year == p['name'])}
            if period_id and period_id not in eligible: raise HTTPException(422, 'Reporting year does not match this period')
            users = [self.accounts.public_user(raw) for raw in db.execute('SELECT * FROM users ORDER BY name')
                     if visible_user(user, self.accounts.public_user(raw)) and
                     self.accounts.public_user(raw).get('firm_id') == firm['id']]
            if role(user) in {'consultant_manager','consultant'} and not any(u['id'] == user['id'] for u in users):
                users.append({k:user.get(k) for k in ['id','name','email','role','firm_id','company_id','active','created_at']})
            records = [json.loads(row['payload']) for row in db.execute('SELECT * FROM boutique_records WHERE firm_id=? ORDER BY created_at DESC', (firm['id'],))
                       if (not row['company_id'] or row['company_id'] in cids) and
                       (not row['period_id'] or row['period_id'] in eligible)]
            grouped = {kind: [r for r in records if r['kind'] == kind] for kind in ['owner', 'source', 'dataset', 'request', 'evidence', 'reminder', 'definition']}
            activities = [json.loads(row['payload']) for row in db.execute('SELECT * FROM activities ORDER BY created_at DESC')
                          if row['company_id'] in cids and row['period_id'] in eligible]
            reports = []
            answers = []
            for row in db.execute('SELECT * FROM brsr_reports ORDER BY financial_year DESC,created_at DESC'):
                if row['company_id'] not in cids: continue
                report = json.loads(row['payload'])
                if year and report['financial_year'] != year: continue
                if period_id:
                    matching = next(p for p in periods if p['id'] == period_id)
                    if report['financial_year'] != matching['financial_year']: continue
                current = [json.loads(a[0]) for a in db.execute('SELECT payload FROM brsr_answers WHERE report_id=?', (row['id'],))]
                catalogue = db.execute('SELECT payload FROM brsr_catalog_versions WHERE version=?', (report['catalog_version'],)).fetchone()
                questions = json.loads(catalogue[0])['items'] if catalogue else []
                required = {q['id'] for q in questions if q.get('required')}
                reviewed = {a['question_id'] for a in current if a['status'] == 'reviewed' and a.get('applicability') != 'missing'}
                report['progress'] = {'required': len(required), 'required_reviewed': len(required & reviewed),
                                      'completion_percent': round(100*len(required & reviewed)/len(required), 1) if required else 0}
                reports.append(report)
                answers += [{**a, 'company_id': row['company_id'], 'report_id': row['id']} for a in current]
            for request in grouped['request']:
                dataset = next((d for d in grouped['dataset'] if d['id'] == request.get('dataset_id')), None)
                request['status'] = 'closed' if dataset and dataset['status'] == 'reviewed' else 'received' if dataset and dataset['status'] == 'submitted' else 'open'
                request['overdue'] = bool(request['status'] == 'open' and request.get('due_date') and request['due_date'] < date.today().isoformat())
                active=self._active_links(db,request,dataset) if request['status']=='open' else []
                request['has_submission_link']=bool(active)
                request['submission_link_expires_at']=self._expiry(active[0]['expires_at']) if active else None
            consultant_used = db.execute("SELECT COUNT(*) FROM users WHERE firm_id=? AND access_role='consultant' AND active=1", (firm['id'],)).fetchone()[0]
            firm.update(consultant_used=consultant_used, company_used=db.execute('SELECT COUNT(*) FROM companies WHERE firm_id=? AND active=1', (firm['id'],)).fetchone()[0])
        with self.bill_connection() as db:
            bills = [json.loads(row[0]) for row in db.execute('SELECT payload FROM bills ORDER BY rowid DESC')]
        bills = [b for b in bills if b.get('company_id') in cids]
        # Bills have a printed billing period, not a reporting-year allocation.
        # Only include a year-filtered bill if a ledger entry assigns it there.
        if year or period_id:
            allocated = {a.get('source_bill_id') or (a.get('source') or {}).get('id') for a in activities}
            bills = [b for b in bills if b['id'] in allocated]
        bill_summaries = [{k:b.get(k) for k in ['id','company_id','filename','status','created_at','reviewed_at','page','sha256']} for b in bills]
        for bill in bill_summaries:
            grouped['evidence'].append({**bill, 'kind':'bill', 'bill_id':bill['id'], 'title':bill['filename'],
                                        'url':'/app?company='+bill['company_id']+'&bill='+bill['id'], 'has_preview':True,
                                        'download_url':None, 'preview_url':'/api/bills/'+bill['id']+'/preview'})
        queue = [{'kind':'dataset','id':d['id'],'company_id':d['company_id'],'period_id':d['period_id'],
                  'title':d['name'],'status':d['status'],'url':None} for d in grouped['dataset'] if d['status'] in {'submitted','returned'}]
        queue += [{'kind':'activity','id':a['id'],'company_id':a['company_id'],'period_id':a['period_id'],
                   'title':a['title'],'status':a['status'],'url':'/company?company='+a['company_id']+'&view=inventory'}
                  for a in activities if a['status'] != 'reviewed']
        queue += [{'kind':'bill','id':b['id'],'company_id':b['company_id'],'title':b['filename'],'status':b['status'],
                   'url':'/app?company='+b['company_id']+'&bill='+b['id']} for b in bill_summaries if b['status'] == 'needs_review']
        queue += [{'kind':'disclosure','id':a['question_id'],'report_id':a['report_id'],'company_id':a['company_id'],
                   'title':a['question_id'],'status':a['status'],'url':'/brsr?company='+a['company_id']+'&report='+a['report_id']+'#reports'}
                  for a in answers if a['status'] == 'submitted']
        for company in companies:
            company.update(dataset_count=sum(d['company_id'] == company['id'] for d in grouped['dataset']),
                           missing_count=sum(d['company_id'] == company['id'] and d['status'] in {'missing','returned'} for d in grouped['dataset']),
                           overdue_count=sum(r['company_id'] == company['id'] and r['overdue'] for r in grouped['request']),
                           review_count=sum(r['company_id'] == company['id'] for r in queue),
                           report_count=sum(r['company_id'] == company['id'] for r in reports))
        result = {'firm':firm, 'company':selected, 'companies':companies, 'users':users, 'facilities':facilities,
                  'periods':periods, 'selected_period':next((p for p in periods if p['id'] == period_id), None),
                  'owners':grouped['owner'], 'sources':grouped['source'], 'datasets':grouped['dataset'],
                  'requests':grouped['request'], 'evidence':grouped['evidence'], 'reminders':grouped['reminder'],
                  'definitions':[d for d in grouped['definition'] if not selected or d.get('scope') == 'firm' or d.get('company_id') == selected['id']],
                  'activities':activities,'reports':reports,'bills':bill_summaries,'review_queue':queue,
                  'stats':{'companies':len(companies),'owners':len(grouped['owner']),'sources':len(grouped['source']),
                           'datasets':len(grouped['dataset']),'missing':sum(d['status'] in {'missing','returned'} for d in grouped['dataset']),
                           'submitted':sum(d['status'] == 'submitted' for d in grouped['dataset']),
                           'reviewed':sum(d['status'] == 'reviewed' for d in grouped['dataset']),
                           'overdue':sum(r['overdue'] for r in grouped['request']),'evidence':len(grouped['evidence']),
                           'reports':len(reports),'pending_reviews':len(queue)}}
        return visible_record(self.accounts, user, result)

    def overview(self, user, firm_id=None): return self.context(user, firm_id=firm_id)

    def save_definition(self, user, payload, rid=None, firm_id=None):
        data = self._validate(DefinitionInput, payload)
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fid = self._scope(user, db, firm_id)['id']
            if role(user) not in {'master','consultant_manager'}: raise HTTPException(403, 'Only the firm manager can maintain definitions')
            if data['scope'] == 'company':
                if not data.get('company_id'): raise HTTPException(422, 'Select the client for this definition')
                self._company(user, db, data['company_id'], fid)
            elif data.get('company_id'): raise HTTPException(422, 'A firm-wide definition cannot target one company')
            old = None
            if rid:
                row, old = self._record(user, db, rid, 'definition', fid)
                self._expected(row, data['expected_revision'])
                if old['status'] != 'draft': raise HTTPException(409, 'Create a revised draft to change a published definition')
                if (old.get('company_id'), old['scope']) != (data.get('company_id'), data['scope']):
                    raise HTTPException(422, 'Definition scope cannot change within a version')
            data.update(status='draft', family_id=old['family_id'] if old else str(uuid.uuid4()), version=old['version'] if old else 1)
            result = self._save(db, user, 'definition', fid, data, old, 'updated' if old else 'created')
        return visible_record(self.accounts, user, result)

    def publish_definition(self, user, rid, payload, firm_id=None):
        body = self._validate(Publication, payload)
        if not body['confirmed']: raise HTTPException(422, 'Confirm that you checked the source and applicability')
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fid = self._scope(user, db, firm_id)['id']
            if role(user) not in {'master','consultant_manager'}: raise HTTPException(403, 'Only the firm manager can publish definitions')
            row, old = self._record(user, db, rid, 'definition', fid)
            self._expected(row, body['expected_revision'])
            if old['status'] != 'draft': raise HTTPException(409, 'Only draft definitions can be published')
            if not old.get('source_title'): raise HTTPException(422, 'Provide a source or internal method title before publication')
            changed = {**old, 'status':'published', 'published_at':timestamp(), 'published_by':user['id']}
            result = self._save(db, user, 'definition', fid, changed, old, 'published')
        return visible_record(self.accounts, user, result)

    def revise_definition(self, user, rid, payload, firm_id=None):
        body = self._validate(Publication, payload)
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            fid = self._scope(user, db, firm_id)['id']
            if role(user) not in {'master','consultant_manager'}: raise HTTPException(403, 'Only the firm manager can revise definitions')
            row, old = self._record(user, db, rid, 'definition', fid)
            self._expected(row, body['expected_revision'])
            if not body['confirmed'] or old['status'] != 'published': raise HTTPException(409, 'Confirm a published version before creating its revision')
            versions = [json.loads(r[0]) for r in db.execute("SELECT payload FROM boutique_records WHERE kind='definition' AND firm_id=?", (fid,))]
            if any(d['family_id'] == old['family_id'] and d['status'] == 'draft' for d in versions): raise HTTPException(409, 'This definition already has a draft revision')
            changed = {k:v for k,v in old.items() if k not in {'id','revision','created_at','updated_at','created_by','updated_by','published_at','published_by'}}
            changed.update(status='draft', version=max(d['version'] for d in versions if d['family_id'] == old['family_id'])+1)
            result = self._save(db, user, 'definition', fid, changed, action='revised')
        return visible_record(self.accounts, user, result)

    def standards(self, user, company_id=None, firm_id=None):
        context = self.context(user, company_id=company_id, firm_id=firm_id)
        return {'catalog_version':self.catalog.get('version'), 'items':self.catalog.get('items', []), 'definitions':context['definitions']}

    @staticmethod
    def _file_info(filename, blob):
        extension = Path(filename).suffix.lower()
        types = {'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.csv':'text/csv','.xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
        if extension not in types: raise HTTPException(415, 'Upload a PDF, PNG, JPG, CSV or XLSX evidence file')
        if not blob or len(blob) > MAX_UPLOAD: raise HTTPException(422, 'Choose a nonempty evidence file no larger than 20 MB')
        preview = None
        try:
            if extension in {'.pdf','.png','.jpg','.jpeg'}:
                with pymupdf.open(stream=blob, filetype=extension.lstrip('.')) as doc:
                    if doc.needs_pass or not len(doc): raise ValueError('Password-protected or empty document')
                    rect = doc[0].rect
                    if not rect.width or not rect.height: raise ValueError('Empty document page')
                    scale = min(1.5, math.sqrt(2_000_000/(rect.width*rect.height)))
                    preview = doc[0].get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False).tobytes('png')
            elif extension == '.csv':
                text = blob.decode('utf-8-sig')
                if '\x00' in text: raise ValueError('Binary CSV data')
                for index, _ in enumerate(csv.reader(io.StringIO(text))):
                    if index >= 100000: raise ValueError('CSV exceeds the supported row count')
            else:
                with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                    if len(archive.infolist()) > 10000 or sum(x.file_size for x in archive.infolist()) > 100*1024*1024:
                        raise ValueError('Workbook is too large after decompression')
                    if '[Content_Types].xml' not in archive.namelist() or 'xl/workbook.xml' not in archive.namelist(): raise ValueError('Invalid workbook')
        except Exception:
            raise HTTPException(422, 'The evidence file is invalid, encrypted or exceeds supported document limits')
        return extension, types[extension], preview

    def upload_evidence(self, user, company_id, blob, filename, period_id=None, dataset_id=None, title='', firm_id=None):
        # Check scope before invoking document parsers or writing source bytes.
        with self.accounts.db() as db:
            fid = self._scope(user, db, firm_id)['id']
            self._company(user, db, company_id, fid)
        extension, mime, preview = self._file_info(filename, blob)
        paths = []
        try:
            with self.accounts.db() as db:
                db.execute('BEGIN IMMEDIATE')
                fid = self._scope(user, db, firm_id)['id']
                if dataset_id:
                    _, dataset = self._record(user, db, dataset_id, 'dataset', fid)
                    period_id = period_id or dataset['period_id']
                data = {'company_id':company_id,'period_id':period_id,'dataset_id':dataset_id}
                self._relations(user, db, data, fid)
                record = self._save(db, user, 'evidence', fid, {**data,'title':title.strip()[:180] or Path(filename).name,
                    'filename':Path(filename).name[:255],'mime':mime,'size':len(blob),'sha256':hashlib.sha256(blob).hexdigest(),
                    'extension':extension,'status':'stored','has_preview':preview is not None})
                path = self.storage/(record['id']+extension)
                path.write_bytes(blob); paths.append(path)
                if preview is not None:
                    path = self.storage/(record['id']+'.preview.png'); path.write_bytes(preview);paths.append(path)
                record.update(download_url='/api/boutique/evidence/'+record['id']+'/download',preview_url='/api/boutique/evidence/'+record['id']+'/preview' if preview is not None else None)
                db.execute('UPDATE boutique_records SET payload=? WHERE id=?',(json.dumps(record),record['id']))
                if dataset_id:
                    changed = {**dataset,'evidence_ids':list(dict.fromkeys(dataset.get('evidence_ids',[])+[record['id']]))}
                    if changed['status'] == 'reviewed': changed['status'] = 'draft'
                    self._save(db,user,'dataset',fid,changed,dataset,'evidence_attached')
            return visible_record(self.accounts,user,record)
        except Exception:
            for path in paths: path.unlink(missing_ok=True)
            raise

    def evidence_path(self,user,rid,preview=False,firm_id=None):
        with self.accounts.db() as db:
            fid = self._scope(user,db,firm_id)['id']
            _, record = self._record(user,db,rid,'evidence',fid)
        if preview and not record.get('has_preview'): raise HTTPException(404,'This file has no image preview')
        path = self.storage/(rid+('.preview.png' if preview else record['extension']))
        if not path.is_file(): raise HTTPException(404,'Evidence file not found')
        return path,record


def install_boutique(app,accounts,root,bill_connection,get_bill):
    service = BoutiqueService(accounts,root,bill_connection,get_bill)
    app.state.boutique = service

    def user(request): return getattr(request.state,'user',None)

    class CollectionLogFilter(logging.Filter):
        name='boutique_collection_credentials'
        def filter(self,record):
            if isinstance(record.args,tuple) and len(record.args)>=3 and isinstance(record.args[2],str):
                args=list(record.args)
                args[2]=re.sub(r'(/collect/)[^/?#\s]+',r'\1[redacted]',args[2])
                record.args=tuple(args)
            return True
    access_logger=logging.getLogger('uvicorn.access')
    if not any(getattr(f,'name','')=='boutique_collection_credentials' for f in access_logger.filters):
        access_logger.addFilter(CollectionLogFilter('boutique_collection_credentials'))

    collection_headers={'Cache-Control':'no-store','Referrer-Policy':'origin',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}
    def collection_message(title,message,status=200):
        return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title><body style="font:16px/1.7 system-ui;background:#f3f5ec;color:#193d31;padding:40px"><main style="max-width:600px;margin:auto;background:white;padding:32px;border-radius:16px"><strong>↗ trace AI</strong><h1>'+escape(title)+'</h1><p>'+escape(message)+'</p></main></body></html>',status_code=status,headers=collection_headers)

    @app.get('/collect/{token}',include_in_schema=False)
    def collection_page(token:str):
        try:return HTMLResponse(service.collection_form(token),headers=collection_headers)
        except HTTPException as exc:return collection_message('Collection link unavailable',str(exc.detail),exc.status_code)

    @app.post('/collect/{token}',include_in_schema=False)
    async def collection_submit(token:str,request:Request,value:str=Form(...),notes:str=Form(''),csrf:str=Form(''),file:UploadFile|None=File(None)):
        expected_origin=str(request.base_url).rstrip('/')
        if request.headers.get('origin')!=expected_origin or request.headers.get('sec-fetch-site')=='cross-site':
            return collection_message('Submission not accepted','Open the collection link directly and submit from its form.',403)
        expected_nonce=hashlib.sha256((token+':submission-form').encode()).hexdigest()
        if not hmac.compare_digest(csrf,expected_nonce):
            return collection_message('Submission not accepted','Reload the collection form before submitting.',403)
        try:
            blob=await file.read(MAX_UPLOAD+1) if file and file.filename else None
            result=service.submit_collection(token,value,notes,blob,file.filename if file else '')
            return collection_message('Submission received',result['message']+' You can close this page.')
        except HTTPException as exc:return collection_message('Submission not accepted',str(exc.detail),exc.status_code)

    @app.get('/api/boutique/overview')
    def overview(request:Request,firm_id:str|None=None): return service.overview(user(request),firm_id)

    @app.get('/api/boutique/company')
    def company(request:Request,company_id:str,year:str|None=None,period_id:str|None=None,firm_id:str|None=None):
        return service.context(user(request),company_id,period_id,year,firm_id)

    models = {'owners':('owner',OwnerInput),'sources':('source',SourceInput),'datasets':('dataset',DatasetInput),'requests':('request',RequestInput)}
    for plural,(kind,model) in models.items():
        def handlers(kind,model):
            def create(payload:dict,request:Request,firm_id:str|None=None):
                return service._write(user(request),kind,model,payload,firm_id=firm_id)
            def update(rid:str,payload:dict,request:Request,firm_id:str|None=None):
                return service._write(user(request),kind,model,payload,rid,firm_id)
            return create,update
        create,update = handlers(kind,model)
        app.add_api_route('/api/boutique/'+plural,create,methods=['POST'],status_code=201,name='boutique_create_'+kind)
        app.add_api_route('/api/boutique/'+plural+'/{rid}',update,methods=['PUT'],name='boutique_update_'+kind)

    @app.post('/api/boutique/datasets/{rid}/review')
    def review(rid:str,body:DatasetReview,request:Request,firm_id:str|None=None): return service.review_dataset(user(request),rid,body.model_dump(),firm_id)

    @app.post('/api/boutique/requests/{rid}/reminder',status_code=201)
    def reminder(rid:str,body:ReminderInput,request:Request,firm_id:str|None=None): return service.prepare_reminder(user(request),rid,body.channel,firm_id)

    @app.post('/api/boutique/requests/{rid}/submission-link',status_code=201)
    def submission_link(rid:str,body:SubmissionLinkInput,request:Request,firm_id:str|None=None):
        return service.create_submission_link(user(request),rid,body.model_dump(),firm_id)

    @app.post('/api/boutique/evidence',status_code=201)
    async def evidence(request:Request,file:UploadFile=File(...),company_id:str=Form(...),period_id:str|None=Form(None),dataset_id:str|None=Form(None),title:str=Form(''),firm_id:str|None=Form(None)):
        blob = await file.read(MAX_UPLOAD+1)
        return service.upload_evidence(user(request),company_id,blob,file.filename or 'evidence',period_id,dataset_id,title,firm_id)

    @app.get('/api/boutique/evidence/{rid}/download')
    def download(rid:str,request:Request,firm_id:str|None=None):
        path,record = service.evidence_path(user(request),rid,firm_id=firm_id)
        return FileResponse(path,media_type=record['mime'],filename=record['filename'],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.get('/api/boutique/evidence/{rid}/preview')
    def preview(rid:str,request:Request,firm_id:str|None=None):
        path,_ = service.evidence_path(user(request),rid,True,firm_id)
        return FileResponse(path,media_type='image/png',headers={'Cache-Control':'no-store'})

    @app.get('/api/boutique/standards')
    def standards(request:Request,company_id:str|None=None,firm_id:str|None=None): return service.standards(user(request),company_id,firm_id)

    @app.get('/api/boutique/definitions')
    def definitions(request:Request,company_id:str|None=None,firm_id:str|None=None): return service.context(user(request),company_id=company_id,firm_id=firm_id)['definitions']

    @app.post('/api/boutique/definitions',status_code=201)
    def create_definition(body:DefinitionInput,request:Request,firm_id:str|None=None): return service.save_definition(user(request),body.model_dump(),firm_id=firm_id)

    @app.put('/api/boutique/definitions/{rid}')
    def update_definition(rid:str,body:DefinitionInput,request:Request,firm_id:str|None=None): return service.save_definition(user(request),body.model_dump(),rid,firm_id)

    @app.post('/api/boutique/definitions/{rid}/publish')
    def publish(rid:str,body:Publication,request:Request,firm_id:str|None=None): return service.publish_definition(user(request),rid,body.model_dump(),firm_id)

    @app.post('/api/boutique/definitions/{rid}/revise',status_code=201)
    def revise(rid:str,body:Publication,request:Request,firm_id:str|None=None): return service.revise_definition(user(request),rid,body.model_dump(),firm_id)
    return service
