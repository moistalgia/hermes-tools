#!/usr/bin/env python3
"""
aiden-mcp - brew coffee on a Fellow Aiden, and confirm it actually started.

Fellow publishes no API. Everything here - the endpoints, the headers, the
profile and schedule validation rules, the safety preconditions for a remote
start - was learned from two open-source projects that reverse-engineered the
mobile app: `9b/fellow-aiden` (the original Python library, v1) and
`kristofferR/FellowAiden-HomeAssistant` (the Home Assistant integration, v2).
Credit to both. This file is an independent implementation against the same
undocumented interface, for three reasons that all point the same way:

  1. **Remote start is v2 only.** `9b/fellow-aiden` talks to `/v1`, which has no
     start route at all. The one that works is `PATCH /devices/{id}/start`.
     The obvious-looking alternative - setting `doBrew: true` on the device - is
     accepted by the cloud and never reaches the brewer; that is FellowAiden-
     HomeAssistant issue #48, and it is why this file does not go near it.

  2. **That library logs its own bearer tokens.** It hardcodes DEBUG and prints
     the parsed login response, access and refresh token included, to a handler
     bound to `sys.stdout`. In an MCP server stdout *is* the JSON-RPC transport.
     mcpkit repoints stdout to stderr before any of that could land, so the
     damage would be tokens in a log rather than a corrupted stream - but a
     credential leak prevented by somebody else's unrelated defence is not a
     property worth depending on. Nothing here ever logs a token.

  3. **It costs two dependencies.** `requests` and `pydantic`, for what is ten
     HTTP calls and a handful of range checks. Every other server in this repo
     but `plex-mcp` runs on the standard library, and this one does too.

What the brewer will and will not let you do, because the asymmetry shapes the
whole tool surface:

  **`/start` itself takes no arguments** - it brews whatever Instant Brew preset
  the device has selected. The recipe is therefore a separate write that has to
  happen first, and `set_instant_brew` is it: `ibSelectedProfileId` through the
  generic device PATCH, verified against the device afterwards.

  That route was not supposed to work. Fellow's gateway rejects its own mobile
  client's dedicated selected-profile route for a live Aiden, which is why the
  Home Assistant integration exposes no such control, and the first version of
  this file recorded the same conclusion. The generic PATCH is a different door
  to the same setting and nobody had tried it; against a real brewer it takes.
  Nothing here relies on that continuing to be true - the selection is read back
  off the machine every time, and a write that changed nothing is reported as a
  failure naming the preset that survived.

  So `brew_now(profile=...)` selects, verifies, and only then starts. A
  selection that does not take stops the brew rather than falling through to
  it, because brewing the wrong recipe under the right name is worse than
  brewing nothing.

  Scheduled brews never depended on any of this - a schedule carries its own
  profile and water volume, and Fellow caps them at ten per brewer.

  **There is no remote stop.** A stop route exists in the mobile client and was
  observed failing to cancel a live brew, so exposing it would be a button that
  lies. Once a brew starts, it finishes at the brewer.

Safety preconditions are checked before dispatch and are not a formality: an
Aiden with no basket in it, or a carafe missing under a batch brew, pours hot
water onto the counter. The gate is ported from the Home Assistant integration's
`can_start_brew`, with each condition reported separately - "the lid is open"
sends someone to the kitchen, "cannot start" sends them to the logs.

A start is never retried. Every other call in here retries on 5xx; this one
cannot, because a request that timed out on the response may well have started a
brew, and the failure mode of guessing wrong is a second pot of coffee.

Two ways to run it:

  1. As an MCP server over stdio (what the agent uses):
         python aiden_mcp_server.py serve

  2. As a plain CLI (what a human uses to prove it works):
         python aiden_mcp_server.py brew_status
         python aiden_mcp_server.py brew_now
         python aiden_mcp_server.py list_profiles
         python aiden_mcp_server.py schedule_brew time=06:45 days=weekdays profile="Morning" water_ml=950
"""

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from time import monotonic, sleep

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcpkit import ToolError, b, i, n, run, s, tool  # noqa: E402

# v2. The older v1 base has no start route - see the module docstring.
BASE_URL = os.environ.get(
    "FELLOW_BASE_URL", "https://l8qtmnc692.execute-api.us-west-2.amazonaws.com/v2"
).rstrip("/")

FELLOW_EMAIL = os.environ.get("FELLOW_EMAIL", "").strip()
FELLOW_PASSWORD = os.environ.get("FELLOW_PASSWORD", "")
TIMEOUT = int(os.environ.get("FELLOW_TIMEOUT", "30"))

# The mobile app's User-Agent. Fellow's gateway is picky about clients it does
# not recognise, and this is the one both reference projects settled on.
USER_AGENT = "Fellow/5 CFNetwork/1568.300.101 Darwin/24.2.0"

# How long to wait for the brewer to actually enter a brewing state after a
# start is accepted. The cloud acknowledges in milliseconds; the machine takes
# several seconds to wake the pump and report it. Under this, a perfectly good
# brew gets reported as unconfirmed.
CONFIRM_TIMEOUT = int(os.environ.get("FELLOW_CONFIRM_TIMEOUT", "25"))
CONFIRM_INTERVAL = 2.0

# Remote start landed in this firmware. Below it the button exists in the cloud
# and the brewer ignores it, which is the worst possible combination.
MIN_REMOTE_START_FIRMWARE = (1, 5, 16)
_FIRMWARE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\+[0-9A-Za-z.-]+)?$")

# Fellow's own bounds. Enforced here so a bad value is a sentence from this
# server rather than a 400 with a server-side field name in it.
MIN_WATER_ML = 150
MAX_WATER_ML = 1500

# Fellow caps schedules per brewer and answers a create past it with a 400.
# Verified against a real Aiden.
MAX_SCHEDULES = 10

RATIO_STEPS = [14 + 0.5 * i for i in range(13)]          # 14 .. 20 by halves
BLOOM_RATIO_STEPS = [1 + 0.5 * i for i in range(5)]      # 1 .. 3 by halves
TEMP_STEPS = [50 + 0.5 * i for i in range(99)]           # 50 .. 99 by halves
BLOOM_DURATION_RANGE = (1, 120)                          # seconds
PULSES_RANGE = (1, 10)
PULSE_INTERVAL_RANGE = (5, 60)                           # seconds

# Title characters Fellow accepts. A rejected title comes back as a generic 400,
# so it is worth catching the apostrophe in "Nick's Morning" before it ships.
TITLE_RE = re.compile(r"[A-Za-z0-9 !@#$%&*\-+?/.,:)(]+")
MAX_TITLE_LEN = 50

# Fields the server owns. Sending any of them back on a create or update is
# rejected, and a profile imported from a share link arrives carrying them.
SERVER_OWNED_FIELDS = (
    "id", "createdAt", "deletedAt", "lastUsedTime", "sharedFrom",
    "isDefaultProfile", "instantBrew", "folder", "duration", "lastGBQuantity",
)

# Sunday-first, because that is how the brewer indexes its own day array and a
# translation layer that disagrees with the device is a silent Monday/Sunday
# bug that only shows up once a week.
DAY_NAMES = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
DAY_ABBR = {
    "sun": 0, "mon": 1, "tue": 2, "tues": 2, "wed": 3, "weds": 3,
    "thu": 4, "thur": 4, "thurs": 4, "fri": 5, "sat": 6,
}
DAY_GROUPS = {
    "daily": [True] * 7,
    "everyday": [True] * 7,
    "every day": [True] * 7,
    "weekdays": [False, True, True, True, True, True, False],
    "weekends": [True, False, False, False, False, False, True],
}

