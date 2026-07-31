"""Targeted tests for remaining coverage gaps."""
# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

# --- _format.py ---

def test_format_load_render_prefs_with_injected_config():
    from _format import load_render_prefs, reset_prefs_cache, set_load_config_fn
    reset_prefs_cache()
    set_load_config_fn(lambda: {"render_mode": "tui", "theme": "midnight"})
    try:
        prefs = load_render_prefs()
        assert prefs == {"render_mode": "tui", "theme": "midnight"}
        # Second call returns cached value
        assert load_render_prefs() is prefs
    finally:
        set_load_config_fn(None)
        reset_prefs_cache()


def test_format_load_render_prefs_fallback_on_error():
    import _format
    _format.reset_prefs_cache()
    def _raise(): raise RuntimeError("fail")
    _format.set_load_config_fn(_raise)
    try:
        prefs = _format.load_render_prefs()
        assert prefs["render_mode"] == "auto"
        assert prefs["theme"] == "default"
    finally:
        _format.set_load_config_fn(None)
        _format.reset_prefs_cache()


# --- _provider.py ---

def test_provider_is_our_base_url():
    from _provider import _is_our_base_url
    assert _is_our_base_url("http://localhost:8088/v1")
    assert _is_our_base_url("http://127.0.0.1:8088/v1")
    assert not _is_our_base_url("http://example.com:8088/v1")
    assert not _is_our_base_url(123)


def test_provider_is_our_entry():
    from _provider import _is_our_entry
    assert _is_our_entry({"base_url": "http://localhost:8088/v1"})
    assert not _is_our_entry({"base_url": "http://example.com/v1"})
    assert not _is_our_entry("not-a-dict")


def test_provider_register_skips_when_import_fails():
    from _provider import register_provider
    with patch.dict("sys.modules", {"hermes_cli": None, "hermes_cli.config": None}):
        result = register_provider(8088)
    assert result is False


def test_provider_unregister_skips_when_import_fails():
    from _provider import unregister_provider
    with patch.dict("sys.modules", {"hermes_cli": None, "hermes_cli.config": None}):
        result = unregister_provider()
    assert result is False


def test_provider_register_with_mock_config():
    from _provider import PROVIDER_SLUG, register_provider
    cfg = {}
    mock_load = MagicMock(return_value=cfg)
    mock_save = MagicMock()
    mock_module = MagicMock(load_config=mock_load, save_config=mock_save)
    with patch.dict("sys.modules", {"hermes_cli": mock_module, "hermes_cli.config": mock_module}):
        result = register_provider(8088)
    assert result is True
    mock_save.assert_called_once()
    saved = mock_save.call_args[0][0]
    assert PROVIDER_SLUG in saved.get("providers", {})


def test_provider_unregister_with_mock_config():
    from _provider import PROVIDER_SLUG, unregister_provider
    cfg = {"providers": {PROVIDER_SLUG: {"base_url": "http://localhost:8088/v1"}}}
    mock_load = MagicMock(return_value=cfg)
    mock_save = MagicMock()
    mock_module = MagicMock(load_config=mock_load, save_config=mock_save)
    with patch.dict("sys.modules", {"hermes_cli": mock_module, "hermes_cli.config": mock_module}):
        result = unregister_provider()
    assert result is True


# --- adapter.py ---

def test_extract_balanced_brace_not_brace():
    from adapter import _extract_balanced_brace
    assert _extract_balanced_brace("hello", 0) == (None, 0)


def test_extract_balanced_brace_with_string_escape():
    from adapter import _extract_balanced_brace
    text = '{"key": "val\\"ue"}'
    obj, end = _extract_balanced_brace(text, 0)
    assert obj == text
    assert end == len(text)


def test_tool_calls_frames_with_text():
    from adapter import _tool_calls_frames
    calls = [{"name": "fs_read", "arguments": '{"path": "a.txt"}'}]
    out = _tool_calls_frames(calls, text="here is context", model="auto")
    assert b"fs_read" in out
    assert b"here is context" in out
    assert b"tool_calls" in out
    assert b"[DONE]" in out


