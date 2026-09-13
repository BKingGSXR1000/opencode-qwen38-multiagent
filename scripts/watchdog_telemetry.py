#!/usr/bin/env python3
"""Backend/GPU telemetry and pure watchdog decisions for V2."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

INVISIBLE_BASE_SECONDS = 600
INVISIBLE_SHARED_EXTENSION_SECONDS = 900
INVISIBLE_EXCLUSIVE_EXTENSION_SECONDS = 1800
INVISIBLE_QUEUE_EXTENSION_SECONDS = 1200
BACKEND_PROGRESS_FRESH_SECONDS = 15
GPU_BUSY_PERCENT = 15.0

_METRIC_NAMES = {
    "running": ("vllm:num_requests_running", "vllm_num_requests_running"),
    "waiting": ("vllm:num_requests_waiting", "vllm_num_requests_waiting"),
    "prompt_tokens": ("vllm:prompt_tokens_total", "vllm_prompt_tokens_total"),
    "generation_tokens": ("vllm:generation_tokens_total", "vllm_generation_tokens_total"),
    "kv_usage": (
        "vllm:kv_cache_usage_perc", "vllm_kv_cache_usage_perc",
        "vllm:gpu_cache_usage_perc", "vllm_gpu_cache_usage_perc",
    ),
}


def _metric_name(line: str) -> str:
    token=line.split(None,1)[0]
    return token.split("{",1)[0]


def parse_prometheus_metrics(text: str) -> dict[str,float]:
    """Aggregate selected vLLM metrics across labels."""
    wanted={name for names in _METRIC_NAMES.values() for name in names}
    totals: dict[str,float]={}
    for raw in (text or "").splitlines():
        line=raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            head,value=line.rsplit(None,1)
            name=head.split("{",1)[0]
            if name not in wanted:
                continue
            value=float(value)
        except (ValueError,IndexError):
            continue
        totals[name]=totals.get(name,0.0)+value
    out={}
    for key,names in _METRIC_NAMES.items():
        for name in names:
            if name in totals:
                out[key]=totals[name]
                break
    return out


def parse_gpu_csv_line(line: str) -> dict[str,float]:
    """Parse the nvidia-smi CSV emitted by start-syv-mtp.sh."""
    if not isinstance(line,str) or not line.strip():
        return {}
    # timestamp,index,name,util.gpu,util.mem,mem.used,mem.total,power.draw,...
    parts=[p.strip() for p in line.strip().split(",")]
    if len(parts) < 7 or parts[0].lower().startswith("timestamp"):
        return {}
    def number(index: int) -> float|None:
        if index >= len(parts):
            return None
        m=re.search(r"[-+]?\d+(?:\.\d+)?",parts[index])
        return float(m.group(0)) if m else None
    util=number(3); util_mem=number(4); used=number(5); total=number(6); power=number(7)
    out={}
    if util is not None: out["gpu_util"]=util
    if util_mem is not None: out["gpu_memory_util"]=util_mem
    if used is not None: out["gpu_memory_used_mib"]=used
    if total is not None: out["gpu_memory_total_mib"]=total
    if power is not None: out["gpu_power_w"]=power
    return out


def tail_last_data_line(path: Path, max_bytes: int=16384) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0,2)
            size=fh.tell(); fh.seek(max(0,size-max_bytes))
            data=fh.read().decode("utf-8","replace")
    except OSError:
        return ""
    lines=[line for line in data.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.lower().startswith("timestamp"):
            return line
    return ""


class BackendTelemetrySampler:
    """Cached vLLM/GPU sampler. Failures are data, never supervisor failures."""
    def __init__(self, root: Path, metrics_url: str|None=None, min_interval: float=1.0):
        port=os.environ.get("MA_PORT","18030")
        self.root=Path(root)
        self.metrics_url=metrics_url or os.environ.get(
            "V2_BACKEND_METRICS_URL",f"http://127.0.0.1:{port}/metrics"
        )
        self.min_interval=float(min_interval)
        self._last_sample_mono=0.0
        self._last: dict[str,Any]={}
        self._prev_counters: dict[str,float]={}
        self._last_backend_progress_mono: float|None=None
        self._last_prompt_progress_mono: float|None=None
        self._last_generation_progress_mono: float|None=None

    def _fetch_metrics(self) -> tuple[dict[str,float],str]:
        try:
            req=urllib.request.Request(self.metrics_url,headers={"Accept":"text/plain"})
            with urllib.request.urlopen(req,timeout=0.7) as response:
                text=response.read(2_000_000).decode("utf-8","replace")
            return parse_prometheus_metrics(text),""
        except Exception as exc:
            return {},repr(exc)

    def _gpu(self) -> dict[str,float]:
        pointer=self.root/"current-gpu-log.txt"
        try:
            target=Path(pointer.read_text().strip()).expanduser()
        except OSError:
            return {}
        return parse_gpu_csv_line(tail_last_data_line(target))

    def sample(self, now: float|None=None) -> dict[str,Any]:
        now=time.monotonic() if now is None else float(now)
        if self._last and now-self._last_sample_mono < self.min_interval:
            return dict(self._last)
        metrics,error=self._fetch_metrics()
        prompt=float(metrics.get("prompt_tokens",0.0)) if metrics else None
        generation=float(metrics.get("generation_tokens",0.0)) if metrics else None
        progressed=False
        prompt_progressed=False
        generation_progressed=False
        if metrics:
            for key,value in (("prompt_tokens",prompt),("generation_tokens",generation)):
                prev=self._prev_counters.get(key)
                changed=prev is not None and value is not None and value > prev
                if changed:
                    progressed=True
                    if key=="prompt_tokens": prompt_progressed=True
                    if key=="generation_tokens": generation_progressed=True
                if value is not None:
                    self._prev_counters[key]=value
        if prompt_progressed:
            self._last_prompt_progress_mono=now
        if generation_progressed:
            self._last_generation_progress_mono=now
        if progressed:
            self._last_backend_progress_mono=now
        progress_age=(
            None if self._last_backend_progress_mono is None
            else max(0.0,now-self._last_backend_progress_mono)
        )
        prompt_progress_age=(
            None if self._last_prompt_progress_mono is None
            else max(0.0,now-self._last_prompt_progress_mono)
        )
        generation_progress_age=(
            None if self._last_generation_progress_mono is None
            else max(0.0,now-self._last_generation_progress_mono)
        )
        gpu=self._gpu()
        snapshot={
            "sample_mono":now,
            "metrics_url":self.metrics_url,
            "metrics_available":bool(metrics),
            "metrics_error":error,
            "running":int(metrics.get("running",0.0)) if metrics else None,
            "waiting":int(metrics.get("waiting",0.0)) if metrics else None,
            "prompt_tokens":prompt,
            "generation_tokens":generation,
            "kv_usage":metrics.get("kv_usage") if metrics else None,
            "backend_progress_age":progress_age,
            "prompt_progress_age":prompt_progress_age,
            "generation_progress_age":generation_progress_age,
            "backend_progressed":progressed,
            "prompt_progressed":prompt_progressed,
            "generation_progressed":generation_progressed,
            **gpu,
        }
        self._last_sample_mono=now
        self._last=snapshot
        return dict(snapshot)


def backend_phase(snapshot: dict[str,Any]) -> str:
    """Coarse server phase. This is intentionally global, not per-request."""
    if not snapshot or not snapshot.get("metrics_available"):
        return "backend-unknown"
    age=snapshot.get("backend_progress_age")
    fresh=isinstance(age,(int,float)) and age <= BACKEND_PROGRESS_FRESH_SECONDS
    prompt_age=snapshot.get("prompt_progress_age")
    generation_age=snapshot.get("generation_progress_age")
    prompt_fresh=isinstance(prompt_age,(int,float)) and prompt_age <= BACKEND_PROGRESS_FRESH_SECONDS
    generation_fresh=isinstance(generation_age,(int,float)) and generation_age <= BACKEND_PROGRESS_FRESH_SECONDS
    running=int(snapshot.get("running") or 0)
    waiting=int(snapshot.get("waiting") or 0)
    gpu=float(snapshot.get("gpu_util") or 0.0)
    if prompt_fresh and generation_fresh:
        return "backend-mixed-prefill-decode"
    if generation_fresh:
        return "backend-decode"
    if prompt_fresh:
        return "backend-prefill"
    if fresh:
        return "backend-token-progress"
    if running>0 and gpu >= GPU_BUSY_PERCENT:
        return "backend-compute-active"
    if running<=0 and waiting>0:
        return "backend-queued"
    if running>0:
        return "backend-running-no-token-progress"
    if gpu >= GPU_BUSY_PERCENT:
        return "gpu-active-unattributed"
    return "backend-idle"


def invisible_watchdog_decision(elapsed: float, snapshot: dict[str,Any]) -> dict[str,Any]:
    """Decide whether an invisible session should be retired.

    vLLM metrics are server-global, so healthy evidence extends the old 600s
    deadline but never suppresses it forever. One running request gives the
    strongest attribution; shared activity gets only a bounded 900s extension.
    """
    elapsed=max(0.0,float(elapsed))
    phase=backend_phase(snapshot)
    result={"abort":False,"phase":phase,"limit":INVISIBLE_BASE_SECONDS,"reason":""}
    if elapsed < INVISIBLE_BASE_SECONDS:
        return result
    if phase=="backend-unknown":
        result.update(abort=True,reason="backend-telemetry-unavailable")
        return result
    running=int(snapshot.get("running") or 0)
    waiting=int(snapshot.get("waiting") or 0)
    healthy=phase in {
        "backend-token-progress","backend-prefill","backend-decode",
        "backend-mixed-prefill-decode","backend-compute-active",
    }
    if healthy:
        limit=(INVISIBLE_EXCLUSIVE_EXTENSION_SECONDS if running==1
               else INVISIBLE_SHARED_EXTENSION_SECONDS)
        result["limit"]=limit
        if elapsed < limit:
            return result
        result.update(abort=True,reason=f"healthy-backend-extension-exhausted-{limit}s")
        return result
    if phase=="backend-queued" and waiting>0:
        result["limit"]=INVISIBLE_QUEUE_EXTENSION_SECONDS
        if elapsed < INVISIBLE_QUEUE_EXTENSION_SECONDS:
            return result
        result.update(abort=True,reason=f"backend-queue-extension-exhausted-{INVISIBLE_QUEUE_EXTENSION_SECONDS}s")
        return result
    if phase=="gpu-active-unattributed":
        result["limit"]=INVISIBLE_SHARED_EXTENSION_SECONDS
        if elapsed < INVISIBLE_SHARED_EXTENSION_SECONDS:
            return result
        result.update(abort=True,reason=f"unattributed-gpu-extension-exhausted-{INVISIBLE_SHARED_EXTENSION_SECONDS}s")
        return result
    result.update(abort=True,reason="backend-not-progressing")
    return result


def visible_progress_marker(shape: dict[str,Any], live: dict[str,Any]|None=None):
    live=live or {}
    return (
        shape.get("message_id") or "",
        shape.get("last_tool_id") or "",
        int(shape.get("reasoning") or 0),
        int(shape.get("text") or 0),
        int(shape.get("reasoning_tokens") or 0),
        int(shape.get("output_tokens") or 0),
        int(live.get("progress_seq") or 0),
        int(live.get("tool_successes") or 0),
    )
