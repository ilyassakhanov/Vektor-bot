"""Tests for the CI archive-discipline check script.

The script lives outside a package (``scripts/`` has no ``__init__.py``),
so it is loaded from its file path. Input is fed through ``sys.stdin`` —
exactly the pipe shape CI uses — and output is captured from stdout.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "check_openspec_archived.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_openspec_archived", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _change(name: str, completed: int, total: int) -> dict[str, Any]:
    return {"name": name, "completedTasks": completed, "totalTasks": total}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    changes: list[dict[str, Any]],
    step_summary: str | None = None,
) -> int:
    payload = json.dumps(
        {"changes": changes, "root": {"path": "/tmp", "source": "nearest"}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    if step_summary is None:
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", step_summary)
    return script.main()  # type: ignore[attr-defined]


def test_all_tasks_complete_fails(monkeypatch: pytest.MonkeyPatch, capsys):
    assert _run(monkeypatch, [_change("done-change", 3, 3)]) == 1
    out = capsys.readouterr().out
    assert "::error::change done-change: all tasks complete" in out
    assert "openspec archive done-change" in out


def test_zero_tasks_fails(monkeypatch: pytest.MonkeyPatch, capsys):
    assert _run(monkeypatch, [_change("empty-change", 0, 0)]) == 1
    out = capsys.readouterr().out
    assert "::error::change empty-change: has no tasks" in out


def test_open_tasks_pass_and_are_listed(monkeypatch: pytest.MonkeyPatch, capsys):
    assert _run(monkeypatch, [_change("wip-change", 1, 4)]) == 0
    out = capsys.readouterr().out
    assert "::error" not in out
    assert "| `wip-change` | 1/4 |" in out


def test_no_changes_passes(monkeypatch: pytest.MonkeyPatch, capsys):
    assert _run(monkeypatch, []) == 0
    assert "None." in capsys.readouterr().out


def test_mixed_changes_fail_with_each_error(monkeypatch: pytest.MonkeyPatch, capsys):
    assert (
        _run(
            monkeypatch,
            [
                _change("done-change", 2, 2),
                _change("empty-change", 0, 0),
                _change("wip-change", 1, 4),
            ],
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "::error::change done-change: all tasks complete" in out
    assert "::error::change empty-change: has no tasks" in out
    assert "| `wip-change` | 1/4 |" in out


def test_step_summary_file_receives_the_table(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
):
    summary = tmp_path / "summary.md"
    summary.write_text("# Prior content\n", encoding="utf-8")
    assert (
        _run(monkeypatch, [_change("wip-change", 1, 4)], step_summary=str(summary)) == 0
    )
    assert capsys.readouterr().out == ""
    text = summary.read_text(encoding="utf-8")
    assert "# Prior content" in text
    assert "| `wip-change` | 1/4 |" in text
