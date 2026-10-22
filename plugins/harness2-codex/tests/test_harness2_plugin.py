import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from h2 import common as C, project as P, guard
from h2 import server
from h2.deploy import deploy, rollback
from h2.engine import ProjectAdapter, verify_pair, summarize_validation
from h2.runtime import host_config, Runtime
from h2.budget import Budget, allocation

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project';self.root.mkdir()
        self.state=Path(self.temp.name)/'state'
        self.old=C.STATE;C.STATE=self.state;self.addCleanup(setattr,C,'STATE',self.old)
        (self.root/'AGENTS.md').write_text('# Existing\nNever weaken permissions.\n')
    def test_pinned_source_integrity(self):C.verify_upstream()
    def test_validation_quotas_are_equal_and_cannot_be_stolen(self):
        limits,plan=allocation({'max_requests':560,'max_output_tokens':2293760,'max_total_tokens':50000000,'validation_repeats':5})
        budget=Budget(limits,plan)
        self.assertEqual(plan['holdout-0-baseline'],plan['holdout-4-candidate'])
        for key in limits:self.assertEqual(sum(row[key] for row in plan.values()),limits[key])
        for _ in range(plan['optimization']['requests']):
            r=budget.reserve('roll-base1',100);budget.settle(r,{'total_tokens':100})
        with self.assertRaisesRegex(ValueError,'optimization'):budget.reserve('roll-base1',100)
        self.assertEqual(budget.reserve('holdout-4-baseline',100)['output'],4096)
        self.assertEqual(budget.reserve('holdout-4-candidate',100)['output'],4096)
    def test_missing_usage_retains_reservation_and_tokens_cannot_overrun(self):
        limits,plan=allocation({'max_requests':80,'max_output_tokens':327680,'max_total_tokens':100000,'validation_repeats':2})
        budget=Budget(limits,plan);r=budget.reserve('holdout-0-baseline',5000)
        before=budget.ledger()['charged']['holdout-0-baseline']['tokens']
        budget.settle(r,None)
        self.assertEqual(budget.ledger()['charged']['holdout-0-baseline']['tokens'],before)
        with self.assertRaisesRegex(ValueError,'resource budget'):budget.reserve('holdout-0-baseline',5000)
        with self.assertRaisesRegex(ValueError,'exceeds'):budget.settle(r,{'total_tokens':1000000})
    def test_runtime_assets_survive_plugin_cache_removal_and_launcher_failure_is_invalid(self):
        import shutil
        run=self.state/'assets';run.mkdir(parents=True)
        cache=Path(self.temp.name)/'removable-cache'
        relay=cache/'scripts/h2/relay.py';relay.parent.mkdir(parents=True);relay.write_text('# fixture relay\n')
        config={'model':'test-only','sandbox_mode':'workspace-write','approval_policy':'on-request'}
        with patch('h2.runtime.ROOT',cache),patch('h2.runtime.binary_path',return_value=Path('/usr/bin/true')):
            runtime=Runtime(run,threading.Event(),config)
        try:
            shutil.rmtree(cache)
            self.assertEqual(runtime.relay.read_text(),'# fixture relay\n')
            failure={'exit_code':1,'wall_s':0.01,'stop_reason':None,'stderr':'launcher fixture failure','stdout':''}
            with patch('h2.runtime.guard.process',return_value=failure):
                result=runtime.call(self.root,'fixture','holdout-0-baseline')
            self.assertIn('before any provider request',result['infrastructure_errors'][0])
        finally:runtime.close()
    def test_task_diff_excludes_prematerialized_harness(self):
        run=self.state/'clean-deliverable';run.mkdir(parents=True)
        P.snapshot(self.root,run/'snapshot')
        class RepairRuntime:
            config={'model':'test-only'}
            def call(self,work,*args,**kwargs):
                (work/'fixed.py').write_text('fixed=True\n')
                return {'exit_code':0,'last_message':'fixture repair only'}
        adapter=ProjectAdapter(run,run/'snapshot','test',RepairRuntime())
        self.assertEqual(adapter.task_files('project-task'),run/'snapshot')
        out=adapter.execute('project-task',None,'base1')
        diff=(out/'deliverable.diff').read_text()
        self.assertIn('fixed.py',diff)
        self.assertNotIn('AGENTS.md',diff);self.assertNotIn('harness2-tools.json',diff)
    def test_budget_or_transport_failures_cannot_be_success_gain(self):
        rows=[{'trial':i//2,'kind':arm,'passed':True,'check':{'stop_reason':None}} for i,arm in enumerate(('baseline','candidate','candidate','baseline'))]
        rows[-1].update(passed=False,infrastructure_errors=['Request/output budget exhausted'])
        result=summarize_validation(rows,2)
        self.assertEqual((result['baseline_passed'],result['candidate_passed']),(1,2))
        self.assertFalse(result['comparison_valid']);self.assertFalse(result['non_regression']);self.assertFalse(result['measured_success_gain'])
        rows[-1].update(infrastructure_errors=[],agent_stop_reason='timeout')
        self.assertFalse(summarize_validation(rows,2)['comparison_valid'])
        rows[-1].update(agent_stop_reason=None,agent_exit_code=1,known_provider_usage={'responses_with_usage':0})
        self.assertFalse(summarize_validation(rows,2)['comparison_valid'])
    def test_audit_invalidation_blocks_human_approved_deploy(self):
        run_id,run=self.make_deployment()
        C.atomic(run/'audit-invalidation.json',C.seal({'reason':'budget confound','validation':{'comparison_valid':False}}))
        with self.assertRaisesRegex(ValueError,'invalidated'):
            deploy(run_id,'user-token',approve=lambda *a:True)
    def test_host_disabled_tools_cannot_be_reenabled_by_surface(self):
        run=self.state/'tool-envelope';run.mkdir(parents=True)
        P.snapshot(self.root,run/'snapshot')
        class LimitedRuntime:config={'model':'test-only','tool_envelope':{'shell':False,'apply_patch':False}}
        adapter=ProjectAdapter(run,run/'snapshot','test',LimitedRuntime())
        self.assertEqual(json.loads(adapter.original[P.TOOLS]),{'shell':False,'apply_patch':False})
        selected={**adapter.original,P.TOOLS:json.dumps({'shell':True,'apply_patch':False})}
        with self.assertRaisesRegex(ValueError,'expand'):adapter.validate_tree(selected)
    def test_project_policy_is_not_silently_removed(self):
        (self.root/'.codex').mkdir();(self.root/'.codex/config.toml').write_text('sandbox_mode="read-only"\n')
        with self.assertRaisesRegex(ValueError,'policy translation'):P.check_policy_context(self.root)
    def test_worker_links_rejected_before_host_evidence_reads(self):
        (self.root/'stolen').symlink_to('/root/.codex/auth.json')
        with self.assertRaisesRegex(ValueError,'Unsafe link'):P.reject_worker_links(self.root)
    def test_holdout_records_both_arms_without_event_collision(self):
        run=self.state/'paired';run.mkdir(parents=True)
        P.snapshot(self.root,run/'snapshot')
        class FakeRuntime:
            config={'model':'deterministic-test-only'}
            def call(self,work,*args,**kwargs):return {'exit_code':0}
        adapter=ProjectAdapter(run,run/'snapshot','test',FakeRuntime())
        selected={**adapter.original,'AGENTS.md':adapter.original['AGENTS.md']+'Run tests.\n'}
        check={'exit_code':0,'wall_s':0,'stop_reason':None}
        with patch('h2.engine.guard.check_command',return_value=check):
            result=verify_pair(adapter,selected,{'check_argv':['/usr/bin/true'],'validation_repeats':2},threading.Event())
        self.assertEqual([r['kind'] for r in result['rows']],['baseline','candidate','candidate','baseline'])
        self.assertEqual(len(C.read(run/'holdout-results.json')),4)
        self.assertTrue(result['non_regression']);self.assertFalse(result['measured_success_gain'])
        events=[json.loads(x) for x in (run/'events.jsonl').read_text().splitlines()]
        self.assertTrue(all(e['kind']=='holdout_check' for e in events))
    def test_unsupported_permission_profiles_fail_closed(self):
        home=Path(self.temp.name)/'codex';home.mkdir()
        with patch.dict(os.environ,{'CODEX_HOME':str(home)}):
            for extra in ('default_permissions="custom"','[permissions.custom]\nallow=true','profile="special"'):
                (home/'config.toml').write_text('model="configured"\n'+extra+'\n')
                with self.assertRaises(ValueError):host_config()
    def test_keep_base_can_validate_without_deployment_token(self):
        from h2 import engine
        class KeepBaseRuntime:
            def __init__(self,run,cancel,config,**kwargs):self.run,self.config=run,config
            def call(self,work,mission,label,**kwargs):
                if label.startswith('improve-select.'):
                    text='<why>fixture only</why>\n```choice\nA\n```'
                elif label.startswith('improve-'):text='<notes>keep the base</notes>'
                else:text='fixture only'
                events=self.run/(label+'.jsonl');events.write_text('')
                return {'exit_code':0,'last_message':text,'stderr':'','wall_s':0,'events':str(events)}
            def ledger(self):return {'test_only':True}
            def close(self):pass
        run=self.state/'20261022T160000-123456789abc';run.mkdir(parents=True)
        request={'project_root':str(self.root),'task':'fixture','k':1,'check_argv':['/usr/bin/true'],'validate_unchanged':True}
        with patch('h2.engine.Runtime',KeepBaseRuntime),patch('h2.engine.host_config',return_value={'model':'test-only'}):
            report=engine.experiment(run,request,threading.Event())
        self.assertEqual(report['status'],'no_adoption')
        self.assertEqual(report['changed_files'],[])
        self.assertIsNone(report['confirmation_token'])
        self.assertTrue(report['validation']['comparison_valid'])
        self.assertEqual(len(report['validation']['rows']),4)
    def test_complete_adapter_parallel_and_sequential_with_test_only_runtime(self):
        # This deterministic fixture tests integration plumbing, not model
        # quality. Production Runtime is separately exercised with paid runs.
        from h2 import engine
        class TestRuntime:
            def __init__(self,run,cancel,config,**kwargs):
                self.run,self.config,self.calls=run,config,[]
            def call(self,work,mission,label,**kwargs):
                self.calls.append(label)
                if label.startswith('improve-select.'):
                    text='<why>Only integration-test evidence.</why>\n```choice\nA\n```'
                elif label.startswith('improve-experience.'):
                    text='NO_ENTRY'
                elif label.startswith('improve-'):
                    path=work/'context/harness/AGENTS.md'
                    with path.open('a') as f:f.write('Run original checks after edits.\n')
                    text='<notes>Add original-check reminder, integration fixture only.</notes>'
                else:text='Test-only execution completed.'
                events=self.run/(label+'.jsonl');events.write_text('')
                return {'exit_code':0,'last_message':text,'stderr':'','wall_s':0,'events':str(events)}
            def ledger(self):return {'test_only':True,'invocations':len(self.calls),'cost_usd':None}
            def close(self):pass
        original=(self.root/'AGENTS.md').read_bytes()
        for index,mode in enumerate(('parallel','sequential')):
            run=self.state/f'20261022T15000{index}-123456789abc';run.mkdir(parents=True)
            request={'project_root':str(self.root),'task':'integration fixture','mode':mode,'k':2,'check_argv':['/usr/bin/true']}
            with patch('h2.engine.Runtime',TestRuntime),patch('h2.engine.host_config',return_value={'model':'test-only'}):
                report=engine.experiment(run,request,threading.Event())
            self.assertEqual(report['status'],'awaiting_confirmation')
            self.assertEqual((report['method']['ledger']['R'],report['method']['ledger']['P']),(8,6))
            self.assertEqual(len(report['validation']['rows']),4)
            self.assertTrue(report['validation']['non_regression'])
            self.assertFalse(report['validation']['measured_success_gain'])
            self.assertEqual(C.unseal(C.read(run/'report.json'))['run_id'],run.name)
            self.assertEqual((self.root/'AGENTS.md').read_bytes(),original)
    def test_real_namespace_denies_host_write_read_and_network(self):
        guard.available()
        outside=Path(self.temp.name)/'protected.txt';outside.write_text('host-private')
        script='import os,socket; from pathlib import Path; assert not Path('+repr(str(outside))+').exists(); assert not Path("/root/.codex/auth.json").exists(); Path("/work/local.txt").write_text("ok"); s=socket.socket(); s.settimeout(.1); assert s.connect_ex(("1.1.1.1",443)) != 0; print("isolated")'
        result=guard.process(guard.prefix(self.root)+['--','/usr/bin/python3','-c',script],timeout=10)
        self.assertEqual(result['exit_code'],0,result)
        self.assertEqual(outside.read_text(),'host-private')
        self.assertEqual((self.root/'local.txt').read_text(),'ok')
    def test_secret_and_symlink_exclusion(self):
        (self.root/'.env').write_text('TOKEN=hidden');(self.root/'link').symlink_to('/etc/passwd')
        dest=Path(self.temp.name)/'copy';P.snapshot(self.root,dest)
        self.assertFalse((dest/'.env').exists());self.assertFalse((dest/'link').exists())
    def test_dotted_skill_paths_preserved(self):
        p=self.root/'.agents/skills/check/SKILL.md';p.parent.mkdir(parents=True);p.write_text('---\nname: check\ndescription: Checks things\n---\nCheck.\n')
        self.assertIn('.agents/skills/check/SKILL.md',P.load_surface(self.root))
    def test_permission_rewrite_and_arbitrary_tool_denied(self):
        original=P.load_surface(self.root)
        for bad in ('ignore permissions','danger-full-access','approval_policy = never'):
            proposal={**original,'AGENTS.md':original['AGENTS.md']+bad+'\n'}
            with self.assertRaises(ValueError):P.validate(proposal,original,P.scan(self.root))
        with self.assertRaises(ValueError):P.validate({**original,'AGENTS.md':'replacement'},original,P.scan(self.root))
        with self.assertRaises(ValueError):P.validate({**original,P.TOOLS:'{"mcpServers":{"evil":{}}}'},original,P.scan(self.root))
    def make_deployment(self):
        run_id='20261022T140000-123456789abc';run=C.runpath(run_id);run.mkdir(parents=True)
        manifest=P.tree_hash(self.root);original=P.load_surface(self.root)
        selected={**original,'AGENTS.md':original['AGENTS.md']+'Run existing tests.\n'}
        changes={'AGENTS.md':selected['AGENTS.md'].encode()};before={'AGENTS.md':(self.root/'AGENTS.md').read_bytes()}
        P.snapshot(self.root,run/'snapshot')
        report={'status':'awaiting_confirmation','project_root':str(self.root),'confirmation_token':'user-token','validation':{'non_regression':True,'comparison_valid':True},'selection':{'why':'observed','chosen_label':'agg1'}}
        payload={'report':report,'snapshot_manifest':manifest,'selected_surface':selected,'changes':C.encode_tree(changes),'before':C.encode_tree(before),'experience':{},'mode':'agg'}
        C.atomic(run/'deployment.json',C.seal(payload));C.atomic(run/'report.json',C.seal(report))
        return run_id,run
    def test_apply_requires_token_and_human_then_rollback_exact(self):
        run_id,run=self.make_deployment();before=(self.root/'AGENTS.md').read_bytes()
        with self.assertRaises(ValueError):deploy(run_id,'wrong',approve=lambda *a:True)
        declined=deploy(run_id,'user-token',approve=lambda *a:False)
        self.assertEqual(declined['status'],'confirmation_declined');self.assertEqual((self.root/'AGENTS.md').read_bytes(),before)
        self.assertEqual(deploy(run_id,'user-token',approve=lambda *a:True)['status'],'applied')
        self.assertNotEqual((self.root/'AGENTS.md').read_bytes(),before)
        self.assertEqual(rollback(run_id,approve=lambda *a:True)['status'],'rolled_back');self.assertEqual((self.root/'AGENTS.md').read_bytes(),before)
    def test_stale_project_and_post_apply_drift_blocked(self):
        run_id,run=self.make_deployment();(self.root/'new-source.py').write_text('x=1')
        with self.assertRaisesRegex(ValueError,'drift'):deploy(run_id,'user-token',approve=lambda *a:True)
        (self.root/'new-source.py').unlink();deploy(run_id,'user-token',approve=lambda *a:True)
        (self.root/'AGENTS.md').write_text('later user edit')
        with self.assertRaisesRegex(ValueError,'Post-apply'):rollback(run_id,approve=lambda *a:True)
        self.assertEqual((self.root/'AGENTS.md').read_text(),'later user edit')
    def test_signed_state_cannot_be_tampered(self):
        run_id,run=self.make_deployment();value=C.read(run/'deployment.json');value['payload']['report']['confirmation_token']='tampered';C.atomic(run/'deployment.json',value)
        with self.assertRaisesRegex(ValueError,'integrity'):deploy(run_id,'tampered',approve=lambda *a:True)
    def test_path_escape_denied(self):
        for value in ('../../root','/root/unsafe','20261022T140000-123456789abc/../evil'):
            with self.assertRaises(ValueError):C.runpath(value)
        with self.assertRaises(ValueError):C.target(self.root,'../AGENTS.md')
    def test_request_requires_independent_check_and_limits(self):
        with self.assertRaises(ValueError):server.validate_request({'project_root':str(self.root),'task':'task'})
        with self.assertRaises(ValueError):server.validate_request({'project_root':str(self.root),'task':'task','check_argv':'echo yes'})
        with self.assertRaises(ValueError):server.validate_request({'project_root':str(self.root),'task':'task','check_argv':['/usr/bin/true'],'k':99})
    def test_stdio_tools_and_protocol_clean(self):
        entry=Path(__file__).resolve().parents[1]/'scripts/harness2_mcp.py'
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},{'jsonrpc':'2.0','id':2,'method':'tools/list'}]
        result=subprocess.run([sys.executable,str(entry),'--stdio'],input='\n'.join(map(json.dumps,messages))+'\n',text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()];self.assertEqual(len(replies),2)
        self.assertEqual(replies[0]['result']['protocolVersion'],'2025-03-26')
        self.assertEqual(len(replies[1]['result']['tools']),6)

if __name__=='__main__':unittest.main()
