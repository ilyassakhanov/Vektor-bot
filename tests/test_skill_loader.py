"""Tests for SkillLoader — discovers .md files from a directory."""

from __future__ import annotations

import tempfile
from pathlib import Path

from skills.loader import SkillLoader


def _write_skill(dir_path: Path, name: str, content: str) -> None:
    (dir_path / f"{name}.md").write_text(content, encoding="utf-8")


def test_loader_finds_md_files():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_skill(tmp_path, "cve", "# CVE Skill\n\nInstructions here.")
        _write_skill(tmp_path, "other", "# Other Skill\n\nOther instructions.")
        loader = SkillLoader(tmp_path)
        skills = loader.load()
        names = {s.name for s in skills}
        assert names == {"cve", "other"}


def test_loader_returns_content():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_skill(tmp_path, "cve", "# CVE Skill\n\nDo things.")
        loader = SkillLoader(tmp_path)
        skills = loader.load()
        assert len(skills) == 1
        assert "# CVE Skill" in skills[0].content
        assert skills[0].name == "cve"


def test_loader_empty_directory():
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(Path(tmp))
        skills = loader.load()
        assert skills == []


def test_loader_nonexistent_directory():
    loader = SkillLoader(Path("/nonexistent/path/xyz123"))
    skills = loader.load()
    assert skills == []


def test_loader_ignores_non_md_files():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_skill(tmp_path, "cve", "# CVE")
        (tmp_path / "readme.txt").write_text("not a skill", encoding="utf-8")
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        loader = SkillLoader(tmp_path)
        skills = loader.load()
        assert len(skills) == 1
        assert skills[0].name == "cve"


def test_loader_skill_name_from_filename():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_skill(tmp_path, "my-skill", "# My Skill")
        loader = SkillLoader(tmp_path)
        skills = loader.load()
        assert skills[0].name == "my-skill"


def test_loader_combines_all_skills_into_system_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_skill(tmp_path, "cve", "# CVE Skill\n\nDo CVE things.")
        _write_skill(tmp_path, "general", "# General Skill\n\nBe helpful.")
        loader = SkillLoader(tmp_path)
        system = loader.system_prompt()
        assert "CVE Skill" in system
        assert "General Skill" in system
        assert "Do CVE things." in system
        assert "Be helpful." in system


def test_loader_system_prompt_empty_when_no_skills():
    with tempfile.TemporaryDirectory() as tmp:
        loader = SkillLoader(Path(tmp))
        system = loader.system_prompt()
        assert system == ""
