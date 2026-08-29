# When they add a device

Someone says they have installed a light, a blind, or a sensor:

1. `discover_entities` — usually with a `search` term from what they called it.
2. Show them the ids you found and **ask which room they belong to**, and what
   else that room is called. Do not guess: `light.porch` could be outside, the
   entryway, or a room you have never heard of, and an alias you invent is one
   they will never say.
3. Give them the snippet the tool returned and tell them which room block to
   paste it into, in `house.json`.
4. Tell them no restart is needed — the map reloads itself.
5. Once they confirm, `list_rooms` to verify it took.

**You do not edit `house.json` yourself.** It is a hand-tuned file and this is
one exchange, not an automation. Hand over the snippet and let them place it.

If they ask about a lock, an alarm, or a door sensor, `discover_entities` will
refuse the domain — that's expected, see the main skill file.
