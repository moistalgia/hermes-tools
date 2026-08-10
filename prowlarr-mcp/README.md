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

**It returns a magnet, or says why it cannot.** Prowlarr leaves `magnetUrl`
null for plenty of indexers and hands back a proxied `.torrent` URL instead.
Where an info hash is present the magnet is rebuilt from it; where it is not,
the row says so plainly. A `.torrent` link is never returned in the magnet
slot — everything downstream takes a magnet, and a near-miss is the kind of
failure that surfaces three steps later with nothing pointing back at the cause.

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
| `PROWLARR_FETCH_PREFIX` | `!fetch` | Prepended to each magnet as a ready-to-send `fetch_command`. Set it empty to drop the field. |

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
`magnet:?xt=urn:btih:`. A row with `magnet: null` and a note about `.torrent`
files means that indexer cannot feed the handoff, which is worth knowing before
the agent discovers it.

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