# A profile payload is a few hundred bytes and a device list a few kilobytes.
# Anything past this is not a Fellow response and should not be read into
# memory to find out.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

_ssl_context = ssl.create_default_context()

_session = {"access": None, "refresh": None, "brewer_id": None, "display_name": None}


def log(msg):
    """stderr only, and never with a token in it."""
    print(f"[aiden-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _request(method, path, body=None, params=None, auth=True, retry_5xx=True):
    """
    One HTTP call against Fellow's gateway.

    Returns (status, parsed_json_or_None). Does not raise on HTTP status - the
    callers all want to branch on it, especially 401, which is the ordinary way
    an access token expires rather than an error.

    `retry_5xx=False` exists for exactly one caller. See `brew_now`.
    """
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth and _session["access"]:
        headers["Authorization"] = "Bearer " + _session["access"]

    attempts = 3 if retry_5xx else 1
    last_error = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES)
                return resp.status, _parse(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES)
            if exc.code >= 500 and attempt < attempts - 1:
                sleep(1.0 * (attempt + 1))
                continue
            return exc.code, _parse(raw)
        except OSError as exc:
            # OSError, not URLError. urllib wraps a failed *connect* in URLError
            # but lets a connection dropped mid-response through raw - a
            # ConnectionAbortedError or ConnectionResetError straight off the
            # socket. URLError and TimeoutError are both OSError subclasses, so
            # one clause covers the lot; the narrower one let a WinError escape
            # as an unhandled exception with no sentence attached to it.
            last_error = getattr(exc, "reason", None) or exc
            if attempt < attempts - 1:
                sleep(1.0 * (attempt + 1))
                continue

    # "Nothing was sent" is only true for the calls that are safe to retry. The
    # one that is not - the brew start - reaches here having possibly been
    # received and acted on, with only the response lost, and telling somebody
    # nothing happened is how they start a second pot.
    if retry_5xx:
        raise ToolError(
            f"Could not reach Fellow's API ({last_error}). The brewer talks to "
            f"the cloud, not to this machine, so this is an internet or "
            f"Fellow-side problem, not the brewer being off. Nothing was sent."
        )
    raise ToolError(
        f"Lost the connection to Fellow's API ({last_error}) while sending a "
        f"command that cannot be safely repeated. It may or may not have "
        f"arrived. Do NOT retry - call brew_status and see whether a brew is "
        f"now running."
    )


