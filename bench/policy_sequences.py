"""Offline policy state-machine study with frozen operation inputs and raw receipts."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from time import perf_counter_ns
from unittest.mock import patch

from scopelock.core import consult, db
from scopelock.core.policy import PolicyService


@dataclass
class Reference:
    now: float = 1000
    version: int = 0
    expires: float = 0
    revoked: bool = False
    decisions: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)

    def active(self):
        return self.version > 0 and not self.revoked and self.now < self.expires

    def step(self, op):
        kind = op['op']
        if kind == 'confirm':
            if not self.active():
                self.version += 1
                self.expires = self.now + 5
                self.revoked = False
        elif kind == 'tick':
            self.now += op['seconds']
        elif kind == 'revoke':
            if not self.version:
                self.version = 1
            self.revoked = True
            for decision in self.decisions.values():
                if decision['status'] == 'pending':
                    decision['status'] = 'cancelled'
        elif kind == 'request':
            self.decisions[op['id']] = {'status':'pending','version':self.version,
                                        'verb':op['verb'],'yes':False}
        elif kind == 'answer':
            decision = self.decisions.get(op['id'])
            if decision and decision['status'] == 'pending':
                if self.active():
                    decision['status'] = 'resolved'
                    decision['yes'] = op['yes_es'] and op['yes_en']
                else:
                    decision['status'] = 'cancelled'
        elif kind == 'restart':
            for decision in self.decisions.values():
                if decision['status'] == 'pending':
                    decision['status'] = 'cancelled'
        elif kind == 'effect':
            signature = (op['verb'], op.get('decision'))
            if op['key'] in self.effects:
                return 'duplicate' if self.effects[op['key']] == signature else 'refused'
            if not self.active() or op['verb'] not in {'request_ref','escalate'}:
                return 'refused'
            if op['verb'] == 'escalate':
                decision = self.decisions.get(op.get('decision'))
                if not decision or decision['status'] != 'resolved' or not decision['yes'] or decision['verb'] != op['verb'] or decision['version'] != self.version:
                    return 'refused'
                if any(sig[1] == op.get('decision') for sig in self.effects.values()):
                    return 'refused'
            elif op.get('decision') is not None:
                return 'refused'
            self.effects[op['key']] = signature
            return 'committed'
        return None


def execute(sequence, database):
    existing = getattr(db._local, 'conn', None)
    if existing is not None:
        existing.close()
        del db._local.conn
    db.DB_PATH = database
    db.reset_db()
    consult.PENDING.clear()
    case = db.create_case(holder_name='Synthetic Holder', holder_lang='spanish',
                          institution='Synthetic Institution', issue_type='request', issue_summary='Fixture')
    service = PolicyService()
    service.create_draft(case)
    reference = Reference()
    ids = {}
    rows = []
    with patch.object(db, 'epoch', lambda: reference.now):
        for index, op in enumerate(sequence['operations']):
            expected = reference.step(op)
            start = perf_counter_ns()
            actual = None
            if op['op'] == 'confirm':
                service.confirm_mandate(case, 'Sí, claro', ttl_seconds=5)
            elif op['op'] == 'revoke':
                service.revoke_mandate(case, 'Synthetic withdrawal', 'study')
            elif op['op'] == 'request':
                ids[op['id']] = consult.request_holder_decision(case, op['verb'], 'Confirm?', '¿Confirma?')
            elif op['op'] == 'answer' and op['id'] in ids:
                try:
                    consult.resolve_holder_decision(ids[op['id']], 'Sí, claro' if op['yes_es'] else 'No, gracias',
                                                    'Yes' if op['yes_en'] else 'No', 10)
                except consult.DecisionNotPending:
                    pass
            elif op['op'] == 'restart':
                db.connect().close()
                del db._local.conn
                consult.recover_after_restart()
            elif op['op'] == 'effect':
                result = service.authorize_effect(case, op['verb'], 'Reference request',
                    'study', op['key'], ids.get(op.get('decision')))
                actual = result.status
            effects = db.connect().execute('SELECT COUNT(*) FROM action_effect WHERE case_id=?',(case,)).fetchone()[0]
            row = {'step':index,'operation':op,'expected':expected,'actual':actual,
                   'expected_effect_count':len(reference.effects),'actual_effect_count':effects,
                   'wall_ns':perf_counter_ns()-start}
            row['matches'] = expected == actual and effects == len(reference.effects)
            rows.append(row)
    db.connect().close()
    del db._local.conn
    return {'sequence_id':sequence['sequence_id'],'rows':rows,
            'all_match':all(row['matches'] for row in rows)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    inputs = json.loads(args.inputs.read_text())
    sequences = inputs['sequences']
    if len(sequences)<1000 or len({s['sequence_id'] for s in sequences}) != len(sequences):
        parser.error('Requires at least 1000 distinct frozen sequences')
    with tempfile.TemporaryDirectory(prefix='scopelock-study-') as tmp:
        raw = [execute(s, Path(tmp)/'study.sqlite') for s in sequences]
    steps = [row for sequence in raw for row in sequence['rows']]
    effects = [row for row in steps if row['expected'] is not None]
    summary = {'n_synthetic_sequences':len(raw),'n_operations':len(steps),
               'sequences_matching_reference':sum(r['all_match'] for r in raw),
               'effect_comparisons':len(effects),'operation_mismatches':sum(not r['matches'] for r in steps),
               'unauthorized_new_effects':sum(r['actual']=='committed' and r['expected']!='committed' for r in effects),
               'valid_effect_controls':sum(r['expected']=='committed' for r in effects),
               'overblocked_effects':sum(r['expected']=='committed' and r['actual']!='committed' for r in effects),
               'duplicate_effect_writes':sum(r['actual_effect_count']>r['expected_effect_count'] for r in steps)}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'summary':summary,'rows':raw},indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    raise SystemExit(bool(summary['operation_mismatches']))


if __name__=='__main__':
    main()
