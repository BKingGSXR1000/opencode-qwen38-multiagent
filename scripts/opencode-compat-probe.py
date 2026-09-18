#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path


def run(cmd, timeout=8):
    try:
        proc=subprocess.run(
            [str(x) for x in cmd],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode":proc.returncode,
            "stdout":proc.stdout.strip(),
            "stderr":proc.stderr.strip(),
        }
    except Exception as exc:
        return {
            "returncode":None,
            "stdout":"",
            "stderr":f"{type(exc).__name__}: {exc}",
        }


def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()


def package_metadata(runtime_root):
    found=[]
    if not runtime_root.exists():
        return found
    for path in runtime_root.rglob("package.json"):
        try:
            rel=path.relative_to(runtime_root)
            if len(rel.parts)>6:
                continue
            data=json.loads(path.read_text(errors="replace"))
        except Exception:
            continue
        name=str(data.get("name") or "")
        version=str(data.get("version") or "")
        if "opencode" in name.lower() or version.startswith("0.0.0-beta-"):
            found.append({
                "path":str(path),
                "name":name,
                "version":version,
            })
    return sorted(found,key=lambda x:(x["name"],x["path"]))[:100]


def static_contracts(root):
    plugin=root/"xdg/config/opencode/plugins/v2-bounded-subagent.js"
    supervisor=root/"scripts/supervisor.py"
    env=root/"scripts/env.sh"
    ptext=plugin.read_text(errors="replace") if plugin.exists() else ""
    stext=supervisor.read_text(errors="replace") if supervisor.exists() else ""
    etext=env.read_text(errors="replace") if env.exists() else ""
    return {
        "plugin_exists":plugin.exists(),
        "uses_execute_before":'api.tool.hook("execute.before"' in ptext,
        "uses_execute_after":'api.tool.hook("execute.after"' in ptext,
        "uses_model_request_hook":'api.session.hook("model.request"' in ptext,
        "uses_native_interrupt":"api.session.interrupt" in ptext,
        "sets_background_true":"args.background = true" in ptext,
        "background_feature_flag":
            "OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true" in etext,
        "read_adapter_accepts_path":'args.get("path")' in stext,
        "read_adapter_accepts_filePath":'args.get("filePath")' in stext,
        "read_adapter_ambiguity_guard":"ambiguous-path-keys" in stext,
    }


