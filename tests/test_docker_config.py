"""Static hardening invariants for Dockerfile, compose.yaml and .dockerignore.

Plain text/regex parsing only: no pyyaml, no docker daemon. Runs offline in
the default pytest suite. ``docker compose config`` in CI adds the
structural check.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PROCESS_CONTROL_BINARIES = ("ps", "kill", "pkill", "pgrep", "killall", "top")
EXEC_BINARIES = ("curl", "dig", "nslookup", "whois", "host")
SAFE_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


# --- helpers ------------------------------------------------------------------


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _dockerfile_instructions() -> list[str]:
    """Return Dockerfile instructions with line continuations joined, comments dropped."""
    instructions: list[str] = []
    current = ""
    for raw in _read("Dockerfile").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1].strip() + " "
            continue
        instructions.append(current + line)
        current = ""
    if current:
        instructions.append(current.strip())
    return instructions


def _runtime_instructions() -> list[str]:
    """Instructions of the final (runtime) stage: everything after the last FROM."""
    instructions = _dockerfile_instructions()
    froms = [
        i for i, instr in enumerate(instructions) if instr.upper().startswith("FROM ")
    ]
    assert froms, "no FROM instruction"
    return instructions[froms[-1] + 1 :]


def _instructions(keyword: str, *, all_stages: bool = False) -> list[str]:
    """Instructions with this keyword; runtime stage only unless ``all_stages``."""
    prefix = keyword.upper() + " "
    source = _dockerfile_instructions() if all_stages else _runtime_instructions()
    return [i for i in source if i.upper().startswith(prefix)]


def _run_containing(needle: str) -> str:
    matches = [r for r in _instructions("RUN") if needle in r]
    assert matches, f"no RUN instruction contains {needle!r}"
    return matches[0]


def _env_vars() -> dict[str, str]:
    env: dict[str, str] = {}
    for instr in _instructions("ENV"):
        for key, value in re.findall(r"(\w+)=(\S+)", instr[len("ENV ") :]):
            env[key] = value
    return env


def _copy_sources() -> list[str]:
    """Sources of every COPY that reads the build context (not --from=)."""
    sources: list[str] = []
    for instr in _instructions("COPY", all_stages=True):
        tokens = instr.split()[1:]
        if any(t.startswith("--from=") for t in tokens):
            continue
        args = [t for t in tokens if not t.startswith("--")]
        sources.extend(args[:-1])
    return sources


def _yaml_block(file: str, name: str) -> str:
    """Extract one two-space-indented block (a compose service, a CI job)."""
    lines = _read(file).splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.rstrip() == f"  {name}:"), None
    )
    assert start is not None, f"{name}: not found in {file}"
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) <= 2:
            break
        block.append(line)
    return "\n".join(block)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _list_items(block: str, key: str) -> list[str]:
    """Items of a ``key:`` YAML list (``- item`` lines) inside a service block."""
    lines = block.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"{key}:"), None)
    assert start is not None, f"{key}: not found"
    items: list[str] = []
    for line in lines[start + 1 :]:
        if _indent(line) <= _indent(lines[start]):
            break
        if line.lstrip().startswith("- "):
            items.append(line.strip()[2:].strip().strip("\"'"))
    return items


def _dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _is_ignored(path: str) -> bool:
    """Approximate docker's matcher: last matching pattern wins, ``!`` re-includes."""
    path = path.rstrip("/")
    ignored = False
    for pattern in _dockerignore_patterns():
        negate = pattern.startswith("!")
        pat = pattern.lstrip("!").rstrip("/")
        candidates = {pat, pat.removeprefix("**/")}
        if any(
            fnmatch.fnmatch(path, c) or path.startswith(c + "/") for c in candidates
        ):
            ignored = not negate
    return ignored


# --- A1: Dockerfile -----------------------------------------------------------


def test_dockerfile_syntax_header_is_first_line() -> None:
    assert _read("Dockerfile").splitlines()[0] == "# syntax=docker/dockerfile:1"


def test_dockerfile_last_user_is_10001() -> None:
    users = _instructions("USER")
    assert users, "no USER instruction"
    assert users[-1] == "USER 10001:10001"


def test_dockerfile_useradd_uid_10001_with_nologin_shell() -> None:
    run = _run_containing("useradd")
    assert "--uid 10001" in run
    assert "--gid 10001" in run
    assert re.search(r"--shell \S*/nologin\b", run)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PYTHONDONTWRITEBYTECODE", "1"),
        ("HOME", "/tmp"),
        ("KB_DB_PATH", "/data/vektor.db"),
    ],
)
def test_dockerfile_env(key: str, value: str) -> None:
    assert _env_vars().get(key) == value


