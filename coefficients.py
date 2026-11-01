"""Versioned, tenant-scoped calculation coefficients and immutable calculation receipts.

Values are Decimal strings. Publication never overwrites an earlier version. A
coefficient is a governed record; retrieval or an LLM cannot change its value.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from hierarchy import allowed_company, role, visible_record
from security import timestamp
from coefficient_rag import AskInput, CoefficientKnowledge


FIELDS = (
    'name', 'category', 'activity', 'value', 'input_unit', 'source_input_unit', 'output_unit', 'kind',
    'gas_basis', 'gas_coverage', 'geography', 'reporting_year', 'publication_year',
    'valid_from', 'valid_to', 'accounting_boundary', 'methodology', 'notes',
    'source_title', 'source_url', 'source_locator', 'source_excerpt', 'source_version',
    'scope', 'scope_id',
)
SOURCE_FIELDS = ('source_title', 'source_url', 'source_locator', 'source_excerpt', 'source_version')
MAX_IMPORT_ROWS = 1000


def decimal_string(value: Any) -> str:
    if len(str(value)) > 160:
        raise ValueError('Use a decimal number with at most 160 characters')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Use a finite decimal number')
    if not number.is_finite() or number < 0 or number > Decimal('1e24'):
        raise ValueError('Use a nonnegative finite number no greater than 1e24')
    if abs(number.as_tuple().exponent) > 100:
        raise ValueError('Use a decimal with at most one hundred fractional places')
    # No binary-float conversion or scientific notation in stored calculations.
    return format(number, 'f')


class CoefficientInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=240)
    category: str = Field(min_length=1, max_length=100)
    activity: str = Field(default='', max_length=500)
    value: str
    input_unit: str = Field(min_length=1, max_length=120)
    source_input_unit: str = Field(default='', max_length=120)
    output_unit: str = Field(min_length=1, max_length=120)
    kind: Literal['emission_factor', 'conversion', 'parameter'] = 'emission_factor'
    gas_basis: str = Field(default='', max_length=100)
    gas_coverage: str = Field(default='', max_length=300)
    geography: str = Field(default='', max_length=200)
    reporting_year: str = Field(default='', max_length=80)
    publication_year: int | None = Field(default=None, ge=1900, le=2200)
    valid_from: str | None = None
    valid_to: str | None = None
    accounting_boundary: str = Field(default='', max_length=2000)
    methodology: str = Field(default='', max_length=6000)
    notes: str = Field(default='', max_length=6000)
    source_title: str = Field(default='', max_length=500)
    source_url: str = Field(default='', max_length=2000)
    source_locator: str = Field(default='', max_length=1000)
    source_excerpt: str = Field(default='', max_length=6000)
    source_version: str = Field(default='', max_length=300)
    scope: Literal['platform', 'firm', 'company'] = 'platform'
    scope_id: str | None = Field(default=None, max_length=150)

    @field_validator('value', mode='before')
    @classmethod
    def validate_decimal(cls, value):
        return decimal_string(value)

    @field_validator('valid_from', 'valid_to', mode='before')
    @classmethod
    def validate_date(cls, value):
        if value in ('', None):
            return None
        return date.fromisoformat(str(value)).isoformat()

    @field_validator('publication_year', mode='before')
    @classmethod
    def empty_year(cls, value):
        return None if value in ('', None) else value

    @field_validator('source_url')
    @classmethod
    def validate_url(cls, value):
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ('https', 'http') or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError('Source URL must be a public http(s) citation without credentials')
        return value


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)


class CalculateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version_id: str
    quantity: str
    unit: str = Field(min_length=1, max_length=120)
    company_id: str | None = None

    @field_validator('quantity', mode='before')
    @classmethod
    def decimal_quantity(cls, value):
        return decimal_string(value)


class ImportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    csv: str = Field(max_length=3_000_000)
    commit: bool = False
    scope: Literal['platform', 'firm', 'company'] = 'platform'
    scope_id: str | None = None


def actor(request: Request) -> dict:
    user = getattr(request.state, 'user', None)
    if not user or not user.get('active', True):
        raise HTTPException(401, 'Sign in to use the coefficient library')
    return user


class CoefficientService:
    def __init__(self, accounts, root: Path):
        self.accounts = accounts
        self.root = Path(root)
        self.fts_available = False
        self.seed_warning = None
        self._migrate()
        self._seed()

    def _migrate(self):
        with self.accounts.db() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='coefficient_versions'").fetchone()
            backup = self.accounts.path.with_name('accounts.pre-coefficients-v1.sqlite3')
            if not exists and not backup.exists():
                target = sqlite3.connect(backup)
                try:
                    db.backup(target)
                finally:
                    target.close()
            db.executescript('''
                CREATE TABLE IF NOT EXISTS coefficient_families(
                    id TEXT PRIMARY KEY, external_id TEXT UNIQUE, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coefficient_sources(
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coefficient_versions(
                    id TEXT PRIMARY KEY, family_id TEXT NOT NULL REFERENCES coefficient_families(id),
                    version INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ('draft','published','retired')),
                    scope TEXT NOT NULL CHECK(scope IN ('platform','firm','company')), scope_id TEXT,
                    payload TEXT NOT NULL, source_id TEXT REFERENCES coefficient_sources(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT,
                    published_at TEXT, published_by TEXT, retired_at TEXT, retired_by TEXT,
                    provenance TEXT NOT NULL DEFAULT '{}', UNIQUE(family_id,version));
                CREATE INDEX IF NOT EXISTS coefficient_scope ON coefficient_versions(scope,scope_id,status);
                CREATE TABLE IF NOT EXISTS coefficient_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL REFERENCES coefficient_versions(id),
                    at TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coefficient_calculations(
                    id TEXT PRIMARY KEY, at TEXT NOT NULL, actor_id TEXT, company_id TEXT,
                    version_id TEXT NOT NULL REFERENCES coefficient_versions(id), receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS coefficient_seed_runs(
                    digest TEXT PRIMARY KEY, seed_version TEXT NOT NULL, installed_at TEXT NOT NULL, count INTEGER NOT NULL);
            ''')
            try:
                db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS coefficient_search USING fts5(version_id UNINDEXED, document)')
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False
            if self.fts_available:
                present = {r[0] for r in db.execute('SELECT version_id FROM coefficient_search')}
                for row in db.execute('SELECT id,payload FROM coefficient_versions').fetchall():
                    if row['id'] not in present:
                        self._index(db, row['id'], json.loads(row['payload']))

    def _index(self, db, version_id, payload):
        if not self.fts_available:
            return
        document = ' '.join(str(payload.get(field) or '') for field in FIELDS if field not in {'scope_id', 'value'})
        db.execute('DELETE FROM coefficient_search WHERE version_id=?', (version_id,))
        db.execute('INSERT INTO coefficient_search(version_id,document) VALUES(?,?)', (version_id, document))

    @staticmethod
    def _validated(payload):
        try:
            data = CoefficientInput.model_validate(payload).model_dump()
        except Exception as exc:
            # Pydantic's exception contains only the submitted coefficient fields.
            raise HTTPException(422, str(exc))
        if data['scope'] == 'platform':
            if data['scope_id']:
                raise HTTPException(422, 'Platform definitions cannot have a scope_id')
            data['scope_id'] = None
        elif not data['scope_id']:
            raise HTTPException(422, 'Firm and company definitions require scope_id')
        if data['valid_from'] and data['valid_to'] and data['valid_from'] > data['valid_to']:
            raise HTTPException(422, 'valid_to must be on or after valid_from')
        return data

    def _company(self, user, cid, db):
        if not allowed_company(self.accounts, user, cid, db):
            raise HTTPException(404, 'Company not found')
        return db.execute('SELECT * FROM companies WHERE id=?', (cid,)).fetchone()

    def _can_manage(self, user, payload, db):
        payload = dict(payload)
        scope, sid = payload['scope'], payload.get('scope_id')
        if role(user) == 'master':
            if scope == 'company':
                return bool(db.execute('SELECT 1 FROM companies WHERE id=?', (sid,)).fetchone())
            if scope == 'firm':
                return bool(db.execute('SELECT 1 FROM firms WHERE id=?', (sid,)).fetchone())
            return scope == 'platform'
        if role(user) == 'consultant_manager':
            if scope == 'firm':
                return bool(sid and sid == user.get('firm_id') and db.execute('SELECT 1 FROM firms WHERE id=? AND active=1', (sid,)).fetchone())
            return scope == 'company' and allowed_company(self.accounts, user, sid, db)
        return role(user) == 'company_admin' and scope == 'company' and allowed_company(self.accounts, user, sid, db)

    def _can_read(self, user, row, db, company_id=None):
        scope, sid = row['scope'], row['scope_id']
        if company_id:
            company = self._company(user, company_id, db)
            applicable = scope == 'platform' or (scope == 'firm' and sid == company['firm_id']) or (scope == 'company' and sid == company_id)
        elif role(user) == 'master':
            applicable = True
        elif scope == 'platform':
            applicable = True
        elif scope == 'company':
            applicable = allowed_company(self.accounts, user, sid, db)
        elif role(user) in {'consultant_manager', 'consultant'}:
            applicable = bool(sid and sid == user.get('firm_id'))
        else:
            company = db.execute('SELECT firm_id FROM companies WHERE id=? AND active=1', (user.get('company_id'),)).fetchone()
            applicable = bool(company and sid and sid == company['firm_id'])
        if not applicable:
            return False
        return row['status'] != 'draft' or self._can_manage(user, row, db)

    def permissions(self, user):
        r = role(user)
        return {'create': r in {'master', 'consultant_manager', 'company_admin'},
                'platform_publish': r == 'master',
                'firm_publish': r in {'master', 'consultant_manager'},
                'company_publish': r in {'master', 'consultant_manager', 'company_admin'}}

    def _public(self, user, row, db):
        data = json.loads(row['payload'])
        manageable = self._can_manage(user, row, db)
        data.update({
            'id': row['id'], 'version_id': row['id'], 'family_id': row['family_id'],
            'version': row['version'], 'revision': row['revision'], 'status': row['status'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'published_at': row['published_at'], 'retired_at': row['retired_at'],
            'provenance': json.loads(row['provenance']),
            'permissions': {'can_edit': manageable and row['status'] == 'draft',
                            'can_revise': manageable and row['status'] in {'published', 'retired'},
                            'can_publish': manageable and row['status'] == 'draft',
                            'can_retire': manageable and row['status'] == 'published'},
        })
        if role(user) == 'master':
            data.update({key: row[key] for key in ('created_by', 'published_by', 'retired_by')})
        # Definitions are shared down the hierarchy; account identities are not.
        return data

    def _event(self, db, user, vid, action, details=None):
        at = timestamp()
        db.execute('INSERT INTO coefficient_history(version_id,at,actor_id,action,details) VALUES(?,?,?,?,?)',
                   (vid, at, user.get('id'), action, json.dumps(details or {})))
        self.accounts.audit(user.get('id'), 'coefficient.' + action, vid, details, db=db)

    def _source(self, db, payload):
        source = {key: payload[key] for key in SOURCE_FIELDS}
        encoded = json.dumps(source, sort_keys=True, ensure_ascii=False)
        sid = hashlib.sha256(encoded.encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO coefficient_sources(id,payload,created_at) VALUES(?,?,?)', (sid, encoded, timestamp()))
        return sid

    def _insert(self, db, user, data, family_id=None, external_id=None, provenance=None, status='draft'):
        now, vid = timestamp(), str(uuid.uuid4())
        if family_id is None:
            family_id = str(uuid.uuid4())
            db.execute('INSERT INTO coefficient_families(id,external_id,created_at) VALUES(?,?,?)', (family_id, external_id, now))
        version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM coefficient_versions WHERE family_id=?', (family_id,)).fetchone()[0]
        source_id = self._source(db, data)
        db.execute('''INSERT INTO coefficient_versions(id,family_id,version,revision,status,scope,scope_id,payload,source_id,
            created_at,updated_at,created_by,published_at,published_by,provenance) VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)''',
                   (vid, family_id, version, status, data['scope'], data['scope_id'], json.dumps(data), source_id,
                    now, now, user.get('id'), now if status == 'published' else None,
                    user.get('id') if status == 'published' else None, json.dumps(provenance or {})))
        self._index(db, vid, data)
        self._event(db, user, vid, 'seeded' if provenance else 'created', {'version': version, 'status': status})
        return db.execute('SELECT * FROM coefficient_versions WHERE id=?', (vid,)).fetchone()

    def create(self, user, payload):
        data = self._validated(payload)
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._can_manage(user, data, db):
                raise HTTPException(403, 'You cannot create definitions in this scope')
            row = self._insert(db, user, data)
            return self._public(user, row, db)

    def _get(self, db, user, vid, company_id=None):
        row = db.execute('SELECT * FROM coefficient_versions WHERE id=?', (vid,)).fetchone()
        if not row or not self._can_read(user, row, db, company_id):
            raise HTTPException(404, 'Coefficient version not found')
        return row

    def get(self, user, vid, company_id=None):
        with self.accounts.db() as db:
            row = self._get(db, user, vid, company_id)
            history = [dict(r) for r in db.execute('SELECT at,actor_id,action,details FROM coefficient_history WHERE version_id=? ORDER BY id', (vid,))]
            for event in history:
                event['details'] = json.loads(event['details'])
            versions = [self._public(user, other, db) for other in db.execute('SELECT * FROM coefficient_versions WHERE family_id=? ORDER BY version DESC', (row['family_id'],)) if self._can_read(user, other, db, company_id)]
            return {'item': self._public(user, row, db), 'history': visible_record(self.accounts, user, history), 'versions': versions}

    def list(self, user, *, q='', category='', geography='', reporting_year='', company_id=None,
             status='published', limit=50, offset=0, include_superseded=False):
        if status not in {'all', 'draft', 'published', 'retired'}:
            raise HTTPException(422, 'Unknown coefficient status')
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        with self.accounts.db() as db:
            if company_id:
                self._company(user, company_id, db)
            rows = db.execute('SELECT * FROM coefficient_versions ORDER BY version DESC,created_at DESC').fetchall()
            found, families = [], set()
            categories, geographies, years = set(), set(), set()
            terms = re.findall(r'\w+', q.casefold())
            for row in rows:
                if (status != 'all' and row['status'] != status) or not self._can_read(user, row, db, company_id):
                    continue
                data = json.loads(row['payload'])
                categories.add(data['category'])
                if data['geography']: geographies.add(data['geography'])
                if data['reporting_year']: years.add(data['reporting_year'])
                if category and category.casefold() != data['category'].casefold(): continue
                if geography and geography.casefold() not in data['geography'].casefold(): continue
                if reporting_year and reporting_year.casefold() != data['reporting_year'].casefold(): continue
                haystack = ' '.join(str(data.get(k) or '') for k in FIELDS if k != 'scope_id').casefold()
                if terms and not all(term in haystack for term in terms): continue
                key = (row['family_id'], row['status'])
                if not include_superseded and key in families: continue
                families.add(key)
                found.append(row)
            found.sort(key=lambda r: (json.loads(r['payload'])['name'].casefold(), -r['version']))
            return {'items': [self._public(user, row, db) for row in found[offset:offset + limit]],
                    'total': len(found), 'limit': limit, 'offset': offset, 'permissions': self.permissions(user),
                    'facets': {'categories': sorted(categories), 'geographies': sorted(geographies), 'reporting_years': sorted(years, reverse=True)}}

    @staticmethod
    def _revision(row, revision):
        if not isinstance(revision, int) or isinstance(revision, bool) or revision != row['revision']:
            raise HTTPException(409, 'This draft changed. Reload it before applying your change.')

    def update(self, user, vid, payload):
        payload = dict(payload)
        revision = payload.pop('revision', None)
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._get(db, user, vid)
            if not self._can_manage(user, row, db): raise HTTPException(403, 'Definition editing is not permitted')
            if row['status'] != 'draft': raise HTTPException(409, 'Published versions are immutable. Create a revision.')
            self._revision(row, revision)
            original = json.loads(row['payload'])
            data = self._validated({**original, **payload})
            if (data['scope'], data['scope_id']) != (row['scope'], row['scope_id']):
                raise HTTPException(422, 'A version family keeps its scope. Create a separate definition to change scope.')
            source_id = self._source(db, data)
            db.execute('UPDATE coefficient_versions SET payload=?,source_id=?,revision=revision+1,updated_at=? WHERE id=?', (json.dumps(data), source_id, timestamp(), vid))
            self._index(db, vid, data)
            self._event(db, user, vid, 'edited', {'fields': sorted(payload), 'revision': revision + 1})
            return self._public(user, db.execute('SELECT * FROM coefficient_versions WHERE id=?', (vid,)).fetchone(), db)

    def revise(self, user, vid, revision=None):
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._get(db, user, vid)
            if not self._can_manage(user, row, db): raise HTTPException(403, 'Definition revision is not permitted')
            if row['status'] == 'draft': raise HTTPException(409, 'Edit the existing draft')
            if revision is not None: self._revision(row, revision)
            if db.execute("SELECT 1 FROM coefficient_versions WHERE family_id=? AND status='draft'", (row['family_id'],)).fetchone():
                raise HTTPException(409, 'This family already has a draft. Finish that draft first.')
            latest = db.execute('SELECT MAX(version) FROM coefficient_versions WHERE family_id=?', (row['family_id'],)).fetchone()[0]
            if row['version'] != latest:
                raise HTTPException(409, 'Revise the latest version in this family')
            new = self._insert(db, user, json.loads(row['payload']), family_id=row['family_id'])
            self._event(db, user, new['id'], 'revised', {'previous_version_id': vid})
            return self._public(user, new, db)

    @staticmethod
    def _publication_errors(data):
        required = ['source_title', 'source_locator', 'source_version', 'geography', 'reporting_year', 'accounting_boundary', 'methodology']
        if data['kind'] == 'emission_factor': required += ['gas_basis', 'gas_coverage']
        missing = [key for key in required if not data.get(key)]
        if not data.get('source_url') and not data.get('source_excerpt'):
            missing.append('source_url or source_excerpt')
        if missing: raise HTTPException(422, 'Before publication provide: ' + ', '.join(missing))
        if data['kind'] == 'emission_factor':
            label = re.sub(r'\s+', '', data['output_unit']).casefold()
            gas = re.sub(r'\s+', '', data['gas_basis']).casefold()
            if ('co2e' in label and 'co2e' not in gas) or ('co2' in label and 'co2e' not in label and ('co2' not in gas or 'co2e' in gas)):
                raise HTTPException(422, 'The output unit and gas basis disagree. CO2 and CO2e must remain distinct.')

    def transition(self, user, vid, revision, target):
        with self.accounts.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._get(db, user, vid)
            if not self._can_manage(user, row, db): raise HTTPException(403, 'Publication is not permitted in this scope')
            self._revision(row, revision)
            expected = 'draft' if target == 'published' else 'published'
            if row['status'] != expected: raise HTTPException(409, f'Only {expected} definitions can be {target}')
            if target == 'published': self._publication_errors(json.loads(row['payload']))
            at = timestamp()
            prefix = 'published' if target == 'published' else 'retired'
            db.execute(f'UPDATE coefficient_versions SET status=?,revision=revision+1,updated_at=?,{prefix}_at=?,{prefix}_by=? WHERE id=?', (target, at, at, user.get('id'), vid))
            self._event(db, user, vid, prefix, {'revision': revision + 1})
            return self._public(user, db.execute('SELECT * FROM coefficient_versions WHERE id=?', (vid,)).fetchone(), db)

    def resolve_for_calculation(self, user, version_id, company_id=None):
        with self.accounts.db() as db:
            row = self._get(db, user, version_id, company_id)
            if row['status'] != 'published':
                raise HTTPException(409, 'Choose a published, active coefficient version')
            record = self._public(user, row, db)
            snapshot = {**json.loads(row['payload']), 'version_id': row['id'], 'family_id': row['family_id'],
                        'version': row['version'], 'published_at': row['published_at'],
                        'status_at_calculation': 'published',
                        'provenance': json.loads(row['provenance'])}
            canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            snapshot['sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
            return {'record': record, 'snapshot': snapshot,
                    'applicability': {'status': 'published_scope_checked_context_review_required',
                                      'checks_required': ['activity', 'geography', 'reporting_year', 'accounting_boundary', 'validity_dates']}}

    @staticmethod
    def normalize_quantity(quantity, unit, expected_unit):
        amount = Decimal(decimal_string(quantity))
        unit, expected_unit = unit.strip(), expected_unit.strip()
        if unit == expected_unit:
            return amount
        # Exact SI conversions only. Source qualifiers (net/gross CV, vehicle
        # occupancy, component boundaries) deliberately remain distinct units.
        groups = (
            {'Wh': Decimal('0.001'), 'kWh': Decimal(1), 'MWh': Decimal(1000), 'GWh': Decimal(1_000_000)},
            {'g': Decimal('0.001'), 'kg': Decimal(1), 't': Decimal(1000), 'tonne': Decimal(1000)},
            {'mL': Decimal('0.001'), 'L': Decimal(1), 'litres': Decimal(1), 'm3': Decimal(1000), 'm³': Decimal(1000)},
            {'m': Decimal('0.001'), 'km': Decimal(1)},
            {'kJ': Decimal('0.001'), 'MJ': Decimal(1), 'GJ': Decimal(1000)},
        )
        for group in groups:
            if unit in group and expected_unit in group:
                return amount * group[unit] / group[expected_unit]
        raise HTTPException(422, f'Unit mismatch: this coefficient requires {expected_unit}. Qualified/composite units must match exactly.')

    def calculate(self, user, version_id, quantity, unit, company_id=None, record=True):
        if company_id is None and role(user) in {'company_admin', 'data_entry'}:
            company_id = user.get('company_id')
        resolved = self.resolve_for_calculation(user, version_id, company_id)
        snapshot = resolved['snapshot']
        try:
            with localcontext() as ctx:
                ctx.prec = 400
                amount = self.normalize_quantity(quantity, unit, snapshot['input_unit'])
                result = amount * Decimal(snapshot['value'])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        receipt = {'calculation_id': str(uuid.uuid4()), 'at': timestamp(), 'company_id': company_id,
                   'quantity': decimal_string(quantity), 'input_unit': unit,
                   'normalized_quantity': format(amount, 'f'), 'coefficient_input_unit': snapshot['input_unit'],
                   'factor_snapshot': snapshot, 'result_value': format(result, 'f'),
                   'result_unit': snapshot['output_unit'], 'gas_basis': snapshot['gas_basis'],
                   'applicability': resolved['applicability']}
        if record:
            with self.accounts.db() as db:
                db.execute('INSERT INTO coefficient_calculations(id,at,actor_id,company_id,version_id,receipt) VALUES(?,?,?,?,?,?)',
                           (receipt['calculation_id'], receipt['at'], user.get('id'), company_id, version_id, json.dumps(receipt)))
                self.accounts.audit(user.get('id'), 'coefficient.calculated', receipt['calculation_id'], {'version_id': version_id, 'company_id': company_id}, db=db)
        return receipt

    def import_csv(self, user, csv_text, *, commit=False, scope='platform', scope_id=None):
        errors, validated = [], []
        try:
            reader = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff'), newline=''))
            headers = reader.fieldnames or []
            if not headers or len(headers) != len(set(headers)) or any(h not in FIELDS for h in headers):
                raise ValueError('Use the template headers exactly; unknown/duplicate headers are not accepted')
            for line, raw in enumerate(reader, 2):
                if line > MAX_IMPORT_ROWS + 1: raise ValueError(f'Import at most {MAX_IMPORT_ROWS} rows at a time')
                if None in raw: raise ValueError(f'Row {line} has extra columns')
                if not any(str(v or '').strip() for v in raw.values()): continue
                raw = {key: (value or '').strip() for key, value in raw.items()}
                if raw.get('scope') and raw['scope'] != scope: raise ValueError(f'Row {line}: scope must match the selected import scope')
                if raw.get('scope_id') and raw['scope_id'] != (scope_id or ''): raise ValueError(f'Row {line}: scope_id must match the selected import scope')
                raw['scope'], raw['scope_id'] = scope, scope_id
                try:
                    validated.append((line, self._validated(raw)))
                except HTTPException as exc:
                    errors.append({'row': line, 'message': str(exc.detail)})
        except (ValueError, csv.Error) as exc:
            errors.append({'row': None, 'message': str(exc)})
        with self.accounts.db() as db:
            if commit: db.execute('BEGIN IMMEDIATE')
            for line, data in validated:
                if not self._can_manage(user, data, db):
                    errors.append({'row': line, 'message': 'You cannot import definitions in this scope'})
            if not validated and not errors: errors.append({'row': None, 'message': 'The CSV contains no coefficient rows'})
            result = {'valid': not errors, 'rows': len(validated), 'errors': errors, 'committed': False,
                      'preview': [data for _, data in validated[:20]], 'items': []}
            if commit and not errors:
                result['items'] = [self._public(user, self._insert(db, user, data), db) for _, data in validated]
                result['committed'] = True
            return result

    def _seed(self):
        path = self.root / 'research' / 'coefficient-seed.json'
        if not path.exists(): return
        try:
            raw = path.read_bytes()
            seed = json.loads(raw)
            digest = hashlib.sha256(raw).hexdigest()
            with self.accounts.db() as db:
                if db.execute('SELECT 1 FROM coefficient_seed_runs WHERE digest=?', (digest,)).fetchone(): return
                candidates = []
                for item in seed.get('items', []):
                    external_id = str(item.get('id', ''))
                    if not external_id: raise ValueError('Seed item is missing a stable id')
                    if db.execute('SELECT 1 FROM coefficient_families WHERE external_id=?', (external_id,)).fetchone(): continue
                    data = self._validated({key: item[key] for key in FIELDS if key in item})
                    if data['scope'] != 'platform': raise ValueError('Bundled seed definitions must be platform-scoped')
                    status = 'published' if item.get('reviewed') is True else 'draft'
                    if status == 'published': self._publication_errors(data)
                    candidates.append((external_id, data, status))
                system = {'id': 'official-seed-import', 'role': 'master'}
                for external_id, data, status in candidates:
                    self._insert(db, system, data, external_id=external_id, status=status,
                                 provenance={'type': 'bundled_official_seed', 'seed_version': str(seed.get('version', '')), 'seed_id': external_id,
                                             'seed_sha256': digest, 'reviewed': status == 'published'})
                db.execute('INSERT INTO coefficient_seed_runs(digest,seed_version,installed_at,count) VALUES(?,?,?,?)',
                           (digest, str(seed.get('version', '')), timestamp(), len(candidates)))
        except Exception as exc:
            # An invalid research bundle never prevents the existing product from
            # starting, and the atomic transaction leaves the library untouched.
            self.seed_warning = f'Coefficient seed was not installed: {exc}'


def install_coefficients(app, accounts, root, ai_client=None, model=None):
    service = CoefficientService(accounts, Path(root))
    knowledge = CoefficientKnowledge(service, ai_client=ai_client, model=model)
    app.state.coefficients = service
    app.state.coefficient_knowledge = knowledge

    # Static routes precede /{version_id} so template/import/calculate are never
    # interpreted as coefficient identifiers.
    @app.get('/api/coefficients')
    def coefficient_list(request: Request, q: str = '', category: str = '', geography: str = '', reporting_year: str = '',
                         company_id: str | None = None, status: str = 'published', limit: int = 50, offset: int = 0,
                         include_superseded: bool = False):
        return service.list(actor(request), q=q, category=category, geography=geography, reporting_year=reporting_year,
                            company_id=company_id, status=status, limit=limit, offset=offset, include_superseded=include_superseded)

    @app.post('/api/coefficients', status_code=201)
    def coefficient_create(body: CoefficientInput, request: Request):
        return {'item': service.create(actor(request), body.model_dump())}

    @app.get('/api/coefficients/template')
    def coefficient_template(request: Request):
        actor(request)
        output = io.StringIO(newline='')
        csv.writer(output).writerow(FIELDS)
        return Response(output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="trace-ai-coefficients-template.csv"'})

    @app.post('/api/coefficients/import')
    def coefficient_import(body: ImportInput, request: Request):
        return service.import_csv(actor(request), body.csv, commit=body.commit, scope=body.scope, scope_id=body.scope_id)

    @app.post('/api/coefficients/calculate')
    def coefficient_calculate(body: CalculateInput, request: Request):
        return service.calculate(actor(request), body.version_id, body.quantity, body.unit, body.company_id)

    @app.get('/api/knowledge/status')
    def knowledge_status(request: Request, company_id: str | None = None):
        return knowledge.status(actor(request), company_id)

    @app.post('/api/knowledge/ask')
    def knowledge_ask(body: AskInput, request: Request):
        return knowledge.ask(actor(request), body)

    @app.get('/api/coefficients/{version_id}')
    def coefficient_detail(version_id: str, request: Request, company_id: str | None = None):
        return service.get(actor(request), version_id, company_id)

    @app.put('/api/coefficients/{version_id}')
    def coefficient_edit(version_id: str, body: dict, request: Request):
        return {'item': service.update(actor(request), version_id, body)}

    @app.post('/api/coefficients/{version_id}/revise', status_code=201)
    def coefficient_revise(version_id: str, request: Request, body: dict | None = None):
        if body and set(body) - {'revision'}: raise HTTPException(422, 'Only revision is accepted')
        return {'item': service.revise(actor(request), version_id, (body or {}).get('revision'))}

    @app.post('/api/coefficients/{version_id}/publish')
    def coefficient_publish(version_id: str, body: RevisionInput, request: Request):
        return {'item': service.transition(actor(request), version_id, body.revision, 'published')}

    @app.post('/api/coefficients/{version_id}/retire')
    def coefficient_retire(version_id: str, body: RevisionInput, request: Request):
        return {'item': service.transition(actor(request), version_id, body.revision, 'retired')}

    return service
