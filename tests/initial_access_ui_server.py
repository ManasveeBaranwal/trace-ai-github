"""Disposable two-access browser fixture; no live data or AI calls.

Run with the existing backend Python. It serves loopback port 8003 only when
executed directly. Every company, disclosure and coefficient is fictional.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def build_fixture():
    """Load a fresh real application against a new, isolated temporary store.

    Tests must patch/restore the environment around this function because the
    application's access middleware reads its local-mode flags per request.
    """
    storage = tempfile.TemporaryDirectory(prefix='trace-initial-access-qa-')
    os.environ.update(TRACE_DATA=storage.name, TRACE_REQUIRE_LOGIN='0', TRACE_TWO_ACCESS='1')
    sys.path.insert(0, str(ROOT))
    module_name = 'trace_initial_access_browser_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(module_name, ROOT/'app.py')
    product = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = product
    spec.loader.exec_module(product)
    client = TestClient(product.app, client=('127.0.0.1', 45001))
    response = client.get('/super-admin')
    assert response.status_code == 200, response.text
    session = client.get('/auth/session').json()
    assert session['user']['role'] == 'master', session
    client.headers['X-CSRF-Token'] = session['csrf']

    def create(path, body):
        result = client.post(path, json=body)
        assert result.status_code == 201, result.text
        return result.json()['id']

    cid = create('/api/access/companies', {
        'name':'Cedar Works · Fictional Demo', 'industry':'Manufacturing',
        'country':'India', 'firm_id':None})
    other = create('/api/access/companies', {
        'name':'Harbor Services · Fictional Demo', 'industry':'Professional services',
        'country':'India', 'firm_id':None})
    for company, name in [(cid, 'Cedar manager · Fictional Demo'),
                          (other, 'Harbor manager · Fictional Demo')]:
        response = client.put('/api/initial/companies/'+company+'/manager', json={'name':name})
        assert response.status_code == 200, response.text
    site = create('/api/facilities?company_id='+cid, {
        'name':'Cedar demonstration site', 'location':'Fictional site',
        'meter':'FICTIONAL-METER-01'})
    period = create('/api/reporting-periods?company_id='+cid, {
        'name':'October 2026 · Fictional Demo', 'start':'2026-10-01', 'end':'2026-10-31'})
    activity = create('/api/activities?company_id='+cid, {
        'title':'October electricity · Fictional draft', 'facility_id':site,
        'period_id':period, 'category':'electricity', 'scope':'2',
        'quantity':'1250', 'unit':'kWh', 'factor_value':'0.5', 'gas':'CO2',
        'factor_source':'Fictional QA coefficient, not a reporting factor',
        'factor_url':'https://example.org/fictional-qa', 'factor_year':'2026',
        'notes':'Fictional UI fixture. No real operational or emissions claim.'})
    report = create('/api/brsr/reports?company_id='+cid, {
        'name':'BRSR FY 2026-27 · Fictional Demo', 'financial_year':'2026-27',
        'previous_financial_year':'2025-26', 'boundary':'standalone',
        'reporting_start':'2026-04-01', 'reporting_end':'2027-03-31',
        'listed':True, 'notes':'Fictional QA only. Other disclosures remain missing.'})
    response = client.put('/api/brsr/reports/'+report+'/answers/P1-E-8', json={
        'values':{'accounts_payable_current':'250', 'accounts_payable_previous':'200',
                  'cost_procured_current':'1000', 'cost_procured_previous':'800',
                  'payables_basis':'Fictional schedules for browser QA; amounts in INR'},
        'narrative':'Fictional calculation to exercise a pending review.',
        'evidence':[{'type':'reference','reference':'Fictional procurement ledger, page 1'}],
        'applicability':'applicable', 'status':'submitted', 'expected_revision':0})
    assert response.status_code == 200, response.text

    # Use the real upload pipeline while explicitly preventing model execution.
    sample = client.get('/api/sample').content
    with patch.object(product, 'launch'):
        response = client.post('/api/bills', data={'company_id':cid}, files={
            'file':('Fictional electricity bill.pdf', sample)})
    assert response.status_code == 202, response.text
    bill = response.json()
    bill.update(status='needs_review', fields={key:{'value':None,'evidence':None}
                                                for key in product.FIELDS})
    bill['fields']['consumption'] = {'value':'1250','evidence':'1250','source_page':1}
    bill['fields']['unit'] = {'value':'kWh','evidence':'kWh','source_page':1}
    product.save(bill)

    return SimpleNamespace(product=product, storage=storage, client=client,
                           cid=cid, other=other, site=site, period=period,
                           activity=activity, report=report, bill=bill['id'])


if __name__ == '__main__':
    import uvicorn
    fixture = build_fixture()
    print('Isolated two-access browser QA: http://127.0.0.1:8003/super-admin', flush=True)
    print('Temporary data: '+fixture.storage.name, flush=True)
    print('Company A: '+fixture.cid+'; Company B: '+fixture.other, flush=True)
    uvicorn.run(fixture.product.app, host='127.0.0.1', port=8003)
