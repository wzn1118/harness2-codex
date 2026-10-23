"""Project adapter; all PPC scheduling/prompts/selection use Google code."""
from __future__ import annotations
import contextlib
import difflib
import hashlib
import io
import json
import os
import random
import re
import secrets
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from . import common as C, guard, project as P
from .runtime import Runtime, host_config

sys.path.insert(0, str(C.UPSTREAM))
from harness2.core import recursion, selection, tree
from harness2.core.library import Library
from harness2.core.proposer import ProposerError, Reply
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec, RunView
from harness2.substrate.codex import CODEX_SURFACE, render_events

ENGINE_LOCK = threading.Lock()
MIN_CONFIRMATORY_FAMILIES = 10
MAX_WORKER_RETRIES = 2

def _pause_before_retry(runtime, attempt):
    delay=min(2**(attempt+1),30)
    cancel=getattr(runtime,"cancel",None)
    if cancel is not None:
        if cancel.wait(delay):
            raise RuntimeError("Experiment cancelled before transient retry")
    else:
        time.sleep(delay)

def _retry_reason(result):
    if result.get("stop_reason") == "timeout":
        return "Codex worker execution timed out"
    messages=[str(error.get("message","")) for error in result.get("codex_errors",[])]
    messages.extend(str(error) for error in result.get("infrastructure_errors",[]))
    for message in messages:
        if "stream disconnected before completion" in message.casefold():
            return message[:1000]
        if "upstream_unavailable" in message.casefold():
            return f"Transient provider upstream_unavailable: {message[:900]}"
        match=re.search(r"(?:\bHTTP\s*|\bstatus\s+)(502|503|504)\b",message,re.I)
        if match:
            return f"Transient provider HTTP {match.group(1)}: {message[:900]}"
    return None

def _prepare_transient_retry(runtime, attempt, failed_label, reason):
    _pause_before_retry(runtime,attempt)
    if reason.casefold().startswith(("transient provider http ",
                                    "transient provider upstream_unavailable:")):
        reset=getattr(runtime,"reset_transient_provider_circuit",None)
        if reset:
            reset(failed_label,reason)

def _retry_recovered(result):
    return (result.get("exit_code")==0 and not result.get("stop_reason")
            and not result.get("infrastructure_errors") and not result.get("codex_errors"))

class Substrate:
    name = "cx"
    def __init__(self, original):
        self.spec = replace(CODEX_SURFACE, always_on=("AGENTS.md",P.TOOLS,P.STRATEGY),always_on_max=max(16000,sum(len(v) for v in original.values())+8000),hooks_file=None,hook_events=frozenset(),stubs=())
    def surface(self): return self.spec

