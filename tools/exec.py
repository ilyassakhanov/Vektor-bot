"""Hardened exec tool — runs an allowlisted, network read-only command.

The command string is never handed to a shell. It is checked for shell
metacharacters, split with :mod:`shlex`, and the first word must be a bare
name from an allowlist (default ``curl, dig, nslookup, whois, host``;
``EXEC_ALLOWED_COMMANDS`` overrides). The name is resolved on a fixed safe
``PATH`` and the resolved absolute path is executed. Per-binary argument
allowlists keep the tools read-only: curl accepts a short list of options and
exactly one https URL to a public host (GET/HEAD, no ``@``, no globbing);
DNS/whois tools accept only a query name and an optional record type.

The child runs isolated from the bot: a scrubbed environment (``PATH``,
``LANG``, ``HOME`` only), a fresh temporary working directory, best-effort
rlimits, its own session and stdin from ``/dev/null``. Output is read with a
byte cap; on overflow or timeout the whole process group is SIGKILLed.

These are code-level controls: they hold even if the model fully complies
with a hostile request. The system prompt is defense in depth only.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import metrics
from tools.base import Tool, ToolError
from tools.truncation import max_output_chars_from_env, truncate

log = logging.getLogger("vektor.tools.exec")

_DEFAULT_TIMEOUT = 30.0

ALLOWED_COMMANDS: tuple[str, ...] = ("curl", "dig", "nslookup", "whois", "host")
_ENV_ALLOWED_COMMANDS = "EXEC_ALLOWED_COMMANDS"
_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Checked on the raw string, before parsing: without a shell most of these
# would be inert, but rejecting them outright is simpler to audit. NUL would
# otherwise make Popen raise; CR is a newline variant.
_METACHARS = frozenset("|;&$`><()\n\r\x00")

_REJECT_PREFIX = "Command rejected:"
_METACHAR_REJECT = f"{_REJECT_PREFIX} shell operators are not allowed."
_PARSE_REJECT = f"{_REJECT_PREFIX} could not parse command."

# Best-effort per-child resource limits. ``None`` = derived from the timeout
# (CPU seconds). Each limit is applied independently and skipped if the
# platform refuses it (macOS commonly rejects RLIMIT_AS). RLIMIT_NPROC is
# deliberately absent: it counts every process/thread of the uid (Linux
# threads included), so a low cap breaks curl's and dig's resolver threads;
# the process-group kill covers runaway forks.
_MAX_FILE_BYTES = 10 * 1024 * 1024
_RLIMITS: tuple[tuple[str, int | None], ...] = (
    ("RLIMIT_CPU", None),
    ("RLIMIT_AS", 512 * 1024 * 1024),
    ("RLIMIT_FSIZE", _MAX_FILE_BYTES),
    ("RLIMIT_CORE", 0),
)

# Output is read with a byte cap so a flooding command cannot exhaust the
# bot's memory: max(64 KiB, 4 x max_output_chars) bytes, stdout + stderr.
_MIN_OUTPUT_BYTES = 64 * 1024
_OUTPUT_BYTES_PER_CHAR = 4
_READ_CHUNK = 65536
# Bounded wait for readers/the child after a kill.
_POST_KILL_GRACE_S = 1.0


def allowed_commands_from_env() -> tuple[str, ...]:
    """Resolve the allowlist from ``EXEC_ALLOWED_COMMANDS`` (warn and fall back).

    Comma-separated, entries trimmed, empty entries dropped. Unset -> default;
    set but empty after cleaning -> default plus a warning.
    """
    raw = os.environ.get(_ENV_ALLOWED_COMMANDS)
    if raw is None:
        return ALLOWED_COMMANDS
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    if not names:
        log.warning(
            "%s is set but empty; using the default allowlist", _ENV_ALLOWED_COMMANDS
        )
        return ALLOWED_COMMANDS
    return names


def build_exec_env(home: str, path: str = _SAFE_PATH) -> dict[str, str]:
    """Return the complete environment for an exec child.

    An allowlist, not a denylist: exactly ``PATH``, ``LANG`` and ``HOME``.
    Nothing else from the bot's environment (``TELEGRAM_BOT_TOKEN`` in
    particular) is copied.
    """
    return {
        "PATH": path,
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "HOME": home,
    }


def _make_preexec(timeout: float, resource_module: Any = None) -> Callable[[], None]:
    """Build the ``preexec_fn`` that applies :data:`_RLIMITS` in the child.

    Runs between fork and exec, so it does nothing but ``setrlimit``: no
    logging, no locks. A limit the platform refuses is silently skipped.
    """
    if resource_module is None:
        import resource as resource_module  # POSIX only

    res = resource_module
    limits: list[tuple[int, int]] = []
    for name, value in _RLIMITS:
        which = getattr(res, name, None)
        if which is None:
            continue
        limit = max(1, math.ceil(timeout)) if value is None else value
        limits.append((which, limit))

    def _preexec() -> None:
        for which, limit in limits:
            try:
                res.setrlimit(which, (limit, limit))
            except (ValueError, OSError):
                pass

    return _preexec


# --- curl rules --------------------------------------------------------------
#
# An OPTION ALLOWLIST: anything not listed here is rejected. Long options
# never take the ``--opt=value`` form (curl itself rejects it). Short options
# may be clustered (``-sSI``) only if every character is allowlisted, and a
# value-taking short option must be the last character of its cluster; its
# value is always the next argument.

# No -L/--location: a redirect could hop from a public host to a private
# one, bypassing the literal-host check on the URL.
_CURL_FLAGS_SHORT = frozenset("sSIifv")
_CURL_VALUE_SHORT = frozenset("HAXmw")
_CURL_FLAGS_LONG = frozenset(
    {
        "--silent",
        "--show-error",
        "--head",
        "--include",
        "--fail",
        "--verbose",
        "--compressed",
    }
)
# Long value option -> its short-form spelling for value validation.
_CURL_VALUE_LONG: dict[str, str] = {
    "--header": "-H",
    "--user-agent": "-A",
    "--request": "-X",
    "--max-time": "-m",
    "--connect-timeout": "-m",
    "--retry": "-m",
    "--write-out": "-w",
    "--url": "--url",
}
_CURL_METHODS = frozenset({"GET", "HEAD"})
_CURL_MAX_FILESIZE = str(_MAX_FILE_BYTES)
_CURL_MAX_URLS = 1
_GLOB_CHARS = frozenset("[]{}")


# IPv6 ranges that carry an IPv4 address in their low 32 bits.
_EMBEDDED_V4_NETS = (
    ipaddress.IPv6Network("::/96"),  # IPv4-compatible (deprecated)
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64 well-known prefix
    ipaddress.IPv6Network("::ffff:0:0:0/96"),  # SIIT IPv4-translated
)


def _bad_host(host: str) -> bool:
    """True for hosts that must never be fetched: local names and non-global IPs.

    Literal addresses only (including curl's legacy numeric forms such as
    ``127.1`` or ``2130706433``). A public name that DNS-resolves to a
    private address is a residual risk, documented in design.md.
    """
    host = host.rstrip(".").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is None and all(c in "0123456789abcdefx." for c in host):
        try:
            addr = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            addr = None
    if addr is None:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif any(addr in net for net in _EMBEDDED_V4_NETS):
            return True  # IPv4-compatible / NAT64: can embed a private IPv4
    return not addr.is_global or addr.is_multicast


def _check_curl_url(url: str) -> str | None:
    if "@" in url:
        return f"{_REJECT_PREFIX} curl URLs cannot contain '@'."
    if not url.lower().startswith("https://"):
        return f"{_REJECT_PREFIX} curl only accepts https:// URLs."
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return f"{_REJECT_PREFIX} curl URL could not be parsed."
    if "%" in parts.netloc:
        # curl percent-decodes the host, which would bypass the IP checks.
        return f"{_REJECT_PREFIX} curl URL hosts cannot be percent-encoded."
    if _bad_host(host):
        return f"{_REJECT_PREFIX} curl cannot reach local or private hosts."
    rest = parts.path + parts.query + parts.fragment
    if any(c in _GLOB_CHARS for c in rest):
        return f"{_REJECT_PREFIX} curl URL globbing is not allowed."
    return None


def _check_curl_value(flag: str, value: str) -> str | None:
    if flag == "--url":
        return _check_curl_url(value)
    if "@" in value:
        return f"{_REJECT_PREFIX} curl option values cannot contain '@'."
    if flag == "-X" and value.upper() not in _CURL_METHODS:
        return f"{_REJECT_PREFIX} curl only allows GET and HEAD requests."
    if flag == "-m":
        try:
            float(value)
        except ValueError:
            return f"{_REJECT_PREFIX} curl timeout and retry values must be numbers."
    if flag == "-w" and "%output" in value.lower():
        return f"{_REJECT_PREFIX} curl cannot write local files."
    return None


def _check_curl(args: Sequence[str]) -> str | None:
    urls = 0
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        value_flag: str | None = None
        if arg.startswith("--"):
            if arg in _CURL_FLAGS_LONG:
                continue
            if arg not in _CURL_VALUE_LONG:
                return f"{_REJECT_PREFIX} curl option '{arg[:40]}' is not allowed."
            value_flag = _CURL_VALUE_LONG[arg]
        elif arg.startswith("-") and len(arg) > 1:
            cluster = arg[1:]
            for pos, ch in enumerate(cluster):
                if ch in _CURL_FLAGS_SHORT:
                    continue
                if ch in _CURL_VALUE_SHORT and pos == len(cluster) - 1:
                    value_flag = f"-{ch}"
                    break
                return f"{_REJECT_PREFIX} curl option '-{ch}' is not allowed here."
            if value_flag is None:
                continue
        else:
            err = _check_curl_url(arg)
            if err:
                return err
            urls += 1
            if urls > _CURL_MAX_URLS:
                return f"{_REJECT_PREFIX} curl accepts exactly one URL."
            continue
        if i >= len(args):
            return f"{_REJECT_PREFIX} curl option '{arg[:40]}' needs a value."
        value = args[i]
        i += 1
        err = _check_curl_value(value_flag, value)
        if err:
            return err
        if value_flag == "--url":
            urls += 1
            if urls > _CURL_MAX_URLS:
                return f"{_REJECT_PREFIX} curl accepts exactly one URL."
    return None


# --- DNS / whois rules ---------------------------------------------------------
#
# Only ``<tool> [record-type] <name>``: no options that pick a server, port or
# transport, and no second positional (which host/nslookup treat as a server).

_DNS_TYPES = frozenset({"A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "PTR", "ANY"})
_QUERY_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,252}")


def _dns_reject(name: str) -> str:
    return (
        f"{_REJECT_PREFIX} {name} accepts only a domain or IP and an optional "
        f"record type ({', '.join(sorted(_DNS_TYPES))})."
    )


def _check_dns(name: str, args: Sequence[str]) -> str | None:
    names: list[str] = []
    types: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if name == "dig" and arg == "+short":
            continue
        if name == "host" and arg == "-t" and i < len(args):
            types.append(args[i])
            i += 1
            continue
        if name == "nslookup" and arg.startswith(("-type=", "-query=")):
            types.append(arg.split("=", 1)[1])
            continue
        if name == "dig" and arg.upper() in _DNS_TYPES:
            types.append(arg)
            continue
        if not _QUERY_NAME.fullmatch(arg):
            return _dns_reject(name)
        names.append(arg)
    if len(names) != 1 or len(types) > 1:
        return _dns_reject(name)
    if name == "whois" and types:
        return _dns_reject(name)
    if any(t.upper() not in _DNS_TYPES for t in types):
        return _dns_reject(name)
    return None


_DNS_COMMANDS = frozenset({"dig", "nslookup", "whois", "host"})


def _check_args(name: str, argv: Sequence[str]) -> str | None:
    """Return a rejection message if ``argv`` breaks ``name``'s rules, else None.

    Commands without rules (an operator-added allowlist entry) pass.
    """
    args = argv[1:]
    if name == "curl":
        return _check_curl(args)
    if name in _DNS_COMMANDS:
        return _check_dns(name, args)
    return None


def _build_argv(resolved: str, argv: Sequence[str], timeout: float) -> list[str]:
    """Return the argv to execute: resolved binary + args (+ forced curl flags).

    The forced curl flags come last so they win over any user-supplied value.
    """
    out = [resolved, *argv[1:]]
    if os.path.basename(argv[0]) == "curl":
        out += [
            "--globoff",
            "--max-time",
            str(max(1, math.ceil(timeout))),
            "--max-filesize",
            _CURL_MAX_FILESIZE,
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
        ]
    return out


class ExecTool(Tool):
    """Execute an allowlisted command without a shell; return stdout/stderr/exit code.

    Failures (rejections, non-zero exit, timeout, missing binary) are
    returned as strings to the LLM — the tool never raises
    :class:`ToolError` for command failures. Only a missing ``command``
    argument raises :class:`ToolError`.

    Combined stdout/stderr output is capped at ``max_output_chars`` (env
    ``EXEC_MAX_OUTPUT_CHARS``, default 4000) with head+tail truncation; the
    ``exit_code:`` line is always preserved.
    """

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        max_output_chars: int | None = None,
        allowed_commands: Sequence[str] | None = None,
        search_path: str = _SAFE_PATH,
    ) -> None:
        self._timeout = timeout
        self._max_output_chars = max_output_chars_from_env(max_output_chars)
        self._allowed: tuple[str, ...] = (
            tuple(allowed_commands)
            if allowed_commands is not None
            else allowed_commands_from_env()
        )
        self._search_path = search_path

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        allowed = ", ".join(self._allowed)
        return (
            f"Run one network read-only command: {allowed}. No shell operators "
            "(pipes, redirects, ;, &, $, backticks); curl is https-only GET/HEAD. "
            "Returns stdout, stderr, exit code."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The command line to run, e.g. 'curl -sI https://example.com'. "
                        "Allowed commands: " + ", ".join(self._allowed) + "."
                    ),
                },
            },
            "required": ["command"],
        }

    def execute(self, **kwargs: Any) -> str:
        command = kwargs.get("command")
        if command is None:
            raise ToolError("Missing required argument: command")
        command = str(command)
        if not command.strip():
            return self._format("", "", 0)

        if any(ch in _METACHARS for ch in command):
            return self._reject(_first_word(command), "metachar", _METACHAR_REJECT)
        try:
            argv = shlex.split(command)
        except ValueError:
            return self._reject(_first_word(command), "parse", _PARSE_REJECT)
        if not argv:
            return self._format("", "", 0)

        name = argv[0]
        if "/" in name or name not in self._allowed:
            msg = (
                f"{_REJECT_PREFIX} '{name}' is not an allowed command. "
                f"Allowed: {', '.join(self._allowed)}"
            )
            return self._reject(name, "not_allowed", msg)
        err = _check_args(name, argv)
        if err is not None:
            return self._reject(name, "bad_arg", err)

        resolved = shutil.which(name, path=self._search_path)
        if resolved is None:
            log.warning("exec argv0=%s outcome=error exit=-1", name)
            return self._format("", f"Error: command '{name}' not found.", -1)
        return self._run(name, _build_argv(resolved, argv, self._timeout))

    def _run(self, name: str, argv: list[str]) -> str:
        cap = max(_MIN_OUTPUT_BYTES, _OUTPUT_BYTES_PER_CHAR * self._max_output_chars)
        with tempfile.TemporaryDirectory(prefix="vektor-exec-") as workdir:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workdir,
                    env=build_exec_env(workdir, self._search_path),
                    start_new_session=True,
                    # setrlimit only, no locks or logging: see design.md
                    # ("preexec_fn in a threaded process").
                    preexec_fn=(  # noqa: PLW1509
                        _make_preexec(self._timeout) if os.name == "posix" else None
                    ),
                )
            except OSError as exc:
                log.warning("exec argv0=%s outcome=error exit=-1", name)
                return self._format("", f"Error: {exc}", -1)
            out = _collect(proc, self._timeout, cap)
        stdout = out.stdout.decode("utf-8", errors="replace")
        stderr = out.stderr.decode("utf-8", errors="replace")
        if out.exceeded:
            log.warning("exec argv0=%s outcome=output_cap exit=-1", name)
            msg = f"[output exceeded {cap} bytes; command killed]"
            return self._format(stdout, f"{stderr}\n{msg}", -1)
        if out.timed_out:
            log.warning("exec argv0=%s outcome=timeout exit=-1", name)
            msg = f"Command timed out after {self._timeout}s."
            return self._format(stdout, f"{msg}\n{stderr}", -1)
        log.info("exec argv0=%s outcome=allowed exit=%d", name, out.returncode)
        return self._format(stdout, stderr, out.returncode)

    def _reject(self, argv0: str, reason: str, message: str) -> str:
        metrics.exec_rejected_total.labels(reason=reason).inc()
        shown = argv0 if argv0 in self._allowed else "<other>"
        log.warning("exec argv0=%s outcome=rejected:%s exit=-1", shown, reason)
        return f"{message}\nexit_code: -1"

    def _format(self, stdout: str, stderr: str, exit_code: int) -> str:
        body = truncate(f"stdout:\n{stdout}\nstderr:\n{stderr}", self._max_output_chars)
        return f"{body}\nexit_code: {exit_code}"


def _first_word(command: str) -> str:
    """Best-effort command name for logs — never the arguments.

    Stops at the first whitespace or metacharacter and is capped, so a
    rejected command's secret-bearing arguments never reach the log.
    """
    word = []
    for ch in command.lstrip():
        if ch.isspace() or ch in _METACHARS or ch in "'\"":
            break
        word.append(ch)
    return "".join(word)[:32] or "?"


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group; fall back to the child alone."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:  # PermissionError included
        try:
            proc.kill()
        except OSError:
            pass


@dataclass
class _Output:
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool
    exceeded: bool


def _collect(proc: subprocess.Popen[bytes], timeout: float, cap: int) -> _Output:
    """Read stdout/stderr with a combined byte cap and a wall-clock timeout.

    Reader threads keep at most ``cap`` bytes; once more arrives the process
    group is killed (they keep draining and discarding until EOF so the child
    never blocks on a full pipe). On timeout the group is killed too. Joins
    after a kill are bounded, so a process that escaped the group cannot hang
    the bot.
    """
    assert proc.stdout is not None and proc.stderr is not None
    bufs = (bytearray(), bytearray())
    lock = threading.Lock()
    state = {"total": 0, "exceeded": False}

    def reader(stream: Any, buf: bytearray) -> None:
        try:
            while True:
                chunk = stream.read1(_READ_CHUNK)
                if not chunk:
                    return
                with lock:
                    room = cap - state["total"]
                    if room > 0:
                        buf += chunk[:room]
                    state["total"] += len(chunk)
                    trip = state["total"] > cap and not state["exceeded"]
                    if trip:
                        state["exceeded"] = True
                if trip:
                    _kill_group(proc)
        except (OSError, ValueError):
            return

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, bufs[0]), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, bufs[1]), daemon=True),
    ]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            timed_out = True
            break
    if timed_out:
        _kill_group(proc)
    # After a kill, readers normally hit EOF at once. A child that escaped
    # the group (its own setsid) can keep the pipes open, so the extra wait
    # is short and shared.
    grace = time.monotonic() + _POST_KILL_GRACE_S
    for t in threads:
        t.join(max(0.0, grace - time.monotonic()))
    try:
        returncode = proc.wait(
            timeout=max(_POST_KILL_GRACE_S, deadline - time.monotonic())
        )
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        timed_out = True
        returncode = -1
    with lock:
        exceeded = bool(state["exceeded"])
        stdout, stderr = bytes(bufs[0]), bytes(bufs[1])
    for stream, t in zip((proc.stdout, proc.stderr), threads, strict=True):
        # Closing a BufferedReader blocks on its lock while a reader thread
        # sits in read1(); a still-running daemon reader keeps its stream and
        # exits when the escaped process closes the pipe.
        if t.is_alive():
            continue
        try:
            stream.close()
        except OSError:
            pass
    return _Output(stdout, stderr, returncode, timed_out and not exceeded, exceeded)
