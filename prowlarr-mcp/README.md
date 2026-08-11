# prowlarr-mcp

One search across every indexer, returning magnet links.

No dependencies — `urllib` and the standard library. Unlike the other servers
here, this directory ships its own copy of [mcpkit.py](mcpkit.py) so it can be
dropped into an MCP folder on its own. A test asserts the copy has not drifted
from [../mcpkit.py](../mcpkit.py), and CI runs the server from a scratch
directory with no repo above it to prove the copy is sufficient.

## The division of labour

**Prowlarr owns the indexers. This server owns the answer.**

That split is the whole design. Prowlarr holds the indexer definitions, the
credentials, and the challenge-solving proxy for indexers that sit behind one.
By the time a result reaches this server that work is finished, and none of it
is reimplemented here — no browser, no cookies, no challenge handling, ever. If
an indexer is unreachable, the correct output is "that indexer is failing", and
that is what you get.

What this server adds on top is the part Prowlarr's raw API does not do:

**It returns a magnet, or says why it cannot.** This is most of the work.
Prowlarr's `magnetUrl` is null for a great many indexers — often for every
result of a search — and what it hands back instead is a proxy link to a
`.torrent`. The magnet is recovered in four stages, cheapest first:

| Stage | Source | Cost |
| --- | --- | --- |
| 1 | `magnetUrl`, when the indexer supplied one | free |
| 2 | `infoHash`, rebuilt into a magnet | free |
| 3 | the `link=` parameter inside Prowlarr's own proxy URL | free |
| 4 | fetching the download link itself | one request |

**Stage 3 is the one that matters.** Prowlarr wraps every download behind its
own proxy so it can attach credentials, and Base64-encodes the indexer's real
link into the query string. For a magnet-based tracker that link *is* the
magnet — it simply never appears in `magnetUrl`. Decoding it costs nothing.

Stage 4 is the universal fallback and the reason the promise can be kept at
all. The download link either redirects to a magnet, or serves the `.torrent`,
and a `.torrent` contains everything a magnet needs: the info hash is the SHA-1
of its `info` dictionary, and the name and trackers come along with it. The
hash is taken over those bytes exactly as they arrived — decoding and
re-encoding would produce a different hash for any file whose encoder ordered
keys differently, and that hash would be silently wrong rather than obviously
broken.

Only stage 4 touches the network, only for the releases actually being
returned, and up to four at a time. A `.torrent` link is never returned in the
magnet slot — everything downstream takes a magnet, and a near-miss is the kind
of failure that surfaces three steps later with nothing pointing back at the
cause.

**It parses the title.** Raw search returns hundreds of rows whose only
structure is a filename convention. Resolution, source, codec, size, seeders and
cam-rip detection become fields, so the choosing happens against data instead of
against a regex the agent improvised in the moment.

**Empty is a diagnosis.** Zero rows because nothing matched, zero rows because
everything was filtered on seeders, and zero rows because both indexers are
failing are identical over the wire and need opposite responses. Every empty
result here says which one it is.

## Scope

Search only. No grab, no download client, no torrent state — the magnet goes to
whatever handles fetching and this server's job ends there. A search tool that
can also start downloads has a much wider blast radius than the job needs.

What the indexers list is whatever they list; deciding what to do with a result
is yours.

## Set up Prowlarr first

The server is a client. Nothing below happens automatically, and none of it is
something the agent should be doing.

**1. Start the containers.**

```bash
docker compose -f E:/hermes-mcp/hermes-tools/prowlarr-mcp/docker-compose.yml up -d
```

Prowlarr comes up on <http://127.0.0.1:9696> and asks you to set a login on
first run.

**2. Add the proxy, then tag the indexers that need it.**

In Prowlarr: **Settings → Indexers → Indexer Proxies → + → FlareSolverr**.

| Field | Value |
| --- | --- |
| Name | `FlareSolverr` |
| Tags | `flaresolverr` |
| Host | `http://flaresolverr:8191` |

