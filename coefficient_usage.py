"""Explicit, unit-checked use of published coefficients in existing ledgers."""
from decimal import Decimal
from fastapi import HTTPException


def resolve_emission_factor(app, user, version_id, company_id, category, unit):
    """Return canonical metadata; callers must persist the immutable snapshot.

    The ledger stores kg gas and has only one automatic unit conversion (MWh to
    kWh electricity). Other library calculations stay in their own calculator.
    """
    resolved = app.state.coefficients.resolve_for_calculation(
        user, version_id, company_id=company_id)
    item = resolved['record']
    basis = 'kWh' if category == 'electricity' else unit
    output = item['output_unit'].replace('₂', '2').strip()
    gas = {'kg CO2': 'CO2', 'kg CO2e': 'CO2e'}.get(output)
    if item.get('kind') != 'emission_factor' or not gas or item.get('gas_basis') != gas:
        raise HTTPException(422, 'This ledger requires an emission factor with output kg CO2 or kg CO2e. Use the library calculator for other coefficient types.')
    if item['input_unit'] != basis:
        raise HTTPException(422, 'Coefficient input unit must match '+basis+'. Choose a compatible factor or use the library calculator for explicit unit conversions.')
    value = Decimal(item['value'])
    if value > Decimal('1000000000'):
        raise HTTPException(422, 'This coefficient exceeds the activity ledger limit. Use the library calculator for this method.')
    categories = {'fuel': {'fuel', 'fuel_combustion', 'fuel_upstream'},
                  'travel': {'travel', 'travel_air', 'travel_land', 'travel_sea'}}
    if category != 'other' and item['category'] not in categories.get(category, {category}):
        raise HTTPException(422, 'Choose a coefficient for the same activity category. Electricity losses and upstream factors cannot replace purchased-electricity factors.')
    return {
        'factor_value': value,
        'factor_source': item['source_title'],
        'factor_url': item['source_url'],
        'factor_year': item['reporting_year'],
        'factor_region': item['geography'],
        'gas': gas,
        'coefficient_version_id': item['id'],
        'coefficient_snapshot': resolved['snapshot'],
        'factor_method': item['methodology'],
        'accounting_boundary': item['accounting_boundary'],
    }
