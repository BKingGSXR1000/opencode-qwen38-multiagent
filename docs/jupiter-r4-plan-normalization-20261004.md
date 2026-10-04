# Jupiter R4 plan normalization

Fresh original Jupiter R4 reached structured planning and reduced its initial
repair packet from eight errors to one, but terminalized after three planner
sessions. The remaining blocker and a hidden coverage regression were reproduced
from durable model/tool records.

The server leaf explicitly described an already-frozen locally-vendored
three.js input, a local file copy, and "not a fresh external acquisition".
Both structured_plan and control-guard nevertheless matched this as active
external acquisition. Two causes were found: "locally-vendored" was treated as
an action, and the path segment public/vendor/ was treated as the verb vendor.
Both matchers now scrub explicit pre-existing/negated phrases and require
whitespace after acquisition verbs, so path segments do not count. Active
download/vendor/fetch/research/curl cases remain fail-closed.

R4 also assigned A006-A009+A014 to both the UI implementation leaf and its
behavioral controls test leaf. The repair model removed A014 from both, losing a
MUST entirely. The compiler now prunes only redundant acceptance assignments
when a leaf exceeds its S/M ID limit. It never removes the last global owner of
an Axxx ID, never invents IDs, and leaves truly non-redundant oversized leaves
for normal planner repair. Replaying the original R4 planner write preserves
global A001-A014 coverage while removing the two redundant overflow errors.

After those deterministic normalizations, the original R4 plan had only four
ordinary repair items: three repeated-operation limits and one
public/index.html ownership overlap. Simulating exactly those simple repairs
exposed one genuine server Verify syntax error:
spawn("node":["server.js"],...) instead of spawn("node",["server.js"],...).

Once that syntax was corrected in a disposable replay, the validator still
failed to recognize the real Node -e runtime service test because the command
uses a PORT=... prefix and launches the server inside child_process.spawn.
leaf_contract now recognizes only syntactically valid Node -e programs that
contain executable spawn/child_process.spawn, a .js server argument, a localhost
fetch call, and explicit fail-closed throw or non-zero process.exit. Static
string mentions, spawn-only, fetch-only, and invalid JavaScript remain invalid.

With only the genuine spawn syntax correction applied, the archived R4 plan
passes structured compilation and control-guard --finalize-plan, produces
IMPLEMENTATION_PLAN.ready, and leaves no repair packet.
