"""Human-gated, signed, journaled project updates with drift-safe rollback."""
from __future__ import annotations
import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path
from . import common as C, project as P

def native_confirm(title, message):
    # This is an OS GUI confirmation, not an LLM-supplied `confirmed: true`.
    # No environment variable can bypass it. Headless hosts fail closed.
    try:
        import tkinter as tk
        from tkinter import messagebox
        root=tk.Tk();root.withdraw()
        try:return bool(messagebox.askyesno(title,message,default="no",parent=root))
        finally:root.destroy()
    except Exception as exc:
        raise RuntimeError("Native user confirmation unavailable; nothing changed. Open the local approval dialog in a graphical desktop: " + str(exc))

def write_file(target,data,mode=None):
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".h2-",dir=target.parent)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        if mode is not None:os.chmod(tmp,mode)
        os.replace(tmp,target)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def preview(run_id):
    run=C.runpath(run_id)
    if (run/"audit-invalidation.json").exists():
        invalidation=C.unseal(C.read(run/"audit-invalidation.json"))
        return {"run_id":run_id,"eligible":False,"reason":"independent audit invalidated this run","audit_invalidation":invalidation}
    payload=C.unseal(C.read(run/"deployment.json"));report=payload["report"]
    validation=report.get("validation") or {}
    root=Path(report["project_root"])
    drift=P.tree_hash(root)!=payload["snapshot_manifest"]
    issues=[]
    if report.get("status")!="awaiting_confirmation":issues.append("run is not awaiting confirmation")
    if report.get("ppc_infrastructure_failures"):issues.append("PPC had provider or worker infrastructure failures")
    if validation.get("comparison_valid") is not True:issues.append("paired comparison is incomplete or invalid")
    if validation.get("effect_conclusion")!="improvement_supported":issues.append("held-out family-bootstrap interval does not support an improvement")
    if validation.get("non_regression") is not True:issues.append("candidate does not satisfy the non-regression gate")
    if not validation.get("confirmatory_suite"):issues.append("confirmatory held-out suite is missing")
    if drift:issues.append("project snapshot drift detected; rerun before applying")
    selected=payload.get("selected_surface")
    if isinstance(selected,dict):P.validate(selected,P.load_surface(run/"snapshot"),P.scan(run/"snapshot"))
    else:issues.append("no selected candidate surface exists")
    return {"run_id":run_id,"eligible":not issues,"issues":issues,"project_root":str(root),"changed_files":report.get("changed_files",[]),"diff":report.get("diff",""),"validation":validation,"project_drift":drift,"writes_performed":False,"confirmation_opened":False}

