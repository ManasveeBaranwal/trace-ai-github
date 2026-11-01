"""Company-scoped BRSR preparation records and immutable draft report snapshots.

The question catalogue is sourced and maintained separately. This module does
not certify regulatory compliance or submit exchange filings.
"""
import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hierarchy import allowed_bill, allowed_company, role, visible_record
from security import timestamp


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Profile(Input):
    name: str = Field(min_length=2, max_length=180)
    financial_year: str = Field(pattern=r'^\d{4}-(?:\d{2}|\d{4})$')
    previous_financial_year: str | None = None
    boundary: Literal['standalone', 'consolidated'] = 'standalone'
    reporting_start: date
    reporting_end: date
    listed: bool = True
    notes: str = Field(default='', max_length=6000)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def valid_period(self):
        start_year = int(self.financial_year[:4])
        end = self.financial_year.split('-')[1]
        expected = str(start_year+1) if len(end) == 4 else str(start_year+1)[-2:]
        if end != expected:
            raise ValueError('Financial year must identify consecutive years')
        previous = f'{start_year-1}-{str(start_year)[-2:]}'
        if self.previous_financial_year:
            if not re.fullmatch(r'\d{4}-(?:\d{2}|\d{4})',self.previous_financial_year):
                raise ValueError('Use a financial year such as 2025-26')
            pstart, pend = self.previous_financial_year.split('-')
            if int(pstart) != start_year-1 or pend != (str(start_year) if len(pend)==4 else str(start_year)[-2:]):
                raise ValueError('Previous financial year must immediately precede the reporting year')
        else:
            self.previous_financial_year = previous
        if self.reporting_start > self.reporting_end:
            raise ValueError('Reporting end must follow reporting start')
        if not (date(start_year, 4, 1) <= self.reporting_start <= self.reporting_end <= date(start_year+1, 3, 31)):
            raise ValueError('Reporting dates must fall within the selected Indian financial year')
        return self


class Evidence(Input):
    type: Literal['reference', 'bill'] = 'reference'
    reference: str = Field(min_length=1, max_length=1500)
    page: int | None = Field(default=None, ge=1, le=100000)


class Answer(Input):
    values: dict[str, Any] = Field(default_factory=dict, max_length=200)
    narrative: str = Field(default='', max_length=20000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)
    applicability: Literal['applicable', 'not_applicable', 'missing'] = 'applicable'
    applicability_reason: str = Field(default='', max_length=6000)
    status: Literal['draft', 'submitted'] = 'draft'
    expected_revision: int = Field(ge=0)


class Review(Input):
    confirmed: bool
    expected_revision: int = Field(ge=1)


class Assignment(Input):
    question_ids: list[str] = Field(default_factory=list, max_length=500)


class Snapshot(Input):
    confirmed: bool


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Enter a decimal number')
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError('Enter a decimal number')
    if not result.is_finite() or abs(result) > Decimal('1e30') or result.as_tuple().exponent < -18:
        raise ValueError('Enter a finite number within the supported precision and range')
    return result


def _number_string(value):
    return format(value, 'f')


def _blank(value):
    return value is None or value == '' or value == [] or value == {}


def _validate_value(value, field):
    if _blank(value):
        return None
    kind = field.get('type', 'text')
    if kind in {'number', 'integer'}:
        number = _decimal(value)
        if kind == 'integer' and number != number.to_integral_value():
            raise ValueError('Enter a whole number')
        if field.get('min') is not None and number < _decimal(field['min']):
            raise ValueError('Value is below the permitted minimum')
        if field.get('max') is not None and number > _decimal(field['max']):
            raise ValueError('Value exceeds the permitted maximum')
        return _number_string(number)
    if kind in {'boolean', 'bool'}:
        if not isinstance(value, bool):
            raise ValueError('Choose true or false')
        return value
    if kind == 'table':
        if not isinstance(value, list) or len(value) > 500:
            raise ValueError('Enter a table with no more than 500 rows')
        columns = {c['id']: c for c in field.get('columns', [])}
        rows = []
        for row in value:
            if not isinstance(row, dict) or len(row) > 50:
                raise ValueError('Each table row must contain named columns')
            if columns:
                if set(row) - set(columns):
                    raise ValueError('Table contains an unknown column')
                rows.append({key: _validate_value(row.get(key), column) for key, column in columns.items()})
            else:
                if any(not isinstance(k, str) or len(k) > 120 for k in row):
                    raise ValueError('Use short text column names')
                if any(not isinstance(v, (str, int, float, bool, type(None))) for v in row.values()):
                    raise ValueError('Table cells must be text, numbers, booleans or blank')
                rows.append({key: (str(cell) if cell is not None else None) for key, cell in row.items()})
        return rows
    if not isinstance(value, str) or len(value) > 20000:
        raise ValueError('Enter text within 20,000 characters')
    value = value.strip()
    if kind == 'date':
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError('Use an ISO date, YYYY-MM-DD')
    if kind == 'url' and (urlsplit(value).scheme not in {'https', 'http'} or not urlsplit(value).netloc):
        raise ValueError('Enter an HTTP or HTTPS source URL')
    if kind == 'select':
        options = [o.get('value') if isinstance(o, dict) else o for o in field.get('options', [])]
        if options and value not in options:
            raise ValueError('Choose one of the listed options')
    return value


