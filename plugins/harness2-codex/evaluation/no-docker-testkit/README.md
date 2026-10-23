# No-Docker Evaluation Testkit

This standard-library-only testkit checks that three frozen synthetic judges
reject known-buggy implementations and accept known fixes. It is an offline
judge smoke test only: it does not call a model and does not implement or
measure Harness².

```bash
python3 evaluation/no-docker-testkit/testkit.py preflight
python3 evaluation/no-docker-testkit/testkit.py offline
python3 evaluation/no-docker-testkit/testkit.py ab --tasks discount,slug --repeats 1
```

`ab` prints a no-call session plan. The testkit deliberately refuses `--live`:
the supplied starter used a removed Codex CLI approval option and would create a
separate model-execution path. Use the plugin's `harness2_run` for actual PPC,
protected hold-out tests, the Codex sandbox, token accounting and signed reports.
Never cite a passing testkit run as evidence that a harness configuration
improves model performance.
