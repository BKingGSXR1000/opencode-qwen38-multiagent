#!/usr/bin/env python3
"""Print the read-only V2 control-plane status projection as JSON."""
import argparse
import json
from control_state import snapshot
from state_io import StateCorruptionError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=".")
    args = parser.parse_args()
    try:
        data=snapshot(args.project)
        data={"state_error":False,**data}
        print(json.dumps(data, indent=2, sort_keys=True))
    except Exception as exc:
        print(json.dumps({
            "owner":"supervisor",
            "protocol":"v2-control-status-error-v1",
            "state_error":True,
            "resume_phase":"execution-blocked",
            "execution_blockers":[{
                "deliverable":"",
                "reason":"control_state_error",
                "detail":str(exc),
            }],
        }, indent=2, sort_keys=True))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
