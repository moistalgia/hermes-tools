#!/usr/bin/env python3
"""
mcpkit - the protocol half of an MCP server, so the servers in this repo are
nothing but domain logic.

`plex-mcp` proved the shape: a tool registry, a JSON-RPC-over-stdio loop, and a
CLI that runs the identical functions through the identical argument handling.
That last part is the important one. A human proves a call works from a shell,
the agent then makes the same call over MCP, and when something breaks there is
exactly one place it can be breaking.

This module is that machinery, extracted. It has no dependencies outside the
standard library and it knows nothing about any particular domain.

Writing a server against it:

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mcpkit import ToolError, b, i, n, run, s, tool

    @tool("Say hello to someone.", {"who": s("Name to greet.")}, required=["who"])
    def greet(who):
        if not who.strip():
            raise ToolError("Nobody named. Pass who= with a name.")
        return {"ok": True, "summary": f"Hello, {who}."}

    if __name__ == "__main__":
        run("greeter", "1.0", lambda: "nothing to configure")

That gives you `python server.py serve` for the agent and
`python server.py greet who=Nathan` for yourself.

Import path note: the `sys.path` line above resolves from `__file__`, not the
working directory, so it holds wherever the process is started from. That
matters because MCP clients launch servers with an unpredictable cwd.

Opt-in capture: set HERMES_CAPTURE=1 to append one JSON line per tool call to
~/.hermes/training/<server>.jsonl (override base dir with HERMES_CAPTURE_DIR).
Call capture_reasoning(text) before a tool call to attach reasoning to that
record; it is cleared automatically after each capture.
"""

import datetime
import inspect
import json
import os
import sys
import threading
import traceback

PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Opt-in capture (HERMES_CAPTURE=1)
# ---------------------------------------------------------------------------

_capture_local = threading.local()


def capture_reasoning(text):
    """Attach a reasoning string to the next captured tool call.

    The caller (orchestrator / agent layer) may call this before invoking a
    tool so that the reasoning trace rides along in the JSONL record.  mcpkit
    itself never calls this — it knows nothing about what "reasoning" means.
    The slot is cleared automatically after each capture so stale text never
    leaks into an unrelated call.
    """
    _capture_local.reasoning = text


def _capture(server, tool_name, args, result):
    """Append one JSON line to the per-server capture file.

    Called at the end of call_tool() when HERMES_CAPTURE is set.  Any failure
    here is logged to stderr and swallowed — a capture write error must never
    affect the tool result the caller receives.
    """
    # Consume reasoning unconditionally so stale text never leaks into a later
    # call even if capture is currently disabled.
    reasoning = getattr(_capture_local, "reasoning", None)
    _capture_local.reasoning = None
    if not os.environ.get("HERMES_CAPTURE", "").strip():
        return
    base = os.environ.get("HERMES_CAPTURE_DIR", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".hermes", "training")
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "server": server,
        "tool": tool_name,
        "args": args,
        "result": result,
        "ok": bool(result.get("ok", False)),
    }
    if reasoning is not None:
        record["reasoning"] = reasoning
    try:
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, f"{server}.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[{server}] capture write failed ({exc}); tool result unaffected",
              file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """An error whose message is meant to be shown to the agent verbatim.

    The message should name the cause and the next move. `404` teaches nothing
    and invites three wrong retries; "No room named 'back room'. Known rooms:
    office, kitchen. Add an alias if this is a new name for one of them." ends
    the guessing. Extra keyword arguments are merged into the error payload -
    use them for the short list of plausible alternatives, never the full set.
    """

    def __init__(self, message, **extra):
        super().__init__(message)
        self.extra = extra


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS = {}


def tool(description, schema=None, required=None):
    """Register a function as an MCP tool and a CLI subcommand."""

    def decorator(fn):
        TOOLS[fn.__name__] = {
            "fn": fn,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": schema or {},
                "required": required or [],
                "additionalProperties": False,
            },
        }
        return fn

    return decorator


def s(desc, default=None, enum=None):
    d = {"type": "string", "description": desc}
    if default is not None:
        d["default"] = default
    if enum:
        d["enum"] = list(enum)
    return d


