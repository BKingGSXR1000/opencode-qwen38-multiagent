#!/usr/bin/env python3
from __future__ import annotations

import re

CHECK_RE = re.compile(r"^- \[ \] (A\d{3}):\s+\S.*$")
MUST_HEADING_RE = re.compile(r"^#{2,6}\s+MUST(?:\s+checks)?\s*$", re.I)
HEADING_RE = re.compile(r"^#{1,6}\s+\S")


def must_acceptance_ids(text: str) -> list[str]:
    """Return checkbox Axxx IDs from the MUST section, with legacy fallback."""
    lines = str(text or "").splitlines()
    start = next((i for i, raw in enumerate(lines) if MUST_HEADING_RE.fullmatch(raw.strip())), None)
    scan = lines if start is None else lines[start + 1 :]
    result = []
    for raw in scan:
        line = raw.strip()
        if start is not None and HEADING_RE.match(line):
            break
        match = CHECK_RE.fullmatch(line)
        if match:
            result.append(match.group(1))
    return result
