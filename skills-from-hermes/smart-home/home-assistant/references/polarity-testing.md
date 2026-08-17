# Cover Polarity Testing Protocol

When you encounter unknown cover/blind integrations, run this test to establish direction semantics before taking further action.

## Step 1: Note current position

```
ha_get_state(entity_id='cover.xxx')
```

Record `current_position` (0-100) and state (`open`/`closed`). Start with something partially open if possible, so you can detect movement in either direction.

## Step 2: Send an unambiguous command

Use `set_cover_position` rather than binary open/close — it eliminates polarity ambiguity entirely:
```python
current = ha_get_state(entity_id='cover.xxx')  # saves position_0
# move to known value
ha_call_service(domain='cover', service='set_cover_position')  # with data={"position": (position_0 + 10) % 101, "entity_id": "..."}
```

If set_cover_position works, you're golden. The scale is always absolute regardless of polarity quirks.

## Step 3: Verify movement

```
ha_get_state(entity_id='cover.xxx')
# new position should equal old + delta from step 2
```

If the numbers match what you sent (`open_cover`/`close_cover`) — standard polarity.

**But if `open_cover` actually closed them physically and vice versa** — note this for future commands with those entity IDs within the same HA session/integration, then proceed as usual: use set_cover_position (it should still follow standard scale even when binary commands are reversed) or instruct user to confirm direction before relying on open/close pairs.

## When you get "success but affected_entities: 0"

This happens with certain platforms even when hardware moves:
1. Send the command
2. Wait for response time (covers take 5-30s physically)
3. Read state again — if position changed, the command worked despite empty entity count
4. If position unchanged OR tool blocked entirely, verify HA container can reach host: `curl -v http://host.docker.internal`