def deploy(run_id, token, *, approve=native_confirm):
    run=C.runpath(run_id)
    lockfile=run/"deployment.lock"
    with lockfile.open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if (run/"audit-invalidation.json").exists():
            invalidation=C.unseal(C.read(run/"audit-invalidation.json"))
            raise ValueError("Run invalidated by independent audit: "+invalidation["reason"])
        payload=C.unseal(C.read(run/"deployment.json"));report=payload["report"]
        if report["status"]!="awaiting_confirmation":raise ValueError("Run is not awaiting confirmation")
        if report.get("ppc_infrastructure_failures"):raise ValueError("PPC infrastructure failures invalidate the candidate")
        root=Path(report["project_root"])
        if P.tree_hash(root)!=payload["snapshot_manifest"]:raise ValueError("Project drift detected since experiment; rerun instead of overwriting changes")
        if not report.get("validation",{}).get("non_regression"):
            raise ValueError("Independent non-regression validation is required before application")
        if report["validation"].get("comparison_valid") is not True:
            raise ValueError("Valid, resource-unconfounded paired comparison required; older runs must be repeated")
        if report["validation"].get("effect_conclusion")!="improvement_supported" or report["validation"].get("confirmatory_suite") is not True:
            raise ValueError("A statistically supported improvement on a protected multi-family hold-out suite is required before application")
        if not token or not secrets_equal(token,report["confirmation_token"]):raise ValueError("Invalid confirmation token")
        changes=C.decode_tree(payload["changes"]);before=C.decode_tree(payload["before"])
        P.validate(payload["selected_surface"],P.load_surface(run/"snapshot"),P.scan(run/"snapshot"))
        summary="Project: "+str(root)+"\nRun: "+run_id+"\nFiles: "+", ".join(changes)+"\nIndependent validation: "+json.dumps(report["validation"])+"\n\nApply only the reviewed additive harness changes? A backup and rollback record will be retained."
        if not approve("Harness2: apply verified harness",summary):return {"status":"confirmation_declined","run_id":run_id}
        if P.tree_hash(root)!=payload["snapshot_manifest"]:raise ValueError("Project changed during confirmation; nothing applied")
        journal={"status":"applying","files":[],"at":C.stamp()}
        C.atomic(run/"journal.json",C.seal(journal))
        try:
            for name,data in changes.items():
                path=C.target(root,name)
                expected=C.digest(before[name]) if name in before else None
                if C.filehash(path)!=expected:raise ValueError("Target changed before atomic write: "+name)
                mode=(path.stat().st_mode&0o777) if path.exists() else 0o644
                entry={"path":name,"before":expected,"after":C.digest(data),"mode":mode}
                journal["files"].append(entry);C.atomic(run/"journal.json",C.seal(journal))
                write_file(path,data,mode)
            journal["status"]="applied";C.atomic(run/"journal.json",C.seal(journal))
        except Exception:
            for item in reversed(journal["files"]):
                path=C.target(root,item["path"])
                if C.filehash(path)!=item["after"]:continue
                if item["path"] in before:write_file(path,before[item["path"]],item["mode"])
                else:path.unlink()
            journal["status"]="apply_failed_recovered";C.atomic(run/"journal.json",C.seal(journal))
            raise
        report["status"]="applied";report["applied_at"]=C.stamp()
        payload["report"]=report
        C.atomic(run/"deployment.json",C.seal(payload));C.atomic(run/"report.json",C.seal(report))
        C.atomic(run/"status.json",{"run_id":run_id,"status":"applied"})
        # Persistent experience is exposed to future experiments only after a
        # validated candidate has actually been human-approved and deployed.
        if payload.get("experience"):
            try:
                from harness2.core.library import Library
                from .engine import Substrate
                library=Library(C.STATE/"experience"/C.digest(str(root).encode())[:20]/payload["mode"],"project",Substrate(payload["selected_surface"]).surface())
                library.add(run_id,payload["selected_surface"],why=report["selection"]["why"],label=report["selection"]["chosen_label"],experience=payload["experience"])
            except Exception as exc:
                C.event(run,"experience_persistence_failed",error=str(exc))
        C.event(run,"applied",files=sorted(changes))
        return {"status":"applied","run_id":run_id,"files":sorted(changes)}

def rollback(run_id, *, approve=native_confirm):
    run=C.runpath(run_id)
    with (run/"deployment.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        payload=C.unseal(C.read(run/"deployment.json"));report=payload["report"]
        journal=C.unseal(C.read(run/"journal.json"))
        if journal["status"] not in {"applied","applying"}:raise ValueError("Run is not applied or recovering an interrupted apply")
        root=Path(report["project_root"]);before=C.decode_tree(payload["before"])
        restore=[]
        for item in journal["files"]:
            path=C.target(root,item["path"]);current=C.filehash(path)
            if current==item["after"]:restore.append(item)
            elif current!=item["before"]:raise ValueError("Post-apply changes detected; rollback refuses to overwrite "+item["path"])
        if not approve("Harness2: rollback", "Restore original harness bytes in "+str(root)+"?\nFiles: "+", ".join(x["path"] for x in restore)):return {"status":"confirmation_declined"}
        for item in restore:
            path=C.target(root,item["path"])
            if C.filehash(path)!=item["after"]:raise ValueError("File changed during rollback confirmation")
        for item in restore:
            path=C.target(root,item["path"])
            if item["path"] in before:write_file(path,before[item["path"]],item["mode"])
            else:path.unlink()
        journal["status"]="rolled_back";report["status"]="rolled_back"
        payload["report"]=report
        C.atomic(run/"journal.json",C.seal(journal));C.atomic(run/"deployment.json",C.seal(payload));C.atomic(run/"report.json",C.seal(report))
        C.atomic(run/"status.json",{"run_id":run_id,"status":"rolled_back"})
        lib=C.STATE/"experience"/C.digest(str(root).encode())[:20]/payload["mode"]
        if lib.exists():
            from harness2.core.library import Library
            from .engine import Substrate
            Library(lib,"project",Substrate(payload["selected_surface"]).surface()).forget([run_id])
        C.event(run,"rolled_back")
        return {"status":"rolled_back","run_id":run_id,"files":[x["path"] for x in restore]}

def secrets_equal(a,b):
    import hmac
    return hmac.compare_digest(a,b)
