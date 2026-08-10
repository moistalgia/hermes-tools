# Prompt for Hermes

The one-time bootstrap prompt for getting `prowlarr-mcp` installed and proven.
Once it works, day-to-day behaviour lives in
[skills/media-acquisition](../skills/media-acquisition/SKILL.md) — use that, not
this.

Paste everything below the line.

---

Stop trying to get past the bot check on indexer sites. That path is closed, and it is not a "try harder" problem — the checkbox is specifically designed to stop an automated browser, and mirror domains loop it indefinitely for exactly the same reason. Do not open those sites again, do not look for another mirror, and do not write your own scraper. I am giving you a finished MCP server for this; your job is to install it and confirm it works, not to design it.

The thing that actually solves this is Prowlarr, which holds the indexer definitions, the credentials, and a challenge-solving proxy for the indexers that need one. The MCP server is a client of Prowlarr and never touches a challenge itself.

**Install**

1. Start the containers. The compose file is at `E:/hermes-mcp/hermes-tools/prowlarr-mcp/docker-compose.yml`:

```bash
docker compose -f E:/hermes-mcp/hermes-tools/prowlarr-mcp/docker-compose.yml up -d
```

2. Configure Prowlarr at <http://127.0.0.1:9696>. Follow [the README](README.md) — set a login, add **FlareSolverr** under Settings → Indexers → Indexer Proxies with host `http://flaresolverr:8191` and tag `flaresolverr`, add your indexers, and put that same tag on any indexer that needs it. **The tag is the step that gets missed**: a proxy with no matching tag applies to nothing and looks broken.

3. Hit **Test** on each indexer in the Prowlarr UI before going further. A green tick there is the difference between an indexer problem and an MCP problem, and knowing which you have is worth thirty seconds.

4. The server is a git checkout on the Windows host, at `E:/hermes-mcp/hermes-tools/prowlarr-mcp/`. Read it and run it; do not edit it — your edits would be overwritten on the next pull. It has no dependencies and needs no venv, no install, and no packaging.

5. Register it in your MCP config as the server `prowlarr`:

```yaml
mcp_servers:
  prowlarr:
    command: "python"
    args: ["E:/hermes-mcp/hermes-tools/prowlarr-mcp/prowlarr_mcp_server.py", "serve"]
    env:
      PROWLARR_URL: "http://127.0.0.1:9696"
      PROWLARR_API_KEY: "<literal key from Settings → General → API Key>"
```

`PROWLARR_API_KEY` goes in as a **literal value**, the same way `HASS_TOKEN` does — not a path, not a `.env` file. The server runs as a subprocess with the environment its parent hands it and will not read a file you point it at. Do not search the filesystem for the key; take it from the Prowlarr UI.

The `serve` argument is required. Omitting it prints usage to stderr, deliberately, so a mistake in the config produces a readable log instead of a corrupted handshake.

**Prove the protocol before touching config.yaml**

Run the server by hand first. It is a CLI as well as an MCP server, through the identical dispatch path, so anything that works here works over MCP:

```bash
python E:/hermes-mcp/hermes-tools/prowlarr-mcp/prowlarr_mcp_server.py prowlarr_status
```

If that returns JSON with `"ok": true`, the server, the key, and Prowlarr are all good and the only thing left is the config. If it does not, the error text names the cause — fix that before editing any YAML, because a failed handshake tells you nothing.

**Verify, in this order, and stop at the first failure**

1. `tool_search` for `prowlarr` — confirms the server is attached and its tools are visible.
2. `tool_describe` on `search` — read the argument list properly before calling it. It has more than you will guess.
3. `prowlarr_status` — connection, key, and the health of every indexer.
4. `list_indexers` — the exact indexer names. Use those verbatim from here on; do not invent or abbreviate them.
5. `search` with a film title you know exists — this is the actual test.
6. `search` with `kind=tv`, `season=1`, `episode=1` on a show you know exists — the episode path builds its query differently and is worth proving separately.

Report the output of each step. On steps 5 and 6, tell me specifically whether every result carried a `magnet` starting `magnet:?xt=urn:btih:`. That is the whole point of the server and it is the one thing I want confirmed by eye.

If a step fails, report the `error` field **exactly as returned** and stop. Do not retry with altered arguments, do not open a browser, and do not write a replacement script. The error text names the cause; I will decide the next move.

**Standing rules**

- **A search takes 30 to 60 seconds.** An indexer behind a challenge is solved inside Prowlarr, which is slow and is working correctly. Wait for it. Do not fire a second search because the first is taking a while — it queues behind the first and makes the wait longer.
- **`query` takes the title and nothing else.** No resolution, no release group, no codec, no year in the string. There are separate arguments for `year`, `season` and `episode`. Extra words in `query` return nothing, because you are searching an index and not a filename.
- **An empty result is not automatically "not found".** The tool distinguishes nothing-matched from everything-filtered from every-indexer-failing, and says which. Read it. If it says the indexers are failing, that is final for now — report it and stop.
- **Never a cam.** `cam: true` means a recording of a cinema screen. It ranks last for that reason and it is never the right answer, whatever its seeder count.
- **A result with `magnet: null` cannot be used.** That indexer serves `.torrent` files. Pick another release. Do not build a magnet yourself and do not pass a download URL off as one.
- **Do not configure Prowlarr.** Adding indexers, editing proxies, changing categories — all mine. If something is missing, name it and stop.
- **Do not send the `!fetch` handoff during this test.** I want the search proven first. Once I have confirmed it, the [media-acquisition](../skills/media-acquisition/SKILL.md) skill covers the handoff, including asking me before it sends anything.
- **Do not edit any skill file until I have personally confirmed the search test worked.**
