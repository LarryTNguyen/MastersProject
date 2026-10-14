"""Deterministic ID helpers.

OTLP needs 32-hex-character trace IDs and 16-hex-character span IDs. Sources
like Alibaba use their own ID formats, so we hash them into valid OTLP IDs and
keep the original value in an attribute so nothing is lost.
"""
import hashlib
import re

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def is_otlp_trace_id(value: str) -> bool:
    return bool(value) and bool(_HEX32.match(value)) and value != "0" * 32


def is_otlp_span_id(value: str) -> bool:
    return bool(value) and bool(_HEX16.match(value)) and value != "0" * 16


def hashed_trace_id(value: str) -> str:
    return hashlib.blake2b(("trace|" + value).encode("utf-8"), digest_size=16).hexdigest()


def hashed_span_id(*parts: str) -> str:
    return hashlib.blake2b(("span|" + "|".join(parts)).encode("utf-8"), digest_size=8).hexdigest()


def otlp_trace_id(value: str) -> str:
    v = value.lower()
    return v if is_otlp_trace_id(v) else hashed_trace_id(value)


def otlp_span_id(value: str) -> str:
    v = value.lower()
    return v if is_otlp_span_id(v) else hashed_span_id(value)