@pytest.mark.parametrize("binary", PROCESS_CONTROL_BINARIES)
def test_dockerfile_removes_process_control_binary(binary: str) -> None:
    run = _run_containing("rm -f")
    rm_loop = re.search(r"for b in ([^;]+);\s*do\s*for d in [^;]+;\s*do rm -f", run)
    assert rm_loop, "rm loop not found"
    assert binary in rm_loop.group(1).split()


def test_dockerfile_absence_check_does_not_use_command_v() -> None:
    # Regression (F1): `kill` is a dash builtin, so `command -v kill` always
    # succeeds and the build would always fail.
    assert not any("command -v" in run for run in _instructions("RUN"))


def test_dockerfile_absence_check_tests_files_on_safe_path() -> None:
    run = _run_containing('test ! -e "$d/$b"')
    # The failure branch is required: `|| true` or no `|| { ...; exit 1; }`
    # would let a present binary slip through.
    check = re.search(
        r"for b in ([^;]+);\s*do\s*for d in ([^;]+);\s*do\s*"
        r'test ! -e "\$d/\$b"\s*\|\|\s*\{[^}]*\bexit 1;\s*\}',
        run,
    )
    assert check, "file-based absence check not found"
    assert set(PROCESS_CONTROL_BINARIES) <= set(check.group(1).split())
    assert set(SAFE_PATH_DIRS) <= set(check.group(2).split())


def test_dockerfile_presence_check_covers_exec_binaries() -> None:
    run = _run_containing('test -x "/usr/bin/$b"')
    check = re.search(
        r'for b in ([^;]+);\s*do\s*test -x "/usr/bin/\$b"\s*\|\|\s*\{[^}]*\bexit 1;\s*\}',
        run,
    )
    assert check, "presence check not found"
    assert set(EXEC_BINARIES) <= set(check.group(1).split())


def test_dockerfile_never_copies_whole_context() -> None:
    assert not any(src in (".", "./") for src in _copy_sources())


@pytest.mark.parametrize("forbidden", [".env", "data", "tests"])
def test_dockerfile_never_copies_secrets_or_data(forbidden: str) -> None:
    for src in _copy_sources():
        assert src.rstrip("/").removeprefix("./") != forbidden
        assert not src.removeprefix("./").startswith(forbidden + "/")


def test_dockerfile_has_healthcheck() -> None:
    assert _instructions("HEALTHCHECK")


# --- A2: compose.yaml ---------------------------------------------------------


@pytest.fixture(scope="module")
def vektor() -> str:
    return _yaml_block("compose.yaml", "vektor")


@pytest.mark.parametrize(
    "pattern",
    [
        r"^\s*read_only:\s*true\s*$",
        r"^\s*init:\s*true\s*$",
        r"^\s*restart:\s*unless-stopped\s*$",
        r'^\s*user:\s*"10001:10001"\s*$',
        r"^\s*pids_limit:\s*\d+\s*$",
        r"^\s*mem_limit:\s*\S+\s*$",
        r"^\s*cpus:\s*[\d.]+\s*$",
        r"^\s*image:\s*\$\{VEKTOR_IMAGE:-vektor:local\}\s*$",
        r"^\s*-?\s*path:\s*\$\{VEKTOR_ENV_FILE:-\.env\}\s*$",
    ],
)
def test_compose_vektor_has(vektor: str, pattern: str) -> None:
    assert re.search(pattern, vektor, re.MULTILINE), pattern


def test_compose_cap_drop_all(vektor: str) -> None:
    assert _list_items(vektor, "cap_drop") == ["ALL"]


def test_compose_no_new_privileges(vektor: str) -> None:
    assert "no-new-privileges:true" in _list_items(vektor, "security_opt")


def test_compose_tmp_tmpfs_is_size_capped(vektor: str) -> None:
    tmp = [t for t in _list_items(vektor, "tmpfs") if t.split(":", 1)[0] == "/tmp"]
    assert tmp, "no /tmp tmpfs"
    assert re.search(r"size=\d+[kmg]?", tmp[0])


def test_compose_ports_bind_loopback_only(vektor: str) -> None:
    ports = _list_items(vektor, "ports")
    assert ports
    assert all(p.startswith("127.0.0.1:") for p in ports), ports


def test_compose_data_is_named_volume(vektor: str) -> None:
    data = [v for v in _list_items(vektor, "volumes") if v.split(":")[1:2] == ["/data"]]
    assert data, "no /data mount"
    source = data[0].split(":", 1)[0]
    assert not source.startswith((".", "/", "~")), "/data must be a named volume"
    top_level_volumes = rf"^volumes:\s*\n\s+{re.escape(source)}:"
    assert re.search(top_level_volumes, _read("compose.yaml"), re.MULTILINE)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^\s*privileged:",
        r"^\s*network_mode:\s*[\"']?host",
        r"^\s*pid:\s*[\"']?host",
        r"docker\.sock",
    ],
)
def test_compose_vektor_has_no(vektor: str, pattern: str) -> None:
    assert not re.search(pattern, vektor, re.MULTILINE), pattern