def i(desc, default=None, minimum=None, maximum=None):
    d = {"type": "integer", "description": desc}
    if default is not None:
        d["default"] = default
    if minimum is not None:
        d["minimum"] = minimum
    if maximum is not None:
        d["maximum"] = maximum
    return d


def n(desc, default=None, minimum=None, maximum=None):
    """A number that may have a fraction. Use where halves are real.

    A thermostat in Celsius moves in half degrees, so `i()` would quietly make
    20.5 unreachable - the kind of limit nobody notices until they cannot set
    the temperature they actually want.
    """
    d = {"type": "number", "description": desc}
    if default is not None:
        d["default"] = default
    if minimum is not None:
        d["minimum"] = minimum
    if maximum is not None:
        d["maximum"] = maximum
    return d


def b(desc, default=False):
    return {"type": "boolean", "description": desc, "default": default}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def coerce(entry, args):
    """Cast arguments to the type the schema declares.

    Two callers send arguments and neither is careful. The CLI turns bare
    digits into ints, so `qty=2` arrives as an int for a field declared string.
    Agents send `"40"` for a field declared integer. Both then blow up deep
    inside domain code with a TypeError that names nothing useful. Fixing it
    here means no tool has to defend itself.
    """
    props = entry["inputSchema"]["properties"]
    out = {}
    for key, value in args.items():
        declared = (props.get(key) or {}).get("type")
        if value is None or declared is None:
            out[key] = value
        elif declared == "string" and not isinstance(value, str):
            out[key] = str(value)
        elif declared == "integer" and not isinstance(value, int):
            try:
                out[key] = int(str(value).strip())
            except (TypeError, ValueError):
                raise ToolError(f"{key} must be a whole number, got {value!r}.")
        elif declared == "number" and not isinstance(value, (int, float)):
            try:
                out[key] = float(str(value).strip())
            except (TypeError, ValueError):
                raise ToolError(f"{key} must be a number, got {value!r}.")
        elif declared == "boolean" and not isinstance(value, bool):
            out[key] = str(value).strip().lower() in ("1", "true", "yes", "on")
        else:
            out[key] = value
    return out


def call_tool(name, args):
    """Run a tool. Never raises: failures come back as ok:false with real text."""
    entry = TOOLS.get(name)
    if entry is None:
        result = {"ok": False, "error": f"Unknown tool {name!r}", "available_tools": sorted(TOOLS)}
        _capture(_server_name, name, args or {}, result)
        return result
    try:
        signature = inspect.signature(entry["fn"])
        accepted = {k: v for k, v in coerce(entry, args or {}).items() if k in signature.parameters}
        rejected = sorted(set(args or {}) - set(accepted))
        result = entry["fn"](**accepted)
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        result.setdefault("ok", True)
        if rejected:
            # Loud, because a silently dropped argument looks like the tool
            # ignoring an instruction rather than a caller typo.
            result["ignored_arguments"] = rejected
    except ToolError as exc:
        result = {"ok": False, "error": str(exc)}
        result.update(exc.extra)
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "Report this error verbatim. Do not retry with altered arguments "
                "until the cause is understood."
            ),
        }
    _capture(_server_name, name, args or {}, result)
    return result


# ---------------------------------------------------------------------------
# stdio server (JSON-RPC 2.0, newline-delimited)
# ---------------------------------------------------------------------------

_stdout = sys.stdout
_server_name = "mcpkit"


def log(msg):
    print(f"[{_server_name}] {msg}", file=sys.stderr, flush=True)


def emit(payload):
    _stdout.write(json.dumps(payload, default=str) + "\n")
    _stdout.flush()


def _force_utf8(stream, errors):
    """Some hosts replace stdio with something that isn't a TextIOWrapper
    (test runners, frozen builds) - an encoding mismatch there is not this
    function's problem, so failing to reconfigure is not fatal."""
    try:
        stream.reconfigure(encoding="utf-8", errors=errors)
    except (AttributeError, ValueError):
        pass


