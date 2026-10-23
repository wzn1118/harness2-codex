from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .jobs import HarnessPilot

mcp = FastMCP("harnesspilot")
pilot = HarnessPilot()


@mcp.tool()
def hp_scan_project(path: str) -> dict:
    """Create a secret-filtered, immutable text snapshot of an explicit project path."""
    return pilot.scan(path)


@mcp.tool()
def hp_list_tasks(split: str = "all") -> dict:
    """List pinned smoke tasks; tool output withholds toy cases that are public in this demo suite."""
    return pilot.tasks(split)


@mcp.tool()
def hp_estimate_budget(kind: str, snapshot_id: str, baseline_job: str = "",
                       candidate_id: str = "", k: int = 1, limits: dict | None = None,
                       allow_hooks: bool = False) -> dict:
    """Prepare a frozen cost/token plan. No model requests are sent by this tool."""
    return pilot.estimate(kind, snapshot_id, baseline_job=baseline_job,
                          candidate_id=candidate_id, k=k, limits=limits,
                          allow_hooks=allow_hooks)


@mcp.tool()
def hp_start_experiment(plan_id: str, plan_sha256: str, approval_token: str) -> dict:
    """Start one reviewed baseline or optimization plan using its one-time approval token."""
    return pilot.start(plan_id, plan_sha256, approval_token)


@mcp.tool()
def hp_probe(plan_id: str, plan_sha256: str, approval_token: str) -> dict:
    """Start the sealed re-evaluation plan after validation has frozen its candidate."""
    plan = pilot.store.get("plan", plan_id)
    if plan.get("kind") != "sealed":
        raise ValueError("hp_probe 只接受 sealed 复评计划")
    return pilot.start(plan_id, plan_sha256, approval_token)


@mcp.tool()
def hp_get_status(job_id: str = "") -> dict:
    """Read current or recent experiment status and persisted budget usage."""
    return pilot.status(job_id)


@mcp.tool()
def hp_cancel(job_id: str) -> dict:
    """Request cancellation of a running experiment."""
    return pilot.cancel(job_id)


@mcp.tool()
def hp_resume(job_id: str) -> dict:
    """Resume a failed or interrupted job from its persistent per-task checkpoints."""
    return pilot.resume(job_id)


@mcp.tool()
def hp_get_report(job_id: str) -> dict:
    """Return audit metadata, paired validation results, confidence intervals, and candidates."""
    return pilot.report(job_id)


@mcp.tool()
def hp_preview_apply(job_id: str, candidate_id: str) -> dict:
    """Show the exact proposed file diff and mint a short-lived, one-time apply approval."""
    return pilot.preview_apply(job_id, candidate_id)


@mcp.tool()
def hp_apply_candidate(plan_id: str, plan_sha256: str, approval_token: str) -> dict:
    """Apply a reviewed candidate transactionally. This operation changes project files."""
    return pilot.apply(plan_id, plan_sha256, approval_token)


@mcp.tool()
def hp_preview_rollback(application_id: str) -> dict:
    """Show the exact rollback diff and mint a one-time rollback approval."""
    return pilot.preview_rollback(application_id)


@mcp.tool()
def hp_rollback(plan_id: str, plan_sha256: str, approval_token: str) -> dict:
    """Execute a separately reviewed rollback plan."""
    plan = pilot.store.get("plan", plan_id)
    if plan.get("kind") != "rollback":
        raise ValueError("hp_rollback 只接受回滚计划")
    return pilot.apply(plan_id, plan_sha256, approval_token)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
