"""Disposable external-command KMS for engine integration, never production use."""
import base64
import json
import os
from pathlib import Path
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag


def decoded(value):
    return base64.b64decode(value, validate=True)


def main():
    request = json.load(sys.stdin)
    if (request.get("crypto_key_id") != os.environ["TANDEM_MEMORY_KEK_ID"]
            or request.get("kek_version") != os.environ["TANDEM_MEMORY_KEK_VERSION"]
            or request.get("runtime_principal_id") != os.environ["TANDEM_MEMORY_DECRYPT_PRINCIPAL_ID"]):
        raise ValueError("wrong fixture key scope")
    key = Path(__file__).with_name("memory-kms-key").read_bytes()
    aad = decoded(request["additional_authenticated_data_base64"])
    cipher = AESGCM(key)
    if "plaintext_base64" in request:
        nonce = os.urandom(12)
        wrapped = nonce + cipher.encrypt(nonce, decoded(request["plaintext_base64"]), aad)
        output = {"wrapped_dek_base64": base64.b64encode(wrapped).decode(),
                  "kek_version": request["kek_version"]}
    else:
        wrapped = decoded(request["ciphertext_base64"])
        output = {"plaintext_base64": base64.b64encode(
            cipher.decrypt(wrapped[:12], wrapped[12:], aad)).decode(),
            "kek_version": request["kek_version"]}
    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, InvalidTag):
        raise SystemExit(1) from None
