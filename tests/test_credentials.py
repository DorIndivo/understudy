"""Key diagnostics: reporting a malformed key as malformed, and never printing one."""

from __future__ import annotations

from understudy import credentials


class TestReadingTheEnvironment:
    def test_an_unset_key_is_none(self, monkeypatch):
        monkeypatch.delenv(credentials.ENV_VAR, raising=False)
        assert credentials.api_key() is None

    def test_a_whitespace_only_key_is_none(self, monkeypatch):
        # An exported-but-empty variable is a common shell accident.
        monkeypatch.setenv(credentials.ENV_VAR, "   ")
        assert credentials.api_key() is None

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv(credentials.ENV_VAR, "  sk-ant-abc  ")
        assert credentials.api_key() == "sk-ant-abc"

    def test_workspace_id_reads_its_own_variable(self, monkeypatch):
        monkeypatch.setenv(credentials.WORKSPACE_ENV_VAR, "wrkspc_01ABC")
        assert credentials.workspace_id() == "wrkspc_01ABC"

    def test_an_unset_workspace_is_none(self, monkeypatch):
        monkeypatch.delenv(credentials.WORKSPACE_ENV_VAR, raising=False)
        assert credentials.workspace_id() is None


class TestKeyShape:
    def test_a_real_looking_key_passes(self):
        ok, reason = credentials.looks_like_key("sk-ant-api03-" + "x" * 90)
        assert ok and reason == ""

    def test_a_password_typed_by_mistake_is_rejected(self):
        # The accident this check exists for.
        ok, reason = credentials.looks_like_key("d1d2d3d4!")
        assert not ok and "sk-ant-" in reason

    def test_a_truncated_key_is_rejected(self):
        ok, reason = credentials.looks_like_key("sk-ant-abc")
        assert not ok and "too short" in reason

    def test_nothing_is_rejected(self):
        ok, reason = credentials.looks_like_key("   ")
        assert not ok and reason == "nothing entered"


class TestMasking:
    def test_a_long_key_is_partially_hidden(self):
        masked = credentials.masked("sk-ant-api03-abcdefghijklmnop")
        assert "abcdefghijkl" not in masked
        assert masked.startswith("sk-ant-a") and masked.endswith("mnop")

    def test_a_short_secret_is_fully_hidden(self):
        assert credentials.masked("shortkey") == "set"

    def test_absent_key_masks_to_a_dash(self):
        assert credentials.masked(None) == "-"