# --- A3: .dockerignore --------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        ".env.*",
        "data/",
        ".venv/",
        ".git/",
        "tests/",
        ".claude/",
        ".agents/",
        "results/",
    ],
)
def test_dockerignore_excludes(pattern: str) -> None:
    assert pattern in _dockerignore_patterns()


def test_dockerignore_only_reinclude_is_env_example() -> None:
    assert [p for p in _dockerignore_patterns() if p.startswith("!")] == [
        "!.env.example"
    ]


@pytest.mark.parametrize("path", [".env", ".env.local", "data", "tests", ".git"])
def test_dockerignore_matcher_excludes(path: str) -> None:
    assert _is_ignored(path)


def test_dockerignore_keeps_env_example() -> None:
    assert not _is_ignored(".env.example")


def test_dockerfile_copied_dirs_exist_and_are_not_ignored() -> None:
    dirs = [s for s in _copy_sources() if s.endswith("/")]
    assert dirs, "Dockerfile copies no directories"
    for src in dirs:
        assert (ROOT / src).is_dir(), f"{src} does not exist"
        assert not _is_ignored(src), f"{src} is excluded by .dockerignore"


# --- A5: CI docker job --------------------------------------------------------


@pytest.fixture(scope="module")
def docker_job() -> str:
    return _yaml_block(".github/workflows/ci.yml", "docker")


def _words(match: re.Match[str], group: int = 1) -> set[str]:
    return set(match.group(group).split())


@pytest.mark.parametrize(
    "needle",
    [
        "timeout-minutes:",
        "docker compose config -q",
        "docker build -t vektor:ci .",
        "docker run --rm --read-only --tmpfs /tmp -e TELEGRAM_BOT_TOKEN=123:dummy",
        'vektor:ci python -c "import bot"',
    ],
)
def test_ci_docker_job_runs(docker_job: str, needle: str) -> None:
    assert needle in docker_job


def test_ci_docker_job_every_step_is_strict_bash(docker_job: str) -> None:
    assert re.search(
        r"defaults:\s*\n\s+run:\s*\n\s+shell:\s*bash\s*$", docker_job, re.MULTILINE
    )
    runs = len(re.findall(r"^\s*run: \|\s*$", docker_job, re.MULTILINE))
    assert runs
    assert docker_job.count("set -euo pipefail") == runs


def test_ci_docker_job_feeds_assertions_on_stdin(docker_job: str) -> None:
    # Without -i, `sh -s` reads nothing and exits 0: every check would be skipped.
    assert re.search(r"docker run --rm -i\b[^\n]*\bvektor:ci sh -s <<'SH'", docker_job)


def test_ci_docker_job_asserts_uid(docker_job: str) -> None:
    assert re.search(r'\[ "\$uid" = 10001 \] \|\| fail', docker_job)


def test_ci_docker_job_asserts_exec_binaries_present(docker_job: str) -> None:
    check = re.search(
        r'for b in ([^;\n]+); do\s*test -x "/usr/bin/\$b" \|\| fail', docker_job
    )
    assert check
    assert _words(check) == set(EXEC_BINARIES)


def test_ci_docker_job_asserts_process_control_binaries_absent(docker_job: str) -> None:
    check = re.search(
        r'for b in ([^;\n]+); do\s*for d in ([^;\n]+); do\s*test ! -e "\$d/\$b" \|\| fail',
        docker_job,
    )
    assert check
    assert _words(check, 1) == set(PROCESS_CONTROL_BINARIES)
    assert _words(check, 2) == set(SAFE_PATH_DIRS)


def test_ci_docker_job_asserts_secrets_and_tests_absent(docker_job: str) -> None:
    check = re.search(r'for p in ([^;\n]+); do\s*test ! -e "\$p" \|\| fail', docker_job)
    assert check
    assert {"/app/.env", "/app/data", "/app/tests"} <= _words(check)


def test_ci_docker_job_uses_dummy_env_file_and_no_secrets(docker_job: str) -> None:
    assert "VEKTOR_ENV_FILE=" in docker_job
    assert "secrets." not in docker_job


def test_ci_keeps_read_only_permissions() -> None:
    assert re.search(
        r"^permissions:\s*\n\s+contents:\s*read\s*$",
        _read(".github/workflows/ci.yml"),
        re.MULTILINE,
    )
