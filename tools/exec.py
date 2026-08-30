"""Generic exec tool — executes a shell command and returns stdout/stderr/exit code.

This tool is intentionally generic. It does not contain any CVE-specific
logic. Its primary purpose is executing read-only HTTP requests such as
``curl <official CVE.org endpoint>`` via the system shell.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from tools.base import Tool, ToolError

log = logging.getLogger("vektor.tools.exec")

_DEFAULT_TIMEOUT = 30.0


class ExecTool(Tool):
    """Execute a shell command, return stdout/stderr/exit code.

    Failures (non-zero exit, timeout, crashes) are returned as strings to
    the LLM — the tool never raises :class:`ToolError` for command failures.
    Only missing arguments raise :class:`ToolError`.
    """

    def __init__(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return stdout, stderr, and exit code. Use for read-only HTTP requests like curl."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
            },
            "required": ["command"],
        }

    def execute(self, **kwargs: Any) -> str:
        command = kwargs.get("command")
        if command is None:
            raise ToolError("Missing required argument: command")
        command = str(command)
        if not command:
            return self._format("", "", 0)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            return self._format(proc.stdout, proc.stderr, proc.returncode)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            msg = f"Command timed out after {self._timeout}s."
            return self._format(str(stdout), f"{msg}\n{stderr}", -1)
        except OSError as exc:
            return self._format("", f"Error: {exc}", -1)

    def _format(self, stdout: str, stderr: str, exit_code: int) -> str:
        return f"stdout:\n{stdout}\nstderr:\n{stderr}\nexit_code: {exit_code}"
