"""One read-only reader for the current support ZIP. No extraction or repair."""
import json
import math
from pathlib import Path, PurePosixPath
import stat
import zipfile

from arch4.support_evidence import OWNER, SNAPSHOT_SCHEMA, validate_support_records

MANIFEST_SCHEMA = "FoundationLightSupportBundleManifest.v2"
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


def read_support_archive(path):
    with zipfile.ZipFile(Path(path)) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        if len(names) > 256 or len(names) != len(set(names)):
            raise ValueError("support_archive_member_count_invalid")
        if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError("support_archive_expanded_limit")
        for info in infos:
            name = info.filename
            parts = PurePosixPath(name).parts
            if (not name or "\\" in name or name.startswith("/") or ":" in name
                    or any(p in {"", ".", ".."} for p in name.split("/"))
                    or not parts or info.flag_bits & 1
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.file_size > MAX_MEMBER_BYTES):
                raise ValueError("support_archive_member_invalid")
        files = {i.filename: archive.read(i.filename) for i in infos}  # ZIP CRC checked here.
    manifest = json.loads(files["bundle_manifest.json"])
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("owner") != OWNER:
        raise ValueError("support_archive_version_unsupported")
    rows = manifest["archive_contract"]["members"]
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("support_archive_inventory_invalid")
    expected = {r["name"]: r for r in rows}
    if len(expected) != len(rows) or set(files) != set(expected) | {"bundle_manifest.json"}:
        raise ValueError("support_archive_member_set_mismatch")
    for name, row in expected.items():
        if set(row) != {"name", "size_bytes"} or len(files[name]) != row["size_bytes"]:
            raise ValueError("support_archive_member_size_mismatch")
        if name.endswith(".json"):
            value = json.loads(files[name])
            if name.endswith("/foundation_evidence_snapshot.json"):
                if not isinstance(value, dict) or value.get("schema") != SNAPSHOT_SCHEMA or value.get("owner") != OWNER:
                    raise ValueError("support_snapshot_version_unsupported")
                if "content_sha256" in value or "snapshot_id" in value:
                    raise ValueError("support_snapshot_runtime_seal_forbidden")
                captured = value.get("captured_at")
                if (type(captured) not in (float, int) or not math.isfinite(captured) or captured <= 0
                        or type(value.get("complete")) is not bool
                        or not isinstance(value.get("events"), list)
                        or not isinstance(value.get("omissions"), list)
                        or (value["complete"] and (value["omissions"] or value.get("current_state") is None
                                                   or value.get("runtime_health") is None))):
                    raise ValueError("support_snapshot_coverage_invalid")
            validate_support_records(value)
    return manifest, files
