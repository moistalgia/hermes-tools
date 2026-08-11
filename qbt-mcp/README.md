# qbt-mcp

Starts a download in qBittorrent and confirms it actually started.

`prowlarr-mcp` stops at finding a magnet, deliberately. This is the other end,
and they stay apart on purpose: a search tool that can also download has a much
wider blast radius than searching needs.

Talks to qBittorrent's Web UI API directly — no bot, no relay, nothing between
deciding to download something and it downloading. Standard library only. No
dependencies, no venv.

## Setup

### 1. qBittorrent

Tools → Options → Web UI → tick **Web User Interface (Remote control)**. Note
the port and set a username and password.

If Hermes runs on a different machine, the Web UI has to listen somewhere that
machine can reach, and you will also want **Bypass authentication for clients
on localhost** left *off*.

Binding the BitTorrent session to a VPN adapter does not affect this — the Web
UI is a separate listener, and enabling it does not move peer traffic off the
VPN.

### 2. Create the library folders

Whatever you use, they must exist and be writable by qBittorrent. `P:/Movies`
and `P:/Shows`, for example. Forward slashes are fine on Windows.

### 3. Hermes

```yaml
  qbt:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/qbt-mcp/qbt_mcp_server.py", "serve"]
    env:
      QBT_URL: "http://<media server>:8080"
      QBT_USER: "admin"
      QBT_PASS: "<Web UI password>"
      QBT_MOVIES_PATH: "P:/Movies"
      QBT_SHOWS_PATH: "P:/Shows"
```

Both paths are **required and have no defaults**. A wrong path does not fail —
it silently files a season pack into the film library, and Plex indexes it
before anyone notices. `qbt_status` reports them, so a mistake is visible in one
call rather than three days later.

`QBT_CONFIRM_TIMEOUT` (default 10s) is how long an add waits for the torrent to
appear before reporting it unconfirmed.

### 4. Prove it

```bash
python qbt-mcp/qbt_mcp_server.py qbt_status
```

That checks reachability, the credentials, and both paths in one call.

## Tools

| Tool | Does |
| --- | --- |
| `qbt_status` | Reachability, credentials, paths, and how much is running. Run first when something is wrong. |
| `download` | Start a download. Files it by kind, reads it back, returns the infohash. |
| `downloads` | What is running and how it is going. Names anything stalled at 0%. |
| `download_cancel` | Remove a torrent. Leaves the files unless told otherwise. |

Every one is also a CLI subcommand through the same dispatch path:

```bash
python qbt-mcp/qbt_mcp_server.py download magnet="magnet:?xt=urn:btih:..."
python qbt-mcp/qbt_mcp_server.py downloads
```

## Films and television

The release name in a magnet's `dn` parameter decides where it lands: anything
matching `S01E02`, `1x02`, `Season 3`, or `S01.Complete` is television and goes
to `QBT_SHOWS_PATH`; everything else is a film and goes to `QBT_MOVIES_PATH`.

`download` takes an optional `kind` to override that. It is for the cases the
regex visibly gets wrong — a film with "Season" in the title, a documentary
series named like a film — and the skill tells the agent not to reach for it
routinely. One decider by default, with an escape hatch, rather than two
opinions that drift.

## An accepted magnet is not a download

`torrents/add` returns `Ok.` when the request was *accepted*. A magnet with no
seeders, a malformed hash, and a healthy release all get the same answer.

So every add is followed by a read-back against `torrents/info` until the
torrent appears, and what comes back is the state qBittorrent actually holds —
the §3 convention from [DESIGN.md](../DESIGN.md). `confirmed: false` means it
was accepted but has not shown up, which is a different thing from a failure and
is reported as such.

A torrent at 0% in `stalledDL` has found no seeders and will not finish on its
own. That gets named specifically rather than averaged in with real progress,
because reporting it as "downloading" is how someone waits all evening for a
file that was never coming.
