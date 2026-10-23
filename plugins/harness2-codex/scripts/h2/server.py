from __future__ import annotations
import argparse
import json
import os
import re
import secrets
import sys
import threading
from datetime import datetime,timezone
from pathlib import Path
from . import common as C, guard, project as P

JOBS={}
JOB_LOCK=threading.RLock()
PROTOCOL_STDOUT=sys.stdout

def validate_request(a):
    allowed={"project_root","task","k","mode","check_argv","validation_tasks","validation_repeats","check_timeout_sec","max_requests","max_output_tokens","max_total_tokens","execution_timeout_sec","validate_unchanged","prices"}
    if set(a)-allowed:raise ValueError("Unknown experiment arguments: "+str(sorted(set(a)-allowed)))
    for key in ("project_root","task"):
        if not a.get(key):raise ValueError(key+" is required")
    if not Path(a["project_root"]).is_absolute():raise ValueError("project_root must be absolute")
    root=Path(a["project_root"]).resolve()
    if not root.is_dir():raise ValueError("Project directory missing")
    if root==Path(root.anchor):raise ValueError("Cannot optimize an entire filesystem")
    if not isinstance(a["task"],str) or len(a["task"])>30000:raise ValueError("Task must be text <=30000 chars")
    if not a["task"].strip():raise ValueError("Task must not be blank")
    def validate_argv(argv):
        if not isinstance(argv,list) or not argv or any(not isinstance(x,str) or not x for x in argv):raise ValueError("check_argv must be a nonempty argument array")
        if not argv[0].startswith("/usr/bin/"):raise ValueError("Validation executable must be an absolute /usr/bin/ path; use python3/node/bash with explicit script arguments")
    tasks=a.get("validation_tasks")
    if tasks is None:
        if "check_argv" not in a:raise ValueError("Supply check_argv or a validation_tasks hold-out suite")
        validate_argv(a["check_argv"])
        project_files=P.scan(root)
        hidden=[name for name in project_files if any(part in {"test","tests","__tests__"} for part in Path(name).parts) or Path(name).name.startswith("test_")]
        tasks=[{"task_id":"primary","family":"single-task","task":a["task"],"check_argv":a["check_argv"],"protected_paths":hidden}]
        a={**a,"private_paths":hidden}
    else:
        if "check_argv" in a:raise ValueError("Use validation_tasks without the legacy top-level check_argv")
        if not isinstance(tasks,list) or not 10<=len(tasks)<=50:raise ValueError("Confirmatory validation_tasks must contain 10..50 tasks")
        seen_ids=set();seen_prompts=set();normalized=[];all_protected=set()
        project_files=P.scan(root)
        train_prompt=" ".join(a["task"].casefold().split())
        for item in tasks:
            if not isinstance(item,dict) or set(item)!={"task_id","family","task","check_argv","protected_paths"}:raise ValueError("Each validation task needs exactly task_id, family, task, check_argv and protected_paths")
            task_id=item["task_id"];family=item["family"];prompt=item["task"]
            if not isinstance(task_id,str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}",task_id) or task_id in seen_ids:raise ValueError("validation task_id must be unique lowercase letters, digits, '_' or '-'")
            if not isinstance(family,str) or not family.strip() or len(family)>100:raise ValueError("validation task family must be nonempty text <=100 chars")
            if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>12000:raise ValueError("validation task prompt must be text <=12000 chars")
            prompt_key=" ".join(prompt.casefold().split())
            if prompt_key==train_prompt or prompt_key in seen_prompts:raise ValueError("Hold-out prompts must be distinct from the PPC task and each other")
            validate_argv(item["check_argv"])
            paths=item["protected_paths"]
            if not isinstance(paths,list) or not paths or any(not isinstance(x,str) for x in paths):raise ValueError("Each held-out verifier needs one or more protected_paths")
            clean=[]
            for name in paths:
                C.relative(name)
                if name not in project_files:raise ValueError("Protected verifier file is missing or excluded from the project snapshot: "+name)
                if name in clean:raise ValueError("Duplicate protected verifier path: "+name)
                clean.append(name);all_protected.add(name)
            seen_ids.add(task_id);seen_prompts.add(prompt_key)
            normalized.append({"task_id":task_id,"family":family.strip(),"task":prompt,"check_argv":item["check_argv"],"protected_paths":clean})
        tasks=normalized
        path_owners={}
        for task in tasks:
            for name in task["protected_paths"]:
                if name in path_owners and path_owners[name]!=task["task_id"]:raise ValueError("Protected verifier files must be task-specific and disjoint: "+name)
                path_owners[name]=task["task_id"]
        if "check_argv" in a:validate_argv(a["check_argv"])
        a={**a,"private_paths":sorted(all_protected)}
    bounds={"k":(1,3),"validation_repeats":(2,5),"check_timeout_sec":(5,600),"max_requests":(8,1000),"max_output_tokens":(8192,4096000),"max_total_tokens":(100000,50000000),"execution_timeout_sec":(30,600)}
    for key,(low,high) in bounds.items():
        if key in a and (type(a[key]) is not int or not low<=a[key]<=high):raise ValueError(f"{key} must be {low}..{high}")
    if a.get("mode","parallel") not in {"parallel","sequential"}:raise ValueError("Unknown recursion mode")
    if 'validate_unchanged' in a and type(a['validate_unchanged']) is not bool:raise ValueError('validate_unchanged must be boolean')
    if a.get("prices"):
        p=a["prices"]
        if set(p)!={"input_per_million","output_per_million"} or any(type(v) not in (int,float) or not 0<=v<=1000 for v in p.values()):raise ValueError("Invalid user-supplied per-million token pricing")
    return {**a,"project_root":str(root),"validation_tasks":tasks,"validate_unchanged":a.get("validate_unchanged",True)}

