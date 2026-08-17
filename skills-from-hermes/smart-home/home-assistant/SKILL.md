---
name: home-assistant
description: "Control and query Home Assistant entities via the REST API — covers, lights, switches, climate."
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Smart-Home, HA, IoT, Automation, Integration]
---

# Home Assistant REST API Control

Control Home Assistant entities (covers, lights, switches) using the `ha_call_service`, `ha_list_entities`, and `ha_get_state` tools. These handle auth and JSON formatting internally — always prefer them over terminal/curl approaches.

## When to Use

- User asks to control any HA device: blinds, curtains, lights, climate, etc.
- Querying sensor readings or entity states
- Testing whether an automation/service call actually worked

## Step-by-step workflow

### 1. Discover entities

Always list relevant entities rather than guessing IDs:
```
ha_list_entities(domain='cover')       # all covers
ha_list_entities(area='theater')       # by room label
```

If request names differ from entity names (e.g., "blinds" but only "curtain" exists), use the correct ID and note the mismatch. Don't guess.

### 2. Read current state BEFORE commanding

```
ha_get_state(entity_id='cover.xxx')
```

Key attributes:
- **`state`**: `open`, `closed`, etc. — but don't trust it alone (see pitfalls).
- **`current_position`**: Numeric position (0–100). Read FIRST for covers; this tells you polarity and how far they've moved.
- **`supported_features`**: Bitmask of capabilities. Only send commands the entity supports.

### 3. Test polarity before batch operations

Run a small test on ONE entity first, then verify by reading state after:

```python
# Send direction command to one cover
ha_call_service(domain='cover', service='open_cover', entity_id='cover.test')
time.sleep(2)  # let hardware respond
ha_get_state(entity_id='cover.test')  # check if position moved as expected
```

### 4. Execute with correct payload format

Commands requiring parameters need both the entity and values in one call:

```python
ha_call_service(domain='cover', service='set_cover_position', 
                data={"position": 25, "entity_id": "cover.theater_curtain"})
# OR — depends on tool variant:
ha_call_service(domain='cover', service='set_cover_position', entity_id='cover.xxx')
# with separate position in the entity context... but this is where failures happen.
```

If `open`/` close_cover ` works but set_cover_position doesn't, use directional commands instead. If neither works or results are ambiguous, verify by reading state afterward rather than relying on tool return value alone.

### 5. Verify physical result (always)

After ANY command where the user cares about the outcome:
```
ha_get_state(entity_id='cover.xxx')  # confirm position changed
```

A `"success": true` with affected_entities:[] means you don't know what happened physically yet. A positive check in get_state means it worked regardless of what the service call reported. **Verifying state is always the last step.**

## Pitfalls (learned from repeated failures)

- Polarity reversal on covers is common with ZHA/Zigbee and Tuya/Shelly integrations. When you command `open_cover` and things close, or vice versa — this is not a bug in your approach, it's an integration quirk. ALWAYS check position after testing once before trusting binary commands.

- `"success: true", "affected_entities: []"` — some HA tool implementations return success with zero affected entities when the platform has quirks. Don't treat this as proof that nothing happened; verify by reading state afterward to know what actually moved physically. Some integrations actuate hardware despite the empty response, others silently drop it.

- "75% closed" = position 25 in HA (0=closed is standard). But if polarity is reversed so open/close don't match this convention, use set_cover_position with explicit values — they typically still work correctly even when binary commands are flipped.

## Anti-patterns

- Don't narrate a plan and not execute. Say what you'll try and DO it in the same turn.
- Don't assume standard polarity on open/close without verifying at least once per integration.
- Don't rely on tool return values alone for physical devices — always verify state.