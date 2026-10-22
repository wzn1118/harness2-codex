"""Project adapter; all PPC scheduling/prompts/selection use Google code."""
from __future__ import annotations
import contextlib
import difflib
import io
import json
import os
import secrets
import shutil
import sys
import threading
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

class Substrate:
    name = "cx"
    def __init__(self, original):
        self.spec = replace(CODEX_SURFACE, always_on=("AGENTS.md",P.TOOLS,P.STRATEGY),always_on_max=max(16000,sum(len(v) for v in original.values())+8000),hooks_file=None,hook_events=frozenset(),stubs=())
    def surface(self): return self.spec

class ProjectAdapter:
    def __init__(self, run, snapshot, task, runtime):
        self.run,self.snapshot,self.task,self.runtime = run,snapshot,task,runtime
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
        work = out / "workspace";P.materialize(self.snapshot,work,surface)
        before = P.tree_hash(work)
        task_files_before=P.scan(work)
        result = self.runtime.call(work,self.task + "\n\nDo the task in /work. Do not alter AGENTS.md, Skills, harness2-tools.json, or harness2-execution.json. Do not change tests or permission policies. Report commands and observed results.","roll-"+run_id,strategy=json.loads(surface[P.STRATEGY]),tools=json.loads(surface[P.TOOLS]))
        P.reject_worker_links(work)
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
        session=self.adapter.run/"sessions"/label
        session.mkdir(parents=True,exist_ok=False)
        shutil.copytree(context_dir,session/"context")
        before=P.tree_hash(session)
        directions="\n\nThis session uses an additive-only safe project adapter. Edit only context/harness/AGENTS.md, .agents/skills/* components within context/harness/, context/harness/harness2-tools.json (shell/apply_patch booleans), or context/harness/harness2-execution.json (reasoning_effort low/medium/high, timeout_sec 30..600). The adapter's JSON configuration files are controls, not prose: keep them valid JSON with exactly their existing keys. Preserve all existing instruction text verbatim as a prefix; existing scripts are immutable. Never create hooks, subagents, arbitrary MCP commands, or permission changes. Editing tool/strategy JSON changes actual executor options. For selection or reflection do not edit any file. Reply using the exact structured blocks requested by the upstream mission."
        readonly=not kwargs.get("harvest")
        r=self.adapter.runtime.call(session,mission+directions,"improve-"+label,readonly=readonly)
        try:P.reject_worker_links(session)
        except ValueError as exc:raise ProposerError(str(exc),attempts=1) from exc
        after=P.tree_hash(session)
        changed={n for n in set(before)|set(after) if before.get(n)!=after.get(n)}
        illegal=[n for n in changed if not n.startswith("context/harness/")]
        if r["exit_code"] or not r["last_message"].strip() or illegal:
            raise ProposerError("Codex session failed or changed evidence outside harness: "+str(illegal)+" "+r["stderr"][-700:],attempts=1)
        reply=Reply(label=label,text=r["last_message"],ok=True,attempts=1,wall_s=r["wall_s"],workspace=session/"context/harness" if kwargs.get("harvest") else None)
        with self.lock:self._calls.append(reply)
        return reply

def summarize_validation(rows,repeats):
    baseline=sum(x["passed"] for x in rows if x["kind"]=="baseline")
    candidate=sum(x["passed"] for x in rows if x["kind"]=="candidate")
    valid=len(rows)==2*repeats and all(sum(x["kind"]==arm for x in rows)==repeats for arm in ("baseline","candidate")) and not any(x.get("infrastructure_errors") or x.get("agent_stop_reason") or x["check"].get("stop_reason") or (x.get('agent_exit_code',0)!=0 and x.get('known_provider_usage',{}).get('responses_with_usage')==0) for x in rows)
    return {"rows":rows,"baseline_passed":baseline,"candidate_passed":candidate,"repeats":repeats,"comparison_valid":valid,"non_regression":valid and candidate==repeats and candidate>=baseline,"measured_success_gain":valid and candidate>baseline}

