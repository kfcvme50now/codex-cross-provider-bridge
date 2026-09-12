# Security

The bridge is intended to listen on loopback only and forward to a configured
HTTP or HTTPS upstream.

It does not log request bodies, authorization headers, API keys, or local user
content. Status snapshots may contain conversation titles and working
directories from the local Codex database; keep the `state/` and backup
directories out of version control.

Do not expose the bridge port to a LAN or the public internet.
