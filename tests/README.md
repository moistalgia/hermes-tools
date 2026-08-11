# tests

No dependencies — `unittest` from the standard library, the same rule the
servers follow. Run everything from the repo root:

```bash
python -m unittest discover -s tests -v
```

Or one file:

```bash
python -m unittest discover -s tests -p test_hass.py
```

`plex-mcp` is not covered here. It is the one server with a dependency and its
own copy of the protocol layer, and the things worth testing about it — whether
a particular Fire TV accepts a command — are facts about hardware rather than
about code. [diagnose_players.py](../plex-mcp/diagnose_players.py) is the tool
for those.

## What is here

| File | Covers |
| --- | --- |
| [test_mcpkit.py](test_mcpkit.py) | Argument coercion and dispatch — every tool in the repo passes through both, so a bug here is a bug in four servers at once. |
| [test_state.py](test_state.py) | Date and recurrence arithmetic, and the writes that have to be honest. |
| [test_hass.py](test_hass.py) | Room resolution and read-back verification, against a fake Home Assistant. |
| [test_notify.py](test_notify.py) | The inbound read cursor and per-backend priority mapping. |
| [test_discord.py](test_discord.py) | The closed recipient list, and that a DM never posts before the channel it's posting to exists. |
| [test_prowlarr.py](test_prowlarr.py) | Title parsing, magnet reconstruction, and the four different reasons a search returns nothing. |
| [test_qbt.py](test_qbt.py) | Which library a release lands in, read-back after adding, and the stall that reads as progress. |
| [test_serve.py](test_serve.py) | The stdio handshake, run as a real subprocess. |
| [support.py](support.py) | Loading a server under a chosen environment, and `FakeHass`. |

## Two things worth knowing

**`FakeHass` can be deaf.** Adding an entity id to `ha.deaf` makes it accept
every service call and change nothing — a bulb switched off at the wall, a
Z-Wave device off the mesh. That is indistinguishable from a working device
from the HTTP response alone, and it is the single failure this repo cares most
about reporting correctly. You cannot ask a real house for it on demand, which
is why the fake exists at all.

**`prowlarr-mcp`'s failures are all quiet ones.** It has no write path, so
nothing it does can be confirmed by reading state back. What it can get wrong
instead is a confident wrong answer: a cam rip presented as the best result, a
`.torrent` URL handed over in the magnet slot, or "nothing found" reported for a
title that exists because every indexer was failing and an empty list looks
identical either way. Its fake Prowlarr does no matching and no ranking on
purpose — those are the parts under test.

**`test_serve.py` runs the servers as subprocesses.** Everything else imports
them and calls functions. That file launches each one the way Hermes does and
talks JSON-RPC to it, because the failures that actually happen in deployment
live in the gap between "the function works" and "the process speaks the
protocol". It enforces the rule that nothing but JSON-RPC frames may reach
stdout — including when the `serve` argument is missing, where usage text on
the protocol channel would fail the handshake with no explanation.

## Adding to them

A new tool deserves a test when it can be wrong in a way that still looks
right. Room resolution, date arithmetic and partial writes are all in that
category: they produce a confident answer either way, and nobody notices until
the chore never comes back or a light was reported on that never came on.
Tools that fail loudly need less.
