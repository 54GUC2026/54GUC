"""Public defaults select the existing built-in synthetic model explicitly.

These tests do not need the omitted historical models folder or recordings.
They do not create a new hardware profile or alter any retained fixture.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _argument(flag):
    tree = ast.parse((ROOT / "arch4/runner.py").read_text(encoding="utf-8-sig"))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "add_argument"
             and any(isinstance(arg, ast.Constant) and arg.value == flag for arg in node.args)]
    assert len(calls) == 1
    return {keyword.arg: ast.literal_eval(keyword.value) for keyword in calls[0].keywords}


def test_arch_config_selects_builtin_model_without_changing_physical_default():
    from arch4.interfaces import Arch4Config
    cfg = Arch4Config()
    assert cfg.model_profile_name == ""
    assert cfg.model_profile_path == ""
    assert cfg.modem_backend == "b818"


def test_model_template_names_no_excluded_file_and_keeps_sample_interval():
    source = (ROOT / "config/modems/model.env").read_text(encoding="utf-8-sig")
    values = {}
    for line in source.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    assert values["MODEL_PROFILE_NAME"] == ""
    assert values["MODEL_PROFILE_PATH"] == ""
    assert values["SAMPLE_INTERVAL"] == "2.0"
    assert "built-in synthetic" in source.lower()


def test_runner_explicitly_documents_builtin_and_keeps_backend_selection():
    name = _argument("--model-profile-name")
    path = _argument("--model-profile-path")
    backend = _argument("--modem")
    assert name["default"] == ""
    assert "built-in synthetic" in name["help"].lower()
    assert path["default"] == "" and "overrides" in path["help"].lower()
    assert backend["default"] == "" and "model" in backend["choices"]


def test_connection_view_passes_empty_selection_without_substituting_a_name():
    from arch4.config_views import build_model_connection_config
    from arch4.interfaces import Arch4Config
    view = build_model_connection_config(Arch4Config(modem_backend="model"))
    assert view.profile_name == "" and view.profile_path == ""


def test_model_factory_uses_builtin_directly_with_models_folder_absent(tmp_path, monkeypatch):
    from arch4.interfaces import Arch4Config
    from arch4.modem import model_backend
    from arch4.modem_live.bootstrap import create_modem_live_backend
    missing = tmp_path / "omitted-models"
    assert not missing.exists()
    monkeypatch.setattr(model_backend, "PROFILE_DIR", missing)
    def unexpected_file(*args, **kwargs):
        pytest.fail("A default model attempted to load an optional descriptor")
    monkeypatch.setattr(model_backend.ModemModelBackend, "_load_profile_file", unexpected_file)
    modem = create_modem_live_backend(Arch4Config(modem_backend="model"))
    try:
        # A missing named file can silently fall back. These assertions also
        # prove that no absent name was requested in the first place.
        assert modem._profile_name == "" and modem._profile_path == ""
        assert modem._profile_label() == "built-in"
        assert modem.connect()
        assert modem.read_modem_input() is not None
        status = modem.get_last_status()
        assert status["router_model"] == "54GUC Model"
        assert status["provider_name"] == "Lab"
        assert status["provider_plmn"] == "99999"
    finally:
        modem.disconnect()


def test_model_live_pair_uses_the_same_builtin_without_starting_a_worker(tmp_path, monkeypatch):
    from arch4.interfaces import Arch4Config
    from arch4.modem import model_backend
    from arch4.modem_live import bootstrap
    monkeypatch.setattr(model_backend, "PROFILE_DIR", tmp_path / "omitted-models")
    wrapped = []
    sentinel = object()
    def wrap(cfg, *, modem):
        wrapped.append((cfg, modem))
        return sentinel
    monkeypatch.setattr(bootstrap, "create_modem_live_module", wrap)
    cfg = Arch4Config(modem_backend="model")
    modem, module = bootstrap.create_model_live_pair(config=cfg)
    assert module is sentinel and wrapped == [(cfg, modem)]
    assert modem._profile_name == "" and modem._profile_path == ""
    assert modem._profile_label() == "built-in"


def test_explicit_custom_file_still_takes_precedence_over_a_name(tmp_path):
    from arch4.interfaces import Arch4Config
    from arch4.modem_live.bootstrap import create_modem_live_backend
    custom = tmp_path / "explicit-test-only.json"
    custom.write_text(json.dumps({
        "identity": {"model": "Explicit test input", "router_model": "Explicit test input"},
        "provider_name": "Test fixture",
    }), encoding="utf-8")
    cfg = Arch4Config(modem_backend="model", model_profile_name="unused-local-name", model_profile_path=str(custom))
    modem = create_modem_live_backend(cfg)
    try:
        assert modem._profile_label() == str(custom)
        assert modem._identity()["router_model"] == "Explicit test input"
        assert modem._profile["provider_name"] == "Test fixture"
    finally:
        modem.disconnect()


def test_explicit_missing_path_is_not_misreported_as_a_builtin_success(tmp_path):
    from arch4.interfaces import Arch4Config
    from arch4.modem_live.bootstrap import create_modem_live_backend
    cfg = Arch4Config(modem_backend="model", model_profile_path=str(tmp_path / "missing-explicit.json"))
    with pytest.raises(FileNotFoundError):
        create_modem_live_backend(cfg)
