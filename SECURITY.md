# Security

The bridge is intended to listen on loopback only and forward to a configured
HTTP or HTTPS upstream.

It does not log request bodies, authorization headers, API keys, or local user
content. Status snapshots and automation status may contain conversation IDs,
titles, and working directories from the local Codex database; keep the
`state/`, migration backups, and snapshot directories out of version control.

Provider probes send a fixed, non-user prompt and persist only status metadata;
they do not persist response text or credentials. Lifecycle status and branch
history also contain conversation IDs, titles, and working directories. Hook
and policy backups may contain the previous local hook configuration and must
be treated with the same local-data restrictions.

Do not expose the bridge port to a LAN or the public internet.
