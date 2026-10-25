"""Proposed exporter checks with neutral in-memory Git data and temp files.

These are validation/ZIP tests, not an application build or Git-CLI integration
claim. No current project source/capture bytes are used as fixture content.
"""
import gzip
import hashlib
import json
import os
from pathlib import Path

import pytest
from tools import prepare_public_source as exporter


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows source-export boundary")


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    source = tmp_path / "neutral-source"
    output_directory = tmp_path / "output"
    source.mkdir()
    output_directory.mkdir()
    # Independent neutral fixture, not a copy of real application files.
    paths = [
        "LICENSE", "LICENSING.md", "README.md", "CONTRIBUTING.md", "SECURITY.md",
        "docs/BUILDING.md", "docs/TESTING.md", "docs/PROTOCOL_REFERENCES.md",
        "VERSION", "RELEASE_CHANNEL", "UPDATE_CHANNEL.json", "open_source_manifest.json",
        "requirements.txt", "requirements-runtime-pins.txt", "requirements-dev.txt",
        "requirements-ngui-visual.txt", "pytest.ini", "LUC-Windows-Native.spec",
        "arch4/windows_native_launcher.py", "arch4/__init__.py",
        "arch4/modem_live/profile_definition/field-definitions.v2.json",
        "arch4/modem_live/profile_definition/metadata-schema.json",
        "arch4/ngui/fonts/LICENSE-SIL-OFL.txt", "third_party/NOTICE.txt",
        "arch4/ngui/fonts/LiberationMono-Regular.ttf", "arch4/ngui/fonts/LiberationMono-Bold.ttf",
        "arch4/ngui/fonts/LiberationMono-Italic.ttf", "arch4/ngui/fonts/LiberationMono-BoldItalic.ttf",
        "third_party/licenses/synthetic/LICENSE.txt", "hooks/hook-synthetic.py",
        "scripts/windows_portable_control.ps1", "scripts/windows_native_bootstrap.ps1",
        "scripts/build_windows_portable.ps1", "scripts/export_windows_build.ps1",
        "scripts/build_windows_native_bundle.ps1", "scripts/test_windows_portable.ps1",
        "scripts/cgf_windows_path_registry.ps1", "tools/generate_open_source_manifest.py",
        "tools/prepare_public_source.py", "scripts/package_active_source.sh",
        "tools/modem_profile_exchange/fields.tsv",
    ]
    paths += ["docs/CGF_MUST_READ/BETA_PACKAGE/" + name for name in (
        "START_HERE.txt", "USER_GUIDE.txt", "SUPPORTED_HARDWARE_AND_LIMITATIONS.txt",
        "RECOVERY_AND_SUPPORT.txt", "BETA_RELEASE_NOTES.txt",
    )]
    paths += ["arch4/modem_live/profile_definition/codecs/" + name for name in (
        "huawei-hilink.v1.json", "routeros.v1.json", "tplink-dev2.v1.json",
        "xiaomi-miwifi.v1.json", "zte-goform.v1.json", "zte-zwrt.v1.json",
    )]
    paths += [f"modem_profiles/synthetic_model_{index:02}.json" for index in range(48)]
    public_documents = {
        "docs/BUILDING.md": b"# Synthetic build guide\nSee [Testing](TESTING.md).\n",
        "docs/TESTING.md": b"# Synthetic test guide\nSee [Building](BUILDING.md).\n",
        "docs/PROTOCOL_REFERENCES.md": b"# Synthetic public protocol provenance\nNo private captures are included.\n",
    }
    contents = {path: b"SYNTHETIC-NEUTRAL-SOURCE-INPUT\n" for path in paths}
    contents.update(public_documents)
    contents["README.md"] = (
        b"# Synthetic public source\n"
        b"[Building](docs/BUILDING.md)\n"
        b"[Testing](docs/TESTING.md)\n"
        b"[Protocol references](docs/PROTOCOL_REFERENCES.md)\n"
    )
    for path, raw in contents.items():
        destination = source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    commit = "1" * 40
    state = {"head": commit, "status": b"", "modes": {path: "100644" for path in paths}}
    def fake_git(root, *arguments):
        assert root == source
        if arguments == ("rev-parse", "HEAD"):
            return state["head"].encode() + b"\n"
        if arguments[:1] == ("status",):
            return state["status"]
        if arguments[:1] == ("ls-tree",):
            return b"".join((state["modes"][path] + " blob " + "0" * 40 + "\t" + path).encode() + b"\0" for path in sorted(contents))
        if arguments[:2] == ("cat-file", "blob"):
            requested_commit, path = arguments[2].split(":", 1)
            assert requested_commit == commit
            return contents[path]
        pytest.fail("Unexpected Git command in isolated exporter test")
    monkeypatch.setattr(exporter, "_git", fake_git)
    manifest = {
        "schema": "54GUC.PublicSourceExportManifest.v1", "source_commit": commit,
        "publication_ready": True, "holds": [],
        "entries": [{"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "review_status": "approved", "role": "public_document" if path in public_documents else "resource"} for path, raw in sorted(contents.items())],
    }
    manifest_file = tmp_path / "reviewed-manifest.json"
    rules_file = tmp_path / "PRIVATE-rules.json"
    output = output_directory / "neutral-source.zip"
    def save_metadata():
        raw = json.dumps(manifest).encode()
        manifest_file.write_bytes(raw)
        rules_file.write_text(json.dumps({
            "schema": "54GUC.PrivateSourcePrivacyRules.v1", "approved": True,
            "reviewed_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "literal_denylist": ["SYNTHETIC-DENIED-VALUE-ONLY"],
        }), encoding="utf-8")
    save_metadata()
    return {"source": source, "output": output, "directory": output_directory,
            "manifest_file": manifest_file, "rules_file": rules_file,
            "manifest": manifest, "contents": contents, "state": state,
            "commit": commit, "save": save_metadata}


def run(candidate):
    return exporter.export_public_source(
        source=candidate["source"], manifest_file=candidate["manifest_file"],
        private_rules_file=candidate["rules_file"], output=candidate["output"],
        expected_commit=candidate["commit"],
    )


def add(candidate, path, data, *, role="resource"):
    candidate["contents"][path] = data
    candidate["state"]["modes"][path] = "100644"
    target = candidate["source"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    candidate["manifest"]["entries"].append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "review_status": "approved", "role": role})
    candidate["save"]()


def test_exact_allowlist_is_exported_with_only_a_sanitized_manifest(candidate):
    result = run(candidate)
    assert result["status"] == "SOURCE_ARCHIVE_CREATED"
    with exporter.zipfile.ZipFile(candidate["output"]) as archive:
        assert set(archive.namelist()) == set(candidate["contents"]) | {"PUBLIC_SOURCE_MANIFEST.json"}
        assert not any(name.startswith(".git/") for name in archive.namelist())
        record = json.loads(archive.read("PUBLIC_SOURCE_MANIFEST.json"))
        assert "source_repo" not in record and "private_rules" not in record
        for path, raw in candidate["contents"].items():
            assert archive.read(path) == raw
    assert candidate["rules_file"].name not in candidate["output"].read_bytes().decode("latin-1")


@pytest.mark.parametrize("change", ["publication", "holds", "entry"])
def test_holds_or_unreviewed_entries_produce_no_files(candidate, change):
    if change == "publication":
        candidate["manifest"]["publication_ready"] = False
    elif change == "holds":
        candidate["manifest"]["holds"] = ["Review required"]
    else:
        candidate["manifest"]["entries"][0]["review_status"] = "pending"
    candidate["save"]()
    with pytest.raises(exporter.ExportBlocked):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_a_leftover_entry_hold_cannot_be_hidden_by_an_approved_label(candidate):
    candidate["manifest"]["entries"][0]["holds"] = ["Still private"]
    candidate["save"]()
    with pytest.raises(exporter.ExportBlocked, match="ENTRY_SHAPE_OR_HOLD_REQUIRES_REVIEW"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


@pytest.mark.parametrize("missing", [
    "docs/CGF_MUST_READ/BETA_PACKAGE/USER_GUIDE.txt",
    "arch4/modem_live/profile_definition/codecs/zte-zwrt.v1.json",
    "arch4/modem_live/profile_definition/metadata-schema.json",
    "modem_profiles/synthetic_model_47.json",
    "third_party/licenses/synthetic/LICENSE.txt",
])
def test_required_guides_codecs_schemas_profiles_and_legal_inputs_cannot_be_omitted(candidate, missing):
    candidate["manifest"]["entries"] = [e for e in candidate["manifest"]["entries"] if e["path"] != missing]
    candidate["save"]()
    with pytest.raises(exporter.ExportBlocked, match="MANDATORY_ALLOWLIST_INPUT_MISSING"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_changed_commit_or_dirty_source_is_refused(candidate):
    candidate["state"]["head"] = "2" * 40
    with pytest.raises(exporter.ExportBlocked, match="SOURCE_COMMIT_CHANGED"):
        run(candidate)
    candidate["state"]["head"] = candidate["commit"]
    candidate["state"]["status"] = b" M arch4/windows_native_launcher.py\n"
    with pytest.raises(exporter.ExportBlocked, match="SOURCE_NOT_CLEAN"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_changed_committed_file_hash_is_refused(candidate):
    candidate["contents"]["README.md"] = b"CHANGED-COMMITTED-CONTENT"
    with pytest.raises(exporter.ExportBlocked, match="SOURCE_HASH_CHANGED"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_external_private_rules_are_mandatory_and_bound_to_this_manifest(candidate):
    candidate["rules_file"].unlink()
    with pytest.raises(exporter.ExportBlocked):
        run(candidate)
    candidate["save"]()
    data = json.loads(candidate["rules_file"].read_bytes())
    data["reviewed_manifest_sha256"] = "0" * 64
    candidate["rules_file"].write_text(json.dumps(data))
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_RULES_MISSING_OR_UNREVIEWED"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_private_rule_file_inside_source_is_refused(candidate):
    inside = candidate["source"] / "PRIVATE-operator-rules.json"
    inside.write_bytes(candidate["rules_file"].read_bytes())
    candidate["rules_file"] = inside
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_RULES_MUST_BE_EXTERNAL"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


@pytest.mark.parametrize("raw", [b"SYNTHETIC-DENIED-VALUE-ONLY", b"SYNTHETIC-\\u0044ENIED-VALUE-ONLY"])
def test_private_literal_hit_never_echoes_the_value_or_writes_an_archive(candidate, raw):
    add(candidate, "docs/public-example.txt", raw)
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_RULE_HIT") as caught:
        run(candidate)
    assert "SYNTHETIC-DENIED" not in str(caught.value)
    assert not list(candidate["directory"].iterdir())


def test_compressed_fixture_is_explicitly_reviewed_and_scanned_after_expansion(candidate):
    add(candidate, "arch4/tests/fixtures/synthetic.jsonl.gz", gzip.compress(b"SYNTHETIC-DENIED-VALUE-ONLY"), role="test_fixture")
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_RULE_HIT"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_private_key_shaped_payload_is_refused_without_an_actual_key(candidate):
    shaped = b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 64 + b"\n-----END PRIVATE KEY-----"
    add(candidate, "arch4/tests/fixtures/private-shaped.txt", shaped, role="test_fixture")
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_KEY_MATERIAL_DETECTED"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "arch4/../escape.py", "arch4\\escape.py", "C:stream", "."])
def test_traversal_or_ambiguous_paths_are_refused(candidate, path):
    candidate["manifest"]["entries"][0]["path"] = path
    candidate["save"]()
    with pytest.raises(exporter.ExportBlocked, match="UNSAFE_RELATIVE_PATH"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


@pytest.mark.parametrize("path", ["guc_memory/history.json", "portable_runtime/config.json", ".git/config", "docs/CGF_MUST_READ/private-runbook.md", "state/new_horizon.sqlite3-wal", "signing/owner.key"])
def test_runtime_history_git_private_documents_and_key_files_cannot_enter(candidate, path):
    add(candidate, path, b"SYNTHETIC-PRIVATE-STATE")
    with pytest.raises(exporter.ExportBlocked):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_git_symlink_mode_is_refused_even_if_the_working_file_looks_regular(candidate):
    candidate["state"]["modes"]["README.md"] = "120000"
    with pytest.raises(exporter.ExportBlocked, match="UNTRACKED_OR_NONREGULAR_INPUT"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_working_tree_symlink_is_refused_before_blob_read(candidate, tmp_path):
    outside = tmp_path / "outside-source.txt"
    outside.write_bytes(b"SYNTHETIC-OUTSIDE-SOURCE")
    selected = candidate["source"] / "README.md"
    selected.unlink()
    try:
        selected.symlink_to(outside)
    except OSError:
        pytest.skip("This native Windows account cannot create the synthetic test symlink")
    with pytest.raises(exporter.ExportBlocked, match="SOURCE_LINK_REFUSED"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_existing_output_is_never_replaced(candidate):
    candidate["output"].write_bytes(b"ORIGINAL-RELEASE")
    with pytest.raises(exporter.ExportBlocked, match="OUTPUT_MUST_BE_NEW_EXTERNAL_ZIP"):
        run(candidate)
    assert candidate["output"].read_bytes() == b"ORIGINAL-RELEASE"


def test_source_change_during_zip_work_rolls_back_only_its_temporary_file(candidate, monkeypatch):
    original = exporter._clean_commit
    calls = []
    def changed_at_publish(source, commit):
        calls.append(True)
        if len(calls) == 2:
            candidate["state"]["head"] = "2" * 40
        original(source, commit)
    monkeypatch.setattr(exporter, "_clean_commit", changed_at_publish)
    with pytest.raises(exporter.ExportBlocked, match="SOURCE_COMMIT_CHANGED"):
        run(candidate)
    assert len(calls) == 2 and not list(candidate["directory"].iterdir())


def test_rename_race_preserves_an_existing_output_and_removes_temp(candidate, monkeypatch):
    original = exporter.os.rename
    def raced(source, target):
        candidate["output"].write_bytes(b"OTHER-EXISTING-RELEASE")
        original(source, target)
    monkeypatch.setattr(exporter.os, "rename", raced)
    with pytest.raises(exporter.ExportBlocked, match="OUTPUT_NOT_PUBLISHED"):
        run(candidate)
    assert candidate["output"].read_bytes() == b"OTHER-EXISTING-RELEASE"
    assert list(candidate["directory"].iterdir()) == [candidate["output"]]



@pytest.mark.parametrize('name', ['public_source_manifest.json', 'Public_Source_Manifest.Json'])
def test_generated_manifest_name_is_reserved_in_every_windows_case(candidate, name):
    add(candidate, name, b'SYNTHETIC-UNAPPROVED-MANIFEST-LOOKALIKE')
    with pytest.raises(exporter.ExportBlocked, match='DUPLICATE_OR_RESERVED_PATH'):
        run(candidate)
    assert not list(candidate['directory'].iterdir())


@pytest.mark.parametrize('name', [
    'private/agents.md', 'config/.ENV.production',
    'Docs/MUST_READ/private-runbook.md', 'Docs/CGF_MUST_READ/private-runbook.md',
])
def test_private_path_rules_apply_in_every_windows_case(candidate, name):
    add(candidate, name, b'SYNTHETIC-PRIVATE-OPERATIONAL-INPUT')
    with pytest.raises(exporter.ExportBlocked):
        run(candidate)
    assert not list(candidate['directory'].iterdir())


def test_mixed_case_fixture_directory_still_requires_fixture_review(candidate):
    add(candidate, 'arch4/tests/Fixtures/neutral.json', b'{}', role='resource')
    with pytest.raises(exporter.ExportBlocked, match='DATA_FIXTURE_REVIEW_REQUIRED'):
        run(candidate)
    assert not list(candidate['directory'].iterdir())


@pytest.mark.parametrize('data', [
    b'BZh9' + b'SYNTHETIC-OPAQUE-DATA',
    b'\xfd7zXZ\x00' + b'SYNTHETIC-OPAQUE-DATA',
    b'\x28\xb5\x2f\xfd' + b'SYNTHETIC-OPAQUE-DATA',
    b'7z\xbc\xaf\x27\x1c' + b'SYNTHETIC-OPAQUE-DATA',
    b'Rar!\x1a\x07\x00' + b'SYNTHETIC-OPAQUE-DATA',
    b'Rar!\x1a\x07\x01\x00' + b'SYNTHETIC-OPAQUE-DATA',
    b'\x00' * 257 + b'ustar\x00' + b'\x00' * 249,
])
@pytest.mark.parametrize('role', ['resource', 'test_fixture'])
def test_known_unsupported_container_is_refused_even_with_a_neutral_filename(candidate, data, role):
    add(candidate, 'examples/opaque-synthetic.dat', data, role=role)
    with pytest.raises(exporter.ExportBlocked, match='UNSUPPORTED_COMPRESSED_CONTAINER'):
        run(candidate)
    assert not list(candidate['directory'].iterdir())


@pytest.mark.parametrize('directory', [False, True])
def test_decoded_legacy_zip_member_name_is_checked_for_private_literals(candidate, directory):
    # ASCII placeholder preserves all header lengths when replaced by one
    # legacy code-page byte. It is synthetic, never an actual private name.
    from io import BytesIO
    import zipfile
    placeholder = b'SYNTHETIC-X-ONLY' + (b'/' if directory else b'.txt')
    encoded = b'SYNTHETIC-\x82-ONLY' + (b'/' if directory else b'.txt')
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(placeholder.decode('ascii'), b'' if directory else b'SYNTHETIC-SAFE-CONTENT')
    wire = stream.getvalue().replace(placeholder, encoded)
    with zipfile.ZipFile(BytesIO(wire)) as archive:
        assert 'SYNTHETIC-\u00e9-ONLY' in archive.namelist()[0]
    add(candidate, 'arch4/tests/fixtures/synthetic-legacy-name.zip', wire, role='test_fixture')
    rules = json.loads(candidate['rules_file'].read_bytes())
    rules['literal_denylist'].append('SYNTHETIC-\u00e9-ONLY')
    candidate['rules_file'].write_text(json.dumps(rules), encoding='utf-8')
    with pytest.raises(exporter.ExportBlocked, match='PRIVATE_RULE_HIT') as caught:
        run(candidate)
    assert 'SYNTHETIC-\u00e9-ONLY' not in str(caught.value)
    assert not list(candidate['directory'].iterdir())


def test_zip_fixture_members_cannot_collide_on_windows(candidate):
    from io import BytesIO
    import zipfile
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('Example.txt', b'SYNTHETIC-FIRST')
        archive.writestr('example.TXT', b'SYNTHETIC-SECOND')
    add(candidate, 'arch4/tests/fixtures/synthetic-case-collision.zip', stream.getvalue(), role='test_fixture')
    with pytest.raises(exporter.ExportBlocked, match='DUPLICATE_FIXTURE_PATH'):
        run(candidate)
    assert not list(candidate['directory'].iterdir())


def test_container_lookalike_text_away_from_header_is_not_rejected(candidate):
    add(candidate, 'docs/public-container-description.txt', b'Example compression markers: BZh9 and 7z are discussed here.')
    assert run(candidate)['status'] == 'SOURCE_ARCHIVE_CREATED'


def test_a_text_filename_is_not_mistaken_for_compressed_file_bytes(candidate):
    add(candidate, 'BZh9-format-notes.txt', b'SYNTHETIC-PLAIN-TEXT')
    assert run(candidate)['status'] == 'SOURCE_ARCHIVE_CREATED'


@pytest.mark.parametrize("missing", [
    "docs/BUILDING.md", "docs/TESTING.md", "docs/PROTOCOL_REFERENCES.md",
])
def test_readme_entry_document_cannot_be_missing_from_reviewed_allowlist(candidate, missing):
    assert missing in candidate["contents"]
    candidate["manifest"]["entries"] = [
        entry for entry in candidate["manifest"]["entries"] if entry["path"] != missing
    ]
    candidate["save"]()
    with pytest.raises(exporter.ExportBlocked, match="MANDATORY_ALLOWLIST_INPUT_MISSING"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


@pytest.mark.parametrize("missing", [
    "docs/BUILDING.md", "docs/TESTING.md", "docs/PROTOCOL_REFERENCES.md",
])
def test_readme_entry_document_cannot_be_missing_from_tracked_snapshot(candidate, missing):
    candidate["contents"].pop(missing)
    candidate["state"]["modes"].pop(missing)
    (candidate["source"] / missing).unlink()
    candidate["manifest"]["entries"] = [
        entry for entry in candidate["manifest"]["entries"] if entry["path"] != missing
    ]
    candidate["save"]()
    with pytest.raises(exporter.ExportBlocked, match="MANDATORY_SOURCE_INPUT_MISSING"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


def test_complete_snapshot_keeps_public_entry_documents_and_their_manifest_receipts(candidate):
    result = run(candidate)
    assert result["status"] == "SOURCE_ARCHIVE_CREATED"
    with exporter.zipfile.ZipFile(candidate["output"]) as archive:
        record = json.loads(archive.read("PUBLIC_SOURCE_MANIFEST.json"))
        entries = {entry["path"]: entry for entry in record["entries"]}
        readme = archive.read("README.md")
        for path in (
            "docs/BUILDING.md", "docs/TESTING.md", "docs/PROTOCOL_REFERENCES.md",
        ):
            assert ("(" + path + ")").encode("ascii") in readme
            assert archive.read(path) == candidate["contents"][path]
            assert entries[path]["role"] == "public_document"
            assert entries[path]["bytes"] == len(candidate["contents"][path])
            assert entries[path]["sha256"] == hashlib.sha256(candidate["contents"][path]).hexdigest()
        assert archive.testzip() is None


def _make_real_git_candidate(candidate, monkeypatch):
    """Use actual native Git/committed bytes, without real project content."""
    import subprocess
    monkeypatch.undo()
    def git(*args):
        return subprocess.check_output(
            ['git', '-C', str(candidate['source']), *args], stderr=subprocess.STDOUT)
    git('init', '-q')
    git('config', 'core.autocrlf', 'false')
    git('config', 'user.name', 'Synthetic export test')
    git('config', 'user.email', 'export-test@example.invalid')
    git('add', '--all')
    git('commit', '-q', '-m', 'Synthetic source boundary')
    commit = git('rev-parse', 'HEAD').decode().strip()
    candidate['commit'] = commit
    candidate['manifest']['source_commit'] = commit
    candidate['save']()
    return git


def test_native_git_cli_exports_exact_committed_bytes_without_private_history(candidate, monkeypatch):
    git = _make_real_git_candidate(candidate, monkeypatch)
    result = run(candidate)
    assert result['status'] == 'SOURCE_ARCHIVE_CREATED'
    with exporter.zipfile.ZipFile(candidate['output']) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(candidate['contents']) | {'PUBLIC_SOURCE_MANIFEST.json'}
        for path, expected in candidate['contents'].items():
            assert archive.read(path) == expected
        assert all('.git' not in exporter.PurePosixPath(name).parts for name in archive.namelist())
        assert json.loads(archive.read('PUBLIC_SOURCE_MANIFEST.json'))['source_commit'] == candidate['commit']
    assert git('status', '--porcelain') == b''


def test_native_git_cli_dirty_tree_blocks_export_before_output(candidate, monkeypatch):
    git = _make_real_git_candidate(candidate, monkeypatch)
    (candidate['source'] / 'README.md').write_bytes(b'UNCOMMITTED-SYNTHETIC-CHANGE\n')
    assert git('status', '--porcelain')
    with pytest.raises(exporter.ExportBlocked, match='SOURCE_NOT_CLEAN'):
        run(candidate)
    assert list(candidate['directory'].iterdir()) == []

@pytest.mark.parametrize("form", ["router-test.yml", "profile-proposal.yml", "bug-report.yml"])
def test_only_reviewed_contributor_forms_can_be_exported(candidate, form):
    path = ".github/ISSUE_TEMPLATE/" + form
    raw = b"name: Synthetic router report\ndescription: Neutral fixture only\nbody: []\n"
    add(candidate, path, raw, role="public_document")
    run(candidate)
    with exporter.zipfile.ZipFile(candidate["output"]) as archive:
        assert archive.read(path) == raw
        assert archive.testzip() is None


@pytest.mark.parametrize("path", [
    ".github/workflows/release.yml",
    ".github/ISSUE_TEMPLATE/unreviewed.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/ROUTER-TEST.yml",
    ".github/private/operator.md",
    ".github/actions/publish/action.yml",
])
def test_reviewed_forms_do_not_allow_other_github_operational_files(candidate, path):
    add(candidate, path, b"SYNTHETIC-NEUTRAL-OPERATIONAL-FIXTURE\n", role="public_document")
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_OPERATIONAL_PATH"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())


@pytest.mark.parametrize("form", ["router-test.yml", "profile-proposal.yml", "bug-report.yml"])
def test_reviewed_contributor_forms_keep_private_literal_scanning(candidate, form):
    add(candidate, ".github/ISSUE_TEMPLATE/" + form, b"SYNTHETIC-DENIED-VALUE-ONLY\n", role="public_document")
    with pytest.raises(exporter.ExportBlocked, match="PRIVATE_RULE_HIT"):
        run(candidate)
    assert not list(candidate["directory"].iterdir())
