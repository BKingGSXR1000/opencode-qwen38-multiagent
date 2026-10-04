#!/usr/bin/env python3
"""Controlled medium-coding A/B: selective project memory OFF versus ON."""
from __future__ import annotations
import argparse, importlib.util, json, statistics
from pathlib import Path

HERE=Path(__file__).resolve().parent
RUN_ROOT=Path("/home/bking/AI/a2-e2e/20261004-selective-memory-v1")
PORTS={"memory-off":58583,"memory-on":58584}

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,HERE/filename)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod

runner=load("memory_ab_medium_runner","benchmark-adaptive-medium.py")
fixture=load("memory_ab_verified_fixture","create-adaptive-medium-verified-canary.py")
project_memory=load("memory_ab_project_memory","project_memory.py")

class MemoryFixture:
    PROTOCOL=fixture.PROTOCOL+"+selective-memory-v1"
    TASK_TEXT=fixture.TASK_TEXT
    @staticmethod
    def build(project,task_file):
        work=Path(project)/".opencode-v2/work"
        work.mkdir(parents=True,exist_ok=True)
        (work/"selective-memory.enabled").write_text(
            project_memory.MARKER+"\n",encoding="utf-8"
        )
        result=fixture.build(Path(project),Path(task_file))
        if not project_memory.enabled(project):
            raise RuntimeError("selective-memory marker lost during bootstrap")
        return result

def memory_metrics(project:Path):
    ctrl=project/".opencode-v2";root=ctrl/"work/selective-project-memory"
    marker=ctrl/"work/selective-memory.enabled"
    contexts=sorted((ctrl/"query/leaves").glob("*-context.json")) if (ctrl/"query/leaves").is_dir() else []
    packet_chars=[];packet_verified=[];packet_omitted=[];with_memory=0
    for path in contexts:
        try:data=json.loads(path.read_text())
        except Exception:continue
        packet=data.get("selective_project_memory")
        if not isinstance(packet,dict):continue
        with_memory+=1
        packet_chars.append(len(json.dumps(packet,sort_keys=True,separators=(",",":"))))
        packet_verified.append(len(packet.get("verified_dependencies") or []))
        packet_omitted.append(int(packet.get("verified_dependencies_omitted") or 0))
    index={}
    if (root/"index.json").exists():index=json.loads((root/"index.json").read_text())
    return {
        "enabled":project_memory.enabled(project),
        "marker_present":marker.is_file(),
        "context_files":len(contexts),
        "contexts_with_memory":with_memory,
        "events_count":int(index.get("events_count") or 0),
        "verified_checkpoint_count":len(index.get("verified") or {}),
        "history_bytes":(root/"events.jsonl").stat().st_size if (root/"events.jsonl").exists() else 0,
        "packet_chars_max":max(packet_chars,default=0),
        "packet_chars_mean":round(statistics.mean(packet_chars),2) if packet_chars else 0,
        "verified_dependencies_mean":round(statistics.mean(packet_verified),2) if packet_verified else 0,
        "verified_dependencies_omitted_total":sum(packet_omitted),
    }

def configure(variant):
    enabled=variant=="memory-on"
    runner.fixture=MemoryFixture if enabled else fixture
    runner.benchmark.fixture=runner.fixture
    runner.benchmark.RUN_ROOT=RUN_ROOT/variant
    runner.benchmark.PORTS={"off":PORTS[variant],"observe":PORTS[variant]+20,"enforce":PORTS[variant]+40}
    return enabled

def run_variant(variant,seconds):
    enabled=configure(variant)
    result=runner.benchmark.run("off",seconds)
    result["experimental_variant"]=variant
    result["selective_memory_expected"]=enabled
    result["heldout"]=runner.run_heldout(Path(result["project"]))
    result["memory"]=memory_metrics(Path(result["project"]))
    if result["memory"]["enabled"] != enabled:
        raise RuntimeError("memory-mode mismatch")
    runner.benchmark.atomic_write_json(RUN_ROOT/variant/"result.json",result)
    return result

def selftest():
    import tempfile
    with tempfile.TemporaryDirectory(prefix="memory-ab-selftest-") as td:
        p=Path(td);off=p/"off";off.mkdir()
        assert memory_metrics(off)["enabled"] is False
        on=p/"on";on.mkdir();work=on/".opencode-v2/work";work.mkdir(parents=True)
        (work/"selective-memory.enabled").write_text(project_memory.MARKER+"\n")
        assert project_memory.enabled(on)
        m=memory_metrics(on);assert m["enabled"] and m["marker_present"]
        source=Path(__file__).read_text()
        assert 'runner.benchmark.run("off",seconds)' in source
        assert PORTS["memory-off"] != PORTS["memory-on"]
    print("selective memory A/B benchmark selftest: OK")

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variants",nargs="+",choices=("memory-off","memory-on"),default=["memory-off","memory-on"])
    ap.add_argument("--seconds-per-run",type=int,default=1100)
    ap.add_argument("--selftest",action="store_true")
    args=ap.parse_args()
    if args.selftest:selftest();return 0
    RUN_ROOT.mkdir(parents=True,exist_ok=True)
    if any((RUN_ROOT/v).exists() for v in args.variants):
        ap.error("refusing to overwrite existing selective-memory result")
    results=[]
    for variant in args.variants:
        result=run_variant(variant,args.seconds_per_run);results.append(result)
        print("MEMORY_AB_RESULT",variant,
              "ACCEPTED",result.get("accepted"),
              "HELDOUT",result.get("heldout",{}).get("pass"),
              "SECONDS",result.get("duration_seconds"),
              "INPUT_TOKENS",result.get("sessions",{}).get("input_tokens"),
              "SESSIONS",result.get("sessions",{}).get("sessions"),
              "TOOLS",result.get("sessions",{}).get("tool_calls"),
              "EVENTS",result.get("memory",{}).get("events_count"),flush=True)
    runner.benchmark.atomic_write_json(RUN_ROOT/"summary.json",{
        "protocol":"v1-selective-memory-ab",
        "controlled_difference":"pre-plan selective-memory.enabled marker only",
        "adaptive_reasoning_mode":"off",
        "shared_backend":True,
        "same_verified_fixture":True,
        "results":results,
        "limitations":[
            "Initial pair is exploratory; Qwen sampling trajectories are stochastic.",
            "Use repeated alternating pairs before attributing token/runtime differences.",
            "Input-token totals are model-provider/session telemetry, not API billing."
        ],
    })
    return 0 if all(r.get("accepted") and r.get("heldout",{}).get("pass") for r in results) else 2

if __name__=="__main__":
    raise SystemExit(main())
