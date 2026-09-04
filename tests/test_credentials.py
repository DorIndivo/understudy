"""Credential resolution: precedence, parsing, and never leaking the key."""

from __future__ import annotations

import pytest

from understudy import credentials
from understudy.credentials import Credential, parse_env_file, project_root, resolve

KEY_ENV = "sk-ant-from-environment-000"
KEY_FILE = "sk-ant-from-dotenv-file-111"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An isolated project root with no key available from any source."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.delenv(credentials.ENV_VAR, raising=False)
    return tmp_path


def write_env(path, key=KEY_FILE):
    (path / ".env").write_text(f"ANTHROPIC_API_KEY={key}\n")


class TestPrecedence:
    def test_environment_wins_over_a_dotenv_file(self, project, monkeypatch):
        monkeypatch.setenv(credentials.ENV_VAR, KEY_ENV)
        write_env(project)
        result = resolve(project)
        assert result.key == KEY_ENV and result.source == "environment"

    def test_dotenv_is_the_fallback(self, project):
        write_env(project)
        result = resolve(project)
        assert result.key == KEY_FILE and result.source == ".env file"

    def test_nothing_anywhere_reports_none(self, project):
        result = resolve(project)
        assert result.key is None and result.source == "none"
        assert result.found is False

    def test_empty_environment_variable_does_not_shadow_other_sources(self, project, monkeypatch):
        # An exported-but-empty variable is a common shell accident; it must not
        # mask a perfectly good stored key.
        monkeypatch.setenv(credentials.ENV_VAR, "   ")
        write_env(project)
        assert resolve(project).source == ".env file"

    def test_empty_dotenv_value_reports_nothing_found(self, project):
        (project / ".env").write_text("ANTHROPIC_API_KEY=\n")
        assert resolve(project).source == "none"


class TestEnvFileParsing:
    def test_quotes_comments_and_export_prefix(self, tmp_path):
        (tmp_path / ".env").write_text(
            '# a comment\n'
            '\n'
            'export ANTHROPIC_API_KEY="sk-quoted"\n'
            "OTHER='single'\n"
            'PLAIN=bare\n'
            'MALFORMED\n'
        )
        values = parse_env_file(tmp_path / ".env")
        assert values["ANTHROPIC_API_KEY"] == "sk-quoted"
        assert values["OTHER"] == "single"
        assert values["PLAIN"] == "bare"
        assert "MALFORMED" not in values

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert parse_env_file(tmp_path / "absent.env") == {}

    def test_value_containing_equals_is_preserved(self, tmp_path):
        (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=abc=def==\n")
        assert parse_env_file(tmp_path / ".env")["ANTHROPIC_API_KEY"] == "abc=def=="


class TestProjectRoot:
    def test_found_from_a_nested_directory(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert project_root(nested) == tmp_path.resolve()

    def test_dotenv_is_found_from_a_subdirectory(self, project, monkeypatch):
        write_env(project)
        nested = project / "deep" / "deeper"
        nested.mkdir(parents=True)
        assert resolve(nested).key == KEY_FILE


class TestMasking:
    def test_a_long_key_is_partially_hidden(self):
        masked = Credential("sk-ant-api03-abcdefghijklmnop", ".env file").masked()
        assert "abcdefghijkl" not in masked
        assert masked.startswith("sk-ant-a") and masked.endswith("mnop")

    def test_a_short_secret_is_fully_hidden(self):
        assert Credential("shortkey", ".env file").masked() == "set"

    def test_absent_key_masks_to_a_dash(self):
        assert Credential(None, "none").masked() == "-"


class TestApplyToEnvironment:
    def test_a_dotenv_key_is_exposed_to_the_sdk(self, project):
        write_env(project)
        credentials.apply_to_environment(project)
        import os
        assert os.environ[credentials.ENV_VAR] == KEY_FILE

    def test_an_existing_export_is_not_overwritten(self, project, monkeypatch):
        monkeypatch.setenv(credentials.ENV_VAR, KEY_ENV)
        write_env(project)
        credentials.apply_to_environment(project)
        import os
        assert os.environ[credentials.ENV_VAR] == KEY_ENV