def verify_pair(adapter,selected,request,cancel):
    """Hold-out checks are never supplied to the proposer/composer/selector."""
    argv=request["check_argv"]
    repeats=request.get("validation_repeats",2)
    rows=[]
    protected={n:data for n,data in adapter.original_files.items() if re_test_path(n)}
    for trial in range(repeats):
        order=("baseline","candidate") if trial%2==0 else ("candidate","baseline")
        for kind in order:
            surface=adapter.original if kind=="baseline" else selected
            dest=adapter.run/"holdout"/f"{trial}-{kind}";dest.mkdir(parents=True)
            work=dest/"workspace";P.materialize(adapter.snapshot,work,surface)
            result=adapter.runtime.call(work,adapter.task+"\n\nComplete the task in /work without changing tests, harness files or permissions. Report actual results.",f"holdout-{trial}-{kind}",strategy=json.loads(surface[P.STRATEGY]),tools=json.loads(surface[P.TOOLS]))
            P.reject_worker_links(work)
            # Restore protected tests byte-for-byte before independent checks.
            for name,data in protected.items():
                path=C.target(work,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            forbidden=[]
            for name,text in surface.items():
                p=work/name
                if not p.exists() or p.read_bytes()!=text.encode():forbidden.append(name)
            check=guard.check_command(work,argv,timeout=request.get("check_timeout_sec",120),cancel=cancel,output=dest/"check.json")
            rows.append({"trial":trial,"kind":kind,"check":check,"harness_mutations":forbidden,"agent_exit_code":result["exit_code"],"agent_stop_reason":result.get("stop_reason"),"infrastructure_errors":result.get("infrastructure_errors",[]),"agent_wall_s":result.get("wall_s"),"known_provider_usage":result.get("known_provider_usage"),"passed":check["exit_code"]==0 and result["exit_code"]==0 and not forbidden})
            C.atomic(adapter.run/"holdout-results.json",rows)
            C.event(adapter.run,"holdout_check",result=rows[-1])
    return summarize_validation(rows,repeats)

def re_test_path(name):
    parts=Path(name).parts
    return any(p in {"tests","test","__tests__"} for p in parts) or Path(name).name.startswith("test_")

def experiment(run,request,cancel):
    C.verify_upstream();guard.available()
    cfg=host_config()
    C.event(run,"preflight",upstream_commit=C.COMMIT,model=cfg["model"])
    project=Path(request["project_root"]).resolve()
    P.check_policy_context(project)
    if C.STATE==project or C.STATE.is_relative_to(project):raise ValueError("State directory must be outside the project")
    manifest=P.snapshot(project,run/"snapshot")
    C.atomic(run/"snapshot-manifest.json",manifest)
    runtime=Runtime(run,cancel,cfg,max_requests=request.get("max_requests",160),max_output_tokens=request.get("max_output_tokens",655360),prices=request.get("prices"),budget_request=request,execution_timeout_sec=request.get('execution_timeout_sec'))
    try:
        adapter=ProjectAdapter(run,run/"snapshot",request["task"],runtime)
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
        experience={};validation=None;status="no_adoption"
        original_files=adapter.original_files
        changes={n:text.encode() for n,text in (selected or {}).items() if original_files.get(n)!=text.encode() and (n in original_files or text!=adapter.original.get(n))}
        if selected and (changes or request.get('validate_unchanged', False)):
            adapter.validate_tree(selected)
            validation=verify_pair(adapter,selected,request,cancel)
            if not changes:
                status='no_adoption'
            elif validation["non_regression"]:
                status="awaiting_confirmation"
                try:
                    with (run/"upstream.log").open("a") as log,contextlib.redirect_stdout(log):
                        experience=selection.reflect_adoption("project-task",chosen,result.rollouts,adapter=adapter,prop=improver,work_dir=run/"method",decisions={s.label:s.notes or s.error for s in result.stages})
                except Exception as exc:C.event(run,"reflection_failed",error=str(exc))
            else: status="validation_failed" if validation["comparison_valid"] else "validation_inconclusive"
        patch=[]
        for name,data in changes.items():
            patch.extend(difflib.unified_diff(original_files.get(name,b"").decode().splitlines(keepends=True),data.decode().splitlines(keepends=True),"before/"+name,"after/"+name))
        report={"run_id":run.name,"status":status,"project_root":str(project),"model":cfg["model"],"upstream_commit":C.COMMIT,"method":result.to_dict(),"selection":chosen.to_dict(),"validation":validation,"ledger":runtime.ledger(),"changed_files":sorted(changes),"diff":"".join(patch),"created_at":C.stamp(),"confirmation_token":secrets.token_urlsafe(24) if status=="awaiting_confirmation" else None,"experience":experience}
        C.atomic(run/"report.json",C.seal(report))
        C.atomic(run/"status.json",{"status":status,"run_id":run.name,"model":cfg["model"]})
        deployment={"report":report,"changes":C.encode_tree(changes),"before":C.encode_tree({n:original_files[n] for n in changes if n in original_files}),"snapshot_manifest":manifest,"selected_surface":selected,"experience":experience,"mode":mode}
        C.atomic(run/"deployment.json",C.seal(deployment))
        C.event(run,"experiment_complete",status=status)
        return report
    finally:
        C.atomic(run/"cost-ledger.json",runtime.ledger())
        runtime.close()
