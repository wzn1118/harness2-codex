# workbuddy-bench runner assets

These runners derive from the workbuddy-bench integration and retain its
[upstream license](LICENSE). See the [pinned revision and patches](../../../dependencies/PINNED.md).

The integration adds editable harness installation and configurable models,
paths, and endpoints. Local model-provider names, environment variables, and
log paths use the `harness2` namespace. Setup installs these agents and harness configurations into the WorkBuddy checkout.

HarnessPilot local adaptation: the Codex agent uses workspace-write and the
non-interactive never approval policy. Sandbox-bypass flags have been removed;
tests/test_workflow.py checks the runner arguments.

[ASSETS.json](../../../dependencies/ASSETS.json) records the shipped file checksums.
Verify them with `python scripts/check_provenance.py`.