def start(a):
    request=validate_request(a);P.check_policy_context(Path(request["project_root"]));guard.available();C.verify_upstream();C.state_key()
    from .budget import allocation,minimum_session_budget_ok
    limits,plan=allocation(request)
    if not minimum_session_budget_ok(plan):raise ValueError("Budget too small for this held-out suite: each validation arm needs at least 4 requests, 16,384 output tokens and 100,000 total tokens. Call harness2_estimate_budget and raise explicit limits before spending.")
    from .runtime import host_config,binary_path
    cfg=host_config();binary_path()
    with JOB_LOCK:
        if any(x["thread"].is_alive() for x in JOBS.values()):raise ValueError("One active experiment per server; inspect or cancel it before starting another")
        run_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+secrets.token_hex(6)
        run=C.runpath(run_id);run.mkdir(mode=0o700)
        C.atomic(run/"request.json",request)
        C.atomic(run/"status.json",{"status":"running","run_id":run_id,"model":cfg["model"],"created_at":C.stamp()})
        cancel=threading.Event()
        def worker():
            from .engine import experiment,ENGINE_LOCK
            try:
                with ENGINE_LOCK:report=experiment(run,request,cancel)
                C.atomic(run/"status.json",{"status":report["status"],"run_id":run_id})
            except Exception as exc:
                C.event(run,"failed",error=str(exc))
                status="cancelled" if cancel.is_set() else "failed"
                ledger=C.read(run/"cost-ledger.json") if (run/"cost-ledger.json").exists() else None
                failure={"run_id":run_id,"status":status,"run_outcome":"experiment_failed_before_valid_comparison","error":str(exc),"validation":{"comparison_valid":False,"effect_conclusion":"invalid_comparison"},"ledger":ledger,"confirmation_token":None,"changed_files":[],"diff":"","created_at":C.stamp()}
                C.atomic(run/"failure-report.json",C.seal(failure))
                C.atomic(run/"status.json",failure|{"error":str(exc)})
        thread=threading.Thread(target=worker,daemon=True)
        JOBS[run_id]={"thread":thread,"cancel":cancel};thread.start()
    return {"run_id":run_id,"status":"running","model":cfg["model"],"upstream_commit":C.COMMIT,"budget_limits":limits,"resource_plan":plan,"note":"Real isolated model executions started. No project modifications. Poll harness2_status; apply always needs a native user confirmation."}