The host is a service name, not an address, because both containers are on the
compose network. **The tag is the part everyone gets wrong**: a proxy applies
only to indexers carrying the matching tag, so adding the proxy and stopping
there changes nothing at all and looks like the proxy is broken.

**3. Add indexers.** **Indexers → Add Indexer**, search by name, add the ones
you want. Put the `flaresolverr` tag on any that need it. Then hit **Test** on
each — a green tick here is the difference between an indexer problem and an
MCP problem, and it is worth knowing which you have before going further.

**4. Take the API key** from **Settings → General → API Key**.

**5. Prove the API answers before involving any of this.**

```bash
curl -s -H "X-Api-Key: $PROWLARR_API_KEY" http://127.0.0.1:9696/api/v1/indexer
```

```bash
curl -s -H "X-Api-Key: $PROWLARR_API_KEY" \
  "http://127.0.0.1:9696/api/v1/search?query=ubuntu&indexerIds=-1&type=search"
```

If the second one returns an empty array while indexers show green, the problem
is category or capability configuration inside Prowlarr, and nothing in this
repo will fix it.

## Install

Nothing to install — no dependencies, no venv, no packaging. Run it from the
checkout:

```bash
python E:/hermes-mcp/hermes-tools/prowlarr-mcp/prowlarr_mcp_server.py prowlarr_status
```

To use it outside the checkout, copy the whole `prowlarr-mcp/` directory
anywhere and point the config at it. It carries everything it needs.

## Configure

| Variable | Default | Notes |
| --- | --- | --- |
| `PROWLARR_URL` | `http://127.0.0.1:9696` | Prowlarr is a container but the port is published, so this is an ordinary host address. |
| `PROWLARR_API_KEY` | *(unset)* | **Settings → General → API Key.** Put the literal value in the MCP config for this server. Not a `.env` file — the server is launched as a subprocess and will not read one. |
| `PROWLARR_TIMEOUT` | `90` | Seconds for a search. Not generous: a query reaching a challenged indexer goes through a solver, and thirty seconds is an ordinary result rather than a hang. |
| `PROWLARR_RESOLVE_TIMEOUT` | `45` | Seconds to spend fetching one download link while deriving a magnet. Up to four run at once. |
| `PROWLARR_FETCH_PREFIX` | `!fetch` | Prepended to each magnet as a ready-to-send `fetch_command`. Set it empty to drop the field. |

**Quote the values under `env:` in the YAML.** Quoting `command` and `args` is
cosmetic — a forward-slash path parses identically either way — but an
unquoted `env` value gets type-inferred, and an environment block is handed
straight to a subprocess, which accepts strings and nothing else.
`PROWLARR_TIMEOUT: 120` becomes the integer 120 and fails the launch with
`TypeError: environment can only contain strings`, before this server runs at
all and with an error that points at Hermes rather than at the config. The same
inference turns `yes` into `True`, `1.10` into `1.1`, and anything containing
` #` into everything before the hash.

## Tools

| Need | Tool |
| --- | --- |
| Is anything wrong? | `prowlarr_status` |
| What can be searched | `list_indexers` |
| Find something | `search` |

Three tools, on purpose. Everything the agent needs is in `search`'s result
rows, and a wider surface is a wider set of ways to pick the wrong call.

### `search`

| Argument | Meaning |
| --- | --- |
| `query` | The title. Just the name — no resolution, no group, no year. |
| `kind` | `movie`, `tv`, `anime`, `any`. Narrows the category. |
| `season`, `episode` | Folded into the query as `S01E02`. `episode` requires `season`. |
| `year` | Separates remakes. Ignored when `season` is given. |
| `indexer` | Restrict to one, named as `list_indexers` reports it. |
| `min_seeders` | Default 1. |
| `limit` | Default 10. |
| `sort` | `best` (resolution, then health), `seeders`, `size`, `newest`. |
| `resolve_magnets` | Default on. Derive a magnet for returned releases that lack one. Off is for browsing titles quickly, and returns rows that cannot be handed off. |

