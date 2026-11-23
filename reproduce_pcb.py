from collections import Counter
from pathlib import Path
from tempfile import mkdtemp
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'cscwd2027_acceptance_audit_union_20261121'))
from literal_gate_replay import D, CASES, DIAGNOSTICS, gate_reason


def replay_cells():
    requests = {case['id']: case for case in D.read(CASES)}
    candidates = D.read(DIAGNOSTICS)
    cases = (
        ('compiled_contract', 'parametric_full', ('advisory', 'empty_defer', 'ewdv')),
        ('central_fresh', 'literal_full', ('advisory', 'empty_defer', 'ewdv')),
        ('central_fresh', 'parametric_full', ('advisory', 'empty_defer', 'ewdv')),
        ('central_fresh', 'numeric_feedback', ('advisory', 'empty_defer', 'ewdv')),
        ('central_fresh', 'gold', ('advisory',)),
    )
    simulator = D.SimulationCache()
    output = Path(mkdtemp(prefix='pcb-audit-'))
    print(f'SQLite read-backs: {output}', file=sys.stderr)
    for controller, method, policies in cases:
        selected = [record for record in candidates if record['method'] == method]
        assert len(selected) == 24, method
        for policy in policies:
            outcome = Counter()
            for record in selected:
                request = requests[record['id']]
                rows = record['contract']
                cache = D.RowCache(simulator, rows)
                reason = (gate_reason(record, request['text']) if policy == 'ewdv'
                          else D.decision(policy, rows, record['final_diagnostics']))
                for script in D.RACES:
                    operation = '/'.join((record['id'], record['model'], method, controller, policy, script))
                    database = output / (D.digest(operation)[:24] + '.sqlite')
                    result = D.replay(simulator, cache, request['gold'], script,
                                      controller, operation, database, reason)
                    outcome['valid'] += result['valid']
                    outcome['invalid'] += result['invalid']
                    outcome['no_new_commit'] += not result['committed']
                    outcome['deferrals'] += result['status'].startswith('DEFER')
            yield controller, method, policy, outcome


if __name__ == '__main__':
    print('controller,method,policy,valid,invalid,no_new_commit,deferrals')
    for controller, method, policy, outcome in replay_cells():
        print(controller, method, policy, outcome['valid'], outcome['invalid'],
              outcome['no_new_commit'], outcome['deferrals'], sep=',')