def call(name,a):
    if name=="harness2_inspect":
        root=Path(a["project_root"]).resolve();files=P.scan(root)
        surface=P.load_surface(root)
        test_files=[name for name in sorted(files) if any(part in {"test","tests","__tests__","verifier","verifiers"} for part in Path(name).parts) or Path(name).name.startswith(("test_","check_")) or Path(name).name.endswith("_test.py")]
        from .runtime import host_config
        try:
            cfg=host_config();runtime={"ready":True,"model":cfg["model"],"sandbox_mode":cfg["sandbox_mode"],"approval_policy":cfg["approval_policy"]}
        except Exception as exc:runtime={"ready":False,"reason":str(exc)}
        return {"project_root":str(root),"supported_surface":sorted(surface),"policy":"Additive AGENTS/Skills only; tool booleans and bounded effort/timeout; no user/global config or MCP command changes","file_count":len(files),"candidate_test_files":test_files[:100],"test_discovery_note":"Filename-only discovery; no test code was executed. Review and supply trusted held-out check_argv plus protected_paths.","upstream_commit":C.COMMIT,"check_required":"Supply trusted validation_tasks for effect claims; a legacy check_argv is exploratory only","namespace_sandbox":guard.available(),"runtime":runtime,"native_confirmation_display_configured":bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),"headless_note":"No GUI means no apply/rollback; no confirmation bypass exists"}
    if name=="harness2_run":return start(a)
    if name=="harness2_estimate_budget":
        from .budget import allocation,minimum_session_budget_ok
        count=a.get("validation_task_count",1);repeats=a.get("validation_repeats",2)
        limits={"max_requests":a.get("max_requests",160),"max_output_tokens":a.get("max_output_tokens",655360),"max_total_tokens":a.get("max_total_tokens",10000000)}
        if type(count) is not int or not 1<=count<=50:raise ValueError("validation_task_count must be 1..50")
        if type(repeats) is not int or not 2<=repeats<=5:raise ValueError("validation_repeats must be 2..5")
        bounds={"max_requests":(8,1000),"max_output_tokens":(8192,4096000),"max_total_tokens":(100000,50000000)}
        for key,(low,high) in bounds.items():
            if type(limits[key]) is not int or not low<=limits[key]<=high:raise ValueError(f"{key} must be {low}..{high}")
        task_ids=["primary"] if count==1 else [f"task-{i}" for i in range(count)]
        _,plan=allocation({**limits,"validation_repeats":repeats,"validation_tasks":[{"task_id":task_id} for task_id in task_ids]})
        first_label="holdout-0-baseline" if count==1 else "holdout-task-0-0-baseline"
        return {"validation_task_count":count,"validation_repeats":repeats,"minimum_families_for_confirmatory_effect":10,"budget_adequate_for_minimum_session_envelope":minimum_session_budget_ok(plan),"minimum_session_envelope":{"requests":4,"output_tokens":16384,"total_tokens":100000},"limits":{"requests":limits["max_requests"],"output_tokens":limits["max_output_tokens"],"total_tokens":limits["max_total_tokens"]},"per_task_arm_envelope":plan[first_label],"resource_plan":plan,"cost_usd":"unknown unless provider rates are supplied; token ceilings are not dollar ceilings"}
    if name=="harness2_get_report":
        run=C.runpath(a["run_id"])
        report_path=run/"report.json" if (run/"report.json").exists() else run/"failure-report.json"
        report=C.unseal(C.read(report_path))
        if (run/"audit-invalidation.json").exists():
            invalidation=C.unseal(C.read(run/"audit-invalidation.json"))
            report={**report,"status":"validation_inconclusive","validation":invalidation["validation"],"audit_invalidation":invalidation,"confirmation_token":None}
        return report
    if name=="harness2_preview_apply":
        from .deploy import preview
        return preview(a["run_id"])
    if name=="harness2_status":
        if a.get("run_id"):
            run=C.runpath(a["run_id"]);value=C.read(run/"status.json")
            if (run/"report.json").exists():
                report=C.unseal(C.read(run/"report.json"));value.update({k:report.get(k) for k in ("status","run_outcome","ppc_infrastructure_failures","changed_files","diff","validation","evaluation_manifest","ledger","confirmation_token","selection","upstream_commit","model")})
            elif (run/"failure-report.json").exists():
                value.update(C.unseal(C.read(run/"failure-report.json")))
            elif (run/"cost-ledger.json").exists():value["ledger"]=C.read(run/"cost-ledger.json")
            if (run/"events.jsonl").exists():value["recent_events"]=[json.loads(x) for x in (run/"events.jsonl").read_text().splitlines()[-8:]]
            if (run/"audit-invalidation.json").exists():
                invalidation=C.unseal(C.read(run/"audit-invalidation.json"))
                value.update({"status":"validation_inconclusive","confirmation_token":None,"audit_invalidation":invalidation,"validation":invalidation["validation"]})
            return value
        return {"runs":[C.read(x/"status.json") for x in sorted(C.STATE.glob("*-*"),reverse=True) if (x/"status.json").exists()][:20]}
    if name=="harness2_cancel":
        with JOB_LOCK:
            job=JOBS.get(a["run_id"])
            if not job:raise ValueError("No active job in this server; stored runs remain readable")
            job["cancel"].set()
        return {"status":"cancellation_requested","run_id":a["run_id"]}
    if name=="harness2_apply":
        from .deploy import deploy
        return deploy(a["run_id"],a["confirmation_token"])
    if name=="harness2_rollback":
        from .deploy import rollback
        return rollback(a["run_id"])
    raise ValueError("Unknown tool: "+name)