# --- __init__.py handler coverage ---

def test_handle_bid_login_already_authenticated():
    import json

    import __init__ as plugin
    with patch.object(plugin, "start_login", return_value={"already_authenticated": True, "phase": "authenticated"}):
        result = json.loads(plugin._handle_bid_login({}))
    assert result["success"] is True
    assert result["phase"] == "authenticated"

def test_handle_bid_logout():
    import __init__ as plugin
    with patch.object(plugin, "logout") as mock_logout:
        result = json.loads(plugin._handle_bid_logout({}))
    mock_logout.assert_called_once()
    assert result.get("success") is True


def test_handle_models():
    import __init__ as plugin
    result = json.loads(plugin._handle_bid_models({}))
    assert isinstance(result, dict) and ("models" in result or "tags" in result)


def test_handle_tags():
    import __init__ as plugin
    result = json.loads(plugin._handle_tags({}))
    assert isinstance(result, dict) and "tags" in result


# --- backend.py ---

def test_load_tags_exception_returns_fallback():
    from backend import STATIC_TAGS, load_tags
    with patch("builtins.open", side_effect=OSError("no file")):
        assert load_tags() == STATIC_TAGS


# --- _provider.py: register/unregister body ---

def _make_hermes_cli_mock(initial_config=None):
    import copy
    import types
    saved = [copy.deepcopy(initial_config or {})]
    fake_cfg = types.ModuleType('hermes_cli.config')
    fake_cfg.load_config = lambda: copy.deepcopy(saved[0])
    fake_cfg.save_config = lambda c: saved.__setitem__(0, copy.deepcopy(c))
    fake_hermes = types.ModuleType('hermes_cli')
    fake_hermes.config = fake_cfg
    return fake_hermes, fake_cfg, saved


def test_provider_register_writes_entry(monkeypatch):
    import sys

    import _provider
    fake_hermes, fake_cfg, saved = _make_hermes_cli_mock()
    monkeypatch.setitem(sys.modules, 'hermes_cli', fake_hermes)
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', fake_cfg)
    result = _provider.register_provider(8088)
    assert result is True
    assert _provider.PROVIDER_SLUG in saved[0].get('providers', {})


def test_provider_register_skips_user_managed(monkeypatch):
    import sys

    import _provider
    existing = {'base_url': 'http://example.com/v1', 'name': 'Other'}
    cfg = {'providers': {_provider.PROVIDER_SLUG: existing}}
    fake_hermes, fake_cfg, _saved = _make_hermes_cli_mock(cfg)
    monkeypatch.setitem(sys.modules, 'hermes_cli', fake_hermes)
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', fake_cfg)
    result = _provider.register_provider(8088)
    assert result is False


def test_provider_unregister_removes_our_entry(monkeypatch):
    import sys

    import _provider
    cfg = {'providers': {_provider.PROVIDER_SLUG: {'base_url': 'http://localhost:8088/v1'}}}
    fake_hermes, fake_cfg, saved = _make_hermes_cli_mock(cfg)
    monkeypatch.setitem(sys.modules, 'hermes_cli', fake_hermes)
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', fake_cfg)
    result = _provider.unregister_provider()
    assert result is True
    assert _provider.PROVIDER_SLUG not in saved[0].get('providers', {})


def test_provider_unregister_leaves_foreign_entry(monkeypatch):
    import sys

    import _provider
    cfg = {'providers': {_provider.PROVIDER_SLUG: {'base_url': 'http://example.com/v1'}}}
    fake_hermes, fake_cfg, _saved = _make_hermes_cli_mock(cfg)
    monkeypatch.setitem(sys.modules, 'hermes_cli', fake_hermes)
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', fake_cfg)
    result = _provider.unregister_provider()
    assert result is False
