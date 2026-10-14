"""Secret redaction and prompt-injection detection.

Everything that leaves the analyzer (evidence values, sources, signals, warnings, paths) goes through
``redact_obj``. Text read from a project is DATA: ``find_injections`` only reports instruction-looking
text so the caller can warn about it; nothing in a file can change what the analyzer does.
"""

from __future__ import annotations

import math
import re
from typing import Any

REDACTED = "[REDACTED]"

# (label, pattern). Order matters only for labelling; every pattern is applied.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN[A-Z0-9 ]*(?:PRIVATE KEY|PRIVATE KEY BLOCK)-----"
                                     r"(?:[\s\S]*?-----END[A-Z0-9 ]*-----)?")),
    ("huggingface token", re.compile(r"\bhf_[A-Za-z0-9]{16,}")),
    ("openai/anthropic style key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("stripe key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("wandb/replicate style key", re.compile(r"\b(?:r8|wandb)_[A-Za-z0-9]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("url credentials", re.compile(r"(?<=://)[^/\s:@]{1,64}:[^/\s@]{1,128}(?=@)")),
    ("long hex token", re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,}(?![A-Za-z0-9])")),
]

# key = value / key: value where the key names a secret. Only the value is replaced.
_ASSIGNMENT = re.compile(
    r"(?i)\b((?:[A-Za-z0-9]+[_\-])*(?:password|passwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|"
    r"secret[_\-]?key|private[_\-]?key|client[_\-]?secret))\b(\s*[:=]\s*)(['\"]?)([^\s'\",;)]{6,})(\3)")


def _is_placeholder(value: str, quoted: bool = False) -> bool:
    lowered = value.lower()
    # an unquoted identifier/attribute reference (HF_TOKEN, tokenizer.eos_token, args.hub_token) is code, not a
    # literal secret
    if (not quoted and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value) and not re.search(r"\d{3}", value)
            and len(value) < 40 and ("." in value or value.isupper() or "_" in value)):
        return True
    return (value.startswith(("$", "{", "<", "os.", "env", "[", "("))
            or lowered in {"none", "null", "true", "false", "required", "optional", "changeme"}
            or "environ" in lowered or "getenv" in lowered or value.endswith("(") or REDACTED in value)

_B64ISH = re.compile(r"(?<![A-Za-z0-9+/=_\-.])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=_\-.])")


def _entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_random(tok: str) -> bool:
    digits = sum(c.isdigit() for c in tok)
    upper = sum(c.isupper() for c in tok)
    lower = sum(c.islower() for c in tok)
    return digits >= 3 and upper >= 3 and lower >= 3 and _entropy(tok) >= 4.0


def find_secrets(text: str) -> list[tuple[int, str]]:
    """Return (line_number, label) for secret-looking values. Never returns the value itself."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            line = line[:4000]
        labels = []
        for label, pat in _SECRET_PATTERNS:
            if label == "long hex token":
                continue  # hashes/commit pins are common in manifests; only redacted, not warned
            if pat.search(line):
                labels.append(label)
        m = _ASSIGNMENT.search(line)
        if m and not labels:
            if not _is_placeholder(m.group(4), bool(m.group(3))):
                labels.append("credential assignment")
        if not labels:
            for tok in _B64ISH.findall(line):
                if _looks_random(tok):
                    labels.append("high-entropy token")
                    break
        for label in dict.fromkeys(labels):
            hits.append((lineno, label))
    return hits


def redact(s: str) -> str:
    """Replace every secret-looking substring of ``s`` with [REDACTED]."""
    if not s:
        return s
    out = s
    for _label, pat in _SECRET_PATTERNS:
        out = pat.sub(REDACTED, out)
    out = _ASSIGNMENT.sub(lambda m: m.group(0) if _is_placeholder(m.group(4), bool(m.group(3)))
                          else f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(5)}", out)
    out = _B64ISH.sub(lambda m: REDACTED if _looks_random(m.group(0)) else m.group(0), out)
    return out


def is_secret_like(s: str) -> bool:
    return isinstance(s, str) and redact(s) != s


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string (keys included) in a JSON-like structure."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


# --------------------------------------------------------------------------------------------------
# Prompt injection

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], bool]] = [
    # (label, pattern, readme_only)
    ("ignore previous instructions", re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,30}\b(?:previous|prior|above|earlier|preceding|"
        r"all|your|the)\b[^.\n]{0,20}\b(?:instructions?|prompts?|rules|guidelines|context)\b"), False),
    ("addresses an AI agent", re.compile(
        r"(?i)\b(?:note to|attention|dear|hey|if you are an?|you are now an?|you are an?)\s+"
        r"(?:ai|llm|language model|assistant|agent|claude|chatgpt|gpt|copilot|bot)s?\b"), False),
    ("fake system/assistant tag", re.compile(r"(?i)<\s*/?\s*(?:system|assistant|instructions?|im_start)\s*>"), False),
    ("new instructions header", re.compile(r"(?i)\b(?:new|updated|system|hidden|secret)\s+(?:instructions?|prompt)\s*:"), False),
    ("asks to exfiltrate secrets", re.compile(
        r"(?i)\b(?:upload|send|exfiltrate|post|copy|transmit|share|leak|cat|print|read|include)\b[^\n]{0,80}"
        r"(?:~/\.ssh|\bid_rsa\b|(?<![\w/])\.env\b|~/\.aws\b|\.netrc\b|private keys?\b|ssh keys?\b)"), False),
    ("asks to exfiltrate secrets", re.compile(
        r"(?i)\b(?:upload|exfiltrate|leak)\b[^\n]{0,60}\b(?:api[_ ]?keys?|secrets?|tokens?|credentials|passwords?)\b"),
     False),
    ("asks to run a command", re.compile(
        r"(?i)\b(?:run|execute|eval)\s+(?:this|the following|these)\s+(?:commands?|scripts?|code)\b"), False),
    ("destructive command", re.compile(r"(?i)\brm\s+-rf\s+(?:~|/|\$HOME)"), False),
    ("hides from the user", re.compile(r"(?i)\bdo not (?:tell|inform|alert|show|notify) the user\b"), False),
    ("download and execute", re.compile(r"(?i)\b(?:curl|wget)\b[^\n|]{0,200}\|\s*(?:ba|z)?sh\b"), True),
]


def find_injections(text: str, *, is_readme: bool = False) -> list[tuple[int, str]]:
    """Return (line_number, label) for text that looks like instructions aimed at an AI agent."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            line = line[:4000]
        for label, pat, readme_only in _INJECTION_PATTERNS:
            if readme_only and not is_readme:
                continue
            if pat.search(line):
                hits.append((lineno, label))
                break
    return hits
