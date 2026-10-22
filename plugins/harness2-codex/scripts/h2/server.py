from __future__ import annotations
import argparse
import json
import os
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
    allowed={"project_root","task","k","mode","check_argv","validation_repeats","check_timeout_sec","max_requests","max_output_tokens","max_total_tokens","execution_timeout_sec","validate_unchanged","prices"}
    if set(a)-allowed:raise ValueError("Unknown experiment arguments: "+str(sorted(set(a)-allowed)))
    for key in ("project_root","task","check_argv"):
        if not a.get(key):raise ValueError(key+" is required")
    if not Path(a["project_root"]).is_absolute():raise ValueError("project_root must be absolute")
    root=Path(a["project_root"]).resolve()
    if not root.is_dir():raise ValueError("Project directory missing")
    if root==Path(root.anchor):raise ValueError("Cannot optimize an entire filesystem")
    if not isinstance(a["task"],str) or len(a["task"])>30000:raise ValueError("Task must be text <=30000 chars")
    argv=a["check_argv"]
    if not isinstance(argv,list) or not argv or any(not isinstance(x,str) or not x for x in argv):raise ValueError("check_argv must be a nonempty argument array")
    if not argv[0].startswith("/usr/bin/"):raise ValueError("Validation executable must be an absolute /usr/bin/ path; use python3/node/bash with explicit script arguments")
    bounds={"k":(1,3),"validation_repeats":(2,5),"check_timeout_sec":(5,600),"max_requests":(8,1000),"max_output_tokens":(8192,4096000),"max_total_tokens":(100000,50000000),"execution_timeout_sec":(30,600)}
    for key,(low,high) in bounds.items():
        if key in a and (type(a[key]) is not int or not low<=a[key]<=high):raise ValueError(f"{key} must be {low}..{high}")
    if a.get("mode","parallel") not in {"parallel","sequential"}:raise ValueError("Unknown recursion mode")
    if 'validate_unchanged' in a and type(a['validate_unchanged']) is not bool:raise ValueError('validate_unchanged must be boolean')
    if a.get("prices"):
        p=a["prices"]
        if set(p)!={"input_per_million","output_per_million"} or any(type(v) not in (int,float) or not 0<=v<=1000 for v in p.values()):raise ValueError("Invalid user-supplied per-million token pricing")
    return {**a,"project_root":str(root)}

def start(a):
    request=validate_request(a);P.check_policy_context(Path(request["project_root"]));guard.available();C.verify_upstream();C.state_key()
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
                C.atomic(run/"status.json",{"status":"cancelled" if cancel.is_set() else "failed","run_id":run_id,"error":str(exc)})
        thread=threading.Thread(target=worker,daemon=True)
        JOBS[run_id]={"thread":thread,"cancel":cancel};thread.start()
    from .budget import allocation
    limits,plan=allocation(request)
    return {"run_id":run_id,"status":"running","model":cfg["model"],"upstream_commit":C.COMMIT,"budget_limits":limits,"resource_plan":plan,"note":"Real isolated model executions started. No project modifications. Poll harness2_status; apply always needs a native user confirmation."}

def call(name,a):
    if name=="harness2_inspect":
        root=Path(a["project_root"]).resolve();files=P.scan(root)
        surface=P.load_surface(root)
        from .runtime import host_config
        try:
            cfg=host_config();runtime={"ready":True,"model":cfg["model"],"sandbox_mode":cfg["sandbox_mode"],"approval_policy":cfg["approval_policy"]}
        except Exception as exc:runtime={"ready":False,"reason":str(exc)}
        return {"project_root":str(root),"supported_surface":sorted(surface),"policy":"Additive AGENTS/Skills only; tool booleans and bounded effort/timeout; no user/global config or MCP command changes","file_count":len(files),"upstream_commit":C.COMMIT,"check_required":"Supply trusted existing check_argv for independent holdout verification","namespace_sandbox":guard.available(),"runtime":runtime,"native_confirmation_display_configured":bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),"headless_note":"No GUI means no apply/rollback; no confirmation bypass exists"}
    if name=="harness2_run":return start(a)
    if name=="harness2_status":
        if a.get("run_id"):
            run=C.runpath(a["run_id"]);value=C.read(run/"status.json")
            if (run/"report.json").exists():
                report=C.unseal(C.read(run/"report.json"));value.update({k:report.get(k) for k in ("status","changed_files","diff","validation","ledger","confirmation_token","selection","upstream_commit","model")})
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
RUN_SCHEMA=schema({"project_root":STRING,"task":STRING,"check_argv":{"type":"array","items":STRING,"minItems":1},"k":{"type":"integer","minimum":1,"maximum":3},"mode":{"type":"string","enum":["parallel","sequential"]},"validation_repeats":{"type":"integer","minimum":2,"maximum":5},"check_timeout_sec":{"type":"integer","minimum":5,"maximum":600},"max_requests":{"type":"integer","minimum":8,"maximum":300},"max_output_tokens":{"type":"integer","minimum":8192,"maximum":1000000},"prices":schema({"input_per_million":{"type":"number","minimum":0},"output_per_million":{"type":"number","minimum":0}},["input_per_million","output_per_million"])},["project_root","task","check_argv"])
RUN_SCHEMA['properties'].update({'max_requests':{'type':'integer','minimum':8,'maximum':1000},'max_output_tokens':{'type':'integer','minimum':8192,'maximum':4096000},'max_total_tokens':{'type':'integer','minimum':100000,'maximum':50000000},'execution_timeout_sec':{'type':'integer','minimum':30,'maximum':600}})
RUN_SCHEMA['properties']['validate_unchanged']={'type':'boolean'}
TOOLS=[
 {"name":"harness2_inspect","description":"Inspect supported project harness components and OS sandbox prerequisites without editing the project.","inputSchema":schema({"project_root":STRING},["project_root"]),"annotations":{"readOnlyHint":True}},
 {"name":"harness2_run","description":"Start actual pinned Harness2 PPC experiments asynchronously. Costs API tokens; needs a trusted holdout check. No project changes. Never simulate missing stages.","inputSchema":RUN_SCHEMA,"annotations":{"readOnlyHint":False,"openWorldHint":True}},
 {"name":"harness2_status","description":"Read progress, actual execution audit, verification contrasts, diff and costs.","inputSchema":schema({"run_id":STRING}),"annotations":{"readOnlyHint":True}},
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
