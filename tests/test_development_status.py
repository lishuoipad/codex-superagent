"""Synthetic read-only panel tests; never inspect the operator's Codex data."""
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

MODULE = pathlib.Path(__file__).resolve().parents[1] / 'tools/development-status/development_status.py'
spec = importlib.util.spec_from_file_location('relay_status_panel', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def make_fixture(root):
    checkpoints = root / 'checkpoints'; checkpoints.mkdir()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    log = root / 'main.jsonl'
    log.write_text(json.dumps({'type':'event_msg','timestamp':stamp,'payload':{'type':'task_started'}})+'\n', encoding='utf-8')
    child_log = root / 'child.jsonl'
    child_log.write_text('', encoding='utf-8')
    db = root / 'state.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, agent_path TEXT, agent_nickname TEXT, updated_at INTEGER, archived INTEGER)')
        conn.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
            ('sample-main',str(log),'','/root',None,0,0),
            ('sample-child',str(child_log),json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'sample-main'}}}),'/root/ui','layout',0,0)])
    conn.close()
    (checkpoints/'sample-main.json').write_text(json.dumps({'task':'UI-01 窗口检查，已交付待接收','updated_at':stamp,'next_actions':['核对受影响项']}),encoding='utf-8')
    (root/'plan.json').write_text(json.dumps({'tasks':[{'id':'UI-01','title':'界面检查','status':'in-progress','acceptance':'未测范围保留'}]}),encoding='utf-8')
    settings = {'state_db':'state.sqlite','checkpoints':'checkpoints','plan':'plan.json','title':'示例项目 · 开发状态','threads':[{'id':'sample-main','name':'主开发对话'}], 'timezone_offset_hours':8,
                'rules':[{'pattern':'窗口','summary':'检查界面','plan_ids':['UI-01'],'validation':'只读检查；未代表验收通过'}],
                'plan_id_pattern':r'\b(?:UI|BUILD)-\d+\b','text_replacements':{'Root':'主协调对话'}}
    config = root/'project.local.json'; config.write_text(json.dumps(settings,ensure_ascii=False),encoding='utf-8')
    return config


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.config = make_fixture(self.root)
        m.configure(self.config)

    def test_paths_are_relative_to_profile(self):
        self.assertEqual(m.DB,self.root/'state.sqlite')
        self.assertEqual(m.CHECKPOINTS,self.root/'checkpoints')

    def test_rules_are_association_not_acceptance(self):
        self.assertEqual(m.describe('检查窗口')[1],['UI-01'])
        self.assertIn('未代表验收',m.describe('检查窗口')[2])

    def test_explicit_number_without_rule(self):
        self.assertEqual(m.describe('BUILD-02 prepare package')[1],['BUILD-02'])

    def test_unknown_work_has_no_invented_number(self):
        self.assertEqual(m.describe('unclassified work')[1],[])

    def test_first_matching_rule_wins(self):
        x=json.loads(self.config.read_text(encoding='utf-8'))
        x['rules'].append({'pattern':'窗口','summary':'other','plan_ids':['OTHER-01'],'validation':'other'})
        self.config.write_text(json.dumps(x),encoding='utf-8');m.configure(self.config)
        self.assertEqual(m.describe('窗口')[1],['UI-01'])

    def test_duplicate_thread_rejected(self):
        x=json.loads(self.config.read_text(encoding='utf-8'));x['threads']*=2
        self.config.write_text(json.dumps(x),encoding='utf-8')
        with self.assertRaises(ValueError):m.configure(self.config)

    def test_bad_regex_rejected(self):
        x=json.loads(self.config.read_text(encoding='utf-8'));x['rules'][0]['pattern']='['
        self.config.write_text(json.dumps(x),encoding='utf-8')
        with self.assertRaises(m.re.error):m.configure(self.config)

    def test_pause_delivery_and_unknown_are_distinct(self):
        now=dt.datetime.now(m.TZ)
        self.assertEqual(m.status_from({'task':'原Goal仍paused'}, {}, {}, now)[1],'paused')
        self.assertEqual(m.status_from({'task':'已交付待接收'}, {}, {}, now)[1],'delivered')
        self.assertEqual(m.status_from({}, {}, {}, now)[1],'unknown')

    def test_ended_and_aborted_events(self):
        now=dt.datetime.now(m.TZ)
        self.assertEqual(m.status_from({}, {'kind':'task_complete'}, {}, now)[1],'ended')
        self.assertEqual(m.status_from({}, {'kind':'turn_aborted'}, {}, now)[1],'paused')

    def test_snapshot_preserves_input_bytes(self):
        sources=[p for p in self.root.rglob('*') if p.is_file()]
        before={p:hashlib.sha256(p.read_bytes()).digest() for p in sources}
        result=m.Collector(self.config).snapshot()
        after={p:hashlib.sha256(p.read_bytes()).digest() for p in sources}
        self.assertEqual(before,after)
        self.assertEqual(result['known_count'],1)
        all_rows=result['rows']+result['history']
        self.assertEqual({x['id'] for x in all_rows},{'sample-main','sample-child'})
        main=next(x for x in all_rows if x['id']=='sample-main')
        self.assertEqual(main['state'],'delivered')
        self.assertEqual(main['plan_entries'][0]['id'],'UI-01')

    def test_missing_plan_is_warning_not_completion(self):
        (self.root/'plan.json').unlink()
        result=m.Collector(self.config).snapshot()
        self.assertTrue(result['warnings'])
        self.assertEqual(result['rows'][0]['plan_entries'],[])

    def test_tail_only_exposes_public_assistant_messages(self):
        path=self.root/'filter.jsonl'
        rows=[{'type':'response_item','payload':{'type':'reasoning','text':'private-test-sentinel'}},
              {'type':'response_item','payload':{'type':'message','role':'user','phase':'commentary','content':[{'type':'text','text':'user-test-sentinel'}]}},
              {'type':'response_item','payload':{'type':'function_call_output','output':'tool-test-sentinel'}},
              {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'commentary','content':[{'type':'output_text','text':'public update'}]}}]
        path.write_text('\n'.join(json.dumps(x) for x in rows)+'\n',encoding='utf-8')
        tail=m.Tail(path,4096);tail.read();tail.read()
        self.assertEqual([x['text'] for x in tail.messages],['public update'])
        self.assertNotIn('sentinel',json.dumps(list(tail.messages)))

    def test_tail_appends_incrementally(self):
        path=self.root/'incremental.jsonl';path.write_text('',encoding='utf-8');tail=m.Tail(path,4096);tail.read()
        row={'type':'event_msg','timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),'payload':{'type':'task_complete'}}
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps(row)+'\n')
        tail.read();self.assertEqual(tail.event['kind'],'task_complete')

    def test_incompatible_schema_warns_and_releases_database(self):
        with sqlite3.connect(self.root/'state.sqlite') as conn:
            conn.execute('DROP TABLE threads')
        conn.close()
        result=m.Collector(self.config).snapshot()
        self.assertTrue(result['warnings'])
        (self.root/'state.sqlite').unlink()

    def test_catalog_limit_stays_bounded(self):
        conn=sqlite3.connect(self.root/'state.sqlite')
        try:
            for i in range(130):
                conn.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',(f'child-{i}',str(self.root/'child.jsonl'),json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'sample-main'}}}),'/root/child',str(i),i,0))
            conn.commit()
        finally:conn.close()
        result=m.Collector(self.config).snapshot()
        self.assertEqual(len(result['rows'])+len(result['history']),120)
        self.assertTrue(result['warnings'])

    def test_cli_requires_explicit_config(self):
        x=subprocess.run([sys.executable,'-B',str(MODULE),'--once'],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(x.returncode,0)
        self.assertIn('--config',x.stderr)

    def test_cli_snapshot_and_clean_quit(self):
        x=subprocess.run([sys.executable,'-B','-X','utf8',str(MODULE),'--config',str(self.config)],input='snapshot\nquit\n',capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(x.returncode,0,x.stderr)
        self.assertEqual(len(x.stdout.splitlines()),1)
        self.assertEqual(json.loads(x.stdout)['known_count'],1)


    def dynamic_fixture(self, states=('paused','waiting'), enabled=False):
        settings=json.loads(self.config.read_text(encoding='utf-8'))
        settings.pop('threads')
        settings['team_registry']='team/team.json'
        self.config.write_text(json.dumps(settings),encoding='utf-8')
        folder=self.root/'team';folder.mkdir(exist_ok=True)
        workers=[{'threadId':f'worker-{i+1}','title':f'Worker {i+1}','taskBindingId':f'JOB-{i+1}','state':state} for i,state in enumerate(states)]
        team={'newThreads':{'main':{'threadId':'sample-main','title':'Router','role':'router'},
                           **{f'worker{i+1}':dict(w,role=f'worker{i+1}') for i,w in enumerate(workers)}},
              'runtime':{'statusPath':'runtime.json'},'development':{'enabled':enabled},
              'workerPolicy':{'maxInUseWorkers':4}}
        runtime={'schema':1,'developmentEnabled':enabled,'maxInUseWorkers':4,'updatedAt':'2026-11-03T10:00:00+08:00',
                 'router':{'threadId':'sample-main','title':'Router'},'workers':workers}
        (folder/'team.json').write_text(json.dumps(team),encoding='utf-8')
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        with sqlite3.connect(self.root/'state.sqlite') as conn:
            conn.executemany('INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?)',
                            [(w['threadId'],str(self.root/'child.jsonl'),'','/root',None,0,0) for w in workers])
        conn.close()
        return folder,team,runtime

    def test_dynamic_roster_uses_relative_runtime_path(self):
        folder,team,runtime=self.dynamic_fixture()
        result=m.Collector(self.config).snapshot()
        self.assertEqual(result['control']['runtime_path'],str(folder/'runtime.json'))
        self.assertEqual(result['control']['in_use_workers'],2)
        self.assertEqual(result['control']['worker_limit'],4)
        self.assertEqual(result['control']['router_count'],1)
        root=next(x for x in result['rows'] if x['id']=='sample-main')
        self.assertEqual(root['name'],'Router')
        self.assertEqual(root['status'],'待命：用户暂停开发')
        self.assertTrue(all(r['state']=='paused' for r in result['rows'] if r['kind']=='对话'))
        self.assertEqual(next(r for r in result['rows'] if r['id']=='worker-1')['task_binding_id'],'JOB-1')

    def test_runtime_roster_changes_without_catalog_wait(self):
        folder,team,runtime=self.dynamic_fixture()
        collector=m.Collector(self.config);collector.snapshot()
        runtime['workers']=[{'threadId':'worker-new','title':'New Worker','taskBindingId':'NEW-1','state':'waiting'}]
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        result=collector.snapshot()
        ids={r['id'] for r in result['rows']+result['history']}
        self.assertIn('worker-new',ids)
        self.assertNotIn('worker-1',ids)
        row=next(r for r in result['rows'] if r['id']=='worker-new')
        self.assertTrue(row['source_error'])
        self.assertEqual(row['state'],'paused')

    def test_completed_workers_and_descendants_are_history(self):
        folder,team,runtime=self.dynamic_fixture(('completed','waiting'),True)
        with sqlite3.connect(self.root/'state.sqlite') as conn:
            conn.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',('finished-child',str(self.root/'main.jsonl'),json.dumps({'subagent':{'thread_spawn':{'parent_thread_id':'worker-1'}}}),'/root/child','finished',0,0))
        conn.close()
        result=m.Collector(self.config).snapshot()
        history={r['id']:r for r in result['history']}
        self.assertEqual(history['worker-1']['state'],'completed')
        self.assertIn('finished-child',history)
        self.assertEqual(result['control']['in_use_workers'],1)
        self.assertEqual(result['control']['completed_workers'],1)
        self.assertEqual(next(r for r in result['rows'] if r['id']=='worker-2')['state'],'waiting')

    def test_excess_workers_remain_visible_with_warning(self):
        self.dynamic_fixture(('paused','waiting','active','waiting','paused'))
        result=m.Collector(self.config).snapshot()
        self.assertEqual(result['control']['in_use_workers'],5)
        self.assertEqual(sum(r['role']=='worker' for r in result['rows']),5)
        self.assertTrue(any('超过登记上限 4' in w for w in result['warnings']))

    def test_missing_runtime_does_not_clear_pause(self):
        folder,team,runtime=self.dynamic_fixture(enabled=True)
        runtime['developmentEnabled']=False
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        collector=m.Collector(self.config);self.assertFalse(collector.snapshot()['control']['development_enabled'])
        (folder/'runtime.json').unlink()
        result=collector.snapshot()
        self.assertFalse(result['control']['development_enabled'])
        self.assertTrue(result['control']['read_error'])
        self.assertTrue(all(r['state']=='paused' for r in result['rows'] if r['role']))

    def test_conflicting_gate_uses_explicit_pause(self):
        folder,team,runtime=self.dynamic_fixture(enabled=False)
        runtime['developmentEnabled']=True
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        result=m.Collector(self.config).snapshot()
        self.assertFalse(result['control']['development_enabled'])
        self.assertTrue(any('开关记录不一致' in w for w in result['warnings']))

    def test_registry_pause_state_is_authoritative(self):
        folder,team,runtime=self.dynamic_fixture(enabled=True)
        team['development']['state']='paused_by_user'
        (folder/'team.json').write_text(json.dumps(team),encoding='utf-8')
        result=m.Collector(self.config).snapshot()
        self.assertFalse(result['control']['development_enabled'])

    def test_malformed_runtime_falls_back_without_inventing_completion(self):
        folder,team,runtime=self.dynamic_fixture(enabled=True)
        runtime['workers'].append(runtime['workers'][0])
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        result=m.Collector(self.config).snapshot()
        self.assertTrue(result['control']['read_error'])
        self.assertIsNone(result['control']['development_enabled'])
        self.assertFalse(any(r['state']=='completed' for r in result['rows']+result['history']))
        self.assertEqual(result['control']['source'],'team_registry')

    def test_missing_bound_thread_stays_visible_unknown(self):
        folder,team,runtime=self.dynamic_fixture(enabled=True)
        runtime['workers']=[{'threadId':'not-in-database','title':'Not yet registered','state':'unknown'}]
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        result=m.Collector(self.config).snapshot()
        row=next(r for r in result['rows'] if r['id']=='not-in-database')
        self.assertEqual(row['state'],'unknown')
        self.assertTrue(row['source_error'])

    def test_host_end_is_not_registered_task_completion(self):
        folder,team,runtime=self.dynamic_fixture(('active',),True)
        (self.root/'child.jsonl').write_text(json.dumps({'type':'event_msg','payload':{'type':'task_complete'}})+'\n',encoding='utf-8')
        result=m.Collector(self.config).snapshot()
        row=next(r for r in result['rows'] if r['id']=='worker-1')
        self.assertEqual(row['state'],'working')
        self.assertIn('状态记录',row['status'])
        self.assertEqual(result['control']['completed_workers'],0)

    def test_dynamic_snapshot_preserves_every_input(self):
        self.dynamic_fixture()
        files=[p for p in self.root.rglob('*') if p.is_file()]
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        m.Collector(self.config).snapshot()
        self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in files})

    def test_explicit_runtime_override_is_profile_relative(self):
        folder,team,runtime=self.dynamic_fixture()
        path=self.root/'override.json';path.write_text(json.dumps(runtime),encoding='utf-8')
        settings=json.loads(self.config.read_text(encoding='utf-8'));settings['runtime_status']='override.json'
        self.config.write_text(json.dumps(settings),encoding='utf-8')
        self.assertEqual(m.Collector(self.config).snapshot()['control']['runtime_path'],str(path))

    def test_dynamic_control_read_is_bounded_and_local(self):
        self.dynamic_fixture()
        (self.root/'team/runtime.json').write_text(' '*(512*1024+1),encoding='utf-8')
        self.assertTrue(m.Collector(self.config).snapshot()['control']['read_error'])
        for path in ('https://example.invalid/state.json',r'\\server\share\state.json'):
            with self.assertRaises(ValueError):m.resolve_local_path(path,self.root)



    def test_valid_explicit_resume_clears_prior_pause(self):
        folder,team,runtime=self.dynamic_fixture(enabled=False)
        collector=m.Collector(self.config);self.assertFalse(collector.snapshot()['control']['development_enabled'])
        team['development']['enabled']=True;runtime['developmentEnabled']=True
        (folder/'team.json').write_text(json.dumps(team),encoding='utf-8')
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        result=collector.snapshot()
        self.assertTrue(result['control']['development_enabled'])
        self.assertFalse(result['control']['read_error'])

    def test_lost_metadata_preserves_roster_without_claiming_activity(self):
        folder,team,runtime=self.dynamic_fixture(('active',),True)
        collector=m.Collector(self.config);collector.snapshot()
        (folder/'team.json').write_text('{',encoding='utf-8')
        (folder/'runtime.json').write_text('{',encoding='utf-8')
        result=collector.snapshot()
        self.assertTrue(result['control']['read_error'])
        self.assertEqual(result['control']['source'],'last_known')
        self.assertIsNone(result['control']['development_enabled'])
        self.assertEqual(next(r for r in result['rows'] if r['id']=='worker-1')['state'],'unknown')

    def test_limit_disagreement_preserves_workers_and_warns(self):
        folder,team,runtime=self.dynamic_fixture(('active',)*5,True)
        runtime['maxInUseWorkers']=6
        (folder/'runtime.json').write_text(json.dumps(runtime),encoding='utf-8')
        result=m.Collector(self.config).snapshot()
        self.assertEqual(result['control']['worker_limit'],4)
        self.assertEqual(result['control']['in_use_workers'],5)
        self.assertTrue(any('上限记录不一致' in w for w in result['warnings']))

if __name__ == '__main__':unittest.main()