def _parse(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _login():
    """Exchange credentials for tokens. Never logs the response."""
    if not FELLOW_EMAIL or not FELLOW_PASSWORD:
        raise ToolError(
            "No Fellow credentials. Set FELLOW_EMAIL and FELLOW_PASSWORD in the "
            "environment - the same account you use in the Fellow app. This is "
            "final until they are set; no tool here can work without them."
        )
    status, parsed = _request(
        "post", "/auth/login",
        body={"email": FELLOW_EMAIL, "password": FELLOW_PASSWORD},
        auth=False,
    )
    if status in (400, 401, 403):
        raise ToolError(
            "Fellow rejected the login. Check FELLOW_EMAIL and FELLOW_PASSWORD "
            "against the Fellow app. Do not retry with altered credentials - "
            "repeated failures can lock the account."
        )
    if not isinstance(parsed, dict) or "accessToken" not in parsed:
        raise ToolError(f"Unexpected login response from Fellow (HTTP {status}).")
    _session["access"] = parsed["accessToken"]
    _session["refresh"] = parsed.get("refreshToken")
    log("authenticated")


def _refresh():
    """Trade the refresh token for a new access token. False if it is spent."""
    if not _session["refresh"]:
        return False
    status, parsed = _request(
        "post", "/auth/refresh-token",
        body={"refreshToken": _session["refresh"]}, auth=False,
    )
    if status != 200 or not isinstance(parsed, dict) or "accessToken" not in parsed:
        return False
    _session["access"] = parsed["accessToken"]
    if "refreshToken" in parsed:
        _session["refresh"] = parsed["refreshToken"]
    log("token refreshed")
    return True


def api(method, path, body=None, params=None, retry_5xx=True):
    """
    An authenticated call, with the token lifecycle handled once.

    A 401 is the normal end of an access token's life, not a failure: refresh,
    retry, and fall back to a full login if the refresh token is spent too.
    """
    if not _session["access"]:
        _login()

    status, parsed = _request(method, path, body, params, retry_5xx=retry_5xx)
    if status != 401:
        return status, parsed

    if not _refresh():
        _login()
    return _request(method, path, body, params, retry_5xx=retry_5xx)


def brewer_id():
    """
    The account's Aiden, resolved once and cached.

    Both reference projects assume a single brewer per account and so does this.
    If someone owns two, FELLOW_BREWER_NAME picks by display name rather than
    silently operating whichever one the API happened to list first.
    """
    if _session["brewer_id"]:
        return _session["brewer_id"]

    status, parsed = api("get", "/devices", params={"dataType": "real"})
    if status != 200 or not isinstance(parsed, list):
        raise ToolError(f"Could not list devices on the Fellow account (HTTP {status}).")
    if not parsed:
        raise ToolError(
            "The Fellow account has no devices on it. Pair the Aiden in the "
            "Fellow app first - this server cannot pair one."
        )

    wanted = os.environ.get("FELLOW_BREWER_NAME", "").strip().lower()
    chosen = None
    if wanted:
        for device in parsed:
            if str(device.get("displayName", "")).strip().lower() == wanted:
                chosen = device
                break
        if chosen is None:
            names = [d.get("displayName") for d in parsed]
            raise ToolError(
                f"No brewer named {wanted!r} on the account. Present: {names}. "
                f"Fix FELLOW_BREWER_NAME or unset it to use the only brewer."
            )
    elif len(parsed) > 1:
        names = [d.get("displayName") for d in parsed]
        raise ToolError(
            f"The account has {len(parsed)} devices ({names}) and no "
            f"FELLOW_BREWER_NAME set. Set it so this does not operate the wrong "
            f"machine."
        )
    else:
        chosen = parsed[0]

    _session["brewer_id"] = chosen.get("id")
    _session["display_name"] = chosen.get("displayName")
    if not _session["brewer_id"]:
        raise ToolError("Fellow returned a device with no id. Nothing can be addressed.")
    return _session["brewer_id"]


def device_config():
    """Live device state. Always fetched - this is what every safety check reads."""
    status, parsed = api("get", f"/devices/{brewer_id()}", params={"dataType": "real"})
    if status != 200 or not isinstance(parsed, dict):
        raise ToolError(f"Could not read brewer state (HTTP {status}).")
    return parsed


# ---------------------------------------------------------------------------
# Telemetry readers
#
# The v2 device payload reports the same fact in more than one place and the
# older flags go stale, so every one of these prefers the live nested `state`
# object and falls back rather than guessing. Ported from the Home Assistant
# integration, where the fallbacks were worked out against real hardware.
# ---------------------------------------------------------------------------

_PHASE_CODES = {"b": "bloom", "d": "drip_finish", "pa": "paused"}


def brew_phase(cfg):
    """A stable phase name: idle, bloom, pulse_N, drip_finish, paused, brewing."""
    if "state" not in cfg:
        brewing = cfg.get("brewing")
        if brewing is True:
            return "brewing"
        if brewing is False:
            return "idle"
        return "unknown"

    state = cfg.get("state")
    if state is None:
        return "idle"
    if not isinstance(state, dict):
        return "unknown"
    value = state.get("value")
    if not isinstance(value, str):
        return "unknown"
    suffix = value[1:]
    if value.startswith("p") and suffix.isascii() and suffix.isdigit():
        number = int(suffix)
        if 1 <= number <= 10:
            return f"pulse_{number}"
    return _PHASE_CODES.get(value, "unknown")


def is_brewing(cfg):
    """True/False, or None when the payload does not say. None is not False."""
    if "state" in cfg:
        return cfg.get("state") is not None
    value = cfg.get("brewing")
    return value if isinstance(value, bool) else None


def is_missing_water(cfg):
    """Either indicator saying 'empty' wins - a false negative here scorches a pump."""
    top = cfg.get("missingWater")
    state = cfg.get("state")
    nested = state.get("missing_water") if isinstance(state, dict) else None
    if top is True or nested is True:
        return True
    if isinstance(top, bool):
        return top
    return nested if isinstance(nested, bool) else None


def has_brew_error(cfg):
    state = cfg.get("state")
    if "state" not in cfg:
        return None
    if state is None:
        return False
    if not isinstance(state, dict):
        return None
    return state.get("error") is not None


def has_unsynced_changes(cfg):
    """
    Whether the cloud is holding changes the brewer has not picked up yet.

    A write can be accepted by Fellow and sit queued while the machine is
    asleep. That is not the same as a write that failed, and the two need
    opposite responses - wait, versus stop and say so.
    """
    unsynced = cfg.get("unsynced")
    if isinstance(unsynced, list):
        return bool(unsynced)
    return None


def supports_remote_start(cfg):
    firmware = cfg.get("firmwareVersion")
    if not isinstance(firmware, str):
        return False
    match = _FIRMWARE_RE.fullmatch(firmware.strip())
    if match is None:
        return False
    return tuple(int(p) for p in match.groups()) >= MIN_REMOTE_START_FIRMWARE


def start_blockers(cfg):
    """
    Every reason this brewer will not start right now, each as a sentence.

    Returned as a list rather than a bool on purpose. "Cannot start" makes
    someone open the logs; "the carafe is missing" makes them walk to the
    kitchen, which is the entire point of asking a brewer whether it is ready.
    """
    blockers = []

    if not supports_remote_start(cfg):
        firmware = cfg.get("firmwareVersion") or "unknown"
        blockers.append(
            f"Firmware {firmware} is below "
            f"{'.'.join(str(p) for p in MIN_REMOTE_START_FIRMWARE)}, which is where "
            f"remote start began working. Below it the cloud accepts the command "
            f"and the brewer ignores it. Update in the Fellow app."
        )
    if cfg.get("isConnected") is not True:
        blockers.append(
            "The brewer is offline - it is not talking to Fellow's cloud. Check "
            "its wifi; nothing can be sent to it until it reconnects."
        )
    if is_brewing(cfg) is True:
        blockers.append(f"A brew is already running (phase: {brew_phase(cfg)}).")
    if cfg.get("lidClosed") is not True:
        blockers.append("The lid is open. Close it.")
    if is_missing_water(cfg) is True:
        blockers.append("The reservoir is empty. Fill it.")
    if cfg.get("cleaning") is True:
        blockers.append("The brewer is running a cleaning cycle. Wait for it to finish.")
    if cfg.get("rinsing") is True:
        blockers.append("The brewer is rinsing. Wait for it to finish.")

    single = cfg.get("singleBrewBasketPresent") is True
    batch = cfg.get("batchBrewBasketPresent") is True
    carafe = cfg.get("carafePresent") is True
    if not single and not batch:
        blockers.append("No brew basket is in the machine. Put one in.")
    elif batch and not single and not carafe:
        blockers.append(
            "The batch basket is in but the carafe is missing - starting now "
            "would pour coffee onto the counter. Put the carafe under it."
        )

    if has_brew_error(cfg) is True:
        state = cfg.get("state") or {}
        blockers.append(f"The brewer is reporting an error: {state.get('error')}.")

    return blockers


# ---------------------------------------------------------------------------
# Resolution - the agent never sees a raw identifier
# ---------------------------------------------------------------------------


def fetch_profiles():
    status, parsed = api("get", f"/devices/{brewer_id()}/profiles")
    if status != 200 or not isinstance(parsed, list):
        raise ToolError(f"Could not list brew profiles (HTTP {status}).")
    return parsed


def fetch_schedules():
    status, parsed = api("get", f"/devices/{brewer_id()}/schedules")
    if status != 200 or not isinstance(parsed, list):
        raise ToolError(f"Could not list schedules (HTTP {status}).")
    return parsed


def resolve_profile(name, profiles=None):
    """
    A recipe title to a profile record. Exact first, then case-insensitive,
    then a containment match - in that order, so "morning" finding exactly one
    "Morning Ethiopian" works while an ambiguous prefix is refused rather than
    picked arbitrarily.
    """
    profiles = profiles if profiles is not None else fetch_profiles()
    if not profiles:
        raise ToolError(
            "The brewer has no profiles saved at all. Create one with "
            "create_profile, or import one with import_profile."
        )

    wanted = (name or "").strip()
    if not wanted:
        raise ToolError("No profile named. Pass profile= with a recipe title.")

    for profile in profiles:
        if profile.get("title") == wanted:
            return profile
    lowered = wanted.lower()
    for profile in profiles:
        if str(profile.get("title", "")).lower() == lowered:
            return profile

    partial = [p for p in profiles if lowered in str(p.get("title", "")).lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ToolError(
            f"{wanted!r} matches more than one profile: "
            f"{[p.get('title') for p in partial]}. Use the full title."
        )

    raise ToolError(
        f"No profile titled {wanted!r}. Saved profiles: "
        f"{[p.get('title') for p in profiles]}."
    )


def instant_brew_selection(cfg, profiles=None):
    """
    What `/start` would actually brew: (profile_id, title, water_ml).

    Four field names are in play and two of them are traps, so this is the only
    place any of them is read:

      `ibSelectedProfileId` / `ibWaterQuantity`
          The Instant Brew preset. This is what a remote start runs, and it is
          populated whether or not a brew is in progress.

      `brewingProfileId` / `brewingWaterVolumeMl`
          The brew that is running, or the one that last ran - the Home
          Assistant integration surfaces the second as `last_brew_volume`.
          `brewingProfileId` is empty on an idle brewer, and the water figure
          is history rather than intent. Reading either as "what happens next"
          answers a question nobody asked, confidently, with a real number
          attached.
    """
    profile_id = cfg.get("ibSelectedProfileId")
    return profile_id, profile_title(profile_id, profiles), cfg.get("ibWaterQuantity")


def current_brew_selection(cfg, profiles=None):
    """The brew in progress, or the last one: (title, water_ml). Not a forecast."""
    return (
        profile_title(cfg.get("brewingProfileId"), profiles),
        cfg.get("brewingWaterVolumeMl"),
    )


def profile_title(profile_id, profiles=None):
    """A profile id back to a human title, for reporting. Never the reverse."""
    if not profile_id:
        return None
    try:
        profiles = profiles if profiles is not None else fetch_profiles()
    except ToolError:
        return profile_id
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile.get("title")
    return profile_id


def parse_days(days):
    """
    'weekdays', 'daily', 'mon,wed,fri', 'Monday Friday' to a Sunday-first array.

    A closed vocabulary because free text becomes 'Mon', 'monday' and 'M' inside
    a week, after which nothing matches.
    """
    text = (days or "").strip().lower()
    if not text:
        raise ToolError(
            "No days given. Use 'daily', 'weekdays', 'weekends', or a list like "
            "'mon,wed,fri'."
        )
    if text in DAY_GROUPS:
        return list(DAY_GROUPS[text])

    flags = [False] * 7
    tokens = [t.strip() for t in re.split(r"[,\s/]+", text) if t.strip()]
    unknown = []
    for token in tokens:
        index = None
        for position, name in enumerate(DAY_NAMES):
            if token == name or (len(token) >= 3 and name.startswith(token)):
                index = position
                break
        if index is None:
            index = DAY_ABBR.get(token)
        if index is None:
            unknown.append(token)
        else:
            flags[index] = True

    if unknown:
        raise ToolError(
            f"Did not recognise {unknown} as days. Use 'daily', 'weekdays', "
            f"'weekends', or day names like 'mon,wed,fri'."
        )
    if not any(flags):
        raise ToolError("That resolved to no days at all. A schedule needs at least one.")
    return flags


def describe_days(flags):
    """The inverse, for reporting back in the words a person would use."""
    if not isinstance(flags, list) or len(flags) != 7:
        return "unknown days"
    if all(flags):
        return "daily"
    if flags == DAY_GROUPS["weekdays"]:
        return "weekdays"
    if flags == DAY_GROUPS["weekends"]:
        return "weekends"
    return ", ".join(DAY_NAMES[i].capitalize() for i, on in enumerate(flags) if on)


def parse_time(value):
    """'HH:MM' (24-hour) to seconds since midnight. Absolute only - no 'in an hour'."""
    text = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ToolError(
            f"Time must be 24-hour HH:MM, got {value!r}. 6:45am is '06:45', "
            f"6:45pm is '18:45'."
        )
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ToolError(f"{text} is not a real time of day.")
    return hours * 3600 + minutes * 60


def describe_time(seconds):
    if not isinstance(seconds, int):
        return "unknown time"
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def resolve_schedule(at, profile=None, schedules=None):
    """
    A schedule by the time it fires, disambiguated by profile when two collide.

    Schedules have ids like 's0' and the agent should never have to hold one.
    Two schedules at the same minute is rare enough that asking for a profile
    only in that case is cheaper than making every call carry one.
    """
    schedules = schedules if schedules is not None else fetch_schedules()
    if not schedules:
        raise ToolError("There are no schedules on the brewer at all.")

    seconds = parse_time(at)
    matches = [s for s in schedules if s.get("secondFromStartOfTheDay") == seconds]
    if not matches:
        existing = [
            f"{describe_time(s.get('secondFromStartOfTheDay'))} {describe_days(s.get('days'))}"
            for s in schedules
        ]
        raise ToolError(f"No schedule at {at}. Existing schedules: {existing}.")

    if len(matches) == 1:
        return matches[0]

    if profile:
        profiles = fetch_profiles()
        target = resolve_profile(profile, profiles)
        narrowed = [s for s in matches if s.get("profileId") == target.get("id")]
        if len(narrowed) == 1:
            return narrowed[0]
        if not narrowed:
            raise ToolError(
                f"No schedule at {at} uses profile {target.get('title')!r}."
            )

    titles = [profile_title(s.get("profileId")) for s in matches]
    raise ToolError(
        f"{len(matches)} schedules fire at {at}, using profiles {titles}. "
        f"Pass profile= to say which one."
    )


# ---------------------------------------------------------------------------
# Profile construction
# ---------------------------------------------------------------------------


def nearest_step(value, steps, label):
    """
    Snap an in-range value to the half-step grid Fellow accepts; refuse the rest.

    The brewer takes half-degrees and half-ratios and nothing between, and
    "96 degrees" spoken out loud arrives as 96.0 while "about 94 and a bit"
    arrives as anything. Snapping 94.3 to 94.5 is right - the difference is
    below what the machine can do. Snapping 25 to 20 is not; that is somebody
    meaning something else, and it gets a sentence instead. Since the grid is
    0.5, nothing within range is ever further than 0.25 away, so the tolerance
    below only ever catches genuinely out-of-range values.
    """
    if value is None:
        return None
    closest = min(steps, key=lambda step: abs(step - value))
    if abs(closest - value) > 0.26:
        raise ToolError(
            f"{label} must be one of {steps[0]}-{steps[-1]} in steps of 0.5, "
            f"got {value}."
        )
    return closest


def check_range(value, bounds, label):
    if value is None:
        return None
    low, high = bounds
    if not (low <= value <= high):
        raise ToolError(f"{label} must be between {low} and {high}, got {value}.")
    return value


def check_title(title):
    text = (title or "").strip()
    if not text:
        raise ToolError("A profile needs a title.")
    if len(text) > MAX_TITLE_LEN:
        raise ToolError(
            f"Profile titles are at most {MAX_TITLE_LEN} characters; that one is "
            f"{len(text)}."
        )
    if not TITLE_RE.fullmatch(text):
        bad = sorted({c for c in text if not TITLE_RE.fullmatch(c)})
        raise ToolError(
            f"Fellow rejects {bad} in a profile title. Letters, digits, spaces "
            f"and !@#$%&*-+?/.,:)( only - note that an apostrophe is not allowed."
        )
    return text


def check_water(ml):
    if not (MIN_WATER_ML <= ml <= MAX_WATER_ML):
        raise ToolError(
            f"Water must be between {MIN_WATER_ML}ml and {MAX_WATER_ML}ml, got {ml}ml."
        )
    return ml


def profile_temperature(profile):
    """
    Brew temperature and where it came from: (celsius, source).

    `overallTemperature` is absent on a good many real profiles, and the pulse
    temperatures are the water that actually goes through the grounds, so they
    are a fair stand-in. The bloom temperature is not, and was briefly used as
    a last resort here - which made a cold brew profile, whose bloom fields are
    leftovers the machine ignores, report brewing at 99C. A recipe with no
    pulses has no brew temperature to report, and saying so is the honest
    answer; inventing one from an unrelated field is how a cold brew ends up
    described as near-boiling.
    """
    overall = profile.get("overallTemperature")
    if overall is not None:
        return overall, "profile"
    for key in ("ssPulseTemperatures", "batchPulseTemperatures"):
        temps = profile.get(key)
        if isinstance(temps, list) and temps:
            return temps[0], "pulses"
    return None, None


def summarize_profile(profile, selected_id=None):
    """
    The shape reported back. Deliberately not the raw record.

    `instantBrew` on a profile is not the Instant Brew selection - it reads
    False on every profile including the selected one - so which recipe is
    loaded is decided by comparing against the device's `ibSelectedProfileId`
    and nothing here reads that field.

    `overallTemperature` is absent on a good many real profiles, including
    Fellow's own presets, so the brew temperature falls back to the pulse
    temperatures. `temperature_source` says which it was, because a number
    inferred from somewhere else should not look like one the recipe states.
    """
    temperature, temperature_source = profile_temperature(profile)
    return {
        "title": profile.get("title"),
        "ratio": profile.get("ratio"),
        "temperature_c": temperature,
        "temperature_source": temperature_source,
        "bloom": (
            {
                "duration_s": profile.get("bloomDuration"),
                "ratio": profile.get("bloomRatio"),
                "temperature_c": profile.get("bloomTemperature"),
            }
            if profile.get("bloomEnabled")
            else None
        ),
        "pulses": profile.get("ssPulsesNumber") if profile.get("ssPulsesEnabled") else None,
        "pulse_interval_s": profile.get("ssPulsesInterval") if profile.get("ssPulsesEnabled") else None,
        "is_instant_brew_recipe": (
            None if selected_id is None else profile.get("id") == selected_id
        ),
        "last_used": profile.get("lastUsedTime"),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "Read the Fellow Aiden's live state: whether it is brewing and at what "
    "stage, whether it is ready to start one, what Instant Brew recipe is "
    "currently loaded, and anything physically in the way (open lid, empty "
    "reservoir, missing carafe). Call this before brew_now to tell someone "
    "what needs doing, and to find out what recipe brew_now would actually make.",
    {},
)
def brew_status():
    cfg = device_config()
    profiles = fetch_profiles()
    blockers = start_blockers(cfg)
    brewing = is_brewing(cfg)
    phase = brew_phase(cfg)

    _, loaded, water = instant_brew_selection(cfg, profiles)
    running, running_water = current_brew_selection(cfg, profiles)
    online = cfg.get("isConnected") is True

    if not online:
        # Everything below this line came from the cloud's last snapshot, which
        # may be a day old. Reporting "lid closed, ready to go" about a brewer
        # that has been unplugged since yesterday is worse than reporting
        # nothing, so the staleness leads and is flagged in the payload too.
        summary = (
            f"{_session['display_name'] or 'The Aiden'} is offline - it is not "
            f"talking to Fellow's cloud. The readings below are its last known "
            f"state and may be stale. Check its wifi."
        )
    elif brewing:
        running_amount = f", {running_water}ml" if running_water else ""
        summary = (
            f"{_session['display_name'] or 'The Aiden'} is brewing "
            f"{running or 'a recipe it has not named'}{running_amount} ({phase})."
        )
    elif not blockers:
        amount = f", {water}ml" if water else ""
        making = (
            f"{loaded}{amount}" if loaded
            else "a recipe the brewer has not reported - no Instant Brew preset "
                 "appears to be selected on it"
        )
        summary = (
            f"{_session['display_name'] or 'The Aiden'} is idle and ready. "
            f"brew_now would make {making}."
        )
    else:
        summary = (
            f"{_session['display_name'] or 'The Aiden'} is idle but not ready: "
            f"{blockers[0]}"
        )

    return {
        "ok": True,
        "summary": summary,
        "brewer": _session["display_name"],
        "online": online,
        "readings_stale": not online,
        "brewing": brewing,
        "phase": phase,
        "ready_to_brew": not blockers,
        "blockers": blockers,
        "instant_brew_recipe": loaded,
        "instant_brew_water_ml": water,
        # What is running now, or last ran. Distinct from the two above, which
        # are what a start would do next.
        "current_or_last_brew_recipe": running,
        "current_or_last_brew_water_ml": running_water,
        "missing_water": is_missing_water(cfg),
        "lid_closed": cfg.get("lidClosed"),
        "carafe_present": cfg.get("carafePresent"),
        "single_basket_present": cfg.get("singleBrewBasketPresent"),
        "batch_basket_present": cfg.get("batchBrewBasketPresent"),
        "cleaning": cfg.get("cleaning"),
        "rinsing": cfg.get("rinsing"),
        "firmware": cfg.get("firmwareVersion"),
        "profile_count": len(profiles),
    }


@tool(
    "Start brewing coffee now. Name a recipe to brew that one, or omit it to "
    "brew whatever the machine currently has selected. Checks the brewer is "
    "physically ready first, selects the recipe and verifies the machine took "
    "it, then confirms the brew actually started. There is no remote stop: "
    "once this succeeds the brew runs to completion at the machine.",
    {
        "profile": s(
            "Title of the saved recipe to brew. See list_profiles. Omit to "
            "brew the recipe already selected on the machine."
        ),
        "water_ml": i(
            f"Water volume in millilitres, {MIN_WATER_ML} to {MAX_WATER_ML}. "
            f"Omit to use the volume already set.",
        ),
        "confirm_seconds": i(
            "How long to wait for the brewer to report it started.",
            default=CONFIRM_TIMEOUT, minimum=0, maximum=120,
        ),
    },
)
def brew_now(profile=None, water_ml=None, confirm_seconds=CONFIRM_TIMEOUT):
    cfg = device_config()
    profiles = fetch_profiles()

    blockers = start_blockers(cfg)
    if blockers:
        raise ToolError(
            "The brewer is not ready: " + " ".join(blockers),
            blockers=blockers,
            phase=brew_phase(cfg),
        )

    loaded_id, loaded, water = instant_brew_selection(cfg, profiles)

    if profile or water_ml is not None:
        if not profile:
            raise ToolError(
                "water_ml only applies alongside a recipe. Name the profile to "
                "brew, or use set_instant_brew to change the volume on its own."
            )
        wanted = resolve_profile(profile, profiles)
        # Select, then verify off the device. A selection that did not take
        # would mean brewing something other than what was asked for, under
        # the name of what was asked for - so a failure here stops the brew
        # rather than falling through to it.
        outcome = apply_instant_brew(wanted, water_ml, profiles, settle_seconds=12)
        if not outcome["ok"]:
            raise ToolError(
                f"Could not select {wanted.get('title')!r}, so nothing was brewed. "
                f"{outcome['error']}",
                instant_brew_recipe=outcome.get("instant_brew_recipe"),
                requested_recipe=wanted.get("title"),
            )
        loaded, water = outcome["recipe"], outcome["water_ml"]
    elif not loaded_id:
        raise ToolError(
            "The brewer is not reporting an Instant Brew preset, so starting now "
            "would brew something this cannot name. Pass profile= with the recipe "
            "to brew, or select one on the machine. Nothing was started."
        )

    # Not retried, at any status. A request that times out waiting for the
    # response may still have started a brew, and there is no remote stop to
    # undo a duplicate with.
    status, parsed = api(
        "patch", f"/devices/{brewer_id()}/start",
        params={"confirm": "true"}, retry_5xx=False,
    )
    if status >= 400:
        message = ""
        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("error") or ""
        raise ToolError(
            f"Fellow refused the start (HTTP {status}{': ' + message if message else ''}). "
            f"The brewer reported itself ready a moment ago, so this is a cloud-side "
            f"refusal. Do NOT retry blindly - call brew_status first and see whether a "
            f"brew is now running."
        )

    # Read-back. The cloud acknowledges long before the machine wakes its pump,
    # and 'accepted' is exactly the claim that makes people stop trusting this.
    deadline = monotonic() + max(0, confirm_seconds)
    phase = brew_phase(cfg)
    confirmed = False
    while monotonic() < deadline:
        sleep(CONFIRM_INTERVAL)
        try:
            current = device_config()
        except ToolError:
            continue
        phase = brew_phase(current)
        if is_brewing(current) is True:
            confirmed = True
            break

    amount = f", {water}ml" if water else ""
    if confirmed:
        return {
            "ok": True,
            "summary": f"Brewing {loaded or 'the loaded recipe'}{amount} (confirmed, phase: {phase}).",
            "confirmed": True,
            "recipe": loaded,
            "water_ml": water,
            "phase": phase,
            "note": "There is no remote stop. This brew runs to completion at the machine.",
        }

    return {
        "ok": True,
        "confirmed": False,
        "summary": (
            f"Fellow accepted the start for {loaded or 'the loaded recipe'}{amount}, but the "
            f"brewer has not reported brewing after {confirm_seconds}s (phase: {phase}). "
            f"It may still be waking up. Check brew_status before starting another - "
            f"a second start would make a second pot and there is no remote stop."
        ),
        "recipe": loaded,
        "water_ml": water,
        "phase": phase,
    }


@tool(
    "Choose which saved recipe, and how much water, the Instant Brew preset "
    "uses - the one brew_now starts and the one the button on the machine "
    "starts. Verifies the brewer actually took the change rather than trusting "
    "the response, and reports plainly if it did not.",
    {
        "profile": s("Title of the saved recipe to select. See list_profiles."),
        "water_ml": i(
            f"Water volume in millilitres, {MIN_WATER_ML} to {MAX_WATER_ML}. "
            f"Omit to leave the current volume alone.",
        ),
        "settle_seconds": i(
            "How long to wait for the brewer to report the new selection.",
            default=12, minimum=0, maximum=60,
        ),
    },
    required=["profile"],
)
def set_instant_brew(profile, water_ml=None, settle_seconds=12):
    """
    Write the Instant Brew preset, then prove it landed.

    Fellow's own mobile client has a dedicated selected-profile route that its
    gateway rejects for a live Aiden, which is why the Home Assistant
    integration does not expose one. This goes at the same setting through the
    generic device PATCH instead - a different door to the same room, and one
    nobody had tried. It may simply not work, so nothing here believes the
    response: the selection is read back off the device and a write that did
    not take is reported as a failure with the old value named, rather than as
    a success that quietly changed nothing.
    """
    profiles = fetch_profiles()
    target = resolve_profile(profile, profiles)
    outcome = apply_instant_brew(target, water_ml, profiles, settle_seconds)
    if not outcome["ok"]:
        return outcome
    if outcome["unchanged"]:
        return {
            "ok": True,
            "summary": (
                f"Instant Brew was already set to {outcome['recipe']!r}"
                f"{f', {outcome['water_ml']}ml' if outcome['water_ml'] else ''}. "
                f"Nothing changed."
            ),
            "confirmed": True,
            "recipe": outcome["recipe"],
            "water_ml": outcome["water_ml"],
        }
    return {
        "ok": True,
        "summary": (
            f"Instant Brew is now {outcome['recipe']!r}"
            f"{f', {outcome['water_ml']}ml' if outcome['water_ml'] else ''} "
            f"(confirmed, was {outcome['previous_recipe']!r})."
            f"{outcome['volume_note']}"
        ),
        "confirmed": True,
        "recipe": outcome["recipe"],
        "water_ml": outcome["water_ml"],
        "previous_recipe": outcome["previous_recipe"],
    }


def apply_instant_brew(target, water_ml, profiles, settle_seconds):
    """
    Point the Instant Brew preset at `target`, then prove the brewer took it.

    Shared by `set_instant_brew` and by `brew_now`'s `profile` argument, and
    it has to be shared: a selection that silently did not take turns
    `brew_now(profile="Light Roast")` into a machine quietly brewing something
    else, which is the exact failure the read-back exists to prevent. Returns a
    dict rather than raising so both callers can phrase the refusal in their
    own terms - "could not select" and "will not brew" are different sentences.
    """
    before = device_config()
    before_id, before_title, before_water = instant_brew_selection(before, profiles)

    payload = {"ibSelectedProfileId": target["id"]}
    if water_ml is not None:
        payload["ibWaterQuantity"] = check_water(water_ml)

    if before_id == target["id"] and (water_ml is None or before_water == water_ml):
        return {
            "ok": True, "unchanged": True, "recipe": before_title,
            "water_ml": before_water, "previous_recipe": before_title,
            "volume_note": "",
        }

    status, parsed = api("patch", f"/devices/{brewer_id()}", body=payload)
    if status >= 400:
        message = ""
        if isinstance(parsed, dict):
            message = parsed.get("message") or parsed.get("error") or ""
        raise ToolError(
            f"Fellow refused to change the Instant Brew selection (HTTP {status}"
            f"{': ' + str(message) if message else ''}). The preset is still "
            f"{before_title!r}. It may only be settable on the machine itself or "
            f"in the Fellow app - schedule_brew carries its own recipe and is "
            f"unaffected by this."
        )

    # Read back. A queued change sits in `unsynced` while the brewer is asleep,
    # so a first miss is worth waiting out before calling it a failure.
    deadline = monotonic() + max(0, settle_seconds)
    after_id, after_title, after_water = before_id, before_title, before_water
    unsynced = None
    while True:
        after = device_config()
        after_id, after_title, after_water = instant_brew_selection(after, profiles)
        unsynced = has_unsynced_changes(after)
        settled = after_id == target["id"] and (
            water_ml is None or after_water == water_ml
        )
        if settled or monotonic() >= deadline:
            break
        sleep(2.0)

    if after_id != target["id"]:
        return {
            "ok": False,
            "error": (
                f"Fellow accepted the change but the brewer still has "
                f"{after_title!r} selected for Instant Brew after {settle_seconds}s"
                f"{' (the cloud reports the change still queued)' if unsynced else ''}. "
                f"The preset does not appear to be settable through this route. "
                f"Set it on the machine or in the Fellow app; schedule_brew carries "
                f"its own recipe and does not depend on this."
            ),
            "instant_brew_recipe": after_title,
            "requested_recipe": target.get("title"),
            "unsynced": unsynced,
        }

    volume_note = ""
    if water_ml is not None and after_water != water_ml:
        volume_note = (
            f" The recipe took, but the volume did not - the brewer still reports "
            f"{after_water}ml rather than {water_ml}ml."
        )

    return {
        "ok": True,
        "unchanged": False,
        "recipe": after_title,
        "water_ml": after_water,
        "previous_recipe": before_title,
        "volume_note": volume_note,
    }


@tool(
    "List the brew recipes saved on the machine, with their parameters and "
    "which one is currently loaded as the Instant Brew recipe. Use this to "
    "find out what can be brewed or scheduled before naming a recipe.",
    {},
)
def list_profiles():
    profiles = fetch_profiles()
    if not profiles:
        return {
            "ok": True,
            "summary": "The brewer has no saved recipes. create_profile or import_profile adds one.",
            "profiles": [],
        }
    cfg = device_config()
    selected_id, loaded, water = instant_brew_selection(cfg, profiles)
    selected = f"{loaded}{f' ({water}ml)' if water else ''}" if loaded else "none"
    return {
        "ok": True,
        "summary": f"{len(profiles)} saved recipes; {selected} is loaded for Instant Brew.",
        "instant_brew_recipe": loaded,
        "instant_brew_water_ml": water,
        "profiles": [summarize_profile(p, selected_id) for p in profiles],
    }


@tool(
    "Save a new brew recipe on the machine. Only the title is required - every "
    "other parameter defaults to a balanced filter recipe. Note this does NOT "
    "make it the Instant Brew recipe (that can only be selected on the device) "
    "and does not brew anything; pass the title to schedule_brew to use it.",
    {
        "title": s("Recipe name, up to 50 characters. No apostrophes - Fellow rejects them."),
        "ratio": n("Water-to-coffee ratio, 14 to 20 in steps of 0.5. Higher is weaker.", default=16),
        "temperature_c": n("Main brew temperature in Celsius, 50 to 99 in steps of 0.5.", default=96),
        "bloom": b("Pre-wet the grounds before the main pour.", default=True),
        "bloom_seconds": i("How long the bloom lasts, 1 to 120 seconds.", default=30),
        "bloom_ratio": n("Bloom water as a multiple of coffee weight, 1 to 3 in steps of 0.5.", default=2),
        "bloom_temperature_c": n("Bloom temperature in Celsius. Defaults to the main brew temperature."),
        "pulses": i("Number of separate pours after the bloom, 1 to 10.", default=3),
        "pulse_interval_s": i("Seconds between pours, 5 to 60.", default=25),
    },
    required=["title"],
)
def create_profile(title, ratio=16, temperature_c=96, bloom=True, bloom_seconds=30,
                   bloom_ratio=2, bloom_temperature_c=None, pulses=3, pulse_interval_s=25):
    title = check_title(title)
    ratio = nearest_step(ratio, RATIO_STEPS, "ratio")
    temperature_c = nearest_step(temperature_c, TEMP_STEPS, "temperature_c")
    bloom_ratio = nearest_step(bloom_ratio, BLOOM_RATIO_STEPS, "bloom_ratio")
    bloom_temperature_c = nearest_step(
        temperature_c if bloom_temperature_c is None else bloom_temperature_c,
        TEMP_STEPS, "bloom_temperature_c",
    )
    bloom_seconds = check_range(bloom_seconds, BLOOM_DURATION_RANGE, "bloom_seconds")
    pulses = check_range(pulses, PULSES_RANGE, "pulses")
    pulse_interval_s = check_range(pulse_interval_s, PULSE_INTERVAL_RANGE, "pulse_interval_s")

    existing = fetch_profiles()
    for profile in existing:
        if str(profile.get("title", "")).lower() == title.lower():
            raise ToolError(
                f"A recipe titled {profile.get('title')!r} already exists. Pick "
                f"another name, or delete_profile that one first."
            )

    # Every pulse at the same temperature. Per-pulse temperature curves are a
    # Brew Studio concern; exposing ten of them here would be a tool nobody
    # could call correctly out loud.
    temps = [temperature_c] * pulses

    payload = {
        "profileType": 0,
        "title": title,
        "ratio": ratio,
        "overallTemperature": temperature_c,
        "bloomEnabled": bool(bloom),
        "bloomRatio": bloom_ratio,
        "bloomDuration": bloom_seconds,
        "bloomTemperature": bloom_temperature_c,
        "ssPulsesEnabled": True,
        "ssPulsesNumber": pulses,
        "ssPulsesInterval": pulse_interval_s,
        "ssPulseTemperatures": temps,
        "batchPulsesEnabled": True,
        "batchPulsesNumber": pulses,
        "batchPulsesInterval": pulse_interval_s,
        "batchPulseTemperatures": temps,
    }

    status, parsed = api("post", f"/devices/{brewer_id()}/profiles", body=payload)
    if status >= 400 or not isinstance(parsed, dict) or "id" not in parsed:
        message = parsed.get("message") if isinstance(parsed, dict) else None
        raise ToolError(f"Fellow rejected the recipe (HTTP {status}): {message or parsed}")

    saved = resolve_profile(title)
    return {
        "ok": True,
        "summary": (
            f"Saved recipe {title!r} (ratio {ratio}, {temperature_c}C, {pulses} pulses). "
            f"It is not the Instant Brew recipe - select that on the brewer - but "
            f"schedule_brew can use it by name."
        ),
        "confirmed": True,
        "profile": summarize_profile(saved),
    }


@tool(
    "Delete a saved brew recipe by title. Use this to undo a create_profile or "
    "import_profile that was wrong. A recipe used by a schedule should not be "
    "deleted without cancelling that schedule first.",
    {"title": s("Exact title of the recipe to delete.")},
    required=["title"],
)
def delete_profile(title):
    profiles = fetch_profiles()
    target = resolve_profile(title, profiles)

    # A schedule pointing at a deleted profile is a brew that silently stops
    # happening, and nobody notices until a morning with no coffee.
    users = [
        f"{describe_time(s.get('secondFromStartOfTheDay'))} {describe_days(s.get('days'))}"
        for s in fetch_schedules()
        if s.get("profileId") == target.get("id")
    ]
    if users:
        raise ToolError(
            f"{target.get('title')!r} is used by {len(users)} schedule(s): {users}. "
            f"Deleting it would leave them pointing at nothing. Cancel them first "
            f"with cancel_schedule, then delete this."
        )

    status, _ = api("delete", f"/devices/{brewer_id()}/profiles/{target['id']}")
    if status >= 400:
        raise ToolError(f"Fellow refused to delete {target.get('title')!r} (HTTP {status}).")

    remaining = fetch_profiles()
    still_there = any(p.get("id") == target["id"] for p in remaining)
    if still_there:
        return {
            "ok": False,
            "error": (
                f"Fellow accepted the delete but {target.get('title')!r} is still on "
                f"the brewer. Nothing was removed; do not retry until this is understood."
            ),
        }
    return {
        "ok": True,
        "summary": f"Deleted recipe {target.get('title')!r} (confirmed). {len(remaining)} left.",
        "confirmed": True,
    }


@tool(
    "Import a shared brew recipe from a brew.link URL and save it on the "
    "machine. Accepts a full https://brew.link/p/xxxx URL or just the code.",
    {"link": s("A brew.link URL or its short code.")},
    required=["link"],
)
def import_profile(link):
    text = (link or "").strip()
    match = re.search(r"(?:.*?/p/)?([a-zA-Z0-9]+)(?:/([a-zA-Z0-9_-]+))?/?$", text)
    if not match:
        raise ToolError(
            f"{link!r} is not a brew link. Expected something like "
            f"https://brew.link/p/ws98 or the code ws98."
        )
    code, drop_type = match.group(1), match.group(2) or "aiden"

    status, parsed = api("get", f"/shared/{drop_type}/{code}")
    if status == 404:
        raise ToolError(
            f"No shared recipe at code {code!r}. Check the link - it may have been "
            f"revoked by whoever shared it."
        )
    if status >= 400 or not isinstance(parsed, dict):
        raise ToolError(f"Could not fetch shared recipe {code!r} (HTTP {status}).")

    for field in SERVER_OWNED_FIELDS:
        parsed.pop(field, None)

    incoming = str(parsed.get("title", "")).strip()
    if not incoming:
        raise ToolError(f"The shared recipe at {code!r} has no title; nothing to save.")
    for profile in fetch_profiles():
        if str(profile.get("title", "")).lower() == incoming.lower():
            raise ToolError(
                f"A recipe titled {profile.get('title')!r} is already saved. Delete "
                f"it first if you want the shared version instead."
            )

    status, created = api("post", f"/devices/{brewer_id()}/profiles", body=parsed)
    if status >= 400 or not isinstance(created, dict) or "id" not in created:
        message = created.get("message") if isinstance(created, dict) else None
        raise ToolError(f"Fellow rejected the shared recipe (HTTP {status}): {message or created}")

    saved = resolve_profile(incoming)
    return {
        "ok": True,
        "summary": f"Imported and saved {incoming!r} from {code}.",
        "confirmed": True,
        "profile": summarize_profile(saved),
    }


@tool(
    "List the brewer's scheduled brews - when each fires, on which days, which "
    "recipe and how much water, and whether it is enabled.",
    {},
)
def list_schedules():
    schedules = fetch_schedules()
    if not schedules:
        return {"ok": True, "summary": "No brews are scheduled.", "schedules": []}

    profiles = fetch_profiles()
    rows = []
    for schedule in schedules:
        rows.append({
            "time": describe_time(schedule.get("secondFromStartOfTheDay")),
            "days": describe_days(schedule.get("days")),
            "recipe": profile_title(schedule.get("profileId"), profiles),
            "water_ml": schedule.get("amountOfWater"),
            "enabled": schedule.get("enabled"),
        })
    active = sum(1 for r in rows if r["enabled"])
    return {
        "ok": True,
        "summary": f"{len(rows)} scheduled brews, {active} enabled.",
        "schedules": rows,
    }


@tool(
    "Schedule a recurring brew at a fixed time of day. Unlike brew_now, a "
    "schedule carries its own recipe and water volume, so this is the way to "
    "get a specific recipe brewed automatically. The brewer still has to be "
    "loaded with coffee and water beforehand - a schedule does not check that.",
    {
        "time": s("Time of day in 24-hour HH:MM, in the brewer's own timezone. 6:45am is '06:45'."),
        "days": s("'daily', 'weekdays', 'weekends', or a list like 'mon,wed,fri'."),
        "profile": s("Title of the saved recipe to brew. See list_profiles."),
        "water_ml": i(f"Water volume in millilitres, {MIN_WATER_ML} to {MAX_WATER_ML}.", default=950),
        "enabled": b("Whether it starts active.", default=True),
    },
    required=["time", "days", "profile"],
)
def schedule_brew(time, days, profile, water_ml=950, enabled=True):
    seconds = parse_time(time)
    flags = parse_days(days)
    water_ml = check_water(water_ml)
    profiles = fetch_profiles()
    target = resolve_profile(profile, profiles)

    existing = fetch_schedules()
    for schedule in existing:
        if (schedule.get("secondFromStartOfTheDay") == seconds
                and schedule.get("profileId") == target.get("id")):
            raise ToolError(
                f"A schedule already brews {target.get('title')!r} at {time} "
                f"({describe_days(schedule.get('days'))}). Cancel it first if you "
                f"want different days or water."
            )

    payload = {
        "days": flags,
        "secondFromStartOfTheDay": seconds,
        "enabled": bool(enabled),
        "amountOfWater": water_ml,
        "profileId": target["id"],
    }
    status, parsed = api("post", f"/devices/{brewer_id()}/schedules", body=payload)
    if status >= 400 or not isinstance(parsed, dict) or "id" not in parsed:
        message = parsed.get("message") if isinstance(parsed, dict) else None
        if message and "maximum number of schedules" in str(message).lower():
            current = [
                f"{describe_time(s.get('secondFromStartOfTheDay'))} "
                f"{describe_days(s.get('days'))} "
                f"({profile_title(s.get('profileId'), profiles)})"
                f"{'' if s.get('enabled') else ' [disabled]'}"
                for s in existing
            ]
            raise ToolError(
                f"The brewer holds a maximum of {MAX_SCHEDULES} schedules and "
                f"already has {len(existing)}. Nothing was added. Cancel one "
                f"first with cancel_schedule, or disable rather than replace if "
                f"it is only temporary. Current schedules: {current}",
                schedule_limit=MAX_SCHEDULES,
                current_schedules=current,
            )
        if message and "Profile could not be found" in str(message):
            raise ToolError(
                f"The brewer does not recognise recipe {target.get('title')!r}. "
                f"Saved recipes: {[p.get('title') for p in profiles]}."
            )
        raise ToolError(f"Fellow rejected the schedule (HTTP {status}): {message or parsed}")

    saved = next(
        (s for s in fetch_schedules() if s.get("id") == parsed["id"]), None
    )
    if saved is None:
        return {
            "ok": False,
            "error": (
                f"Fellow accepted the schedule but it does not appear on the brewer. "
                f"Check list_schedules before creating another - you may end up with two."
            ),
        }

    state = "enabled" if saved.get("enabled") else "created but disabled"
    return {
        "ok": True,
        "summary": (
            f"Scheduled {target.get('title')!r}, {water_ml}ml, at {time} "
            f"{describe_days(flags)} ({state}, confirmed)."
        ),
        "confirmed": True,
        "schedule": {
            "time": describe_time(saved.get("secondFromStartOfTheDay")),
            "days": describe_days(saved.get("days")),
            "recipe": target.get("title"),
            "water_ml": saved.get("amountOfWater"),
            "enabled": saved.get("enabled"),
        },
    }


@tool(
    "Turn a scheduled brew on or off without deleting it. Use this for a week "
    "away rather than cancelling and rebuilding the schedule.",
    {
        "time": s("Time of the schedule in 24-hour HH:MM."),
        "enabled": b("True to arm it, False to skip it.", default=True),
        "profile": s("Recipe title, only needed when two schedules share the same time."),
    },
    required=["time", "enabled"],
)
def set_schedule_enabled(time, enabled, profile=None):
    schedules = fetch_schedules()
    target = resolve_schedule(time, profile, schedules)

    if target.get("enabled") == bool(enabled):
        word = "already enabled" if enabled else "already disabled"
        return {
            "ok": True,
            "summary": f"The {time} brew was {word}. Nothing changed.",
            "confirmed": True,
        }

    status, _ = api(
        "patch", f"/devices/{brewer_id()}/schedules/{target['id']}",
        body={"enabled": bool(enabled)},
    )
    if status >= 400:
        raise ToolError(f"Fellow refused to change the {time} schedule (HTTP {status}).")

    after = next((s for s in fetch_schedules() if s.get("id") == target["id"]), None)
    if after is None or after.get("enabled") != bool(enabled):
        return {
            "ok": False,
            "error": (
                f"Fellow accepted the change but the {time} schedule still reads "
                f"{'enabled' if after and after.get('enabled') else 'disabled'}. "
                f"It did not take."
            ),
        }
    return {
        "ok": True,
        "summary": (
            f"The {time} {describe_days(after.get('days'))} brew "
            f"({profile_title(after.get('profileId'))}) is now "
            f"{'enabled' if enabled else 'disabled'} (confirmed)."
        ),
        "confirmed": True,
    }


@tool(
    "Delete a scheduled brew permanently. To pause one temporarily, use "
    "set_schedule_enabled instead - it can be turned back on.",
    {
        "time": s("Time of the schedule in 24-hour HH:MM."),
        "profile": s("Recipe title, only needed when two schedules share the same time."),
    },
    required=["time"],
)
def cancel_schedule(time, profile=None):
    schedules = fetch_schedules()
    target = resolve_schedule(time, profile, schedules)
    label = (
        f"{describe_time(target.get('secondFromStartOfTheDay'))} "
        f"{describe_days(target.get('days'))} "
        f"({profile_title(target.get('profileId'))})"
    )

    status, _ = api("delete", f"/devices/{brewer_id()}/schedules/{target['id']}")
    if status >= 400:
        raise ToolError(f"Fellow refused to delete the {time} schedule (HTTP {status}).")

    if any(s.get("id") == target["id"] for s in fetch_schedules()):
        return {
            "ok": False,
            "error": f"Fellow accepted the delete but the {label} schedule is still there.",
        }
    return {
        "ok": True,
        "summary": f"Cancelled the {label} brew (confirmed).",
        "confirmed": True,
    }


def banner():
    if not FELLOW_EMAIL or not FELLOW_PASSWORD:
        return "NOT CONFIGURED - set FELLOW_EMAIL and FELLOW_PASSWORD."
    where = FELLOW_EMAIL
    named = os.environ.get("FELLOW_BREWER_NAME", "").strip()
    return f"Account: {where}{', brewer: ' + named if named else ''}\nAPI: {BASE_URL}"


if __name__ == "__main__":
    run("aiden-mcp", "1.0", banner)
