import io
import http.client
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from h2 import common as C, project as P, guard
from h2 import server
from h2.deploy import deploy, rollback
from h2.engine import ProjectAdapter, verify_pair, summarize_validation
from h2.runtime import host_config, ProviderFailure, Runtime, extract_codex_errors
from h2.budget import Budget, allocation

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project';self.root.mkdir()
        self.state=Path(self.temp.name)/'state'
        self.old=C.STATE;C.STATE=self.state;self.addCleanup(setattr,C,'STATE',self.old)
        (self.root/'AGENTS.md').write_text('# Existing\nNever weaken permissions.\n')
    def test_pinned_source_integrity(self):C.verify_upstream()
    def test_codex_stream_errors_are_deduplicated_and_keep_only_safe_diagnostics(self):
        events=Path(self.temp.name)/'worker.jsonl'
        message='stream disconnected before completion; request_id: req-123; token=provider-secret'
        events.write_text('\n'.join((
            json.dumps({'type':'error','message':message}),
            json.dumps({'type':'turn.failed','error':{'message':message}}),
            json.dumps({'type':'item.completed','message':'not a failure'})
        ))+'\n')
        errors=extract_codex_errors(events,'provider-secret')
        self.assertEqual(len(errors),1)
        self.assertEqual(errors[0]['request_id'],'req-123')
        self.assertIsNone(errors[0]['http_status'])
        self.assertIn('[REDACTED]',errors[0]['message'])
        self.assertNotIn('provider-secret',json.dumps(errors))
    def test_a6api_profile_and_restricted_key_file_are_selectable(self):
        home=Path(self.temp.name)/'codex-profile';home.mkdir()
        key_file=Path(self.temp.name)/'a6api-key';key_file.write_text('a6api-test-secret\n');key_file.chmod(0o644)
        catalog=home/'models.json';catalog.write_text(json.dumps({'models':[{'slug':'gpt-6.1-sol'}]}))
        (home/'config.toml').write_text(
            'model_provider="OpenAI"\nmodel="default-model"\n'
            f'model_catalog_json={json.dumps(str(catalog))}\n'
            'sandbox_mode="workspace-write"\napproval_policy="on-request"\n'
            '[model_providers.OpenAI]\nbase_url="https://openai.example/v1"\nwire_api="responses"\n'
            '[model_providers.A6API]\nbase_url="https://api.a6api.com/v1"\nwire_api="responses"\nenv_key="A6API_API_KEY"\n'
        )
        (home/'auth.json').write_text(json.dumps({'OPENAI_API_KEY':'openai-only-secret'}))
        with patch.dict(os.environ,{'CODEX_HOME':str(home),'HARNESS2_PROVIDER_API_KEY_FILE':str(key_file)},clear=True):
            with self.assertRaisesRegex(ValueError,'permissions must be 0600'):
                host_config('A6API','gpt-6.1-sol')
            key_file.chmod(0o600)
            selected=host_config('A6API','gpt-6.1-sol')
            self.assertEqual(selected['model_provider'],'A6API')
            self.assertEqual(selected['model'],'gpt-6.1-sol')
            self.assertEqual(selected['provider_host'],'api.a6api.com')
            self.assertEqual(selected['key'],'a6api-test-secret')
            with self.assertRaisesRegex(ValueError,'model catalog'):
                host_config('A6API','not-in-catalog')
        with patch.dict(os.environ,{'CODEX_HOME':str(home)},clear=True):
            with self.assertRaisesRegex(RuntimeError,'API-key-backed'):
                host_config('A6API','gpt-6.1-sol')
    def test_effect_benchmark_verifiers_reject_buggy_baseline(self):
        benchmark=Path(__file__).resolve().parents[1]/'evaluation/effect-benchmark'
        verifiers=sorted((benchmark/'verifiers').glob('check_*.py'))
        self.assertEqual(len(verifiers),10)
        for verifier in verifiers:
            result=subprocess.run([sys.executable,str(verifier)],cwd=benchmark,
                                  capture_output=True,text=True,timeout=10,
                                  env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
            self.assertNotEqual(result.returncode,0,verifier.name+' unexpectedly accepts baseline')
            self.assertNotIn('ModuleNotFoundError',result.stderr,verifier.name)
            self.assertNotIn('No such file or directory',result.stderr,verifier.name)
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
        config={'model':'test-only','base_url':'https://provider.invalid','key':'fixture-only','sandbox_mode':'workspace-write','approval_policy':'on-request'}
        with patch('h2.runtime.ROOT',cache),patch('h2.runtime.binary_path',return_value=Path('/usr/bin/true')):
            runtime=Runtime(run,threading.Event(),config)
        try:
            shutil.rmtree(cache)
            self.assertEqual(runtime.relay.read_text(),'# fixture relay\n')
            failure={'exit_code':1,'wall_s':0.01,'stop_reason':None,'stderr':'launcher fixture failure','stdout':''}
            def inspect_worker_config(cmd,**kwargs):
                target=cmd.index('/home/worker/config.toml')
                worker_config=Path(cmd[target-1]).read_text()
                self.assertIn('request_max_retries = 0',worker_config)
                self.assertIn('stream_max_retries = 0',worker_config)
                return failure
            with patch('h2.runtime.guard.process',side_effect=inspect_worker_config):
                result=runtime.call(self.root,'fixture','holdout-0-baseline')
            self.assertIn('before any provider request',result['infrastructure_errors'][0])
        finally:runtime.close()
    def test_provider_502_opens_circuit_once_and_records_safe_diagnostics(self):
        run=self.state/'provider-circuit';run.mkdir(parents=True)
        config={'model':'fixture-model','base_url':'https://provider.invalid','key':'never-log-this',
                'sandbox_mode':'read-only','approval_policy':'on-request'}
        with patch('h2.runtime.binary_path',return_value=Path('/usr/bin/true')):
            runtime=Runtime(run,threading.Event(),config)
        def upstream_502(*args,**kwargs):
            headers=io.BytesIO(b'{"error":{"message":"temporary outage"}}')
            response_headers={'x-request-id':'provider-req-123','Retry-After':'17'}
            raise urllib.error.HTTPError(
                'https://provider.invalid/responses',502,'Bad Gateway',response_headers,headers)
        barrier=threading.Barrier(2)
        def call(label):
            barrier.wait()
            try:
                runtime.forward(label,{'model':'fixture-model','input':'short probe'},'application/json')
            except ProviderFailure as exc:
                return exc
            self.fail('mock provider failure unexpectedly succeeded')
        try:
            with patch('h2.runtime.urllib.request.urlopen',side_effect=upstream_502) as upstream:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    errors=list(executor.map(call,('roll-first','roll-second')))
                self.assertEqual(upstream.call_count,1)
            self.assertEqual({error.status for error in errors},{502})
            self.assertTrue(any('circuit opened' in str(error) for error in errors))
            self.assertTrue(any('Provider circuit open' in str(error) for error in errors))
            self.assertTrue(all('never-log-this' not in str(error) for error in errors))
            with runtime.lock:
                runtime.tokens['proxy-fixture-token']='roll-proxy'
            connection=http.client.HTTPConnection('localhost')
            connection.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            connection.sock.connect(runtime.server.server_address)
            connection.request('POST','/responses',body=json.dumps({'model':'fixture-model','input':'probe'}),
                               headers={'Authorization':'Bearer proxy-fixture-token','Content-Type':'application/json'})
            response=connection.getresponse()
            error_body=json.loads(response.read())
            connection.close()
            self.assertEqual(response.status,502)
            self.assertEqual(error_body['error']['type'],'upstream_error')
            self.assertIn('provider-req-123',error_body['error']['message'])
            self.assertNotIn('temporary outage',json.dumps(error_body))
            ledger=runtime.ledger()
            self.assertEqual(ledger['provider_requests'],1)
            self.assertEqual(ledger['provider_errors'][0]['request_id'],'provider-req-123')
            self.assertEqual(ledger['provider_errors'][1]['blocked'],True)
            self.assertNotIn('temporary outage',json.dumps(ledger))
            self.assertNotIn('never-log-this',json.dumps(ledger))
            self.assertEqual(runtime.budget.ledger()['charged']['optimization']['requests'],1)
        finally:runtime.close()
    def test_incomplete_provider_stream_is_transport_error_not_upstream_http_status(self):
        run=self.state/'incomplete-stream';run.mkdir(parents=True)
        config={'model':'fixture-model','model_provider':'A6API','base_url':'https://api.a6api.com/v1',
                'key':'never-log-this','sandbox_mode':'read-only','approval_policy':'on-request'}
        with patch('h2.runtime.binary_path',return_value=Path('/usr/bin/true')):
            runtime=Runtime(run,threading.Event(),config)
        class IncompleteResponse:
            status=200
            headers={'x-request-id':'stream-req-123','Content-Type':'text/event-stream'}
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,*args):raise http.client.IncompleteRead(b'data: partial')
        try:
            with patch('h2.runtime.urllib.request.urlopen',return_value=IncompleteResponse()):
                with self.assertRaises(ProviderFailure) as caught:
                    runtime.forward('roll-stream',{'model':'fixture-model','input':'short probe'},'text/event-stream')
            self.assertEqual(caught.exception.status,503)
            self.assertEqual(caught.exception.transport,'IncompleteRead')
            record=runtime.ledger()['provider_errors'][0]
            self.assertEqual(record['status_origin'],'local_transport_mapping')
            self.assertEqual(record['provider_host'],'api.a6api.com')
            bill=runtime.billing[0]
            self.assertEqual(bill['http_status'],200)
            self.assertFalse(bill.get('response_read_complete',False))
            ledger=runtime.ledger()
            self.assertEqual(ledger['provider_responses_missing_usage'],1)
            self.assertFalse(ledger['usage_complete'])
            self.assertNotIn('never-log-this',json.dumps(ledger))
        finally:runtime.close()
    def test_runtime_stream_error_event_reaches_signed_audit_record(self):
        run=self.state/'worker-stream-error';run.mkdir(parents=True)
        config={'model':'fixture-model','model_provider':'A6API','base_url':'https://api.a6api.com/v1',
                'key':'worker-secret','sandbox_mode':'workspace-write','approval_policy':'on-request'}
        with patch('h2.runtime.binary_path',return_value=Path('/usr/bin/true')):
            runtime=Runtime(run,threading.Event(),config)
        message='stream disconnected before completion; request_id: stream-req-456; token=worker-secret'
        def worker_error(cmd,**kwargs):
            Path(kwargs['stdout_path']).write_text('\n'.join((
                json.dumps({'type':'error','message':message}),
                json.dumps({'type':'turn.failed','error':{'message':message}})
            ))+'\n')
            return {'exit_code':1,'wall_s':1.0,'stop_reason':None,'stderr':'','stdout':''}
        try:
            with patch('h2.runtime.guard.process',side_effect=worker_error):
                result=runtime.call(self.root,'fixture','holdout-0-candidate')
            self.assertEqual(len(result['codex_errors']),1)
            self.assertIn('stream-req-456',result['infrastructure_errors'][0])
            self.assertNotIn('worker-secret',json.dumps(result))
            ledger=runtime.ledger()
            self.assertEqual(ledger['codex_worker_errors'][0]['request_id'],'stream-req-456')
            self.assertNotIn('worker-secret',json.dumps(ledger))
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
        rows[-1].update(infrastructure_errors=['Codex worker reported turn.failed: stream disconnected; request_id=req-123'])
        result=summarize_validation(rows,2)
        self.assertFalse(result['comparison_valid'])
        self.assertTrue(any('request_id=req-123' in reason for reason in result['invalidation_reasons']))
    def test_family_bootstrap_only_supports_a_complete_protected_suite(self):
        tasks=[];rows=[]
        for index in range(10):
            task={'task_id':f'task-{index}','family':f'family-{index}','protected_paths':[f'tests/test_{index}.py']}
            tasks.append(task)
            for trial in range(2):
                for kind,passed in (('baseline',False),('candidate',True)):
                    rows.append({'task_id':task['task_id'],'family':task['family'],'trial':trial,'kind':kind,'passed':passed,'check':{'stop_reason':None}})
        result=summarize_validation(rows,2,tasks)
        self.assertTrue(result['comparison_valid']);self.assertTrue(result['confirmatory_suite'])
        self.assertEqual(result['effect_conclusion'],'improvement_supported')
        self.assertEqual(result['success_lift_95_ci']['lower'],1.0)
        self.assertTrue(result['non_regression'])
        self.assertEqual(summarize_validation(rows[:-1],2,tasks)['effect_conclusion'],'invalid_comparison')
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
    def test_holdout_verifier_files_are_hidden_until_judge(self):
        run=self.state/'private-holdout';run.mkdir(parents=True)
        tests=self.root/'tests';tests.mkdir();judge_file=tests/'test_private.py';judge_file.write_text('assert True\n')
        P.snapshot(self.root,run/'snapshot')
        observations=[]
        class FakeRuntime:
            config={'model':'deterministic-test-only'}
            def call(self,work,*args,**kwargs):
                observations.append((work/'tests/test_private.py').exists())
                return {'exit_code':0}
        adapter=ProjectAdapter(run,run/'snapshot','training task',FakeRuntime(),['tests/test_private.py'])
        task={'task_id':'heldout','family':'example','task':'different task','check_argv':['/usr/bin/true'],'protected_paths':['tests/test_private.py']}
        checked=[]
        def judge(work,*args,**kwargs):
            checked.append((work/'tests/test_private.py').read_text())
            return {'exit_code':0,'wall_s':0,'stop_reason':None}
        with patch('h2.engine.guard.check_command',side_effect=judge):
            result=verify_pair(adapter,adapter.original,{'validation_tasks':[task],'validation_repeats':2},threading.Event())
        self.assertEqual(observations,[False]*4)
        self.assertEqual(checked,['assert True\n']*4)
        self.assertEqual(result['effect_conclusion'],'no_candidate_aa_control_only')
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
            self.assertEqual(report['status'],'validation_no_supported_improvement')
            self.assertEqual((report['method']['ledger']['R'],report['method']['ledger']['P']),(8,6))
            self.assertEqual(len(report['validation']['rows']),4)
            self.assertTrue(report['validation']['non_regression'])
            self.assertFalse(report['validation']['measured_success_gain'])
            self.assertEqual(report['validation']['effect_conclusion'],'insufficient_independent_holdout_families')
            self.assertIsNone(report['confirmation_token'])
            self.assertEqual(C.unseal(C.read(run/'report.json'))['run_id'],run.name)
            self.assertEqual((self.root/'AGENTS.md').read_bytes(),original)
    def test_ppc_infrastructure_failure_makes_selected_candidate_inconclusive(self):
        from h2 import engine
        class FaultRuntime:
            def __init__(self,run,cancel,config,**kwargs):
                self.run,self.config,self.calls=run,config,[]
                self.invocations=[]
            def call(self,work,mission,label,**kwargs):
                self.calls.append(label)
                if label.startswith('improve-select.'):
                    text='<why>integration fixture</why>\n```choice\nA\n```'
                elif label.startswith('improve-experience.'):
                    text='NO_ENTRY'
                elif label.startswith('improve-'):
                    path=work/'context/harness/AGENTS.md'
                    with path.open('a') as f:f.write('Extra integration fixture guidance.\n')
                    text='<notes>Fixture guidance.</notes>'
                    self.invocations.append({'label':label,'infrastructure_errors':['simulated provider failure'],'stop_reason':None})
                else:text='Test-only execution completed.'
                events=self.run/(label+'.jsonl');events.write_text('')
                return {'exit_code':0,'last_message':text,'stderr':'','wall_s':0,'events':str(events)}
            def ledger(self):return {'test_only':True,'invocations':len(self.calls)}
            def close(self):pass
        run=self.state/'20261022T170000-123456789abc';run.mkdir(parents=True)
        request={'project_root':str(self.root),'task':'integration fixture','k':1,'check_argv':['/usr/bin/true']}
        with patch('h2.engine.Runtime',FaultRuntime),patch('h2.engine.host_config',return_value={'model':'test-only'}):
            report=engine.experiment(run,request,threading.Event())
        self.assertEqual(report['status'],'optimization_inconclusive')
        self.assertTrue(report['run_outcome'].startswith('ppc_infrastructure_failure_'))
        self.assertTrue(report['ppc_infrastructure_failures'])
        self.assertIsNone(report['validation'])
        self.assertIsNone(report['confirmation_token'])
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
        report={'status':'awaiting_confirmation','project_root':str(self.root),'confirmation_token':'user-token','validation':{'non_regression':True,'comparison_valid':True,'effect_conclusion':'improvement_supported','confirmatory_suite':True},'selection':{'why':'observed','chosen_label':'agg1'}}
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
        selected=server.validate_request({'project_root':str(self.root),'task':'task','check_argv':['/usr/bin/true'],'model_provider':'A6API','model':'gpt-6.1-sol'})
        self.assertEqual((selected['model_provider'],selected['model']),('A6API','gpt-6.1-sol'))
        with self.assertRaisesRegex(ValueError,'model_provider'):
            server.validate_request({'project_root':str(self.root),'task':'task','check_argv':['/usr/bin/true'],'model_provider':'A6API/../../x'})
    def test_request_validates_and_freezes_protected_multitask_suite(self):
        tasks=[]
        for index in range(10):
            path=f'verifiers/check_{index}.py';target=self.root/path;target.parent.mkdir(exist_ok=True)
            target.write_text('assert True\n')
            tasks.append({'task_id':f'task-{index}','family':f'family-{index}','task':f'Independent holdout task {index}','check_argv':['/usr/bin/python3',path],'protected_paths':[path]})
        request=server.validate_request({'project_root':str(self.root),'task':'train task','validation_tasks':tasks})
        self.assertEqual(len(request['private_paths']),10)
        self.assertTrue(request['validate_unchanged'])
        tasks[1]['protected_paths']=[tasks[0]['protected_paths'][0]]
        with self.assertRaisesRegex(ValueError,'task-specific and disjoint'):
            server.validate_request({'project_root':str(self.root),'task':'train task','validation_tasks':tasks})
    def test_estimate_tool_exposes_preallocated_multi_family_envelopes(self):
        result=server.call('harness2_estimate_budget',{'validation_task_count':5,'validation_repeats':2})
        self.assertEqual(result['validation_task_count'],5)
        self.assertEqual(result['per_task_arm_envelope']['requests'],6)
        self.assertIn('token ceilings are not dollar ceilings',result['cost_usd'])
        larger=server.call('harness2_estimate_budget',{'validation_task_count':10,'validation_repeats':2})
        self.assertFalse(larger['budget_adequate_for_minimum_session_envelope'])
    def test_apply_preview_is_read_only_and_checks_evidence_gate(self):
        from h2.deploy import preview
        run_id,run=self.make_deployment()
        before=(self.root/'AGENTS.md').read_bytes()
        result=preview(run_id)
        self.assertTrue(result['eligible'])
        self.assertFalse(result['writes_performed']);self.assertFalse(result['confirmation_opened'])
        self.assertEqual((self.root/'AGENTS.md').read_bytes(),before)
        (self.root/'new.py').write_text('changed')
        result=preview(run_id)
        self.assertFalse(result['eligible']);self.assertTrue(result['project_drift'])
    def test_ppc_infrastructure_failure_blocks_preview_and_apply(self):
        from h2.deploy import preview
        run_id,run=self.make_deployment()
        payload=C.unseal(C.read(run/'deployment.json'))
        report=payload['report']
        report['ppc_infrastructure_failures']=[{'label':'improve-agg1','error_count':1,'stop_reason':None}]
        payload['report']=report
        C.atomic(run/'deployment.json',C.seal(payload));C.atomic(run/'report.json',C.seal(report))
        result=preview(run_id)
        self.assertFalse(result['eligible'])
        self.assertTrue(any('infrastructure failures' in issue for issue in result['issues']))
        with self.assertRaisesRegex(ValueError,'PPC infrastructure failures'):
            deploy(run_id,'user-token',approve=lambda *a:True)
    def test_stdio_tools_and_protocol_clean(self):
        entry=Path(__file__).resolve().parents[1]/'scripts/harness2_mcp.py'
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},{'jsonrpc':'2.0','id':2,'method':'tools/list'}]
        result=subprocess.run([sys.executable,str(entry),'--stdio'],input='\n'.join(map(json.dumps,messages))+'\n',text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()];self.assertEqual(len(replies),2)
        self.assertEqual(replies[0]['result']['protocolVersion'],'2025-03-26')
        self.assertEqual(len(replies[1]['result']['tools']),9)
        names={tool['name'] for tool in replies[1]['result']['tools']}
        self.assertTrue({'harness2_estimate_budget','harness2_get_report','harness2_preview_apply'}<=names)
        run_tool=next(tool for tool in replies[1]['result']['tools'] if tool['name']=='harness2_run')
        self.assertTrue({'model_provider','model'}<=set(run_tool['inputSchema']['properties']))

if __name__=='__main__':unittest.main()
