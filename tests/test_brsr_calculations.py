"""Selected BRSR arithmetic and source-input integrity, never compliance certification."""
import json
import unittest
from pathlib import Path
from brsr_calculations import calculate_item

CATALOG = json.loads((Path(__file__).resolve().parents[1]/'research/brsr-disclosures.json').read_text())


class Calculations(unittest.TestCase):
    def calculate(self, qid, values):
        item = next(q for q in CATALOG['items'] if q['id'] == qid)
        return {r['id']: r for r in calculate_item(item, values)}

    def test_energy_totals_and_missing_optional_denominators(self):
        values = {key+'_current': str(value) for key, value in [('renew_electricity', 36), ('renew_fuel', 4), ('renew_other', 0), ('nonrenew_electricity', 50), ('nonrenew_fuel', 10), ('nonrenew_other', 0)]}
        values['revenue_operations_inr_current'] = '1000'
        result = self.calculate('P6-E-1', values)
        self.assertEqual(result['energy_total_current']['value'], '100')
        self.assertEqual(result['renewable_share_current']['value'], '40')
        self.assertEqual(result['energy_inr_current']['value'], '0.1')
        self.assertIsNone(result['energy_total_previous']['value'])
        self.assertIsNone(result['energy_ppp_current']['value'])
        self.assertIsNone(result['energy_output_current']['value'])

    def test_zero_and_missing_are_distinct_and_division_by_zero_is_undefined(self):
        result = self.calculate('P1-E-8', {'accounts_payable_current': '0', 'cost_procured_current': '100'})
        self.assertEqual(result['payable_days_current']['value'], '0')
        for denominator in ['', None, '0']:
            result = self.calculate('P1-E-8', {'accounts_payable_current': '250', 'cost_procured_current': denominator})
            self.assertEqual(result['payable_days_current']['status'], 'not_ready')
            self.assertIsNone(result['payable_days_current']['value'])
        result = self.calculate('P1-E-8', {'accounts_payable_current': '250', 'cost_procured_current': '1000'})
        self.assertEqual(result['payable_days_current']['value'], '91.25')

    def test_posh_uses_average_beginning_and_end_female_workforce(self):
        result = self.calculate('P5-E-7', {'posh_complaints_current': '3', 'female_workforce_begin_current': '100', 'female_workforce_end_current': '300'})
        self.assertEqual(result['female_workforce_avg_current']['value'], '200')
        self.assertEqual(result['posh_pct_current']['value'], '1.5')
        result = self.calculate('P5-E-7', {'posh_complaints_current': '4', 'female_workforce_begin_current': '1', 'female_workforce_end_current': '1'})
        self.assertEqual(result['posh_pct_current']['value'], '400')

    def test_safety_person_hours_and_population_separation(self):
        result = self.calculate('P3-E-11', {'employees_lost_time_injuries_current': '2', 'employees_hours_current': '1000000', 'workers_lost_time_injuries_current': '1', 'workers_hours_current': '0'})
        self.assertEqual(result['employees_ltifr_current']['value'], '2')
        self.assertIsNone(result['workers_ltifr_current']['value'])

    def test_distinct_full_brsr_and_core_customer_data_denominators(self):
        result = self.calculate('P9-E-7', {'breaches_current': '2', 'customer_pii_breaches_current': '1', 'customer_data_events_current': '3', 'cyber_events_current': '10'})
        self.assertEqual(result['pii_breach_pct_current']['value'], '50')
        self.assertEqual(result['customer_event_pct_current']['value'], '30')

    def test_ppp_and_output_require_explicit_basis_and_row_denominators(self):
        values = {'scope1_current': '10', 'scope2_current': '20', 'revenue_operations_inr_current': '1000000', 'revenue_operations_ppp_current': '10000', 'physical_output_current': '100'}
        result = self.calculate('P6-E-7', values)
        self.assertEqual(result['ghg_inr_current']['value'], '0.00003')
        tiny = self.calculate('P6-E-7', {'scope1_current':'0.001','scope2_current':'0','revenue_operations_inr_current':'10000000000'})
        from decimal import Decimal
        self.assertEqual(Decimal(tiny['ghg_inr_current']['value']), Decimal('1e-13'))
        self.assertIsNone(result['ghg_ppp_current']['value'])
        self.assertIsNone(result['ghg_output_current']['value'])
        result = self.calculate('P6-E-7', {**values, 'ppp_source': 'Fictional test PPP basis; not a production rate', 'output_unit': 'FTE', 'output_basis': 'Service business FTE'})
        self.assertEqual(result['ghg_ppp_current']['value'], '0.003')
        self.assertEqual(result['ghg_output_current']['value'], '0.3')
        result = self.calculate('P1-E-9', {'openness': [{'numerator_inr': '25', 'denominator_inr': '100'}, {'numerator_inr': '2', 'denominator_inr': '0'}]})
        self.assertEqual(result['openness_0']['value'], '25')
        self.assertIsNone(result['openness_1']['value'])