def db_contracts(db_path, limit):
    result={
        "db_exists":db_path.exists(),
        "tool_input_keysets":{},
        "read_input_keysets":{},
        "read_examples":[],
        "session_parenting":{},
        "errors":[],
    }
    if not db_path.exists():
        return result
    try:
        con=sqlite3.connect(f"file:{db_path}?mode=ro",uri=True,timeout=2)
    except Exception as exc:
        result["errors"].append(f"db-open:{type(exc).__name__}:{exc}")
        return result

    tool_counts=Counter()
    read_counts=Counter()
    examples=[]
    try:
        rows=con.execute(
            "SELECT session_id,data FROM session_message "
            "WHERE type='assistant' ORDER BY time_created DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        for sid,raw in rows:
            try:
                data=json.loads(raw)
            except Exception:
                continue
            content=data.get("content") if isinstance(data,dict) else None
            if not isinstance(content,list):
                continue
            for item in content:
                if not isinstance(item,dict) or item.get("type")!="tool":
                    continue
                name=str(item.get("name") or "")
                state=item.get("state") if isinstance(item.get("state"),dict) else {}
                inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                keys=tuple(sorted(str(k) for k in inp.keys()))
                tool_counts[(name,keys)]+=1
                if name=="read":
                    read_counts[keys]+=1
                    if len(examples)<20:
                        examples.append({
                            "session":sid,
                            "keys":list(keys),
                            "path":inp.get("path"),
                            "filePath":inp.get("filePath"),
                            "status":state.get("status"),
                            "executed":item.get("executed"),
                        })
    except Exception as exc:
        result["errors"].append(f"message-scan:{type(exc).__name__}:{exc}")

    result["tool_input_keysets"]={
        f"{name}:{','.join(keys) or '<none>'}":count
        for (name,keys),count in sorted(tool_counts.items())
    }
    result["read_input_keysets"]={
        ",".join(keys) or "<none>":count
        for keys,count in sorted(read_counts.items())
    }
    result["read_examples"]=examples

    try:
        rows=con.execute(
            "SELECT coalesce(agent,''),"
            "sum(CASE WHEN parent_id IS NOT NULL THEN 1 ELSE 0 END),"
            "sum(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) "
            "FROM session_v2 GROUP BY coalesce(agent,'')"
        ).fetchall()
        result["session_parenting"]={
            str(agent or "<unknown>"):{
                "parented":int(parented or 0),
                "root":int(roots or 0),
            }
            for agent,parented,roots in rows
        }
    except Exception as exc:
        result["errors"].append(f"session-scan:{type(exc).__name__}:{exc}")
    finally:
        con.close()
    return result


def main():
    ap=argparse.ArgumentParser(
        description="Audit the exact OpenCode2 runtime and V2 hook/session contracts."
    )
    default_root=Path(__file__).resolve().parents[1]
    ap.add_argument("--root",default=str(default_root))
    ap.add_argument("--db",default="")
    ap.add_argument("--limit",type=int,default=5000)
    ap.add_argument("--output",default="")
    ap.add_argument("--strict",action="store_true")
    args=ap.parse_args()

    root=Path(args.root).expanduser().resolve()
    runtime=root/"runtime/opencode2/bin/opencode2"
    runtime_root=root/"runtime/opencode2"
    auto=root/"scripts/opencode2-auto.sh"
    db=Path(args.db).expanduser().resolve() if args.db else root/"xdg/data/opencode/opencode.db"

    version=run([runtime,"--version"]) if runtime.exists() else {}
    report={
        "generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "root":str(root),
        "git_head":run(["git","-C",root,"rev-parse","HEAD"])["stdout"],
        "runtime":{
            "path":str(runtime),
            "exists":runtime.exists(),
            "realpath":str(runtime.resolve()) if runtime.exists() else "",
            "version":version,
            "help":run([runtime,"--help"]) if runtime.exists() else {},
            "sha256":sha256(runtime) if runtime.is_file() else "",
            "size":runtime.stat().st_size if runtime.exists() else None,
            "mtime":time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(runtime.stat().st_mtime),
            ) if runtime.exists() else "",
            "auto_wrapper":str(auto),
            "packages":package_metadata(runtime_root),
        },
        "system_opencode":{
            "which":run(["bash","-lc","command -v opencode || true"])["stdout"],
            "version":run([
                "bash","-lc",
                "command -v opencode >/dev/null && opencode --version || true"
            ])["stdout"],
        },
        "static_contracts":static_contracts(root),
        "database":db_contracts(db,args.limit),
    }

    read_sets=report["database"]["read_input_keysets"]
    observed_path=any("path" in key.split(",") for key in read_sets)
    observed_file=any("filePath" in key.split(",") for key in read_sets)
    contracts=report["static_contracts"]
    matches=(
        (not read_sets)
        or (observed_path and contracts["read_adapter_accepts_path"])
        or (observed_file and contracts["read_adapter_accepts_filePath"])
    )
    report["assessment"]={
        "observed_beta_path_shape":observed_path,
        "observed_filePath_shape":observed_file,
        "read_adapter_matches_observed_shape":matches,
    }

    print("OpenCode V2 compatibility probe")
    print("===============================")
    print(f"git HEAD       : {report['git_head'] or '<unknown>'}")
    print(f"runtime        : {runtime}")
    print(f"runtime version: {version.get('stdout') or version.get('stderr') or '<unknown>'}")
    print(f"runtime sha256 : {report['runtime']['sha256'] or '<unknown>'}")
    print(f"runtime mtime  : {report['runtime']['mtime'] or '<unknown>'}")
    print(f"system opencode: {report['system_opencode']['which'] or '<not found>'}")
    print(f"system version : {report['system_opencode']['version'] or '<unknown>'}")
    print()
    print("Observed tool input keysets from the real OpenCode DB:")
    for key,count in report["database"]["tool_input_keysets"].items():
        print(f"  {count:5d}  {key}")
    if not report["database"]["tool_input_keysets"]:
        print("  <none observed>")
    print()
    print("Read adapter:")
    for key in (
        "read_adapter_accepts_path",
        "read_adapter_accepts_filePath",
        "read_adapter_ambiguity_guard",
    ):
        print(f"  {key}: {contracts[key]}")
    print(f"  matches observed shape: {matches}")

    if args.output:
        out=Path(args.output).expanduser().resolve()
    else:
        out=root/"logs"/f"opencode-compat-probe-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    print(f"\nJSON report: {out}")

    if args.strict:
        failures=[]
        if not runtime.exists():
            failures.append("runtime-missing")
        if not (version.get("stdout") or ""):
            failures.append("runtime-version-missing")
        if read_sets and not matches:
            failures.append("read-adapter-does-not-match-observed-shape")
        for name in (
            "uses_execute_before",
            "uses_execute_after",
            "uses_native_interrupt",
            "sets_background_true",
            "background_feature_flag",
        ):
            if not contracts.get(name):
                failures.append(f"missing-contract:{name}")
        if failures:
            print("STRICT FAIL: "+", ".join(failures),file=sys.stderr)
            raise SystemExit(2)
        print("STRICT: PASS")


if __name__=="__main__":
    main()
