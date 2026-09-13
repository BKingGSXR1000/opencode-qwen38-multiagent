#!/usr/bin/env bash
set -Eeuo pipefail
echo "ERROR: leaf readiness is supervisor-owned; workers must return after Verify and must not invoke leaf-complete." >&2
exit 2
