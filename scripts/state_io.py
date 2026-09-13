#!/usr/bin/env python3
"""Crash-safe primitives for canonical V2 control-plane state."""
from __future__ import annotations
import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time


class StateCorruptionError(RuntimeError):
    pass


_MISSING=object()


def load_json_object(path, *, default_missing=_MISSING, label="state"):
    path=Path(path)
    try:
        raw=path.read_text()
    except FileNotFoundError:
        if default_missing is _MISSING:
            raise StateCorruptionError(f"{label} missing: {path}")
        return copy.deepcopy(default_missing)
    except OSError as exc:
        raise StateCorruptionError(f"{label} unreadable: {path}: {exc}") from exc
    try:
        data=json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorruptionError(
            f"{label} malformed JSON: {path}: line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(data,dict):
        raise StateCorruptionError(f"{label} must be a JSON object: {path}")
    return data


def atomic_write_text(path, text):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=str(path.parent))
    tmp_path=Path(tmp)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path,path)
        try:
            dirfd=os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path, data):
    atomic_write_text(path,json.dumps(data,indent=2,sort_keys=True)+"\n")


@contextlib.contextmanager
def exclusive_file_lock(path, timeout=5.0, poll=0.02):
    """Process-crash-safe advisory lock.

    The lock file may remain forever; the kernel lock itself is released
    automatically when a process exits, so stale files cannot deadlock V2.
    """
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    deadline=time.monotonic()+timeout
    acquired=False
    try:
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                acquired=True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"state_lock_timeout: {path}")
                time.sleep(poll)
        try:
            os.ftruncate(fd,0)
            os.write(fd,f"pid={os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd,fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