def validate_values(values, question):
    fields = {f['id']: f for f in question.get('fields', [])}
    if set(values) - set(fields):
        raise HTTPException(422, 'This disclosure contains an unknown input field')
    try:
        return {key: _validate_value(values.get(key), field) for key, field in fields.items()}
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _missing_fields(answer, question):
    missing = []
    for field in question.get('fields', []):
        value = answer.get('values', {}).get(field['id'])
        if field.get('required', False) and _blank(value):
            missing.append(field.get('label', field['id']))
        if field.get('type') == 'table' and isinstance(value, list):
            for index, row in enumerate(value):
                for column in field.get('columns', []):
                    if column.get('required', False) and _blank(row.get(column['id'])):
                        missing.append(f"{field.get('label',field['id'])}, row {index+1}, {column.get('label',column['id'])}")
    return missing


def _calculations(question, values):
    """Execute only explicit, catalogue-defined arithmetic with missing-safe results.

    No conversion factors or denominators are inferred. Ratio outputs do not
    establish applicability of any regulatory metric.
    """
    try:
        from brsr_calculations import calculate_item
    except ImportError:
        calculate_item=None
    if calculate_item is not None:
        return calculate_item(question,values)
    results = []
    for spec in question.get('calculations', []):
        result = {'id': spec['id'], 'label': spec.get('label', spec['id']), 'unit': spec.get('unit', ''), 'source_url': spec.get('source_url', question.get('source_url', '')), 'value': None}
        op = spec.get('operator', spec.get('operation', ''))
        inputs = spec.get('inputs', spec.get('fields', []))
        numerator = spec.get('numerator')
        denominator = spec.get('denominator')
        try:
            with localcontext() as ctx:
                ctx.prec = 60
                if op in {'ratio', 'percent', 'percentage'}:
                    if _blank(values.get(numerator)) or _blank(values.get(denominator)):
                        result['reason'] = 'Missing numerator or denominator'
                    elif _decimal(values[denominator]) <= 0:
                        result['reason'] = 'Denominator must be greater than zero'
                    else:
                        scale = _decimal(spec.get('scale', 100 if op in {'percent', 'percentage'} else 1))
                        result['value'] = _number_string(_decimal(values[numerator]) / _decimal(values[denominator]) * scale)
                elif op in {'sum', 'difference', 'product'}:
                    if not inputs or any(_blank(values.get(key)) for key in inputs):
                        result['reason'] = 'Missing calculation inputs'
                    else:
                        nums = [_decimal(values[key]) for key in inputs]
                        total = sum(nums) if op == 'sum' else nums[0]
                        if op == 'difference':
                            for number in nums[1:]:
                                total -= number
                        elif op == 'product':
                            for number in nums[1:]:
                                total *= number
                        result['value'] = _number_string(total)
                else:
                    result['reason'] = 'Calculation method is not supported; enter and document the result for review'
        except (ValueError, InvalidOperation, TypeError):
            result['reason'] = 'Calculation inputs are not compatible numeric values'
        results.append(result)
    return results


def _csv_safe(value):
    text = str(value if value is not None else '')
    return "'"+text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def _without_actors(value):
    if isinstance(value, list):
        return [_without_actors(item) for item in value]
    if isinstance(value, dict):
        return {key: _without_actors(item) for key, item in value.items() if key not in {'created_by', 'updated_by', 'reviewed_by', 'actor_id', 'history'}}
    return value