Each result carries `title`, `resolution`, `source`, `codec`, `hdr`, `cam`,
`size`, `seeders`, `age`, `indexer`, `magnet`, and — unless the prefix is
cleared — `fetch_command`.

## Behaviour worth knowing

**A search takes 30–60 seconds.** An indexer behind a challenge is solved inside
Prowlarr, which is slow and is working correctly. A second search fired because
the first was taking a while queues behind it and makes things worse.

**Season and episode are folded into the query text, not passed as parameters.**
Prowlarr has typed `tvsearch` and `moviesearch` modes that take them properly,
and they are the better interface right up until an indexer does not advertise
the capability — at which point the search silently returns nothing. Scene
naming is universal, so the string is built instead.

**A year is dropped from an episode search.** The year in a TV title is the
series year and almost never appears in the release name, so including it
matches nothing.

**`kind` can be too narrow.** Not every indexer files things the same way, and a
category filter that misses returns zero rather than an error. If a narrowed
search comes back empty, `kind=any` before trying another title.

**The same release on three indexers is one row.** Collapsed by info hash,
keeping the healthiest copy, with the others listed in `also_on`. Otherwise a
list of ten results is really a list of four.

**Usenet has no seeders, and `None` is not zero.** Only an actual seeder count
is compared against `min_seeders`, or every usenet result would vanish the
moment it rose above nothing.

**Cam rips rank last regardless of health.** A cinema recording with 900 seeders
is not a better answer than a bluray with five. They are flagged, not hidden —
`cam: true`, and the summary says so.

**`TS` is telesync and is also three letters of a release group name.** The
short ambiguous tags only count as a cam when nothing else in the title claims a
real resolution, which is true of every actual cam rip and almost no legitimate
release.

## Manual test sequence

Run these in order. Stop at the first failure and read the error; it names the
cause.

```bash
export PROWLARR_API_KEY=<from Settings → General>
```

```bash
python prowlarr_mcp_server.py prowlarr_status
```

```bash
python prowlarr_mcp_server.py list_indexers
```

```bash
python prowlarr_mcp_server.py search query="a film you own" kind=movie limit=3
```

```bash
python prowlarr_mcp_server.py search query="a show you own" kind=tv season=1 episode=1 limit=3
```

The last two are the real test: every row should carry a `magnet` beginning
`magnet:?xt=urn:btih:`, and the response's `without_magnet` should be `0`. Each
row's `magnet_note` says which of the four stages produced it, which is the
quickest way to see what your indexers actually supply — `taken from the
indexer's own download link` means stage 3 and cost nothing; `computed from the
.torrent file` means stage 4 and cost a request each.

If rows still come back with `magnet: null`, the note names the exception. A
row that cannot produce a magnet cannot feed the handoff, and that is worth
knowing here rather than discovering downstream.

Arguments are `key=value`. Quote values containing spaces. Exit code is 0 on
success, 1 on failure, and the JSON body carries the real error text.

## Wire into Hermes

Host paths — Hermes launches MCP servers from its own Python process. See
[deploying](../README.md#deploying).

```yaml
mcp_servers:
  prowlarr:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/prowlarr-mcp/prowlarr_mcp_server.py", "serve"]
    env:
      PROWLARR_URL: "http://127.0.0.1:9696"
      PROWLARR_API_KEY: "<literal key, not a path to one>"
```

The key goes in as a literal value, the same way `HASS_TOKEN` does. A `.env`
file in this directory is not read — the server runs as a subprocess with the
environment its parent hands it and nothing else.

[HERMES_PROMPT.md](HERMES_PROMPT.md) is the one-time bootstrap prompt for
getting this proven. Day-to-day behaviour lives in the
[media-acquisition](../skills/media-acquisition/SKILL.md) skill.
