# Messages from a phone

These arrive without a Discord id. `from_person` is whatever the source knows —
often nothing, sometimes a name. Do not attribute a message to someone because
its content sounds like them.

When `inbox_fetch` on the `notify` server returns messages:

1. `capture_add` each one, with `source` and `from_person`.
2. Decide what each actually is.
3. Act — `task_add`, `shopping_add`, `appointment_add`, `fact_record`.
4. `capture_file` naming where it went.

Step 4 comes **after** step 3, never before. A capture marked filed with nothing
behind it has vanished from the inbox and exists nowhere, which is strictly
worse than leaving it pending.

Not everything is actionable. `filed_to="nothing — noise"` is a legitimate
outcome for a message that was just chat. Do not manufacture a task out of every
inbound line.
