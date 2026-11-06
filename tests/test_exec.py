"""Tests for ExecTool execution: output contract, timeouts, isolation.

ExecTool runs no shell, so these tests drive a real interpreter instead of
shell builtins. The ``allow_python`` fixture allowlists ``python3`` and puts
``dirname(sys.executable)`` first on the safe search path, so the venv
interpreter is resolved (not a macOS CLT stub). Payloads live in script files
under ``tmp_path``, so command strings never contain shell metacharacters.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import tools.exec as exec_mod
from tools.base import ToolError
from tools.exec import _SAFE_PATH, ExecTool, _make_preexec, build_exec_env

_REPO_ROOT = Path(__file__).resolve().parent.parent

MakeTool = Callable[..., ExecTool]


@pytest.fixture
def scripts(tmp_path: Path) -> Path:
    d = tmp_path / "scripts"
    d.mkdir()
    (d / "out.py").write_text("import sys\nfor a in sys.argv[1:]:\n    print(a)\n")
    (d / "err.py").write_text(
        "import sys\nsys.stderr.write('error\\n')\nsys.exit(int(sys.argv[1]))\n"
    )
    (d / "both.py").write_text("import sys\nprint('out')\nsys.stderr.write('err\\n')\n")
    (d / "big.py").write_text(
        "import sys\n"
        "n = int(sys.argv[1])\n"
        "stream = sys.stderr if sys.argv[2] == 'err' else sys.stdout\n"
        "stream.write(sys.argv[3] * n)\n"
        "if sys.argv[2] == 'out':\n"
        "    stream.write('\\n')\n"
        "sys.exit(int(sys.argv[4]))\n"
    )
    (d / "flood.py").write_text(
        "import sys\nwhile True:\n    sys.stdout.write('y' * 65536)\n"
    )
    (d / "sleep.py").write_text("import time\ntime.sleep(10)\n")
    (d / "grandchild.py").write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "import os\n"
        "with open(sys.argv[1] + '.tmp', 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(30)\n"
    )
    (d / "escaped.py").write_text(
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(10)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "with open(sys.argv[1] + '.tmp', 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
        "time.sleep(30)\n"
    )
    (d / "env.py").write_text(
        "import os\nfor k in sorted(os.environ):\n    print(k + '=' + os.environ[k])\n"
    )
    (d / "cwd.py").write_text(
        "import os\nprint(os.getcwd())\nprint(os.path.exists('.env'))\n"
    )
    return d


@pytest.fixture
def allow_python() -> MakeTool:
    search_path = f"{os.path.dirname(sys.executable)}:{_SAFE_PATH}"

    def make(timeout: float = 10.0, **kwargs: object) -> ExecTool:
        return ExecTool(
            timeout=timeout,
            allowed_commands=("python3",),
            search_path=search_path,
            **kwargs,  # type: ignore[arg-type]
        )

    return make


def _py(script: Path, *args: str) -> str:
    return " ".join(["python3", str(script), *args])


# --- Basic execution --------------------------------------------------------


def test_exec_returns_stdout(allow_python: MakeTool, scripts: Path) -> None:
    result = allow_python().execute(command=_py(scripts / "out.py", "hello"))
    assert "hello" in result
    assert "exit_code: 0" in result


def test_exec_returns_stderr_and_exit_code(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python().execute(command=_py(scripts / "err.py", "3"))
    assert "exit_code: 3" in result
    assert "error" in result


def test_exec_result_contains_all_three_fields(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python().execute(command=_py(scripts / "both.py"))
    assert "stdout:" in result
    assert "stderr:" in result
    assert "exit_code:" in result


def test_exec_with_multiline_output(allow_python: MakeTool, scripts: Path) -> None:
    result = allow_python().execute(
        command=_py(scripts / "out.py", "line1", "line2", "line3")
    )
    assert "line1" in result
    assert "line2" in result
    assert "line3" in result


def test_exec_short_output_unchanged(allow_python: MakeTool, scripts: Path) -> None:
    result = allow_python().execute(command=_py(scripts / "out.py", "hello"))
    assert result == "stdout:\nhello\n\nstderr:\n\nexit_code: 0"


# --- Timeout and process group ----------------------------------------------


def test_exec_timeout_returns_error_not_crash(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python(timeout=0.5).execute(command=_py(scripts / "sleep.py"))
    assert "timed out" in result.lower()
    assert result.endswith("\nexit_code: -1")


def test_exec_timeout_does_not_hang(allow_python: MakeTool, scripts: Path) -> None:
    start = time.monotonic()
    allow_python(timeout=0.3).execute(command=_py(scripts / "sleep.py"))
    assert time.monotonic() - start < 2.0


def test_exec_timeout_kills_grandchild(
    allow_python: MakeTool, scripts: Path, tmp_path: Path
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    result = allow_python(timeout=3.0).execute(
        command=_py(scripts / "grandchild.py", str(pid_file))
    )
    assert "timed out" in result.lower()
    if not pid_file.exists():  # a very slow machine never got to fork
        pytest.skip("grandchild did not start within the timeout")
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        # A killed but unreaped grandchild is a zombie: reparented to init
        # (or launchd), not to us, so it disappears once init reaps it.
        if time.monotonic() > deadline:
            pytest.fail(f"grandchild {pid} survived the timeout")
        time.sleep(0.05)


def test_exec_timeout_returns_promptly_when_a_child_escapes_the_group(
    allow_python: MakeTool, scripts: Path, tmp_path: Path
) -> None:
    # The child calls setsid, so the group kill misses it and it keeps the
    # output pipes open; execute() must not block on them.
    pid_file = tmp_path / "escaped.pid"
    start = time.monotonic()
    try:
        result = allow_python(timeout=1.0).execute(
            command=_py(scripts / "escaped.py", str(pid_file))
        )
        elapsed = time.monotonic() - start
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except ProcessLookupError:
                pass
    assert "timed out" in result.lower()
    assert elapsed < 4.0


# --- Isolation --------------------------------------------------------------

# Variables the interpreter or the OS may add to a child's environment on
# its own (macOS CoreFoundation adds __CF_USER_TEXT_ENCODING).
_INTERPRETER_ADDED = {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}


def test_exec_env_is_scrubbed(
    allow_python: MakeTool, scripts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET-TOKEN-VALUE")
    result = allow_python(max_output_chars=10**6).execute(
        command=_py(scripts / "env.py")
    )
    assert "SECRET-TOKEN-VALUE" not in result
    assert "TELEGRAM_BOT_TOKEN" not in result
    stdout = result.split("stdout:\n")[1].split("\nstderr:")[0]
    keys = {line.split("=", 1)[0] for line in stdout.splitlines() if "=" in line}
    assert {"PATH", "LANG", "HOME"} <= keys
    assert keys <= {"PATH", "LANG", "HOME"} | _INTERPRETER_ADDED


def test_exec_cwd_is_a_temp_dir_not_the_repo(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python().execute(command=_py(scripts / "cwd.py"))
    lines = result.split("stdout:\n")[1].split("\nstderr:")[0].splitlines()
    cwd = Path(lines[0]).resolve()
    assert cwd != _REPO_ROOT
    assert _REPO_ROOT not in cwd.parents
    assert lines[1] == "False"
    assert not cwd.exists()  # removed after the call


def test_build_exec_env_has_exact_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x:y")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    env = build_exec_env("/tmp/h")
    assert env == {"PATH": _SAFE_PATH, "LANG": "en_US.UTF-8", "HOME": "/tmp/h"}


def test_build_exec_env_default_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANG", raising=False)
    assert build_exec_env("/h", path="/p") == {
        "PATH": "/p",
        "LANG": "C.UTF-8",
        "HOME": "/h",
    }


class _FakeResource:
    RLIMIT_CPU = 0
    RLIMIT_AS = 1
    RLIMIT_FSIZE = 2
    RLIMIT_NPROC = 3
    RLIMIT_CORE = 4

    def __init__(self, failing: set[int]) -> None:
        self.failing = failing
        self.calls: list[tuple[int, tuple[int, int]]] = []

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        self.calls.append((which, limits))
        if which in self.failing:
            raise ValueError("refused")


def test_rlimit_nproc_is_not_set() -> None:
    # On Linux RLIMIT_NPROC counts every thread of the uid, so a low cap
    # breaks curl's threaded resolver; the process-group kill covers forks.
    assert "RLIMIT_NPROC" not in {name for name, _ in exec_mod._RLIMITS}


def test_preexec_sets_all_limits() -> None:
    fake = _FakeResource(failing=set())
    _make_preexec(2.5, resource_module=fake)()
    assert dict(fake.calls) == {
        fake.RLIMIT_CPU: (3, 3),
        fake.RLIMIT_AS: (512 * 1024 * 1024,) * 2,
        fake.RLIMIT_FSIZE: (10 * 1024 * 1024,) * 2,
        fake.RLIMIT_CORE: (0, 0),
    }


def test_preexec_survives_refused_limits() -> None:
    fake = _FakeResource(failing={_FakeResource.RLIMIT_AS, _FakeResource.RLIMIT_NPROC})
    _make_preexec(1.0, resource_module=fake)()  # must not raise
    assert len(fake.calls) == 4


def test_popen_gets_isolation_kwargs(
    monkeypatch: pytest.MonkeyPatch, allow_python: MakeTool, scripts: Path
) -> None:
    seen: dict[str, object] = {}
    real_popen = exec_mod.subprocess.Popen

    def spy(argv: list[str], **kwargs: object) -> object:
        seen.update(kwargs)
        return real_popen(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(exec_mod.subprocess, "Popen", spy)
    allow_python().execute(command=_py(scripts / "out.py", "x"))
    assert seen["start_new_session"] is True
    assert seen["stdin"] == exec_mod.subprocess.DEVNULL
    assert callable(seen["preexec_fn"])
    assert seen.get("shell", False) is False


# --- Missing arguments ------------------------------------------------------


def test_exec_missing_command_argument() -> None:
    with pytest.raises(ToolError):
        ExecTool(timeout=10.0).execute()


def test_exec_empty_command() -> None:
    assert ExecTool(timeout=10.0).execute(command="") == (
        "stdout:\n\nstderr:\n\nexit_code: 0"
    )


# --- Tool metadata ----------------------------------------------------------


def test_exec_tool_name() -> None:
    assert ExecTool(timeout=10.0).name == "exec"


def test_exec_tool_description() -> None:
    assert "command" in ExecTool(timeout=10.0).description.lower()


def test_exec_tool_parameters_has_command() -> None:
    params = ExecTool(timeout=10.0).parameters
    assert params["type"] == "object"
    assert "command" in params["properties"]
    assert "command" in params["required"]


# --- Does not crash on bad commands ----------------------------------------


def test_exec_command_not_found() -> None:
    tool = ExecTool(timeout=10.0, allowed_commands=("nonexistent_command_xyz123",))
    result = tool.execute(command="nonexistent_command_xyz123")
    assert isinstance(result, str)
    assert result.endswith("\nexit_code: -1")


# --- Output truncation ------------------------------------------------------


def _big(scripts: Path, n: int, stream: str = "out", exit_code: int = 0) -> str:
    return _py(
        scripts / "big.py",
        str(n),
        stream,
        "x" if stream == "out" else "e",
        str(exit_code),
    )


def test_exec_long_stdout_truncated_with_marker(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python(max_output_chars=200).execute(command=_big(scripts, 5000))
    assert "[truncated " in result
    assert result.startswith("stdout:\nx")
    assert result.endswith("\nexit_code: 0")
    assert len(result[: -len("\nexit_code: 0")]) <= 200


def test_exec_truncation_preserves_exit_code_line(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python(max_output_chars=200).execute(
        command=_big(scripts, 5000, exit_code=3)
    )
    assert "[truncated " in result
    assert result.endswith("\nexit_code: 3")
    assert len(result[: -len("\nexit_code: 3")]) <= 200


def test_exec_stderr_also_truncated(allow_python: MakeTool, scripts: Path) -> None:
    result = allow_python(max_output_chars=200).execute(
        command=_big(scripts, 5000, stream="err")
    )
    assert "[truncated " in result
    assert result.startswith("stdout:\n\nstderr:\ne")
    assert result.endswith("\nexit_code: 0")
    assert len(result[: -len("\nexit_code: 0")]) <= 200


def test_exec_large_cap_disables_truncation(
    allow_python: MakeTool, scripts: Path
) -> None:
    result = allow_python(max_output_chars=10**7).execute(command=_big(scripts, 5000))
    assert "[truncated" not in result
    stdout = result.split("stdout:\n")[1].split("\nstderr:")[0]
    assert stdout == "x" * 5000 + "\n"


def test_exec_env_cap_applied(
    monkeypatch: pytest.MonkeyPatch, allow_python: MakeTool, scripts: Path
) -> None:
    monkeypatch.setenv("EXEC_MAX_OUTPUT_CHARS", "150")
    result = allow_python().execute(command=_big(scripts, 5000))
    assert "[truncated " in result
    assert result.endswith("\nexit_code: 0")
    assert len(result[: -len("\nexit_code: 0")]) <= 150


def test_exec_invalid_env_cap_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, allow_python: MakeTool, scripts: Path
) -> None:
    monkeypatch.setenv("EXEC_MAX_OUTPUT_CHARS", "abc")
    result = allow_python().execute(command=_big(scripts, 5000))
    assert "[truncated " in result
    assert result.endswith("\nexit_code: 0")
    assert len(result[: -len("\nexit_code: 0")]) <= 4000


# --- Output byte cap ----------------------------------------------------------


def test_output_byte_cap_kills_flooding_command(
    allow_python: MakeTool, scripts: Path
) -> None:
    start = time.monotonic()
    result = allow_python(timeout=20.0, max_output_chars=200).execute(
        command=_py(scripts / "flood.py")
    )
    assert time.monotonic() - start < 10.0
    assert "output exceeded" in result
    assert result.endswith("\nexit_code: -1")
    assert len(result[: -len("\nexit_code: -1")]) <= 200


def test_output_byte_cap_bounds_buffered_bytes(
    allow_python: MakeTool, scripts: Path
) -> None:
    # 64 KiB floor: 1 MiB of output is cut, a cap of 10**7 chars keeps it all.
    capped = allow_python(max_output_chars=10**4).execute(
        command=_big(scripts, 1024 * 1024)
    )
    assert "output exceeded 65536 bytes" in capped
    assert capped.endswith("\nexit_code: -1")
    full = allow_python(max_output_chars=10**7).execute(
        command=_big(scripts, 1024 * 1024)
    )
    assert "output exceeded" not in full
    assert full.endswith("\nexit_code: 0")


# --- Kill fallback --------------------------------------------------------------


class _Proc:
    pid = 4242

    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True


@pytest.mark.parametrize("exc", [PermissionError, OSError])
def test_kill_group_falls_back_to_proc_kill(
    monkeypatch: pytest.MonkeyPatch, exc: type[OSError]
) -> None:
    def boom(pid: int, sig: int) -> None:
        raise exc("nope")

    monkeypatch.setattr(exec_mod.os, "killpg", boom)
    proc = _Proc()
    exec_mod._kill_group(proc)  # type: ignore[arg-type]
    assert proc.killed


def test_kill_group_ignores_vanished_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(exec_mod.os, "killpg", gone)
    proc = _Proc()
    exec_mod._kill_group(proc)  # type: ignore[arg-type]
    assert not proc.killed
