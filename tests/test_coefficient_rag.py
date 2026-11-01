"""Grounding and local-model fallback tests with no Ollama/network dependency."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coefficient_rag import AskInput, CoefficientKnowledge
from test_coefficients import CoefficientFixture


class LocalModel:
    def __init__(self, result=None, error=None):
        self.result = result or {'answer': 'The retrieved grid definition covers direct generation. Confirm the reporting boundary before applying the source card.', 'citation_ids': ['S1'], 'needs_context': False}
        self.error = error
        self.calls = []

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model='qwen3-vl:4b')])

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return SimpleNamespace(response=json.dumps(self.result))


class CoefficientRAG(CoefficientFixture):
    def setUp(self):
        super().setUp()
        self.india = self.published(source_excerpt='Fixture table: 0.727 kg CO2/kWh; CO2 only.')
        self.uk = self.published(name='UK grid reference', geography='United Kingdom', reporting_year='2026', value='0.1', gas_basis='CO2e', output_unit='kg CO2e')
        self.secret = self.published(name='Foreign confidential unobtanium', category='secret', scope='company', scope_id='company-b', source_excerpt='foreign-secret-source-never-in-model-context')

    def test_real_fts_retrieval_then_local_generation_uses_canonical_cards(self):
        local = LocalModel()
        rag = CoefficientKnowledge(self.service, local, 'qwen3-vl:4b')
        result = rag.ask(self.admin, AskInput(question='Purchased electricity India FY2023-24'))
        self.assertEqual(result['mode'], 'local_generation')
        self.assertEqual(result['retrieval_backend'], 'sqlite_fts5')
        self.assertEqual([row['id'] for row in result['coefficients']], [self.india['id']])
        self.assertEqual(result['coefficients'][0]['value'], '0.727')
        self.assertEqual(result['citations'][0]['locator'], 'Table 1, row 4')
        call = local.calls[0]
        self.assertTrue(call['raw'])
        self.assertFalse(call['think'])
        self.assertIn('<|im_start|>system', call['prompt'])
        self.assertIn('<think>\n</think>', call['prompt'])
        self.assertNotIn('foreign-secret-source', call['prompt'])
        self.assertNotIn(self.master['id'], call['prompt'])
        self.assertNotIn('0.727', call['prompt'])
        self.assertIn('0.727', result['citations'][0]['excerpt'])
        self.assertEqual(rag.status(self.admin)['counts']['published_versions'], 2)
        self.assertTrue(rag.status(self.admin)['model_available'])

    def test_lexical_fallback_is_labelled_when_fts_is_unavailable(self):
        self.service.fts_available = False
        rag = CoefficientKnowledge(self.service, LocalModel())
        result = rag.ask(self.admin, {'question': 'Purchased electricity India FY2023-24'})
        self.assertEqual(result['mode'], 'local_generation')
        self.assertEqual(result['retrieval_backend'], 'sqlite_lexical_fallback')

    def test_no_cross_tenant_retrieval_or_model_context(self):
        local = LocalModel()
        rag = CoefficientKnowledge(self.service, local)
        result = rag.ask(self.admin, {'question': 'unobtanium confidential source'})
        self.assertEqual(result['mode'], 'needs_context')
        self.assertEqual(result['coefficients'], [])
        self.assertEqual(local.calls, [])
        self.assert_http(404, lambda: rag.ask(self.admin, {'question': 'electricity', 'company_id': 'company-b'}))

    def test_country_year_and_activity_are_constraints_not_substitutions(self):
        local = LocalModel()
        rag = CoefficientKnowledge(self.service, local)
        for question in ('Electricity India 2026', 'Diesel India FY2023-24', 'Electricity United States 2026'):
            result = rag.ask(self.admin, {'question': question})
            self.assertEqual(result['mode'], 'needs_context', question)
            self.assertEqual(result['coefficients'], [], question)
        self.assertEqual(local.calls, [])

    def test_offline_model_is_transparently_retrieval_only(self):
        rag = CoefficientKnowledge(self.service, LocalModel(error=ConnectionError('local private path')))
        result = rag.ask(self.admin, {'question': 'Purchased electricity India FY2023-24'})
        self.assertEqual(result['mode'], 'retrieval_only')
        self.assertEqual(result['reason'], 'local_generation_unavailable_or_not_grounded')
        self.assertEqual(result['coefficients'][0]['value'], '0.727')
        self.assertNotIn('private path', result['answer'])
        no_model = CoefficientKnowledge(self.service)
        self.assertFalse(no_model.status(self.admin)['model_available'])
        self.assertEqual(no_model.ask(self.admin, {'question': 'electricity'})['reason'], 'local_model_not_configured')

    def test_unknown_citations_and_hallucinated_numeric_values_are_rejected(self):
        for answer in (
            {'answer': 'This is a source-grounded explanation.', 'citation_ids': ['NONEXISTENT'], 'needs_context': False},
            {'answer': 'The approved factor is 999.99 and the calculated total is 800.', 'citation_ids': ['S1'], 'needs_context': False},
        ):
            rag = CoefficientKnowledge(self.service, LocalModel(answer))
            result = rag.ask(self.admin, {'question': 'Purchased electricity India FY2023-24'})
            self.assertEqual(result['mode'], 'retrieval_only')
            self.assertEqual(result['coefficients'][0]['value'], '0.727')
            self.assertNotIn('999.99', result['answer'])

    def test_fiscal_year_and_gas_labels_do_not_trigger_numeric_rejection(self):
        answer = {'answer': 'The India grid electricity coefficient is available for FY2023-24 (citation_id: S1). It covers CO2 only, excluding CH4 and N2O; confirm Scope 2 generation boundaries.',
                  'citation_ids': ['S1'], 'needs_context': False}
        rag = CoefficientKnowledge(self.service, LocalModel(answer))
        result = rag.ask(self.admin, {'question': 'Purchased electricity India FY2023-24'})
        self.assertEqual(result['mode'], 'local_generation')

    def test_demo_question_accepts_attached_gas_units_without_weakening_numbers(self):
        item = self.published(name='India grid electricity FY2024-25', reporting_year='FY2024-25')
        answer = {'answer': 'The India grid electricity coefficient for FY2024-25 (citation_id: S1) is a historical weighted-average grid factor. It is expressed in kg CO2 per kWh (equivalent to tCO2/MWh). It covers CO2 only, excluding CH4 and N2O, and must not be relabelled as CO2e.',
                  'citation_ids': ['S1'], 'needs_context': False}
        rag = CoefficientKnowledge(self.service, LocalModel(answer))
        result = rag.ask(self.admin, {'question': 'Explain the India grid electricity coefficient for FY2024-25 and its gas coverage.',
                                     'geography': 'India', 'reporting_year': 'FY2024-25', 'category': 'electricity'})
        self.assertEqual(result['mode'], 'local_generation')
        self.assertEqual(result['coefficients'][0]['id'], item['id'])
        self.assertFalse(rag._safe_explanation('The factor is 999kgCO2/kWh.', [item]))
        self.assertFalse(rag._safe_explanation('The factor is 999tCO2e/MWh.', [item]))

    def test_drafts_retired_factors_and_ambiguous_context(self):
        # Explicitly create an otherwise matching private draft and retire the
        # published India record; neither can enter knowledge retrieval.
        from test_coefficients import factor
        self.service.create(self.admin, factor(name='Draft electricity', scope='company', scope_id='company-a'))
        self.service.transition(self.master, self.india['id'], self.india['revision'], 'retired')
        rag = CoefficientKnowledge(self.service, LocalModel())
        result = rag.ask(self.admin, {'question': 'Electricity India FY2023-24'})
        self.assertEqual(result['coefficients'], [])
        broad = rag.ask(self.admin, {'question': 'electricity'})
        self.assertTrue(broad['needs_context'])
        self.assertEqual([row['id'] for row in broad['coefficients']], [self.uk['id']])


if __name__ == '__main__':
    import unittest
    unittest.main()
