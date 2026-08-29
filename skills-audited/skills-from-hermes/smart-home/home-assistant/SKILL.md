---
name: home-assistant
description: "DEPRECATED — superseded by home-assistant-covers. Do not use; tool names below do not exist in this deployment."
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Smart-Home, HA, IoT, Automation, Integration, deprecated]
---

# Home Assistant REST API Control — DEPRECATED

**This skill is superseded. Do not use it.**

`ha_call_service`, `ha_list_entities`, and `ha_get_state` — the tools this
skill instructs calling — do not exist in this deployment.

Use the **`home-assistant-covers`** skill instead. It documents the real,
verified tool inventory for this deployment: 18 tools under the `mcp__ha__*`
namespace covering covers, lights, scenes, native automations, and areas.

Load `home-assistant-covers` for any HA request — covers, lights, switches,
scenes, or automations.
