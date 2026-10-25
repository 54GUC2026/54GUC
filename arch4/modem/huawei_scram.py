"""Pure selected Huawei challenge codec; no HTTP, sessions or dialect discovery."""
from __future__ import annotations

import hashlib
import hmac
import re
from xml.etree import ElementTree as ET


CONTRACT = "hilink_scram_sha256_c983_assumption_v1"


def response_fields(body: str) -> dict[str, str]:
    if not isinstance(body, str) or len(body.encode("utf-8")) > 16384 or "<!" in body:
        raise ValueError("huawei_scram_invalid_response")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError("huawei_scram_invalid_response") from exc
    if root.tag != "response" or any(len(node) for node in root):
        raise ValueError("huawei_scram_invalid_response")
    result: dict[str, str] = {}
    for node in root:
        if node.tag in result:
            raise ValueError("huawei_scram_duplicate_response_field")
        result[node.tag] = node.text or ""
    return result


def request_xml(fields: dict[str, str]) -> str:
    root = ET.Element("request")
    for tag, value in fields.items():
        ET.SubElement(root, tag).text = value
    return ET.tostring(root, encoding="unicode")


def proofs(password: str, firstnonce: str, challenge: dict[str, str]) -> tuple[str, str, str]:
    """Huawei's HMAC argument ordering differs from ordinary RFC SCRAM."""
    salt = challenge.get("salt", "")
    iterations = challenge.get("iterations", "")
    nonce = challenge.get("servernonce", "")
    if (not re.fullmatch(r"[0-9a-f]{64}", firstnonce)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,256}", salt)
            or not re.fullmatch(r"[0-9]{1,6}", iterations)
            or not 1 <= int(iterations) <= 200000
            or not re.fullmatch(r"[A-Za-z0-9+/=_-]{65,512}", nonce)
            or not nonce.startswith(firstnonce)
            or challenge.get("modeselected") != "1"):
        raise ValueError("huawei_scram_unqualified_challenge")
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
    client_key = hmac.digest(b"Client Key", salted, "sha256")
    server_key = hmac.digest(b"Server Key", salted, "sha256")
    message = f"{firstnonce},{nonce},{nonce}".encode("ascii")
    signature = hmac.digest(message, hashlib.sha256(client_key).digest(), "sha256")
    client_proof = bytes(a ^ b for a, b in zip(client_key, signature)).hex()
    return client_proof, hmac.digest(message, server_key, "sha256").hex(), nonce


def verify_server(body: str, expected: str) -> None:
    proof = response_fields(body).get("serversignature", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", proof) or not hmac.compare_digest(proof.lower(), expected):
        raise ValueError("huawei_scram_server_proof_not_confirmed")
