#!/usr/bin/env python3
"""Validated runtime admission limits for implementation workers."""
from __future__ import annotations
import os,re

ENV_WORKER_LIMIT="V2_MAX_CONCURRENT_IMPLEMENTATION_WORKERS"
DEFAULT_IMPLEMENTATION_WORKER_LIMIT=3
MIN_IMPLEMENTATION_WORKER_LIMIT=1
MAX_IMPLEMENTATION_WORKER_LIMIT=8

class SchedulerPolicyError(RuntimeError):
    pass

def implementation_worker_limit(environ=None):
    env=os.environ if environ is None else environ
    raw=str(env.get(ENV_WORKER_LIMIT,"") or "").strip()
    if not raw:
        return DEFAULT_IMPLEMENTATION_WORKER_LIMIT
    if not re.fullmatch(r"[1-9][0-9]*",raw):
        raise SchedulerPolicyError(
            f"{ENV_WORKER_LIMIT} must be a decimal integer in "
            f"{MIN_IMPLEMENTATION_WORKER_LIMIT}..{MAX_IMPLEMENTATION_WORKER_LIMIT}"
        )
    value=int(raw)
    if not MIN_IMPLEMENTATION_WORKER_LIMIT <= value <= MAX_IMPLEMENTATION_WORKER_LIMIT:
        raise SchedulerPolicyError(
            f"{ENV_WORKER_LIMIT}={value} outside "
            f"{MIN_IMPLEMENTATION_WORKER_LIMIT}..{MAX_IMPLEMENTATION_WORKER_LIMIT}"
        )
    return value

def implementation_worker_limit_source(environ=None):
    env=os.environ if environ is None else environ
    raw=str(env.get(ENV_WORKER_LIMIT,"") or "").strip()
    return "environment" if raw else "default"
