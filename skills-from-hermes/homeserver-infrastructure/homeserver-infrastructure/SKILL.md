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

When the user asks about **Docker containers**, **host volumes mounted into my container**, or **how to connect personal cloud services** (Amazon Alexa, etc.) from my environment running on their personal server — follow this skill. Common first-time and recurring questions for users who run Hermes on a media/compute server.

## 1. Diagnosing Docker Container Topology

User often asks: *"does hermes use N docker containers?"* or *"what are those containers I see in Docker Desktop?"*.

**Step 1 — Clarify which instance.** Explain that each hermes agent session/terminal tab typically gets its own execution container, so the count matches active sessions plus user's other services. Then ask: "Which one do you care about right now?"

**Step 2 — Verify with docker if available on host:**
```bash
docker ps --format "{{.ID}}\t{{.Names}}\t{{.Image}}"
docker inspect <container>          # for volume mounts and details
```

If `docker` is not installed/available (common — tools run inside a sandboxed agent container), **fall back to the user**: tell them how to check Docker Desktop or ask them to run the commands on their host machine. Don't claim "docker not found" as permanent failure from my environment — it's an environment limitation, not actual absence of containers.

## 2. Verifying Host Volume Mounts

When user mentions directories they see in my filesystem (like `/sandbox` with `in/`, `out/`, `temp/`) and asks whether I have write access or what's already mounted:

**Golden rule: list first before suggesting more mounts.**
```bash
ls -la /sandbox          # check what's actually exposed from host as a volume root
df /path                 # "Type" column shows overlay (internal) vs backed fs type for volumes
stat -f /sandbox         # macOS equivalent
mount | grep sandbox     # Linux mount points
```

If directories exist and are writable, they're likely already mounted Docker volumes from the user's compose file. Don't suggest setting up a second identical mount — confirm with the user that what exists fits their plan before proposing changes.

## 3. Integrating Personal Cloud Services via Docker on User's Server

User may want me to control **cloud-only platforms** (Amazon/Alexa, SmartLife/Tuya) from my environment sitting inside their local media server. These services lack public CLI tools like `openhue`, so we bridge them:

> See `references/alexa-integration.md` for detailed path options and trade-offs.

Use the decision matrix in Alexa integration references to guide user through choices (Home Assistant, MQTT bridge, official API). Always prefer **the simplest viable approach** and note maintenance burden as a factor — Amazon's APIs tend to break/chg endpoints or new OAuth flows.

## 4. Delegating vs Direct Execution

- Simple curl/script against an exposed endpoint/API → do directly
- Full container orchestration (Home Assistant, message broker, bridge scripts) with multiple moving parts: delegate the heavy lifting while you guide architecture decisions and verify outcomes
- If user hasn't picked integration path yet: explain options then defer action until ready to build

## Ollama Keep-Alive Pitfall

When the user asks about keeping VRAM models loaded in memory via `OLLAMA_KEEP_ALIVE`, assume a moderate timeout first (like `"30m"`), NOT `-1` for forever. The default is 5 min which is often too aggressive but going "forever" is rarely what users actually want — they usually just want something more reasonable before unloading.

**Always present the value explicitly:**
- `30m` = 30 minutes idle retention (most common use)
- `-1` = keep loaded forever (expensive, rarely needed)

## Pitfalls

- **"docker not found" is an environment limitation**—not a permanent blocker. Agent sessions run inside their own container and may lack Docker socket access even when the user's host has it perfectly set up. Ask the user to run diagnostics on their host machine.
- **Multiple CLI tabs create multiple containers** — always clarify session identity before running docker commands or troubleshooting "why so many."
- **Cloud platform APIs update frequently.** Any bridge script solution carries an implicit maintenance requirement the user should be aware of. Prefer solutions backed by active communities when recommending long-term integrations.
