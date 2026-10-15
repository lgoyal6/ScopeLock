"""Finite bilingual trigger fixture and guard-disabled ablation, no model calls."""
import argparse
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from scopelock.core import db, intent_guard
from scopelock.core.policy import PolicyService


def study(pairs):
    existing = getattr(db._local, 'conn', None)
    if existing is not None:
        existing.close()
        del db._local.conn
    output = []
    with tempfile.TemporaryDirectory(prefix='scopelock-bilingual-') as tmp:
        db.DB_PATH = Path(tmp) / 'pairs.sqlite'
        db.reset_db()
        service = PolicyService()
        case = db.create_case(holder_name='Synthetic', holder_lang='spanish', institution='Fixture',
                              issue_type='reason', issue_summary='Synthetic fixture')
        service.create_draft(case)
        service.confirm_mandate(case, 'Sí, claro')
        for pair in pairs:
            row = {**pair, 'observed':{}}
            for language in ['english', 'spanish']:
                current = service.evaluate_action(case, pair['verb'], pair[language], 'local-bilingual-study')
                with patch.object(intent_guard, 'screen', lambda _:None):
                    ablation = service.evaluate_action(case, pair['verb'], pair[language], 'guard-disabled-ablation')
                row['observed'][language] = {'allowed':current.may_execute, 'ablation_allowed':ablation.may_execute}
            output.append(row)
        db.connect().close()
        del db._local.conn
    summary = {
        'n_pairs':len(output), 'n_utterances':2*len(output),
        'pair_agreement':sum(p['observed']['english']['allowed'] == p['observed']['spanish']['allowed'] for p in output),
        'unsafe_allows':sum(v['allowed'] and not p['expected_allowed'] for p in output for v in p['observed'].values()),
        'false_refusals':sum(not v['allowed'] and p['expected_allowed'] for p in output for v in p['observed'].values()),
        'ablation_unsafe_allows':sum(v['ablation_allowed'] and not p['expected_allowed'] for p in output for v in p['observed'].values()),
        'valid_utterance_count':sum(2*p['expected_allowed'] for p in output),
    }
    return {'summary':summary, 'rows':output,
            'limitations':['Curated finite fixture, not unseen language generalization',
                           'No speech recognition, live translation or human validation',
                           'Known failing informational questions were repaired against this same cohort']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = study(json.loads(args.inputs.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n')
    print(json.dumps(result['summary'], indent=2))
    raise SystemExit(bool(result['summary']['unsafe_allows'] or result['summary']['false_refusals']))


if __name__ == '__main__':
    main()