def schema(properties,required=()):return {"type":"object","properties":properties,"required":list(required),"additionalProperties":False}
STRING={"type":"string"}
VALIDATION_TASK={"type":"object","properties":{"task_id":{"type":"string","pattern":"^[a-z0-9][a-z0-9_-]{0,49}$"},"family":STRING,"task":STRING,"check_argv":{"type":"array","items":STRING,"minItems":1},"protected_paths":{"type":"array","items":STRING,"minItems":1}},"required":["task_id","family","task","check_argv","protected_paths"],"additionalProperties":False}
RUN_SCHEMA=schema({"project_root":STRING,"task":STRING,"check_argv":{"type":"array","items":STRING,"minItems":1},"validation_tasks":{"type":"array","items":VALIDATION_TASK,"minItems":10,"maxItems":50},"k":{"type":"integer","minimum":1,"maximum":3},"mode":{"type":"string","enum":["parallel","sequential"]},"validation_repeats":{"type":"integer","minimum":2,"maximum":5},"check_timeout_sec":{"type":"integer","minimum":5,"maximum":600},"max_requests":{"type":"integer","minimum":8,"maximum":1000},"max_output_tokens":{"type":"integer","minimum":8192,"maximum":4096000},"max_total_tokens":{"type":"integer","minimum":100000,"maximum":50000000},"execution_timeout_sec":{"type":"integer","minimum":30,"maximum":600},"prices":schema({"input_per_million":{"type":"number","minimum":0},"output_per_million":{"type":"number","minimum":0}},["input_per_million","output_per_million"]),"validate_unchanged":{"type":"boolean"}},["project_root","task"])
TOOLS=[
 {"name":"harness2_inspect","description":"Inspect supported project harness components and OS sandbox prerequisites without editing the project.","inputSchema":schema({"project_root":STRING},["project_root"]),"annotations":{"readOnlyHint":True}},
 {"name":"harness2_estimate_budget","description":"Estimate preallocated request/output/token ceilings before any model calls. This is not a dollar quote.","inputSchema":schema({"validation_task_count":{"type":"integer","minimum":1,"maximum":50},"validation_repeats":{"type":"integer","minimum":2,"maximum":5},"max_requests":{"type":"integer","minimum":8,"maximum":1000},"max_output_tokens":{"type":"integer","minimum":8192,"maximum":4096000},"max_total_tokens":{"type":"integer","minimum":100000,"maximum":50000000}}),"annotations":{"readOnlyHint":True}},
 {"name":"harness2_run","description":"Start actual pinned Harness2 PPC then independently evaluate a hidden multi-task hold-out suite. Costs API tokens. No project changes. Never simulate missing stages.","inputSchema":RUN_SCHEMA,"annotations":{"readOnlyHint":False,"openWorldHint":True}},
 {"name":"harness2_status","description":"Read progress, actual execution audit, verification contrasts, diff and costs.","inputSchema":schema({"run_id":STRING}),"annotations":{"readOnlyHint":True}},
 {"name":"harness2_get_report","description":"Read and integrity-check the complete signed report for a finished experiment.","inputSchema":schema({"run_id":STRING},["run_id"]),"annotations":{"readOnlyHint":True}},
 {"name":"harness2_preview_apply","description":"Read-only drift and deployment eligibility check with the exact diff; does not write files or open confirmation.","inputSchema":schema({"run_id":STRING},["run_id"]),"annotations":{"readOnlyHint":True}},
 {"name":"harness2_cancel","description":"Cancel a running experiment and kill its isolated process groups.","inputSchema":schema({"run_id":STRING},["run_id"])},
 {"name":"harness2_apply","description":"Open native user confirmation and apply only independently verified changes; cannot bypass the GUI or stale-file guards.","inputSchema":schema({"run_id":STRING,"confirmation_token":STRING},["run_id","confirmation_token"]),"annotations":{"readOnlyHint":False,"destructiveHint":True}},
 {"name":"harness2_rollback","description":"Native-confirmed rollback, refusing to overwrite post-apply edits.","inputSchema":schema({"run_id":STRING},["run_id"]),"annotations":{"readOnlyHint":False,"destructiveHint":True}},
]

