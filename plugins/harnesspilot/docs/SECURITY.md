# Security Boundaries

## Credentials

- `config.toml` and `auth.json` are read from the selected Codex home. The installer never copies, prints, or edits them and verifies their hashes after plugin registration.
- Only API-key authentication with the Responses wire protocol is supported. OAuth session material is rejected.
- Provider keys, provider headers, and provider query values stay in the MCP broker process. Snapshots and returned diagnostics apply known-token and configured-secret redaction.
- The sandbox receives a short-lived random broker credential, not the provider credential. The broker socket is created in a private temporary directory with mode `0600` and accepts only bounded Responses requests.

## Isolation and writes

- The MCP service runs as the dedicated unprivileged `harnesspilot` Linux account.
- Experiment processes run under bubblewrap with separate user, mount, PID, IPC, UTS, and network namespaces, a temporary home, resource limits, a bounded writable workspace, and no direct network interface.
- Missing bubblewrap or namespace support is a hard failure. There is no unsandboxed execution fallback.
- Project scanning is read-only. Candidate application and rollback require a preview, a one-time approval token, and explicit user confirmation.
- Runtime state and snapshots are stored under the configured HarnessPilot state directory. Review retention and filesystem ACLs before scanning confidential projects.

## Limits

- Token estimates and accounting are not a USD billing cap; provider pricing and tokenizer differences remain outside HarnessPilot's control.
- The public smoke suite contains visible expected answers. Its `sealed` label does not create a private holdout boundary.
- A local Codex plugin, its MCP server, the Linux CLI package, and bubblewrap are separate components. A healthy doctor result proves local wiring and isolation probes, not a successful provider request or measured model improvement.
