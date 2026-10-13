"""Ephemeral SSH keys: when a job has no customer key, the provider still needs exactly one public key
(Lambda: "exactly one SSH key must be specified"). OpenGrid generates an ed25519 pair, keeps ONLY the public
key and its SHA256 fingerprint, and discards the private key immediately (it is never written, logged or
returned). Nobody, OpenGrid included, can log in to such a machine: the job runs unattended and results
come back through the runner callbacks.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NOTE = ("ephemeral key: OpenGrid generated an ed25519 key pair for this job and discarded the private half "
        "immediately; nobody (including OpenGrid) can SSH into the machine")


def generate(comment: str = "opengrid-ephemeral") -> dict:
    """{public_key, fingerprint, mode: 'ephemeral', note}. The private key never leaves this function."""
    from routing import credentials

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    del priv   # dropped: no reference survives this call, nothing serialised it
    line = f"{pub} {comment}"
    parsed = credentials.parse_public_key(line)
    return {"public_key": parsed["public_key"], "fingerprint": parsed["fingerprint"], "mode": "ephemeral",
            "note": NOTE}