def send(obj):
    PROTOCOL_STDOUT.write(json.dumps(obj,ensure_ascii=False)+"\n");PROTOCOL_STDOUT.flush()

def serve():
    for line in sys.stdin:
        try:
            q=json.loads(line)
            if not isinstance(q,dict):raise ValueError("JSON-RPC object required")
            method=q.get("method");qid=q.get("id")
            if qid is None:continue
            if method=="initialize":result={"protocolVersion":q.get("params",{}).get("protocolVersion","2024-11-05"),"capabilities":{"tools":{}},"serverInfo":{"name":"harness2-codex","version":"0.2.0"}}
            elif method=="ping":result={}
            elif method=="tools/list":result={"tools":TOOLS}
            elif method=="tools/call":
                p=q.get("params",{})
                try:
                    out=call(p["name"],p.get("arguments",{}));result={"content":[{"type":"text","text":json.dumps(out,indent=2,ensure_ascii=False)}]}
                except Exception as exc:result={"isError":True,"content":[{"type":"text","text":str(exc)}]}
            else:
                send({"jsonrpc":"2.0","id":qid,"error":{"code":-32601,"message":"Unknown method"}});continue
            send({"jsonrpc":"2.0","id":qid,"result":result})
        except (ValueError,KeyError) as exc:send({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":str(exc)}})
    for job in JOBS.values():job["cancel"].set()
    for job in JOBS.values():job["thread"].join(timeout=10)

def main():
    p=argparse.ArgumentParser();p.add_argument("--stdio",action="store_true")
    if not p.parse_args().stdio:p.error("Use --stdio")
    serve()
