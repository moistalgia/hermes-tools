# Room aliases (`PLEX_ALIASES`)

The Plex MCP supports a `PLEX_ALIASES` env var (JSON map of room → device) so
you can say "the theater" instead of "Streaming Stick 4K". When configured,
`list_players` echoes a `room` field per device, and anything without one
falls into `unmapped`.

## Current mapping (verified 2026-08-16)

`list_players` returns real room strings: Sleepy → `living room`, Andies Tv
For Ants → `andies office`, Roku Express 4K+ → `bedroom`, unknown/Fire TV →
`nicks office`, Streaming Stick 4K → `theater`. Only DESKTOP-CHB1M9E has no
room mapped (`room: null`, appears in `unmapped`). Address any of the mapped
five by room name; use DESKTOP-CHB1M9E's literal Plex name.

## Known device identifiers

For whenever aliasing needs reconfiguring. Map key is the room, first array
element must be the `machine_identifier`, not the display name — display
names are user-settable and unstable.

| Plex name | machine_identifier | Notes |
| --- | --- | --- |
| Sleepy | `a710a60ff65de04711dd2c4f217fada3` | Roku, currently the only reliably controllable player |
| Andies Tv For Ants | `9fa5ef017bd8b903395f9e479aa9bd91` | Roku |
| DESKTOP-CHB1M9E | `t0v7x03y0qggo77gd92xd2t9` | Plex Media Player (Konvergo) |
| Roku Express 4K+ | `95c030af1faf5801835d4601a8b37004` | Roku |
| Streaming Stick 4K | `d2b46d2ad54416315e5e36862d2644a1` | Roku |
| unknown | `gd91wa2zwieprb2mbmd1r0u3` | Amazon Fire TV — never controllable, see troubleshooting.md |
