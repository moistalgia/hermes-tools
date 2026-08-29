---
name: homeserver-infrastructure
description: "Diagnosing running Docker environment, verifying host volume mounts into my container, and integrating personal cloud services (Alexa, SmartLife etc) on user's media/compute server."
version: 1.0.0
author: community
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [docker, homeserver, infrastructure, volume-mounts, alexa, home-automation]
    homepage: https://github.com/nousresearch/hermes-agent-skill-homeserver-infrastructure
---

# Home Server Infrastructure for Hermes Agent Running on It

Load when the user asks about Docker containers, host volumes mounted into
this container, or connecting personal cloud services (Amazon Alexa, etc.)
from this environment running on their personal server.

## 1. Diagnosing Docker Container Topology

User often asks *"does hermes use N docker containers?"* or *"what are those
containers I see in Docker Desktop?"*.

**Container lifecycle is deployment-specific — see the `hermes-local-topology`
skill, not assumptions here.** Ask which container/instance the user means
before running diagnostics.

**Verify with docker if available on host:**
```bash
docker ps --format "{{.ID}}\t{{.Names}}\t{{.Image}}"
docker inspect <container>          # for volume mounts and details
```

If `docker` is not installed/available (common — tools run inside a
sandboxed agent container), fall back to the user: tell them how to check
Docker Desktop or ask them to run the commands on their host machine.
"docker not found" is an environment limitation, not proof containers don't
exist.

## 2. Verifying Host Volume Mounts

When the user asks whether directories visible in this filesystem (like
`/sandbox` with `in/`, `out/`, `temp/`) are mounted or writable:

**Golden rule: list first before suggesting more mounts.**
```bash
ls -la /sandbox          # check what's actually exposed from host as a volume root
df /path                 # "Type" column shows overlay (internal) vs backed fs type for volumes
stat -f /sandbox         # macOS equivalent
mount | grep sandbox     # Linux mount points
```

If directories exist and are writable, they're likely already mounted Docker
volumes from the user's compose file. Confirm with the user before proposing
a second, redundant mount.

## 3. Integrating Personal Cloud Services via Docker on User's Server

Cloud-only platforms (Amazon/Alexa, SmartLife/Tuya) have no public CLI like
`openhue`, so they need a bridge. See `references/alexa-integration.md` for
the decision matrix (Home Assistant, MQTT bridge, official API). Prefer the
simplest viable approach — Amazon's APIs change endpoints/OAuth flows often,
so factor maintenance burden into the recommendation.

## 4. Delegating vs Direct Execution

- Simple curl/script against an exposed endpoint/API → do directly.
- Full container orchestration (Home Assistant, message broker, bridge
  scripts) with multiple moving parts → delegate the heavy lifting, guide
  architecture decisions, verify outcomes.
- If the user hasn't picked an integration path yet: explain options, defer
  action until they're ready to build.

## Ollama Keep-Alive

If asked about `OLLAMA_KEEP_ALIVE`, recommend a moderate value like `"30m"`
rather than `-1` (forever) — the 5-minute default is often too aggressive but
"forever" is rarely what's actually wanted.
