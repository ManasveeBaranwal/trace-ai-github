"""Fictional BRSR browser QA on temporary storage, never the live database."""
import dashboard_ui_server as fixture
from fastapi.testclient import TestClient

client = fixture.client
report = client.post('/api/brsr/reports?company_id='+fixture.cid, json={
    'name':'BRSR FY 2026-27 · Fictional QA', 'financial_year':'2026-27',
    'previous_financial_year':'2025-26', 'boundary':'standalone',
    'reporting_start':'2026-04-01', 'reporting_end':'2027-03-31', 'listed':True,
    'notes':'FICTIONAL QA ONLY. No actual company disclosures or compliance assertions.'})
assert report.status_code == 201, report.text
rid=report.json()['id']
detail=client.get('/api/access/overview').json()
entry_id=next(u['id'] for u in detail['users'] if u['role']=='data_entry')
response=client.put('/api/brsr/reports/'+rid+'/assignments/'+entry_id,json={'question_ids':['P1-E-8','A-2']})
assert response.status_code==200,response.text
response=client.put('/api/brsr/reports/'+rid+'/answers/P1-E-8',json={
    'values':{'accounts_payable_current':'250','accounts_payable_previous':'200',
              'cost_procured_current':'1000','cost_procured_previous':'800',
              'payables_basis':'Fictional financial schedules for browser QA; values in INR'},
    'narrative':'Fictional calculator example. All other required disclosures remain missing.',
    'evidence':[{'type':'reference','reference':'Fictional QA procurement and trade-payables ledger, page 1'}],
    'applicability':'applicable','status':'submitted','expected_revision':0})
assert response.status_code==200,response.text
print('BRSR browser QA seeded in isolated temporary storage.',flush=True)

if __name__=='__main__':
    import uvicorn
    uvicorn.run(fixture.product.app,host='127.0.0.1',port=8003)
