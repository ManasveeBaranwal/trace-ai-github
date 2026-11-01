"""Isolated browser-QA fixture. Never reads or writes the real product database.

Run with the backend Python; serves on loopback port 8003 with login enabled.
All accounts, clients, documents and factor values below are fictional fixtures.
"""
import os
import sys
import tempfile
from pathlib import Path
os.environ['TRACE_DATA'] = tempfile.mkdtemp(prefix='trace-dashboard-ui-')
os.environ['TRACE_REQUIRE_LOGIN'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as product
from fastapi.testclient import TestClient
from unittest.mock import patch

password = 'Preview-password-123'
product.accounts.create_master('founder@fixture.example', 'Preview Founder', password)
client = TestClient(product.app)
login = client.post('/auth/login', json={'email': 'founder@fixture.example', 'password': password})
client.headers['X-CSRF-Token'] = login.json()['csrf']


def create(path, body):
    response = client.post(path, json=body)
    assert response.status_code == 201, response.text
    return response.json()['id']


fid = create('/api/access/firms', {'name': 'Willow Advisory · Fictional Demo', 'consultant_limit': 3, 'company_limit': 5, 'default_data_entry_limit': 2})
cid = create('/api/access/companies', {'name': 'Northstar Manufacturing · Demo', 'country': 'India', 'industry': 'Manufacturing', 'firm_id': fid})
other = create('/api/access/companies', {'name': 'Meadow Offices · Demo', 'country': 'India', 'industry': 'Professional services', 'firm_id': fid})
site = create('/api/facilities?company_id='+cid, {'name': 'Pune plant · Demo', 'location': 'Pune', 'meter': 'DEMO-METER-01'})
create('/api/facilities?company_id='+cid, {'name': 'Mumbai office · Demo', 'location': 'Mumbai'})
period = create('/api/reporting-periods?company_id='+cid, {'name': 'October 2026 · Demo', 'start': '2026-10-01', 'end': '2026-10-31'})
for role, assignment in [('consultant_manager', {'firm_id': fid}), ('consultant', {'firm_id': fid}), ('company_admin', {'company_id': cid}), ('data_entry', {'company_id': cid, 'facility_ids': [site]})]:
    create('/api/access/users', {'name': 'Preview '+role.replace('_', ' ').title(), 'email': role+'@fixture.example', 'password': password, 'role': role, **assignment})
entry = TestClient(product.app)
login = entry.post('/auth/login', json={'email': 'data_entry@fixture.example', 'password': password})
entry.headers['X-CSRF-Token'] = login.json()['csrf']
sample = client.get('/api/sample').content
with patch.object(product, 'launch'):
    bill = entry.post('/api/bills', files={'file': ('Fictional electricity bill.pdf', sample)}).json()
bill.update(status='needs_review', fields={key: {'value': None, 'evidence': None} for key in product.FIELDS})
bill['fields']['consumption'] = {'value': '1250', 'evidence': 'Electricity consumption: 1250 kWh', 'quote_matched': True, 'value_in_quote': True}
bill['fields']['unit'] = {'value': 'kWh', 'evidence': 'Electricity consumption: 1250 kWh', 'quote_matched': True, 'value_in_quote': True}
product.save(bill)
create('/api/activities?company_id='+cid, {'title': 'October electricity · Demo draft', 'facility_id': site, 'period_id': period, 'category': 'electricity', 'scope': '2', 'quantity': '1250', 'unit': 'kWh', 'factor_value': '0.5', 'gas': 'CO2', 'factor_source': 'Fictional browser-QA coefficient', 'factor_url': 'https://example.org/fixture', 'factor_year': '2026'})
print('Isolated fictional dashboard QA ready at http://127.0.0.1:8003/login', flush=True)
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(product.app, host='127.0.0.1', port=8003)
