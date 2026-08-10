#!/usr/bin/env python3
"""Compare the three places Plex reports "players", so you can see which one
actually knows about a device while it is sitting idle.

    python3 diagnose_players.py

Run it twice: once with everything idle, once with something playing. The
difference between the two runs tells you which source to build on.
"""
import os
import sys

PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32400")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")


def probe(url, timeout=3):
    """Is the device's Companion listener actually up right now?"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/resources", timeout=timeout) as r:
            return f"LISTENING (HTTP {r.status}) - controllable now"
    except urllib.error.HTTPError as e:
        return f"LISTENING (HTTP {e.code}) - controllable now"
    except Exception as e:
        return f"no listener ({type(e).__name__}) - Plex app is closed on this device"


def main():
    if not PLEX_TOKEN:
        print("PLEX_TOKEN is not set in the environment.")
        return 1

    from plexapi.server import PlexServer

    print(f"PLEX_URL = {PLEX_URL}\n")
    server = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
    print(f"Server: {server.friendlyName} v{server.version}\n")

    # 1. What the MEDIA SERVER advertises. This is what list_players uses today.
    #    Local-network Companion registrations only.
    print("=" * 70)
    print("1. server.clients()  ->  GET /clients on the PMS")
    print("=" * 70)
    clients = server.clients()
    print(f"count = {len(clients)}")
    for c in clients:
        print(f"  {c.title!r}  product={c.product}  id={c.machineIdentifier}")
        print(f"      protocolCapabilities={c.protocolCapabilities}")
    if not clients:
        print("  (empty is normal for Fire TV / Apple TV even when playing)")

    # 2. What PLEX.TV knows. Survives idle - this is the interesting one.
    print()
    print("=" * 70)
    print("2. account.devices()  ->  plex.tv/devices.xml")
    print("=" * 70)
    try:
        from plexapi.myplex import MyPlexAccount

        account = MyPlexAccount(token=PLEX_TOKEN)
        devices = account.devices()
        players = [d for d in devices if "player" in (d.provides or "")]
        print(f"total devices = {len(devices)}, providing 'player' = {len(players)}")
        for d in players:
            print(f"  {d.name!r}  product={d.product}  platform={d.platform}")
            print(f"      clientIdentifier={d.clientIdentifier}")
            print(f"      provides={d.provides}  lastSeenAt={d.lastSeenAt}")
            # Registered != reachable. The Companion listener only runs while
            # the Plex app is open on the device, so this is the check that
            # actually predicts whether a play command will land.
            for url in d.connections:
                print(f"      {url}  ->  {probe(url)}")
    except Exception as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print("  (a server-only token cannot read plex.tv; needs an account token)")

    # 3. Active sessions. Requires playback by definition.
    print()
    print("=" * 70)
    print("3. server.sessions()  ->  currently streaming")
    print("=" * 70)
    sessions = server.sessions()
    print(f"count = {len(sessions)}")
    for s in sessions:
        for p in getattr(s, "players", []):
            print(f"  {p.title!r}  product={p.product}  id={p.machineIdentifier}")
            print(f"      protocolCapabilities={getattr(p, 'protocolCapabilities', [])}")
            print(f"      state={getattr(p, 'state', '?')}")

    # The verdict.
    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    try:
        idle_ids = {d.clientIdentifier for d in players}
    except NameError:
        idle_ids = set()
    server_ids = {c.machineIdentifier for c in clients}
    only_plextv = idle_ids - server_ids
    if only_plextv:
        print("These players are visible via plex.tv but NOT via /clients:")
        for d in players:
            if d.clientIdentifier in only_plextv:
                print(f"  {d.name!r}  ({d.clientIdentifier})")
        print()
        print("They are addressable with proxyThroughServer(True) using the")
        print("clientIdentifier above - no /clients entry needed. Test one with:")
        print("  python3 diagnose_players.py --try-push <clientIdentifier> <title>")
    elif not idle_ids:
        print("plex.tv reported no 'player' devices. If the Fire TV is powered on,")
        print("its Plex app has not registered as a player target at all, and no")
        print("API on this machine can start playback on it.")
    else:
        print("Every plex.tv player is also in /clients - the existing code path")
        print("is sufficient and nothing here needs to change.")
    return 0


def build_relay_client(server, identifier, title="relay"):
    """A PlexClient that talks to a device through the server, by identifier only.

    The device does not have to appear in /clients. sendCommand puts
    machineIdentifier in the X-Plex-Target-Client-Identifier header and the
    server forwards over the Companion connection the device already holds.

    Note the constructor's `identifier=` argument does NOT do this: it only
    feeds connect()'s client-side lookup, which we are deliberately skipping
    because dialing the device's LAN IP directly is exactly what fails.
    machineIdentifier has to be set directly.
    """
    from plexapi.client import PlexClient

    client = PlexClient(server=server, baseurl=server._baseurl,
                        token=server._token, connect=False)
    client.machineIdentifier = identifier
    client.title = title
    client.product = ""
    # Populated from /clients normally; assert them so sendCommand does not
    # skip on an empty capability list.
    client.protocolCapabilities = ["timeline", "playback", "navigation", "playqueues"]
    client.proxyThroughServer(True, server)
    return client


def try_push(identifier, title):
    """Prove whether a device accepts a relayed playMedia while idle."""
    from plexapi.server import PlexServer

    server = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=15)
    results = server.search(title)
    if not results:
        print(f"No library match for {title!r}")
        return 1
    item = results[0]
    print(f"Matched: {item.title} ({getattr(item, 'year', '?')})  key={item.key}")

    client = build_relay_client(server, identifier)
    print(f"Sending playback/playMedia to {identifier} via the server relay...")
    try:
        client.playMedia(item)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        print("The server relay rejected the command or the client never answered.")
        return 1
    print("ACCEPTED - the server relayed the command without error.")
    print("Confirm on the TV. If nothing started, the client swallowed it.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--try-push":
        if len(sys.argv) < 4:
            print("usage: diagnose_players.py --try-push <clientIdentifier> <title>")
            sys.exit(1)
        sys.exit(try_push(sys.argv[2], " ".join(sys.argv[3:])))
    sys.exit(main())
