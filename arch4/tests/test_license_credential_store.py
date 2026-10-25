from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import arch4.license_credential_store as store
from arch4.tests.license_signing_test_support import generate_license


def _projected_dat(path: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in (path / store.LICENSE_DAT_FILENAME)
        .read_text(encoding="utf-8")
        .splitlines()
    )


def test_atomic_record_is_canonical_and_license_dat_is_write_only_adapter(
    tmp_path: Path,
) -> None:
    external_xml = tmp_path / "guc_config.xml"
    external_xml.write_text(
        "<guc_config><site_name>Keep Me</site_name></guc_config>",
        encoding="utf-8",
    )
    before = external_xml.read_bytes()
    nonce, key = generate_license("MODEM-ONE", tier="tester_trusted")

    receipt = store.save_license_credentials(
        identity="MODEM-ONE",
        nonce=nonce,
        key=key,
        email="owner@example.invalid",
        tier="tester_trusted",
        config_dir=tmp_path,
        now=123.0,
    )

    assert receipt["saved"] is True
    state = store.load_license_credentials(tmp_path)
    assert state is not None
    assert state["identity"] == "MODEM-ONE"
    assert state["key"] == key
    assert external_xml.read_bytes() == before
    assert _projected_dat(tmp_path) == ("MODEM-ONE", nonce, key, "tester_trusted")
    if os.name != "nt":
        for filename in (store.STATE_FILENAME, store.LICENSE_DAT_FILENAME):
            assert stat.S_IMODE((tmp_path / filename).stat().st_mode) == 0o600


def test_adapter_failure_keeps_canonical_commit_for_supervised_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_projection = store._project_license_dat
    monkeypatch.setattr(
        store,
        "_project_license_dat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    nonce, key = generate_license("MODEM-ONE")

    with pytest.raises(OSError, match="injected"):
        store.save_license_credentials(
            identity="MODEM-ONE",
            nonce=nonce,
            key=key,
            config_dir=tmp_path,
            now=123.0,
        )

    state = store.load_license_credentials(tmp_path)
    assert state is not None and state["key"] == key
    assert not (tmp_path / store.LICENSE_DAT_FILENAME).exists()

    monkeypatch.setattr(store, "_project_license_dat", real_projection)
    receipt = store.reconcile_license_credentials(tmp_path)
    assert receipt is not None and receipt["reconciled"] is True
    assert _projected_dat(tmp_path) == ("MODEM-ONE", nonce, key)


def test_corrupt_current_recovers_previous_canonical_record_only(
    tmp_path: Path,
) -> None:
    first_nonce, first_key = generate_license("MODEM-ONE")
    second_nonce, second_key = generate_license("MODEM-ONE")
    store.save_license_credentials(
        identity="MODEM-ONE",
        nonce=first_nonce,
        key=first_key,
        config_dir=tmp_path,
        now=1.0,
    )
    store.save_license_credentials(
        identity="MODEM-ONE",
        nonce=second_nonce,
        key=second_key,
        config_dir=tmp_path,
        now=2.0,
    )
    (tmp_path / store.STATE_FILENAME).write_text("{corrupt", encoding="utf-8")

    receipt = store.reconcile_license_credentials(tmp_path)

    assert receipt is not None and receipt["recovered_previous"] is True
    current = json.loads((tmp_path / store.STATE_FILENAME).read_text(encoding="utf-8"))
    assert current["key"] == first_key
    assert store.load_license_credentials(tmp_path)["key"] == first_key
    assert _projected_dat(tmp_path) == ("MODEM-ONE", first_nonce, first_key)


def test_unverifiable_credentials_never_become_authority(tmp_path: Path) -> None:
    with pytest.raises(
        store.LicenseCredentialStoreError,
        match="license credential verification failed",
    ):
        store.save_license_credentials(
            identity="MODEM-ONE",
            nonce="A1B2C3D4E5F67890",
            key="AABBCCDDEE1122334455",
            config_dir=tmp_path,
        )
    assert store.load_license_credentials(tmp_path) is None


def test_retired_projection_readers_fail_loudly() -> None:
    import arch4.user_config as retired

    with pytest.raises(RuntimeError, match="illegal"):
        getattr(retired, "load_license_dat")
    with pytest.raises(RuntimeError, match="illegal"):
        getattr(retired, "load_license_tier_dat")
