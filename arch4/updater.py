"""Check the declared public release feed for a newer application version.

Only version comparison through the build-carried update-channel descriptor
is implemented. Downloading and applying updates are not implemented here.
The module contains no GUI imports.
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit

import requests
from arch4.versioning import parse_version_key

log = logging.getLogger(__name__)

UPDATE_CHANNEL_FILENAME = "UPDATE_CHANNEL.json"
UPDATE_CHANNEL_SCHEMA = "54GUCUpdateChannel.v1"
UPDATE_CHANNEL_OWNER = "54guc.release"
UPDATE_CHANNEL_PUBLISHED = "published"
UPDATE_CHANNEL_UNPUBLISHED = "unpublished"

_UPDATE_CHANNEL_KEYS = frozenset(
    {"schema", "owner", "status", "release_api_url", "message"}
)
_GITHUB_LATEST_RELEASE_PATH = re.compile(
    r"^/repos/[^/]+/[^/]+/releases/latest$"
)


@dataclass(frozen=True, slots=True)
class UpdateChannel:
    status: str
    release_api_url: str
    message: str


def _validate_public_release_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or _GITHUB_LATEST_RELEASE_PATH.fullmatch(parsed.path) is None
    ):
        raise ValueError("update_channel_public_release_url_invalid")
    return url


def load_update_channel(app_root: str | Path) -> UpdateChannel:
    """Load the sole strict release-source descriptor carried by the build."""

    path = Path(app_root).resolve() / UPDATE_CHANNEL_FILENAME
    if not path.is_file():
        raise RuntimeError("update_channel_descriptor_missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"update_channel_descriptor_corrupt:{type(exc).__name__}"
        ) from exc
    return validate_update_channel_payload(payload)


def validate_update_channel_payload(payload: object) -> UpdateChannel:
    """Validate one decoded build descriptor without consulting another source."""

    if not isinstance(payload, dict):
        raise RuntimeError("update_channel_descriptor_not_object")
    keys = frozenset(str(key) for key in payload)
    if keys != _UPDATE_CHANNEL_KEYS:
        raise RuntimeError("update_channel_descriptor_keys_invalid")
    if (
        payload.get("schema") != UPDATE_CHANNEL_SCHEMA
        or payload.get("owner") != UPDATE_CHANNEL_OWNER
    ):
        raise RuntimeError("update_channel_descriptor_owner_invalid")
    status = str(payload.get("status") or "").strip().lower()
    release_api_url = str(payload.get("release_api_url") or "").strip()
    message = str(payload.get("message") or "").strip()
    if status == UPDATE_CHANNEL_UNPUBLISHED:
        if release_api_url or not message:
            raise RuntimeError("update_channel_unpublished_contract_invalid")
        return UpdateChannel(
            status=status,
            release_api_url="",
            message=message,
        )
    if status == UPDATE_CHANNEL_PUBLISHED:
        try:
            release_api_url = _validate_public_release_url(release_api_url)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return UpdateChannel(
            status=status,
            release_api_url=release_api_url,
            message=message,
        )
    raise RuntimeError("update_channel_status_invalid")


@dataclass(frozen=True)
class UpdateInfo:
    """Information about an available update."""
    tag_name: str
    html_url: str
    download_url: str
    body: str


def _parse_version(tag: str) -> Tuple[int, ...]:
    """Public wrapper over the shared 54GUC version parser."""
    return parse_version_key(tag)


def check_for_update(
    current_version: str,
    *,
    channel: UpdateChannel,
    timeout_s: float = 10.0,
) -> Optional[UpdateInfo]:
    """Check the declared public release for a newer application version.

    Return UpdateInfo when a newer release exists and None when already up
    to date. Network and parse failures propagate to the caller.

    current_version is the running version string. channel must be the exact
    validated public update descriptor; unpublished channels are rejected.
    timeout_s is the HTTP request timeout in seconds.
    """
    if type(channel) is not UpdateChannel:
        raise TypeError("typed_update_channel_required")
    if channel.status != UPDATE_CHANNEL_PUBLISHED:
        raise RuntimeError("update_channel_not_published")
    release_url = _validate_public_release_url(channel.release_api_url)
    headers = {"Accept": "application/vnd.github+json"}

    log.info("Checking for updates at %s", release_url)
    resp = requests.get(release_url, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    data = resp.json()

    remote_tag = str(data.get("tag_name", "") or "").strip()
    if not remote_tag:
        log.warning("GitHub release has no tag_name")
        return None

    current_tuple = _parse_version(current_version)
    remote_tuple = _parse_version(remote_tag)

    log.info(
        "Version check: current=%s (%s) remote=%s (%s)",
        current_version, current_tuple, remote_tag, remote_tuple,
    )

    if remote_tuple <= current_tuple:
        return None

    # Find the first .exe asset, if any
    download_url = ""
    for asset in data.get("assets", []):
        name = str(asset.get("name", "") or "")
        if name.lower().endswith(".exe"):
            download_url = str(asset.get("browser_download_url", "") or "")
            break

    return UpdateInfo(
        tag_name=remote_tag,
        html_url=str(data.get("html_url", "") or ""),
        download_url=download_url,
        body=str(data.get("body", "") or "")[:500],
    )


__all__ = [
    "UPDATE_CHANNEL_FILENAME",
    "UPDATE_CHANNEL_OWNER",
    "UPDATE_CHANNEL_PUBLISHED",
    "UPDATE_CHANNEL_SCHEMA",
    "UPDATE_CHANNEL_UNPUBLISHED",
    "UpdateChannel",
    "UpdateInfo",
    "check_for_update",
    "load_update_channel",
    "validate_update_channel_payload",
]
