---
hookEvent: PreToolUse
type: script
priority: 10
matcher: "^bash$"
timeout: 10000
---

```bash
wsl -d Ubuntu-22.04 -- node /home/jbecerra/projects/bash-mcp/infra/hooks/bash-mcp-redirect.js
```