def install_brsr(app, accounts, root, get_bill=None):
    with accounts.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='brsr_reports'").fetchone()
        backup = accounts.path.with_name('accounts.pre-brsr-v1.sqlite3')
        if not exists and not backup.exists():
            target = sqlite3.connect(backup)
            try:
                db.backup(target)
            finally:
                target.close()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS brsr_catalog_versions(version TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS brsr_reports(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),financial_year TEXT NOT NULL,boundary TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(company_id,financial_year,boundary));
        CREATE TABLE IF NOT EXISTS brsr_answers(report_id TEXT NOT NULL REFERENCES brsr_reports(id),question_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(report_id,question_id));
        CREATE TABLE IF NOT EXISTS brsr_answer_versions(report_id TEXT NOT NULL,question_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(report_id,question_id,revision),FOREIGN KEY(report_id,question_id) REFERENCES brsr_answers(report_id,question_id));
        CREATE TABLE IF NOT EXISTS brsr_assignments(report_id TEXT NOT NULL REFERENCES brsr_reports(id),user_id TEXT NOT NULL REFERENCES users(id),payload TEXT NOT NULL,PRIMARY KEY(report_id,user_id));
        CREATE TABLE IF NOT EXISTS brsr_snapshots(id TEXT PRIMARY KEY,report_id TEXT NOT NULL REFERENCES brsr_reports(id),company_id TEXT NOT NULL REFERENCES companies(id),payload TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS brsr_report_company ON brsr_reports(company_id);
        CREATE INDEX IF NOT EXISTS brsr_snapshot_report ON brsr_snapshots(report_id);
        ''')

    def catalog():
        try:
            payload = json.loads((root/'research/brsr-disclosures.json').read_text())
            if isinstance(payload, list):
                payload = {'version': 'unversioned', 'items': payload}
            items = payload['items']
            if not isinstance(items, list) or not items or len({q['id'] for q in items}) != len(items):
                raise ValueError('Invalid disclosure catalogue')
            return payload
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, 'The sourced BRSR question catalogue is unavailable')

    def store_catalog(db, definition):
        encoded=json.dumps(definition,sort_keys=True,separators=(',',':'),ensure_ascii=False)
        existing=db.execute('SELECT payload FROM brsr_catalog_versions WHERE version=?',(definition['version'],)).fetchone()
        if existing and existing[0]!=encoded:
            raise HTTPException(503,'The catalogue changed without a new version identifier. Update its version before creating reports.')
        db.execute('INSERT OR IGNORE INTO brsr_catalog_versions VALUES(?,?)',(definition['version'],encoded))

    def report_catalog(db, profile):
        stored=db.execute('SELECT payload FROM brsr_catalog_versions WHERE version=?',(profile['catalog_version'],)).fetchone()
        if not stored:
            raise HTTPException(503,'The pinned question catalogue for this report is unavailable')
        return json.loads(stored[0])

    def actor(request):
        user = request.state.user
        if not user:
            raise HTTPException(401, 'Sign in required')
        return user

    def review_role(user):
        if role(user) == 'data_entry':
            raise HTTPException(403, 'Review permission is required for this action')

    def assign_role(user):
        if role(user) not in {'master', 'consultant_manager', 'company_admin'}:
            raise HTTPException(403, 'Account-management permission is required')

    def company(db, user, company_id=None):
        if role(user) in {'company_admin', 'data_entry'}:
            if company_id and company_id != user.get('company_id'):
                raise HTTPException(404, 'Company not found')
            company_id = user.get('company_id')
        if not company_id:
            raise HTTPException(422, 'Choose a company workspace')
        if not allowed_company(accounts, user, company_id, db):
            raise HTTPException(404, 'Company not found')
        row = db.execute('SELECT * FROM companies WHERE id=? AND active=1', (company_id,)).fetchone()
        if not row:
            raise HTTPException(409, 'This company is inactive')
        return dict(row)

    def report(db, user, report_id):
        row = db.execute('SELECT * FROM brsr_reports WHERE id=?', (report_id,)).fetchone()
        if not row or not allowed_company(accounts, user, row['company_id'], db):
            raise HTTPException(404, 'BRSR report not found')
        company(db, user, row['company_id'])
        return json.loads(row['payload'])

    def assigned(db, user, report_id):
        if role(user) != 'data_entry':
            return None
        row = db.execute('SELECT payload FROM brsr_assignments WHERE report_id=? AND user_id=?', (report_id, user['id'])).fetchone()
        return set(json.loads(row['payload'])['question_ids']) if row else set()

    def select_question(db, user, report_id, question_id):
        profile=report(db, user, report_id)
        questions = {q['id']: q for q in report_catalog(db,profile)['items']}
        ids = assigned(db, user, report_id)
        if question_id not in questions or (ids is not None and question_id not in ids):
            raise HTTPException(404, 'Disclosure not found')
        return questions[question_id]

    def evidence_records(items, user, company_id):
        result = []
        for item in items:
            ref = item.model_dump()
            if item.type == 'bill':
                if get_bill is None:
                    raise HTTPException(422, 'Bill evidence integration is unavailable; use a documented reference')
                try:
                    bill = get_bill(item.reference)
                except HTTPException:
                    raise HTTPException(422, 'Bill evidence was not found in this company')
                if bill.get('company_id') != company_id or not allowed_bill(accounts, user, bill):
                    raise HTTPException(422, 'Bill evidence was not found in this company')
                if item.page and item.page > (bill.get('page_count') or 1):
                    raise HTTPException(422, 'Evidence page exceeds the source document')
                ref.update(sha256=bill.get('sha256'), filename=bill.get('filename'))
            result.append(ref)
        return result

    def answer_payload(db, report_id, question_id):
        row = db.execute('SELECT payload FROM brsr_answers WHERE report_id=? AND question_id=?', (report_id, question_id)).fetchone()
        return json.loads(row['payload']) if row else None

    def progress(items, answers):
        by_id = {a['question_id']: a for a in answers}
        required = [q for q in items if q.get('required', False)]
        reviewed_required = sum(by_id.get(q['id'], {}).get('status') == 'reviewed' and by_id[q['id']]['applicability'] != 'missing' for q in required)
        return {'total': len(items), 'required': len(required), 'reviewed': sum(a['status'] == 'reviewed' for a in answers),
                'required_reviewed': reviewed_required, 'missing': sum(q['id'] not in by_id or by_id[q['id']]['applicability'] == 'missing' or (by_id[q['id']]['applicability']=='applicable' and (bool(_missing_fields(by_id[q['id']],q)) or (not any(not _blank(v) for v in by_id[q['id']]['values'].values()) and not by_id[q['id']]['narrative']))) for q in items),
                'submitted': sum(a['status'] == 'submitted' for a in answers), 'drafts': sum(a['status'] == 'draft' for a in answers),
                'completion_percent': round(100*reviewed_required/len(required), 1) if required else 0,
                'complete': bool(required) and reviewed_required == len(required)}

    def snapshot_summaries(db, report_id):
        result = []
        for row in db.execute('SELECT payload FROM brsr_snapshots WHERE report_id=? ORDER BY created_at DESC', (report_id,)):
            snapshot = json.loads(row['payload'])
            result.append({key: snapshot[key] for key in ('id', 'report_id', 'created_at', 'complete', 'catalog_version', 'sha256')})
        return result

    @app.get('/api/brsr/catalog')
    def get_catalog(request: Request):
        actor(request)
        return catalog()

    @app.get('/api/brsr/reports')
    def list_reports(request: Request, company_id: str | None = None):
        user = actor(request)
        with accounts.db() as db:
            selected = company(db, user, company_id)
            rows = db.execute('SELECT payload FROM brsr_reports WHERE company_id=? ORDER BY financial_year DESC,created_at DESC', (selected['id'],)).fetchall()
        return visible_record(accounts, user, [json.loads(r[0]) for r in rows])

    @app.post('/api/brsr/reports', status_code=201)
    def create_report(body: Profile, request: Request, company_id: str | None = None):
        user = actor(request)
        review_role(user)
        definition = catalog()
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            selected = company(db, user, company_id)
            store_catalog(db,definition)
            rid = str(uuid.uuid4())
            payload = body.model_dump(mode='json', exclude={'expected_revision'}) | {'id': rid, 'company_id': selected['id'], 'catalog_version': definition['version'], 'created_at': timestamp(), 'updated_at': timestamp(), 'created_by': user['id'], 'revision': 1}
            try:
                db.execute('INSERT INTO brsr_reports VALUES(?,?,?,?,?,?)', (rid, selected['id'], body.financial_year, body.boundary, json.dumps(payload), payload['created_at']))
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'A report already exists for this company, financial year and boundary')
            accounts.audit(user['id'], 'brsr.report_created', rid, {'company_id': selected['id'], 'financial_year': body.financial_year}, db)
        return visible_record(accounts, user, payload)

    @app.put('/api/brsr/reports/{report_id}')
    def update_report(report_id: str, body: Profile, request: Request):
        user = actor(request)
        review_role(user)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = report(db, user, report_id)
            if body.expected_revision != old['revision']:
                raise HTTPException(409, 'Report profile changed. Refresh before saving.')
            if (body.financial_year != old['financial_year'] or body.boundary != old['boundary']) and db.execute('SELECT 1 FROM brsr_answers WHERE report_id=?', (report_id,)).fetchone():
                raise HTTPException(409, 'Financial year and reporting boundary cannot change after answers are recorded')
            updated = old | body.model_dump(mode='json', exclude={'expected_revision'}) | {'revision': old['revision']+1, 'updated_at': timestamp(), 'updated_by': user['id']}
            try:
                db.execute('UPDATE brsr_reports SET financial_year=?,boundary=?,payload=? WHERE id=?', (body.financial_year, body.boundary, json.dumps(updated), report_id))
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'This company already has a report for that year and boundary')
            accounts.audit(user['id'], 'brsr.report_updated', report_id, {'revision': updated['revision']}, db)
        return visible_record(accounts, user, updated)

    @app.get('/api/brsr/reports/{report_id}')
    def get_report(report_id: str, request: Request):
        user = actor(request)
        with accounts.db() as db:
            profile = report(db, user, report_id)
            definition=report_catalog(db,profile)
            ids = assigned(db, user, report_id)
            items = [q for q in definition['items'] if ids is None or q['id'] in ids]
            answers = [json.loads(r[0]) for r in db.execute('SELECT payload FROM brsr_answers WHERE report_id=?', (report_id,))]
            answers = [a for a in answers if ids is None or a['question_id'] in ids]
            assignments = [json.loads(r[0]) for r in db.execute('SELECT payload FROM brsr_assignments WHERE report_id=?', (report_id,))]
            if ids is not None:
                assignments = [a for a in assignments if a['user_id'] == user['id']]
            can_assign = role(user) in {'master', 'consultant_manager', 'company_admin'}
            contributors = []
            if can_assign:
                contributors = [{'id': r['id'], 'name': r['name'], 'email': r['email'], 'active': bool(r['active'])} for r in db.execute("SELECT * FROM users WHERE company_id=? AND access_role='data_entry' ORDER BY name", (profile['company_id'],))]
            snapshots = snapshot_summaries(db, report_id) if ids is None else []
            selected = company(db, user, profile['company_id'])
        by_id = {a['question_id']: a for a in answers}
        for question in items:
            if question['id'] in by_id:
                by_id[question['id']]['calculations'] = _calculations(question, by_id[question['id']]['values'])
        return visible_record(accounts, user, {'report': profile, 'company': {key: selected[key] for key in ('id', 'name', 'industry', 'country')}, 'answers': answers,
                    'assignments': assignments, 'contributors': contributors, 'snapshots': snapshots,
                    'catalog': {**definition, 'items': items}, 'progress': progress(items, answers),
                    'permissions': {'can_manage': ids is None, 'can_review': ids is None, 'can_export': ids is None, 'can_assign': can_assign,
                                    'editable_question_ids': [q['id'] for q in items if ids is None or q['id'] not in by_id or (by_id[q['id']]['created_by']==user['id'] and by_id[q['id']]['status']!='reviewed')]},
                    'scope_note': definition.get('coverage_note', 'Question-level BRSR preparation catalogue; not a certification or exchange filing.')})

    @app.put('/api/brsr/reports/{report_id}/answers/{question_id}')
    def save_answer(report_id: str, question_id: str, body: Answer, request: Request):
        user = actor(request)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            question = select_question(db, user, report_id, question_id)
            profile = report(db, user, report_id)
            old = answer_payload(db, report_id, question_id)
            revision = old['revision'] if old else 0
            if body.expected_revision != revision:
                raise HTTPException(409, 'This answer changed. Refresh before saving your version.')
            if role(user) == 'data_entry' and old and (old['created_by'] != user['id'] or old['status'] == 'reviewed'):
                raise HTTPException(403, 'You can edit only your own unreviewed assigned answers')
            if body.applicability == 'not_applicable' and not body.applicability_reason:
                raise HTTPException(422, 'Explain why this disclosure does not apply')
            if len(json.dumps(body.values,ensure_ascii=False))>1000000:
                raise HTTPException(413,'Disclosure inputs exceed the 1 MB record limit')
            values = validate_values(body.values, question)
            evidence = evidence_records(body.evidence, user, profile['company_id'])
            payload = {'question_id': question_id, 'report_id': report_id, 'values': values, 'narrative': body.narrative,
                       'applicability': body.applicability, 'applicability_reason': body.applicability_reason, 'evidence': evidence,
                       'status': body.status, 'revision': revision+1, 'created_at': old['created_at'] if old else timestamp(),
                       'created_by': old['created_by'] if old else user['id'], 'updated_at': timestamp(), 'updated_by': user['id'],
                       'calculations': _calculations(question, values)}
            if old:
                db.execute('INSERT INTO brsr_answer_versions VALUES(?,?,?,?,?)', (report_id, question_id, revision, json.dumps(old), timestamp()))
            db.execute('INSERT INTO brsr_answers VALUES(?,?,?) ON CONFLICT(report_id,question_id) DO UPDATE SET payload=excluded.payload', (report_id, question_id, json.dumps(payload)))
            accounts.audit(user['id'], 'brsr.answer_saved', report_id+':'+question_id, {'revision': revision+1, 'status': body.status}, db)
        return visible_record(accounts, user, payload)

    @app.post('/api/brsr/reports/{report_id}/answers/{question_id}/review')
    def review_answer(report_id: str, question_id: str, body: Review, request: Request):
        user = actor(request)
        review_role(user)
        if not body.confirmed:
            raise HTTPException(422, 'Confirm the disclosure, evidence and calculation inputs')
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            question = select_question(db, user, report_id, question_id)
            profile = report(db, user, report_id)
            old = answer_payload(db, report_id, question_id)
            if not old:
                raise HTTPException(404, 'Answer not found')
            if body.expected_revision != old['revision']:
                raise HTTPException(409, 'This answer changed. Refresh before reviewing.')
            if old['applicability'] == 'missing':
                raise HTTPException(422, 'Missing information cannot be approved as a completed disclosure')
            if old['applicability'] == 'not_applicable':
                if not old.get('applicability_reason'):
                    raise HTTPException(422, 'Document why this disclosure does not apply')
            else:
                missing = _missing_fields(old, question)
                if missing:
                    raise HTTPException(422, 'Complete required fields before review: '+', '.join(missing[:8]))
                if not any(not _blank(value) for value in old['values'].values()) and not old['narrative']:
                    raise HTTPException(422, 'Provide the disclosure response before review')
                if not old.get('evidence'):
                    raise HTTPException(422, 'Add at least one source reference or company bill before review')
                # Revalidate bill ownership; stored references never bypass tenant checks.
                evidence_records([Evidence(**{key: e.get(key) for key in ('type', 'reference', 'page')}) for e in old['evidence']], user, profile['company_id'])
            updated = old | {'status': 'reviewed', 'revision': old['revision']+1, 'reviewed_at': timestamp(), 'reviewed_by': user['id'], 'updated_at': timestamp()}
            db.execute('INSERT INTO brsr_answer_versions VALUES(?,?,?,?,?)', (report_id, question_id, old['revision'], json.dumps(old), timestamp()))
            db.execute('UPDATE brsr_answers SET payload=? WHERE report_id=? AND question_id=?', (json.dumps(updated), report_id, question_id))
            accounts.audit(user['id'], 'brsr.answer_reviewed', report_id+':'+question_id, {'revision': updated['revision']}, db)
        return visible_record(accounts, user, updated)

    @app.put('/api/brsr/reports/{report_id}/assignments/{user_id}')
    def assign_questions(report_id: str, user_id: str, body: Assignment, request: Request):
        user = actor(request)
        assign_role(user)
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            profile = report(db, user, report_id)
            questions={q['id'] for q in report_catalog(db,profile)['items']}
            if set(body.question_ids)-questions:
                raise HTTPException(422,'Choose disclosure IDs from this report’s pinned catalogue')
            target = db.execute("SELECT * FROM users WHERE id=? AND company_id=? AND access_role='data_entry' AND active=1", (user_id, profile['company_id'])).fetchone()
            if not target:
                raise HTTPException(404, 'Data-entry account not found in this company')
            payload = {'user_id': user_id, 'report_id': report_id, 'question_ids': sorted(set(body.question_ids)), 'updated_at': timestamp(), 'updated_by': user['id']}
            db.execute('INSERT OR REPLACE INTO brsr_assignments VALUES(?,?,?)', (report_id, user_id, json.dumps(payload)))
            accounts.audit(user['id'], 'brsr.questions_assigned', report_id, {'user_id': user_id, 'question_ids': payload['question_ids']}, db)
        return visible_record(accounts, user, payload)

    @app.get('/api/brsr/reports/{report_id}/snapshots')
    def snapshots(report_id: str, request: Request):
        user = actor(request)
        review_role(user)
        with accounts.db() as db:
            report(db, user, report_id)
            return snapshot_summaries(db, report_id)

    @app.post('/api/brsr/reports/{report_id}/snapshots', status_code=201)
    def create_snapshot(report_id: str, body: Snapshot, request: Request):
        user = actor(request)
        review_role(user)
        if not body.confirmed:
            raise HTTPException(422, 'Confirm that the export is a BRSR preparation draft')
        with accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            profile = report(db, user, report_id)
            definition=report_catalog(db,profile)
            selected = company(db, user, profile['company_id'])
            answers = [json.loads(r[0]) for r in db.execute('SELECT payload FROM brsr_answers WHERE report_id=?', (report_id,))]
            by_id = {a['question_id']: a for a in answers}
            disclosures = []
            for question in definition['items']:
                answer = by_id.get(question['id'])
                if answer:
                    answer = _without_actors(answer)
                    answer['calculations'] = _calculations(question, answer['values'])
                else:
                    answer = {'question_id': question['id'], 'values': {}, 'narrative': '', 'evidence': [], 'applicability': 'missing', 'status': 'missing', 'revision': 0, 'calculations': []}
                disclosures.append({'definition': question, 'answer': answer})
            coverage = progress(definition['items'], answers)
            payload = {'schema_version': 1, 'catalog_version': definition['version'], 'coverage_note': definition.get('coverage_note', ''),
                       'company': {key: selected[key] for key in ('id', 'name', 'industry', 'country')}, 'report': _without_actors(profile),
                       'disclosures': disclosures, 'progress': coverage, 'sources': definition.get('sources', []),
                       'document_status': 'BRSR preparation draft — reviewer-checked snapshot; not an exchange filing'}
            encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            snapshot = {'id': str(uuid.uuid4()), 'report_id': report_id, 'company_id': profile['company_id'], 'created_at': timestamp(),
                        'created_by': user['id'], 'complete': coverage['complete'], 'catalog_version': definition['version'],
                        'sha256': hashlib.sha256(encoded.encode()).hexdigest(), 'payload': payload}
            db.execute('INSERT INTO brsr_snapshots VALUES(?,?,?,?,?)', (snapshot['id'], report_id, profile['company_id'], json.dumps(snapshot), snapshot['created_at']))
            accounts.audit(user['id'], 'brsr.snapshot_created', snapshot['id'], {'report_id': report_id, 'complete': coverage['complete'], 'sha256': snapshot['sha256']}, db)
        return visible_record(accounts, user, {key: value for key, value in snapshot.items() if key != 'payload'})

    def snapshot_record(db, user, snapshot_id):
        row = db.execute('SELECT * FROM brsr_snapshots WHERE id=?', (snapshot_id,)).fetchone()
        if not row or not allowed_company(accounts, user, row['company_id'], db):
            raise HTTPException(404, 'BRSR snapshot not found')
        company(db, user, row['company_id'])
        return json.loads(row['payload'])

    @app.get('/api/brsr/snapshots/{snapshot_id}/export')
    def export_snapshot(snapshot_id: str, request: Request, format: str = 'html'):
        user = actor(request)
        review_role(user)
        with accounts.db() as db:
            snapshot = snapshot_record(db, user, snapshot_id)
        if format == 'json':
            output = json.dumps(visible_record(accounts, user, snapshot), indent=2, ensure_ascii=False)
            mime, extension = 'application/json', 'json'
        elif format == 'csv':
            output_file = io.StringIO()
            writer = csv.writer(output_file)
            writer.writerow(['Disclosure ID', 'Section', 'Principle', 'Disclosure', 'Required in catalogue', 'BRSR Core', 'Applicability', 'Review state', 'Field', 'Value', 'Unit', 'Evidence', 'Narrative', 'Not-applicable reason'])
            for disclosure in snapshot['payload']['disclosures']:
                q, a = disclosure['definition'], disclosure['answer']
                fields = q.get('fields', []) or [{'id': 'response', 'label': 'Response'}]
                for field in fields:
                    value = a['values'].get(field['id'])
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value, ensure_ascii=False)
                    writer.writerow([_csv_safe(v) for v in [q['id'], q.get('section', ''), q.get('principle', ''), q['title'], q.get('required', False), q.get('core', False), a['applicability'], a['status'], field.get('label', field['id']), value, field.get('unit', ''), '; '.join(e['reference'] for e in a['evidence']), a['narrative'], a.get('applicability_reason', '')]])
                for calculation in a.get('calculations', []):
                    writer.writerow([_csv_safe(v) for v in [q['id'], q.get('section', ''), q.get('principle', ''), q['title'], q.get('required', False), q.get('core', False), a['applicability'], a['status'], 'Calculated: '+calculation.get('label', calculation.get('id', '')), calculation.get('value'), calculation.get('unit', ''), '; '.join(e['reference'] for e in a['evidence']), calculation.get('formula', ''), calculation.get('note', '')]])
            output, mime, extension = output_file.getvalue(), 'text/csv', 'csv'
        elif format == 'html':
            output, mime, extension = _render_html(snapshot), 'text/html', 'html'
        else:
            raise HTTPException(422, 'Choose html, json or csv')
        accounts.audit(user['id'], 'brsr.snapshot_exported', snapshot_id, {'format': format})
        return Response(output, media_type=mime, headers={'Content-Disposition': f'{"inline" if format == "html" else "attachment"}; filename="trace-brsr-{snapshot_id}.{extension}"', 'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"})


def _render_html(snapshot):
    payload = snapshot['payload']
    e = lambda value: escape(str(value if value is not None else ''))
    profile, company = payload['report'], payload['company']
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BRSR preparation draft</title><style>',
             'body{font:14px/1.6 Arial,sans-serif;color:#20352d;max-width:1050px;margin:40px auto;padding:0 24px}h1{font-size:30px}h2{font-size:20px}h3{font-size:16px}.warning{background:#fff2d3;border:1px solid #d7b55f;padding:18px}.disclosure{border-top:1px solid #b9cabb;padding:20px 0;break-inside:avoid}.meta{color:#637364;font-size:12px}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{text-align:left;vertical-align:top;border:1px solid #d9e2d5;padding:8px;overflow-wrap:anywhere}th{background:#eff4e9}.missing{color:#964b20}.narrative{white-space:pre-wrap}a{color:#286245}@page{size:A4;margin:16mm}@media print{body{margin:0;padding:0;max-width:none}h1,h2,h3{break-after:avoid}}',
             '</style></head><body><p class="meta">TRACE AI · SUSTIVIOLABS</p><h1>BRSR preparation draft</h1>',
             '<p class="meta">Use your browser’s Print command to save this preparation draft as a PDF.</p><h2>'+e(company['name'])+'</h2><p>'+e(profile['financial_year'])+' · '+e(profile['boundary'])+' reporting · '+e(profile['reporting_start'])+' to '+e(profile['reporting_end'])+'</p>',
             '<div class="warning"><strong>Preparation draft — requires final company review.</strong><p>Missing and unreviewed disclosures remain explicitly identified. This document is not an exchange filing, independent assessment or assurance opinion.</p><p>'+e(payload.get('coverage_note', ''))+'</p></div>',
             '<p class="meta">Snapshot '+e(snapshot['id'])+' · Created '+e(snapshot['created_at'])+'<br>Business-content SHA-256: '+e(snapshot['sha256'])+'</p>',
             '<p>'+e(payload['progress']['required_reviewed'])+' / '+e(payload['progress']['required'])+' required catalogue entries reviewed. '+e(payload['progress']['missing'])+' entries have missing information.</p>']
    last_section = None
    for item in payload['disclosures']:
        q, a = item['definition'], item['answer']
        section = str(q.get('section', ''))
        if section != last_section:
            parts.append('<h2>Section '+e(section)+'</h2>')
            last_section = section
        parts.append('<article class="disclosure"><h3>'+e(q['id'])+' · '+e(q['title'])+'</h3><p class="meta">'+('Required catalogue entry' if q.get('required') else 'Optional / leadership entry')+(' · BRSR Core' if q.get('core') else '')+' · State: '+e(a['status'])+' · Applicability: '+e(a['applicability'])+'</p>')
        if a['applicability'] == 'missing':
            parts.append('<p class="missing"><strong>DATA NOT PROVIDED</strong></p>')
        elif a['status'] != 'reviewed':
            parts.append('<p class="missing"><strong>UNREVIEWED — figures and wording require checking</strong></p>')
        if a.get('applicability_reason'):
            parts.append('<p>Applicability explanation: '+e(a['applicability_reason'])+'</p>')
        for field in q.get('fields', []):
            value = a['values'].get(field['id'])
            parts.append('<h4>'+e(field.get('label', field['id']))+(' · '+e(field['unit']) if field.get('unit') else '')+'</h4>')
            if _blank(value):
                parts.append('<p class="missing">Not provided</p>')
            elif isinstance(value, list):
                columns = field.get('columns', [])
                keys = [c['id'] for c in columns] if columns else sorted({key for row in value for key in row})
                labels = {c['id']: c.get('label', c['id']) for c in columns}
                parts.append('<table><thead><tr>'+''.join('<th>'+e(labels.get(key, key))+'</th>' for key in keys)+'</tr></thead><tbody>')
                for row in value:
                    parts.append('<tr>'+''.join('<td>'+('Not provided' if _blank(row.get(key)) else e(row[key]))+'</td>' for key in keys)+'</tr>')
                parts.append('</tbody></table>')
            else:
                parts.append('<p class="narrative">'+e(value)+'</p>')
        if a.get('narrative'):
            parts.append('<p class="narrative">'+e(a['narrative'])+'</p>')
        for calculation in a.get('calculations', []):
            parts.append('<p><strong>'+e(calculation['label'])+': </strong>'+e(calculation['value'])+' '+e(calculation.get('unit', '')) if calculation['value'] is not None else '<p class="missing">'+e(calculation['label'])+': '+e(calculation.get('reason', calculation.get('status','Not calculated'))))
            parts.append('</p>')
            if calculation.get('formula'):
                parts.append('<p class="meta">Calculation working: '+e(calculation['formula'])+'</p>')
        if a.get('evidence'):
            parts.append('<p><strong>Source evidence references</strong></p><ul>')
            for evidence in a['evidence']:
                parts.append('<li>'+e(evidence.get('filename') or evidence['reference'])+(' · page '+e(evidence['page']) if evidence.get('page') else '')+(' · SHA-256 '+e(evidence['sha256']) if evidence.get('sha256') else '')+'</li>')
            parts.append('</ul>')
        source = q.get('source_url', '')
        if urlsplit(source).scheme in {'https', 'http'} and urlsplit(source).netloc:
            parts.append('<p class="meta">Requirement source: <a href="'+e(source)+'">'+e(source)+'</a></p>')
        parts.append('</article>')
    parts.append('</body></html>')
    return ''.join(parts)
