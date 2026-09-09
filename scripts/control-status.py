#!/usr/bin/env python3
"""Print the read-only V2 control-plane status projection as JSON."""
import argparse
import json
from control_state import snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=".")
    args = parser.parse_args()
    print(json.dumps(snapshot(args.project), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
