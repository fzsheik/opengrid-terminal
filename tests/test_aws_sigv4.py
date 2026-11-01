"""SigV4 signer (providers/aws_sigv4.py) against AWS's own published example vectors. No network.

Vectors:
  * signing key: IAM User Guide, "Create a signed AWS API request" example (secret wJalr...EXAMPLEKEY,
    20150830, us-east-1, iam) -> c4afb1cc...4a4b9
  * GET https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08 with
    Content-Type: application/x-www-form-urlencoded; charset=utf-8 at 20150830T123600Z -> 5d672d79...b5d7
  * aws-sig-v4-test-suite "get-vanilla": GET / on example.amazonaws.com, service "service" -> 5fa00fa3...bf31

Run:  .venv/bin/python tests/test_aws_sigv4.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.aws_sigv4 import canonical_query, canonical_request, sign, signing_key  # noqa: E402

KEY_ID, SECRET = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
WHEN = datetime(2015, 8, 30, 12, 36, tzinfo=timezone.utc)


def test_signing_key():
    assert signing_key(SECRET, "20150830", "us-east-1", "iam").hex() == \
        "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


def test_iam_list_users():
    h = sign("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08", "us-east-1", "iam",
             KEY_ID, SECRET, headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, now=WHEN)
    assert h["X-Amz-Date"] == "20150830T123600Z"
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")


def test_get_vanilla():
    h = sign("GET", "https://example.amazonaws.com/", "us-east-1", "service", KEY_ID, SECRET, now=WHEN)
    assert h["Authorization"].endswith(
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


def test_canonical_parts():
    # Sorted by name, RFC 3986 encoding (space -> %20, never "+"), unreserved characters kept.
    assert canonical_query("b=2&a=x y&c=~-_.&a=1") == "a=1&a=x%20y&b=2&c=~-_."
    creq, signed = canonical_request("post", "https://ec2.us-east-1.amazonaws.com/",
                                     {"Host": "ec2.us-east-1.amazonaws.com", "X-Amz-Date": "20150830T123600Z",
                                      "Content-Type": "a  b"}, "e3b0")
    assert signed == "content-type;host;x-amz-date"
    assert creq.split("\n")[:4] == ["POST", "/", "", "content-type:a b"], "method upper, path /, spaces folded"
    # A body changes the signature (the payload hash is signed); the secret never appears in the headers.
    a = sign("POST", "https://ec2.us-east-1.amazonaws.com/", "us-east-1", "ec2", KEY_ID, SECRET, body=b"x", now=WHEN)
    b = sign("POST", "https://ec2.us-east-1.amazonaws.com/", "us-east-1", "ec2", KEY_ID, SECRET, body=b"y", now=WHEN)
    assert a["Authorization"] != b["Authorization"] and SECRET not in str(a)


if __name__ == "__main__":
    for t in (test_signing_key, test_iam_list_users, test_get_vanilla, test_canonical_parts):
        t()
        print(t.__name__, "ok")
    print("all passed")
