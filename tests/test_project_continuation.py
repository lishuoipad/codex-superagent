import copy
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'skills/codex-superagent/scripts'))
from project_continuation import digest, inspect_project, read_json, verify_reference


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.proof = self.root / 'owner-evidence.json'
        self.proof.write_text('{"fixture":"owner-reviewed"}', encoding='utf-8')
        self.ref = {'path': str(self.proof), 'sha256': hashlib.sha256(self.proof.read_bytes()).hexdigest()}
        self.plan = {'workObjective': {'scope': ['G4']}, 'tasks': [self.task('G4-01')]}
        self.ledger = {'continuation': {'schema': 1, 'readyNextTasks': []}}
        self.team = {'technicalTaskOwner': 'w1', 'businessRecordWriter': 'w1', 'routingOwner': 'router',
                     'development': {'enabled': True}}
        self.runtime = {'developmentEnabled': True, 'maxInUseWorkers': 4, 'router': {'threadId': 'router'},
                        'workers': [{'threadId': 'w1', 'taskBindingId': 'current', 'state': 'waiting'},
                                    {'threadId': 'w2', 'taskBindingId': 'previous', 'state': 'completed'}]}
    def tearDown(self):
        self.tmp.cleanup()
    def task(self, key, implementation='not_started', acceptance='pending', **kw):
        return {'id': key, 'phase': key.split('-')[0], 'title': key, 'status': 'planned',
                'developmentDependsOn': [], 'acceptanceDependsOn': [],
                'progressState': {'implementation': implementation, 'acceptance': acceptance,
                                  'basis': 'Owner reviewed fixture'}, **kw}
    def ready(self, key='G4-01', mode='development', **kw):
        def find(items):
            for item in items:
                if item['id'] == key: return item
                child = find(item.get('subtasks', []))
                if child: return child
        item = find(self.plan['tasks'])
        entry = {'taskId': key, 'mode': mode, 'planUnitSha256': digest(item),
                 'reviewedBy': 'w1', 'evidenceRef': self.ref, **kw}
        self.ledger['continuation']['readyNextTasks'].append(entry)
        return entry
    def report(self, scope=None):
        return inspect_project(self.plan, self.ledger, self.team, self.runtime, scope)
    def accepted(self, key, **kw):
        return self.task(key, 'integrated_pending_acceptance', 'passed', evidence=[self.ref], **kw)
    def completion_review(self, scope=None):
        report = self.report(scope)
        self.ledger['continuation']['completionReview'] = {
            'result': 'passed', 'planSha256': digest(self.plan), 'scopePhases': report['scopePhases'],
            'requiredTaskIds': report['completion']['requiredTaskIds'], 'remainingRequiredItems': [],
            'unresolvedDefects': [], 'reviewedBy': 'w1', 'evidenceRef': self.ref}
    def rejection(self):
        return self.report()['rejectedDeclarations'][0]['reasons']

    def test_legacy_complete_does_not_imply_accepted(self):
        self.plan['tasks'][0].pop('progressState')
        self.plan['tasks'][0]['status'] = 'complete'
        report = self.report()
        self.assertFalse(report['completion']['complete'])
        self.assertEqual(report['units'][0]['implementation'], 'unknown')
        self.assertEqual(report['nextAction']['kind'], 'request_owner_review')

    def test_current_progress_wins_over_legacy_planned(self):
        self.plan['tasks'] = [self.accepted('G4-01')]
        for worker in self.runtime['workers']: worker['state'] = 'completed'
        self.completion_review()
        self.assertTrue(self.report()['completion']['complete'])

    def test_development_does_not_wait_for_acceptance_dependency(self):
        self.plan['tasks'] += [self.task('G4-02')]
        self.plan['tasks'][0]['acceptanceDependsOn'] = ['G4-02']
        self.ready()
        self.assertEqual(self.report()['nextAction']['kind'], 'route_ready')

    def test_acceptance_checks_its_distinct_dependencies(self):
        self.plan['tasks'][0]['progressState']['implementation'] = 'integrated_pending_acceptance'
        self.plan['tasks'] += [self.task('G4-02')]
        self.plan['tasks'][0]['acceptanceDependsOn'] = ['G4-02']
        self.ready(mode='acceptance')
        self.assertIn('unverified_acceptance_dependency:G4-02', self.rejection())

    def test_unknown_dependency_is_not_silently_satisfied(self):
        self.plan['tasks'][0]['developmentDependsOn'] = ['missing']
        self.ready()
        self.assertIn('unknown_development_dependency:missing', self.rejection())
        self.assertEqual(self.report()['nextAction']['kind'], 'request_owner_review')

    def test_duplicate_plan_ids_are_an_error(self):
        self.plan['tasks'].append(copy.deepcopy(self.plan['tasks'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'): self.report()

    def test_duplicate_dependency_is_normalized(self):
        self.plan['tasks'] += [self.accepted('G3-01')]
        self.plan['tasks'][0]['developmentDependsOn'] = ['G3-01', 'G3-01']
        self.ready()
        self.assertEqual(len(self.report()['readyCandidates']), 1)

    def test_waiting_worker_does_not_block_another_existing_worker(self):
        self.ready()
        report = self.report()
        self.assertEqual(report['inUseWorkers'], 1)
        self.assertEqual(report['nextAction']['eligibleExistingWorkerIds'], ['w2'])
        self.assertEqual(report['unfinishedBindings'][0]['state'], 'waiting')

    def test_available_slot_does_not_create_chat_or_release_waiting(self):
        self.runtime['workers'][1]['state'] = 'waiting'
        self.ready()
        report = self.report()
        self.assertEqual(report['inUseWorkers'], 2)
        self.assertEqual(report['nextAction']['kind'], 'waiting_for_existing_capacity')

    def test_live_binding_stays_with_original_owner(self):
        self.ready(taskBindingId='current')
        report = self.report()
        self.assertEqual(report['nextAction']['kind'], 'continue_owner')
        self.assertEqual(report['nextAction']['candidate']['ownerThreadId'], 'w1')
        self.assertEqual(report['inUseWorkers'], 1)

    def test_binding_cannot_be_released_or_redirected_by_declaration(self):
        self.ready(taskBindingId='current', ownerThreadId='w2')
        self.assertIn('binding_owner_conflict', self.rejection())

    def test_paused_worker_remains_counted_and_is_not_woken(self):
        self.runtime['workers'][0]['state'] = 'paused'
        self.ready(taskBindingId='current')
        report = self.report()
        self.assertEqual(report['inUseWorkers'], 1)
        self.assertIn('bound_worker_paused', self.rejection())
        self.assertEqual(report['nextAction']['reason'], 'technical_owner_independently_paused')

    def test_both_live_gates_required(self):
        self.ready()
        self.team['development']['enabled'] = False
        self.assertEqual(self.report()['nextAction']['kind'], 'paused')
        self.team['development']['enabled'] = True
        self.runtime['developmentEnabled'] = False
        self.assertEqual(self.report()['nextAction']['kind'], 'paused')

    def test_independent_paused_dependency_does_not_stop_other_work(self):
        self.plan['tasks'] += [self.task('G4-02'), self.task('G4-03')]
        self.plan['tasks'][0]['developmentDependsOn'] = ['G4-02']
        self.ledger['continuation']['pausedTaskIds'] = ['G4-02']
        self.ready()
        self.ready('G4-03')
        report = self.report()
        self.assertEqual(report['nextAction']['candidate']['taskId'], 'G4-03')
        self.assertEqual(report['pausedTaskIds'], ['G4-02'])
        self.assertFalse(report['completion']['complete'])

    def test_child_inherits_development_dependencies(self):
        child = self.task('G4-01.child')
        child.pop('phase')
        self.plan['tasks'][0]['subtasks'] = [child]
        self.plan['tasks'][0]['developmentDependsOn'] = ['missing']
        self.ready('G4-01.child')
        self.assertIn('unknown_development_dependency:missing', self.rejection())

    def test_parent_is_not_ready_when_required_child_is_unfinished(self):
        self.plan['tasks'][0]['progressState']['implementation'] = 'integrated_pending_acceptance'
        self.plan['tasks'][0]['subtasks'] = [self.task('G4-01.child')]
        self.ready(mode='acceptance')
        self.assertIn('implementation_not_reported_ready_for_acceptance', self.rejection())

    def test_parent_and_child_are_not_both_selected(self):
        self.plan['tasks'][0]['subtasks'] = [self.task('G4-01.child')]
        self.ready(priority=5)
        self.ready('G4-01.child', priority=1)
        self.assertEqual([c['taskId'] for c in self.report()['readyCandidates']], ['G4-01.child'])

    def test_readiness_cannot_use_changed_plan_or_changed_evidence(self):
        self.ready()
        self.plan['tasks'][0]['title'] = 'changed scope'
        self.assertIn('stale_plan_unit', self.rejection())
        self.plan['tasks'][0]['title'] = 'G4-01'
        self.proof.write_text('changed', encoding='utf-8')
        self.assertIn('missing_or_changed_owner_evidence', self.rejection())

    def test_router_cannot_supply_technical_readiness_review(self):
        self.ready(reviewedBy='router')
        self.assertIn('current_technical_owner_required', self.rejection())

    def test_input_evidence_can_confirm_legacy_dependency_without_rewriting_history(self):
        earlier = self.task('G3-01')
        earlier.pop('progressState'); earlier['status'] = 'complete'
        self.plan['tasks'].append(earlier)
        self.plan['tasks'][0]['developmentDependsOn'] = ['G3-01']
        self.ready(dependencyEvidence=[{'taskId':'G3-01', 'kind':'development',
                    'planUnitSha256':digest(earlier), 'evidenceRef':self.ref}])
        self.assertEqual(self.report()['nextAction']['kind'], 'route_ready')
        self.assertNotIn('progressState', earlier)

    def test_development_dependency_cycle_needs_owner_repair(self):
        self.plan['tasks'].append(self.task('G4-02', developmentDependsOn=['G4-01']))
        self.plan['tasks'][0]['developmentDependsOn'] = ['G4-02']
        self.ready()
        self.assertIn('development_dependency_cycle', self.rejection())

    def test_acceptance_cycle_does_not_prevent_development(self):
        self.plan['tasks'].append(self.task('G4-02', acceptanceDependsOn=['G4-01']))
        self.plan['tasks'][0]['acceptanceDependsOn'] = ['G4-02']
        self.ready()
        self.assertEqual(self.report()['nextAction']['kind'], 'route_ready')

    def test_release_or_publish_declaration_only_blocks_itself(self):
        self.plan['tasks'].append(self.task('G4-02'))
        self.ready(actionClass='publish')
        self.ready('G4-02')
        self.assertEqual(self.report()['nextAction']['candidate']['taskId'], 'G4-02')

    def test_complete_requires_entire_explicit_g4_through_g8_scope(self):
        scope = ['G4','G5','G6','G7','G8']
        self.plan['tasks'] = [self.accepted(phase+'-01') for phase in scope]
        for worker in self.runtime['workers']: worker['state'] = 'completed'
        self.completion_review(scope)
        self.assertTrue(self.report(scope)['completion']['complete'])
        self.plan['tasks'][-1]['progressState']['acceptance'] = 'pending'
        self.completion_review(scope)
        self.assertFalse(self.report(scope)['completion']['complete'])
        self.assertIn('G8-01', self.report(scope)['completion']['remainingTaskIds'])

    def test_idle_or_complete_plan_without_owner_review_is_not_project_complete(self):
        self.plan['tasks'] = [self.accepted('G4-01')]
        for worker in self.runtime['workers']: worker['state'] = 'completed'
        self.assertFalse(self.report()['completion']['complete'])
        self.completion_review()
        self.assertTrue(self.report()['completion']['complete'])
        self.runtime['workers'][0]['state'] = 'waiting'
        self.assertFalse(self.report()['completion']['complete'])

    def test_pause_is_not_implicitly_cleared_by_completion(self):
        self.plan['tasks'] = [self.accepted('G4-01')]
        for worker in self.runtime['workers']: worker['state'] = 'completed'
        self.completion_review()
        self.ledger['continuation']['pausedTaskIds'] = ['G4-01']
        self.assertFalse(self.report()['completion']['complete'])

    def test_exclusion_needs_exact_explicit_current_states_and_basis(self):
        self.plan['tasks'][0]['progressState'] = {'implementation':'excluded',
            'acceptance':'not_applicable_for_exact_excluded_scope', 'basis':'Explicit exact scope review'}
        self.assertEqual(self.report()['completion']['requiredTaskIds'], [])
        self.plan['tasks'][0]['progressState']['basis'] = ''
        self.assertEqual(self.report()['completion']['requiredTaskIds'], ['G4-01'])

    def test_scope_is_not_guessed(self):
        with self.assertRaisesRegex(ValueError, 'missing'): self.report(['G4','G8'])
        self.assertEqual(self.report()['scopePhases'], ['G4'])

    def test_full_completion_does_not_return_next_work(self):
        self.plan['tasks'] = [self.accepted('G4-01')]
        for worker in self.runtime['workers']: worker['state'] = 'completed'
        self.completion_review()
        self.assertEqual(self.report()['nextAction']['kind'], 'complete')

    def test_next_task_selected_after_previous_binding_is_released(self):
        self.runtime['workers'][1]['state'] = 'waiting'
        self.ready()
        self.assertEqual(self.report()['nextAction']['kind'], 'waiting_for_existing_capacity')
        self.runtime['workers'][1]['state'] = 'completed'
        self.assertEqual(self.report()['nextAction']['kind'], 'route_ready')

    def test_conflicting_duplicate_readiness_does_not_choose_arbitrary_first(self):
        self.ready(priority=1)
        self.ready(priority=2)
        report = self.report()
        self.assertEqual(report['readyCandidates'], [])
        self.assertEqual(report['rejectedDeclarations'][0]['reason'], 'duplicate_readiness_declaration')

    def test_nonfinite_priority_is_rejected(self):
        self.ready(priority=float('nan'))
        self.assertIn('invalid_priority', self.rejection())
        self.ledger['continuation']['readyNextTasks'][0]['priority'] = float('inf')
        self.assertIn('invalid_priority', self.rejection())

    def test_structured_invalid_task_id_is_rejected_without_crash(self):
        self.ledger['continuation']['readyNextTasks'] = [{'taskId': [], 'mode': 'development'}]
        self.assertEqual(self.report()['readyCandidates'], [])

    def test_all_accepted_missing_review_requests_specific_completion_review(self):
        self.plan['tasks'] = [self.accepted('G4-01')]
        for worker in self.runtime['workers']: worker['state'] = 'completed'
        action = self.report()['nextAction']
        self.assertEqual(action['kind'], 'request_completion_review')
        self.assertEqual(action['ownerThreadId'], 'w1')

    def test_all_accepted_unreleased_binding_requests_owner_closeout(self):
        self.plan['tasks'] = [self.accepted('G4-01')]
        action = self.report()['nextAction']
        self.assertEqual(action['kind'], 'request_owner_closeout')
        self.assertEqual(action['unfinishedBindings'][0]['taskBindingId'], 'current')
        self.assertFalse(self.report()['completion']['complete'])

    def test_omitted_binding_id_keeps_same_id_live_owner(self):
        self.runtime['workers'][0]['taskBindingId'] = 'G4-01'
        self.ready()
        action = self.report()['nextAction']
        self.assertEqual(action['kind'], 'continue_owner')
        self.assertEqual(action['candidate']['ownerThreadId'], 'w1')
        self.assertEqual(action['candidate']['taskBindingId'], 'G4-01')

    def test_explicit_binding_cannot_override_same_id_live_owner(self):
        self.runtime['workers'][0]['taskBindingId'] = 'G4-01'
        self.runtime['workers'][1]['state'] = 'waiting'
        self.ready(taskBindingId='previous')
        self.assertIn('known_live_binding_conflict', self.rejection())

    def test_two_modes_for_one_task_are_not_two_ready_tasks(self):
        self.ready(mode='development')
        self.ready(mode='review')
        report = self.report()
        self.assertEqual(report['readyCandidates'], [])
        self.assertTrue(all(r['reason'] == 'conflicting_modes_for_task' for r in report['rejectedDeclarations']))

    def test_unknown_paused_task_id_is_a_visible_error(self):
        self.ledger['continuation']['pausedTaskIds'] = ['not-a-plan-id']
        self.ready()
        with self.assertRaisesRegex(ValueError, 'unknown: not-a-plan-id'):
            self.report()

    def test_absolute_bounded_reference_and_json_inputs(self):
        self.assertTrue(verify_reference(self.ref))
        self.assertFalse(verify_reference({'path':'relative.json', 'sha256':self.ref['sha256']}))
        self.assertEqual(read_json(self.proof), {'fixture':'owner-reviewed'})
        with self.assertRaises(ValueError): read_json('relative.json')


if __name__ == '__main__':
    unittest.main()