class ProjectAdapter:
    def __init__(self, run, snapshot, task, runtime, private_paths=()):
        self.run,self.snapshot,self.task,self.runtime = run,snapshot,task,runtime
        self.private_paths=tuple(private_paths)
        self.original = P.load_surface(snapshot)
        envelope=runtime.config.get("tool_envelope",{})
        self.original[P.TOOLS]=json.dumps({k:v and envelope.get(k,True) for k,v in json.loads(self.original[P.TOOLS]).items()})
        self.original_files = P.scan(snapshot)
        self.substrate = Substrate(self.original)
        channel = ChannelSpec("opencode","adapter/"+runtime.config["model"])
        self.spec = BenchSpec(name="codex-project",runs_root=run,install_mode="codex_root",prompts=BenchPrompts(work="complete the user's project task correctly and efficiently",domain_word="project",inv_levers="additive project instructions, on-demand Skills, tool availability and bounded execution strategy",dom_levers="project-specific workflow and existing conventions",yardstick="correctness, concrete deliverables, reliability and resource efficiency"),channels={"proposer":channel,"selector":channel},allow_subagents=False)
        self.base = run / "base-harness"
        tree.write_tree(self.substrate.surface(),self.original,self.base,stubs=False)
    def tasks(self,split="all"): return ["project-task"]
    def domain_of(self,task_id): return "project"
    def task_prompt(self,task_id): return self.task
    def task_files(self,task_id): return self.snapshot
    def base_harness(self): return self.base
    def validate_tree(self,surface): P.validate(surface,self.original,self.original_files)
    def execute(self,task_id,harness_dir,run_id,timeout=None):
        surface = tree.read_tree(self.substrate.surface(),harness_dir or self.base)
        self.validate_tree(surface)
        out = self.run / "rollouts" / run_id
        out.mkdir(parents=True,exist_ok=False)
        attempt_labels=[]
        attempt_history=[]
        retry_records=[]
        for attempt in range(MAX_WORKER_RETRIES+1):
            suffix="" if attempt==0 else f".retry{attempt}"
            label="roll-"+run_id+suffix
            work=out/("workspace" if attempt==0 else f"workspace.retry{attempt}")
            P.materialize(self.snapshot,work,surface,exclude=self.private_paths)
            before=P.tree_hash(work)
            task_files_before=P.scan(work)
            result=self.runtime.call(work,self.task + "\n\nDo the task in /work. Do not alter AGENTS.md, Skills, harness2-tools.json, or harness2-execution.json. Do not change tests or permission policies. Report commands and observed results.",label,strategy=json.loads(surface[P.STRATEGY]),tools=json.loads(surface[P.TOOLS]))
            attempt_labels.append(label)
            attempt_history.append({"label":label,"exit_code":result.get("exit_code"),"stop_reason":result.get("stop_reason"),"wall_s":result.get("wall_s"),"infrastructure_errors":result.get("infrastructure_errors",[])})
            P.reject_worker_links(work)
            reason=_retry_reason(result)
            if attempt<MAX_WORKER_RETRIES and reason:
                retry_records.append({"failed_label":label,"retry_label":"roll-"+run_id+f".retry{attempt+1}","reason":reason})
                _prepare_transient_retry(self.runtime,attempt,label,reason)
                continue
            for retry_record in retry_records:
                self.runtime.record_codex_retry(**retry_record,recovered=_retry_recovered(result),
                                                resolved_label=attempt_labels[-1] if _retry_recovered(result) else None)
            break
        result["retry_count"]=len(attempt_labels)-1
        result["attempt_labels"]=attempt_labels
        result["attempt_history"]=attempt_history
        result["retry_reason"]=retry_records[-1]["reason"] if retry_records else None
        changes = P.tree_hash(work)
        edited_harness = [n for n in surface if changes.get(n)!=before.get(n)]
        result["harness_mutated"] = edited_harness
        C.atomic(out/"invocation.json",result)
        (out/"answer.md").write_text(result["last_message"])
        patch=[]
        files=P.scan(work)
        for name in sorted(set(files)|set(task_files_before)):
            if files.get(name)==task_files_before.get(name):continue
            old=task_files_before.get(name,b"").decode(errors="replace").splitlines(keepends=True)
            new=files.get(name,b"").decode(errors="replace").splitlines(keepends=True)
            patch.extend(difflib.unified_diff(old,new,"before/"+name,"after/"+name))
        (out/"deliverable.diff").write_text("".join(patch))
        return out
    def delivered(self,out):
        r=C.read(out/"invocation.json")
        return r["exit_code"]==0 and bool(r["last_message"].strip()) and not r["harness_mutated"]
    def trajectory(self,out,*,obs_trunc):
        r=C.read(out/"invocation.json")
        return render_events(Path(r["events"]),obs_trunc=obs_trunc,max_chars=10_000_000)
    def deliverable_text(self,out): return (out/"answer.md").read_text()+"\n\n"+(out/"deliverable.diff").read_text()
    def deliverable_files(self,out): return {"result.md":self.deliverable_text(out)}

