"""Deterministic selected BRSR metrics. Missing and zero denominators stay undefined.

The versioned catalogue declares formulas. Inputs are supplied and reviewed by
people; this engine does not claim that a source document or inventory is complete.
"""
from decimal import Decimal, InvalidOperation, localcontext


def decimal(value):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def display(value):
    if value == 0:
        return '0'
    # Significant digits preserve very small nonzero per-INR intensities.
    return format(value, '.18g')


def calculate_item(item, values):
    calculated = []
    variables = dict(values)
    for spec in item.get('calculations', []):
        result = {k: spec[k] for k in ('id', 'label', 'unit') if k in spec}
        names = spec.get('inputs', [])
        inputs = [decimal(variables.get(name)) for name in names]
        denominator_name = spec.get('denominator')
        denominator = decimal(variables.get(denominator_name)) if denominator_name else None
        operation = spec.get('operation')
        explanation = None
        value = None
        if not names or any(number is None for number in inputs):
            explanation = 'Required inputs are missing or invalid.'
        elif denominator_name and (denominator is None or denominator == 0):
            explanation = 'The denominator is missing or zero; this result is undefined.'
        elif '_ppp_' in spec['id'] and not str(values.get('ppp_source', '')).strip():
            explanation = 'Provide the PPP source, year, units and conversion working first.'
        elif '_output_' in spec['id'] and (not values.get('output_unit') or not values.get('output_basis')):
            explanation = 'Declare physical-output units and the production/FTE basis first.'
        else:
            with localcontext() as context:
                context.prec = 50
                total = sum(inputs, Decimal(0))
                if operation == 'sum':
                    value = total
                elif operation == 'average':
                    value = total / len(inputs)
                elif operation in {'ratio', 'percentage', 'scaled_ratio'} and denominator is not None:
                    scale = Decimal(100) if operation == 'percentage' else decimal(spec.get('scale', '1'))
                    value = total * scale / denominator if scale is not None else None
                else:
                    explanation = 'Unsupported formula configuration.'
        terms = [name + '=' + str(variables.get(name, 'missing')) for name in names]
        if denominator_name:
            terms.append(denominator_name + '=' + str(variables.get(denominator_name, 'missing')))
        result.update(value=display(value) if value is not None else None,
                      status='ready' if value is not None else 'not_ready',
                      formula=operation + '(' + ', '.join(terms) + ')' + (' × ' + str(spec['scale']) if spec.get('scale') else ''),
                      note=explanation or 'Derived from the entered values; validate evidence, scope and denominator before review.')
        variables[spec['id']] = value
        calculated.append(result)
    for spec in item.get('row_calculations', []):
        rows = values.get(spec['field'], [])
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            numerator, denominator = decimal(row.get(spec['numerator'])), decimal(row.get(spec['denominator']))
            valid = numerator is not None and denominator is not None and denominator > 0
            with localcontext() as context:
                context.prec = 50
                value = numerator * 100 / denominator if valid else None
            calculated.append({'id': spec['field'] + '_' + str(index),
                               'label': spec['label'] + ' · row ' + str(index + 1), 'unit': spec.get('unit', '%'),
                               'value': display(value) if valid else None, 'status': 'ready' if valid else 'not_ready',
                               'formula': str(row.get(spec['numerator'], 'missing')) + ' × 100 / ' + str(row.get(spec['denominator'], 'missing')),
                               'note': 'Confirm the denominator class for this concentration or related-party subpart.'})
    return calculated