def serve(name, version):
    # Windows opens a subprocess's stdio pipes in the OS's ANSI codepage
    # (cp1252 here, verified - not UTF-8) unless told otherwise, even though
    # every byte on the wire is UTF-8 per the JSON-RPC/MCP spec. A client
    # that does not ASCII-escape its JSON (most non-Python ones do not) sends
    # raw multi-byte UTF-8 for anything outside ASCII - a degree sign, an em
    # dash, an emoji - and an unconfigured cp1252 stdin decodes each of those
    # bytes on its own, then a later UTF-8 re-encode doubles the damage: "72
    # \xc2\xb0F" (a correct degree sign) becomes the four characters "Â°".
    # Forcing UTF-8 here is the fix; it must not depend on PYTHONUTF8 or
    # PYTHONIOENCODING being set in the host's environment, because nothing
    # in this repo's deployment sets them.
    _force_utf8(sys.stdin, errors="strict")

    # stdout is the JSON-RPC transport. One stray print() anywhere - here, in a
    # dependency, in a warning - corrupts the stream and the handshake fails
    # with no useful error. Hold the real handle for emit() and point sys.stdout
    # at stderr so accidental writes are merely logged instead of fatal.
    global _stdout
    _stdout = sys.stdout
    _force_utf8(_stdout, errors="replace")
    _force_utf8(sys.stderr, errors="replace")
    sys.stdout = sys.stderr

    log(f"serving {len(TOOLS)} tools over stdio")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad JSON on stdin: {exc}")
            continue

        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}
        response = None

        try:
            if method == "initialize":
                response = {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": name, "version": version},
                }
            elif method == "tools/list":
                response = {
                    "tools": [
                        {
                            "name": tool_name,
                            "description": entry["description"],
                            "inputSchema": entry["inputSchema"],
                        }
                        for tool_name, entry in TOOLS.items()
                    ]
                }
            elif method == "tools/call":
                result = call_tool(params.get("name"), params.get("arguments") or {})
                response = {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2, default=str)}
                    ],
                    "isError": not result.get("ok", False),
                }
            elif method == "ping":
                response = {}
            elif method and method.startswith("notifications/"):
                continue  # notifications take no reply
            else:
                if req_id is not None:
                    emit({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    })
                continue
        except Exception:
            log(traceback.format_exc())
            if req_id is not None:
                emit({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": traceback.format_exc(limit=2)},
                })
            continue

        if req_id is not None:
            emit({"jsonrpc": "2.0", "id": req_id, "result": response})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_cli_args(argv):
    """key=value pairs, with light coercion so quoting stays simple."""
    args = {}
    for token in argv:
        if "=" not in token:
            raise SystemExit(f"Arguments must be key=value, got {token!r}")
        key, _, value = token.partition("=")
        if value.lower() in ("true", "false"):
            args[key] = value.lower() == "true"
        elif value.lstrip("-").isdigit():
            args[key] = int(value)
        else:
            args[key] = value
    return args


def usage(name, banner):
    # stderr, not stdout. An MCP client that launches this without arguments -
    # a typo in the config, a wrapper that drops them - would otherwise get
    # usage text on the protocol channel and fail the handshake with no useful
    # error. Nothing but JSON-RPC frames may ever reach stdout.
    out = sys.stderr
    print(f"{name} - MCP server / CLI\n", file=out)
    print(f"  python {os.path.basename(sys.argv[0])} serve            # run as an MCP server", file=out)
    print(f"  python {os.path.basename(sys.argv[0])} <tool> k=v ...   # run one tool directly\n", file=out)
    print("Tools:", file=out)
    for tool_name, entry in TOOLS.items():
        params = ", ".join(entry["inputSchema"]["properties"]) or "-"
        print(f"  {tool_name:<22} {params}", file=out)
        print(f"  {'':<22} {entry['description']}", file=out)
    if banner:
        print(f"\n{banner() if callable(banner) else banner}", file=out)


def run(name, version, banner=None):
    """Entry point. `serve` speaks MCP; anything else runs one tool and exits."""
    global _server_name
    _server_name = name

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        usage(name, banner)
        return
    if argv[0] == "serve":
        serve(name, version)
        return
    result = call_tool(argv[0], parse_cli_args(argv[1:]))
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("ok") else 1)