class CodexImprover:
    def __init__(self,adapter): self.adapter=adapter;self._calls=[];self.lock=threading.RLock()
    def call(self,mission,context_dir,label,**kwargs):
        directions="\n\nThis session uses an additive-only safe project adapter. Edit only context/harness/AGENTS.md, .agents/skills/* components within context/harness/, context/harness/harness2-tools.json (shell/apply_patch booleans), or context/harness/harness2-execution.json (reasoning_effort low/medium/high, timeout_sec 30..600). The adapter's JSON configuration files are controls, not prose: keep them valid JSON with exactly their existing keys. Preserve all existing instruction text verbatim as a prefix; existing scripts are immutable. Never create hooks, subagents, arbitrary MCP commands, or permission changes. Editing tool/strategy JSON changes actual executor options. For selection or reflection do not edit any file. Reply using the exact structured blocks requested by the upstream mission."
        readonly=not kwargs.get("harvest")
        retry_records=[]
        wall_s=0
        for attempt in range(MAX_WORKER_RETRIES+1):
            suffix="" if attempt==0 else f".retry{attempt}"
            session_label=label+suffix
            session=self.adapter.run/"sessions"/session_label
            session.mkdir(parents=True,exist_ok=False)
            shutil.copytree(context_dir,session/"context")
            before=P.tree_hash(session)
            runtime_label="improve-"+session_label
            r=self.adapter.runtime.call(session,mission+directions,runtime_label,readonly=readonly)
            wall_s+=r.get("wall_s",0)
            try:P.reject_worker_links(session)
            except ValueError as exc:
                for retry_record in retry_records:
                    self.adapter.runtime.record_codex_retry(**retry_record,recovered=False)
                raise ProposerError(str(exc),attempts=attempt+1) from exc
            after=P.tree_hash(session)
            changed={n for n in set(before)|set(after) if before.get(n)!=after.get(n)}
            illegal=[n for n in changed if not n.startswith("context/harness/")]
            if r["exit_code"] or not r["last_message"].strip() or illegal or r.get("stop_reason") or r.get("infrastructure_errors") or r.get("codex_errors"):
                reason=_retry_reason(r)
                if attempt<MAX_WORKER_RETRIES and reason and not illegal:
                    retry_records.append({"failed_label":runtime_label,"retry_label":"improve-"+label+f".retry{attempt+1}","reason":reason})
                    _prepare_transient_retry(self.adapter.runtime,attempt,runtime_label,reason)
                    continue
                for retry_record in retry_records:
                    self.adapter.runtime.record_codex_retry(**retry_record,recovered=False)
                raise ProposerError("Codex session failed or changed evidence outside harness: "+str(illegal)+" "+r["stderr"][-700:],attempts=attempt+1)
            for retry_record in retry_records:
                self.adapter.runtime.record_codex_retry(**retry_record,recovered=_retry_recovered(r),
                                                        resolved_label=runtime_label if _retry_recovered(r) else None)
            reply=Reply(label=label,text=r["last_message"],ok=True,attempts=attempt+1,wall_s=wall_s,workspace=session/"context/harness" if kwargs.get("harvest") else None)
            with self.lock:self._calls.append(reply)
            return reply
        raise ProposerError("Codex session failed after transient stream retries",attempts=MAX_WORKER_RETRIES+1)

def summarize_validation(rows,repeats,tasks=None,*,candidate_unchanged=False):
    if tasks is None:
        tasks=[{"task_id":"primary","family":"single-task","protected_paths":[]}]
        rows=[{**row,"task_id":row.get("task_id","primary")} for row in rows]
    expected={(task["task_id"],trial,arm) for task in tasks for trial in range(repeats) for arm in ("baseline","candidate")}
    actual=[(row.get("task_id"),row.get("trial"),row.get("kind")) for row in rows]
    invalidation_reasons=[]
    if len(rows)!=len(expected):
        invalidation_reasons.append(f"expected {len(expected)} paired rows, observed {len(rows)}")
    if len(set(actual))!=len(actual):
        invalidation_reasons.append("duplicate task/trial/arm rows")
    missing=sorted(expected-set(actual))
    if missing:
        invalidation_reasons.append("missing paired rows: "+", ".join("/".join(map(str,item)) for item in missing[:20]))
    for row in rows:
        label=f"{row.get('task_id')}/{row.get('trial')}/{row.get('kind')}"
        for error in row.get("infrastructure_errors",[]):
            invalidation_reasons.append(f"{label}: {str(error)[:1000]}")
        if row.get("agent_stop_reason"):
            invalidation_reasons.append(f"{label}: agent stopped: {row['agent_stop_reason']}")
        if row.get("check",{}).get("stop_reason"):
            invalidation_reasons.append(f"{label}: verifier stopped: {row['check']['stop_reason']}")
        usage=row.get("known_provider_usage") or {}
        if row.get("agent_exit_code",0)!=0 and usage.get("responses_with_usage",0)==0 and not row.get("infrastructure_errors"):
            invalidation_reasons.append(
                f"{label}: agent exit {row.get('agent_exit_code')} with no provider usage; failure cause unverified"
            )
    valid=not invalidation_reasons
    by_task={}
    for task in tasks:
        task_rows=[row for row in rows if row.get("task_id")==task["task_id"]]
        base=sum(bool(row.get("passed")) for row in task_rows if row.get("kind")=="baseline")
        candidate=sum(bool(row.get("passed")) for row in task_rows if row.get("kind")=="candidate")
        by_task[task["task_id"]]={"family":task["family"],"baseline_passed":base,"candidate_passed":candidate,"trials":repeats,"baseline_rate":base/repeats,"candidate_rate":candidate/repeats,"success_lift":(candidate-base)/repeats,"protected_paths":task.get("protected_paths",[])}
    baseline=sum(value["baseline_passed"] for value in by_task.values())
    candidate=sum(value["candidate_passed"] for value in by_task.values())
    pairs={(row.get("task_id"),row.get("trial"),row.get("kind")):bool(row.get("passed")) for row in rows}
    no_regressed_pair=all(not (pairs.get((task["task_id"],trial,"baseline"),False) and not pairs.get((task["task_id"],trial,"candidate"),False)) for task in tasks for trial in range(repeats))
    family_values={}
    for task in tasks:
        metric=by_task[task["task_id"]]
        family_values.setdefault(task["family"],[]).append(metric["success_lift"])
    family_lifts={family:sum(values)/len(values) for family,values in family_values.items()}
    estimate=sum(family_lifts.values())/len(family_lifts) if family_lifts else None
    confirmatory=len(family_lifts)>=MIN_CONFIRMATORY_FAMILIES and all(task.get("protected_paths") for task in tasks)
    interval=None
    if valid and confirmatory:
        seed=int.from_bytes(hashlib.sha256(json.dumps(family_lifts,sort_keys=True).encode()).digest()[:8],"big")
        rng=random.Random(seed);families=sorted(family_lifts);samples=[]
        for _ in range(10000):
            samples.append(sum(family_lifts[rng.choice(families)] for _ in families)/len(families))
        samples.sort()
        interval={"lower":samples[249],"upper":samples[9749],"confidence":0.95,"method":"deterministic cluster bootstrap over task families; 10,000 resamples"}
    if not valid:conclusion="invalid_comparison"
    elif candidate_unchanged:conclusion="no_candidate_aa_control_only"
    elif not confirmatory:conclusion="insufficient_independent_holdout_families"
    elif interval["lower"]>0:conclusion="improvement_supported"
    elif interval["upper"]<0:conclusion="regression_supported"
    elif estimate==0:conclusion="no_detected_effect"
    else:conclusion="effect_inconclusive"
    return {"rows":rows,"baseline_passed":baseline,"candidate_passed":candidate,"total_task_trials":len(tasks)*repeats,"repeats":repeats,"task_count":len(tasks),"family_count":len(family_lifts),"task_results":by_task,"family_success_lift":family_lifts,"success_lift":estimate,"success_lift_95_ci":interval,"comparison_valid":valid,"invalidation_reasons":invalidation_reasons,"confirmatory_suite":confirmatory,"non_regression":valid and candidate==len(expected)//2 and candidate>=baseline and no_regressed_pair,"measured_success_gain":valid and candidate>baseline,"effect_conclusion":conclusion}

def verify_pair(adapter,selected,request,cancel):
    """Hold-out checks are never supplied to the proposer/composer/selector."""
    repeats=request.get("validation_repeats",2)
    tasks=request.get("validation_tasks") or [{"task_id":"primary","family":"single-task","task":adapter.task,"check_argv":request["check_argv"],"protected_paths":[]}]
    rows=[]
    for task_index,task in enumerate(tasks):
      for trial in range(repeats):
        order=("baseline","candidate") if (trial+task_index)%2==0 else ("candidate","baseline")
        for kind in order:
            surface=adapter.original if kind=="baseline" else selected
            dest=adapter.run/"holdout"/task["task_id"]/f"{trial}-{kind}";dest.mkdir(parents=True)
            label=f"holdout-{trial}-{kind}" if len(tasks)==1 and task["task_id"]=="primary" else f"holdout-{task['task_id']}-{trial}-{kind}"
            attempt_labels=[]
            retry_records=[]
            for attempt in range(MAX_WORKER_RETRIES+1):
                suffix="" if attempt==0 else f".retry{attempt}"
                attempt_label=label+suffix
                work=dest/("workspace" if attempt==0 else f"workspace.retry{attempt}")
                P.materialize(adapter.snapshot,work,surface,exclude=adapter.private_paths)
                result=adapter.runtime.call(work,task["task"]+"\n\nComplete the task in /work. Do not alter harness files, permissions, or tests. The held-out verifier files are intentionally unavailable. Report actual results.",attempt_label,strategy=json.loads(surface[P.STRATEGY]),tools=json.loads(surface[P.TOOLS]))
                attempt_labels.append(attempt_label)
                P.reject_worker_links(work)
                reason=_retry_reason(result)
                if attempt<MAX_WORKER_RETRIES and reason:
                    retry_records.append({"failed_label":attempt_label,"retry_label":label+f".retry{attempt+1}","reason":reason})
                    _prepare_transient_retry(adapter.runtime,attempt,attempt_label,reason)
                    continue
                for retry_record in retry_records:
                    adapter.runtime.record_codex_retry(**retry_record,recovered=_retry_recovered(result),
                                                       resolved_label=attempt_labels[-1] if _retry_recovered(result) else None)
                break
            # Only the independent judge receives its declared verifier files.
            for name in task.get("protected_paths",[]):
                data=(adapter.snapshot/name).read_bytes()
                path=C.target(work,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            forbidden=[]
            for name,text in surface.items():
                p=work/name
                if not p.exists() or p.read_bytes()!=text.encode():forbidden.append(name)
            check=guard.check_command(work,task["check_argv"],timeout=request.get("check_timeout_sec",120),cancel=cancel,output=dest/"check.json")
            passed=check["exit_code"]==0 and result["exit_code"]==0 and not forbidden
            failure_class=("infrastructure_error" if result.get("infrastructure_errors") or result.get("stop_reason") or check.get("stop_reason") else
                           "agent_failure" if result["exit_code"]!=0 else
                           "verifier_failure" if check["exit_code"]!=0 else
                           "harness_mutation" if forbidden else "pass")
            rows.append({"task_id":task["task_id"],"family":task["family"],"trial":trial,"kind":kind,"check":check,"failure_class":failure_class,"harness_mutations":forbidden,"agent_exit_code":result["exit_code"],"agent_stop_reason":result.get("stop_reason"),"infrastructure_errors":result.get("infrastructure_errors",[]),"agent_wall_s":result.get("wall_s"),"known_provider_usage":result.get("known_provider_usage"),"retry_count":len(attempt_labels)-1,"retry_reason":retry_records[-1]["reason"] if retry_records else None,"attempt_labels":attempt_labels,"passed":passed})
            C.atomic(adapter.run/"holdout-results.json",rows)
            C.event(adapter.run,"holdout_check",result=rows[-1])
    changed_surface=any(selected.get(name)!=adapter.original.get(name) for name in set(selected)|set(adapter.original))
    result=summarize_validation(rows,repeats,tasks,candidate_unchanged=not changed_surface)
    result["verifier_independence"]="protected verifier files withheld from all model sessions and restored only for checks" if all(task.get("protected_paths") for task in tasks) else "legacy verifier; no protected-path concealment declared"
    result["family_independence_note"]="Family labels are caller-declared; reviewers must check for related prompts or duplicated task sources. Software cannot infer domain-level independence."
    return result

def re_test_path(name):
    parts=Path(name).parts
    return any(p in {"tests","test","__tests__"} for p in parts) or Path(name).name.startswith("test_")

def experiment(run,request,cancel):
    C.verify_upstream();guard.available()
    cfg=host_config(request.get("model_provider"),request.get("model"))
    provider_name=cfg.get("model_provider","configured")
    provider_host=cfg.get("provider_host","unknown")
    C.event(run,"preflight",upstream_commit=C.COMMIT,model=cfg["model"],model_provider=provider_name,provider_host=provider_host)
    project=Path(request["project_root"]).resolve()
    P.check_policy_context(project)
    if C.STATE==project or C.STATE.is_relative_to(project):raise ValueError("State directory must be outside the project")
    manifest=P.snapshot(project,run/"snapshot")
    C.atomic(run/"snapshot-manifest.json",manifest)
    if "validation_tasks" not in request:
        protected=sorted(name for name in manifest if re_test_path(name))
        request={**request,"validation_tasks":[{"task_id":"primary","family":"single-task","task":request["task"],"check_argv":request["check_argv"],"protected_paths":protected}],"private_paths":protected}
    request.setdefault("validate_unchanged",True)
    runtime=Runtime(run,cancel,cfg,max_requests=request.get("max_requests",160),max_output_tokens=request.get("max_output_tokens",655360),prices=request.get("prices"),budget_request=request,execution_timeout_sec=request.get('execution_timeout_sec'))
    try:
        adapter=ProjectAdapter(run,run/"snapshot",request["task"],runtime,request.get("private_paths",()))
        adapter.validate_tree(adapter.original)
        improver=CodexImprover(adapter)
        mode="agg" if request.get("mode","parallel")=="parallel" else "seq";k=request.get("k",1)
        libroot=C.STATE/"experience"/C.digest(str(project).encode())[:20]/mode
        library=Library(libroot,"project",adapter.substrate.surface())
        base=[]
        for index in range(1,k+1):
            label=f"base{index}";out=adapter.execute("project-task",None,label)
            base.append(RunView(label=label,run_id=label,run_dir=out,ok=True,delivered=adapter.delivered(out)))
        # Redirect upstream progress away from the JSON-RPC stdout channel.
        with (run/"upstream.log").open("a") as log,contextlib.redirect_stdout(log):
            method=recursion.run_parallel if mode=="agg" else recursion.run_sequential
            result=method("project-task",k,base,adapter=adapter,prop=improver,work_dir=run/"method",run_id_of=lambda label:label,base_tree=adapter.original,library_dir=library.context_dir(),propose_workers=2,rollout_workers=2)
            result.ledger.assert_identity()
            pool=selection.selection_pool(mode,k,result.rollouts,base)
            chosen=selection.run_selection("project-task",k,mode,pool,adapter=adapter,prop=improver,work_dir=run/"method",pool_kind="finals")
        C.atomic(run/"upstream-result.json",result.to_dict());C.atomic(run/"selection.json",chosen.to_dict())
        selected=result.trees.get(chosen.chosen_label) if chosen.ok and not chosen.fallback else None
        invocations=getattr(runtime,"invocations",[])
        recovered_labels={item.get("failed_label") for item in getattr(runtime,"codex_retries",[]) if item.get("recovered")}
        ppc_infrastructure_failures=[{"label":item.get("label"),"error_count":len(item.get("infrastructure_errors",[])),"stop_reason":item.get("stop_reason")} for item in invocations if item.get("label") not in recovered_labels and (item.get("infrastructure_errors") or item.get("stop_reason"))]
        experience={};validation=None;status="no_adoption"
        original_files=adapter.original_files
        changes={n:text.encode() for n,text in (selected or {}).items() if original_files.get(n)!=text.encode() and (n in original_files or text!=adapter.original.get(n))}
        if selected and ppc_infrastructure_failures:
            status="optimization_inconclusive"
        elif selected and (changes or request.get('validate_unchanged', True)):
            adapter.validate_tree(selected)
            validation=verify_pair(adapter,selected,request,cancel)
            if not changes:
                status='no_adoption'
            elif validation["non_regression"] and validation.get("effect_conclusion")=="improvement_supported":
                status="awaiting_confirmation"
                try:
                    with (run/"upstream.log").open("a") as log,contextlib.redirect_stdout(log):
                        experience=selection.reflect_adoption("project-task",chosen,result.rollouts,adapter=adapter,prop=improver,work_dir=run/"method",decisions={s.label:s.notes or s.error for s in result.stages})
                except Exception as exc:C.event(run,"reflection_failed",error=str(exc))
            elif not validation["comparison_valid"]:status="validation_inconclusive"
            elif validation.get("effect_conclusion")=="regression_supported":status="validation_failed"
            else:status="validation_no_supported_improvement"
        patch=[]
        for name,data in changes.items():
            patch.extend(difflib.unified_diff(original_files.get(name,b"").decode().splitlines(keepends=True),data.decode().splitlines(keepends=True),"before/"+name,"after/"+name))
        if selected is None and ppc_infrastructure_failures:status="optimization_inconclusive"
        run_outcome=("ppc_infrastructure_failure_no_candidate" if selected is None and ppc_infrastructure_failures else
                     "ppc_infrastructure_failure_candidate_not_evaluated" if selected is not None and ppc_infrastructure_failures else
                     "ppc_no_candidate_selected" if selected is None else
                     ("ppc_selected_no_change_aa_control" if validation else "ppc_selected_no_change_unvalidated") if not changes else
                     "candidate_ready_for_user_confirmation" if status=="awaiting_confirmation" else
                     "valid_evaluation_without_supported_improvement" if validation and validation.get("comparison_valid") else
                     "evaluation_incomplete_or_invalid")
        eval_manifest=[{"task_id":task["task_id"],"family":task["family"],"prompt_sha256":C.digest(task["task"].encode()),"verifier_sha256":C.digest(C.canonical(task["check_argv"])),"protected_path_hashes":{name:manifest[name] for name in task.get("protected_paths",[])}} for task in request["validation_tasks"]]
        report={"run_id":run.name,"status":status,"run_outcome":run_outcome,"ppc_infrastructure_failures":ppc_infrastructure_failures,"project_root":str(project),"model_provider":provider_name,"provider_host":provider_host,"model":cfg["model"],"upstream_commit":C.COMMIT,"method":result.to_dict(),"selection":chosen.to_dict(),"validation":validation,"evaluation_manifest":eval_manifest,"ledger":runtime.ledger(),"changed_files":sorted(changes),"diff":"".join(patch),"created_at":C.stamp(),"confirmation_token":secrets.token_urlsafe(24) if status=="awaiting_confirmation" else None,"experience":experience}
        C.atomic(run/"report.json",C.seal(report))
        C.atomic(run/"status.json",{"status":status,"run_id":run.name,"model_provider":provider_name,"provider_host":provider_host,"model":cfg["model"]})
        deployment={"report":report,"changes":C.encode_tree(changes),"before":C.encode_tree({n:original_files[n] for n in changes if n in original_files}),"snapshot_manifest":manifest,"selected_surface":selected,"experience":experience,"mode":mode}
        C.atomic(run/"deployment.json",C.seal(deployment))
        C.event(run,"experiment_complete",status=status)
        return report
    finally:
        C.atomic(run/"cost-ledger.json",runtime.ledger())
        runtime.close()
