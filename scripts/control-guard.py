#!/usr/bin/env python3
import argparse
import json
import os
import re
import tempfile
from pathlib import Path

ROOT = Path.home() / "AI" / "opencode-qwen38-multiagent-v2"
AGENTS = ROOT / "xdg" / "config" / "opencode" / "agents"

PLAN_MARKER = "<!-- IMPLEMENTATION_PLAN_COMPLETE -->"
ACC_MARKER = "<!-- ACCEPTANCE_COMPLETE -->"
PLAN_MAX_LINES = 400
RUN_CHECKS_COMMAND = ".opencode-v2/bin/run-checks"
INTERNAL_EXTERNAL_REFERENCE_RE = re.compile(
    r"\b(?:skyfield|astropy|jpl|nasa|naif|horizons|de\d{3,4}s?\.bsp|\.bsp\s+kernel)\b",
    re.I,
)

FORBIDDEN_WRITE_ROLES = {
    "investigator",
    "reviewer",
    "reference-researcher",
    "acceptance-validator",
    "acceptance-planner",
    "implementation-planner",
    "lessons-learner",
    "state-writer",
}

WRITE_ROLES = {
    "probe-builder",
    "implementer",
    "core-builder",
    "feature-builder",
    "reasoning-builder",
    "integrator",
    "tester",
    "test-builder",
}

ALIASES = {
    "outcome": ("Outcome",),
    "owned_artifacts": ("Owned artifacts", "Owned artifacts / files", "Owned files"),
    "launch_deps": ("Launch deps", "Launch deps / depends_on", "Depends on"),
    "contract_deps": ("Contract deps",),
    "verify_deps": ("Verify deps",),
    "acceptance_ids": ("Acceptance IDs", "Acceptance IDs / acceptance", "Acceptance"),
    "complexity": ("Complexity", "Complexity / size"),
    "role": ("Role", "Role / role", "Preferred role"),
    "parallel": ("Parallel-safe with", "Parallel-safe"),
    "verify_command": ("Verify command", "Verify command / verify", "Verify"),
    "done_when": ("Done when",),
}

def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)

def final_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""

def deps(value: str):
    return re.findall(r"\bD\d{3}\b", value or "")

def role_can_write(role: str):
    if role not in WRITE_ROLES or role in FORBIDDEN_WRITE_ROLES:
        return False
    p = AGENTS / f"{role}.md"
    if not p.exists():
        return False
    s = p.read_text(errors="replace")
    fm = re.match(r"\A---\n(.*?)\n---\n", s, re.S)
    if not fm:
        return False
    front = fm.group(1)
    if re.search(r"(?m)^\s*edit\s*:\s*deny\s*$", front):
        return False
    return True

# V2.6.9 MULTILINE_PLAN_FIELD_FIX
def field(section: str, names):
    """Read one plan metadata field, including indented continuation lines.

    Planner Markdown frequently wraps long values such as Owned artifacts and
    Done when. The old regex captured only the first physical line, silently
    truncating the machine manifest and causing false ownership violations.
    """
    if isinstance(names, str):
        names = (names,)

    all_aliases = tuple(
        alias
        for aliases in ALIASES.values()
        for alias in aliases
    )
    next_field_re = re.compile(
        r"^\s*(?:-\s*)?(?:"
        + "|".join(re.escape(alias) for alias in all_aliases)
        + r")\s*:\s*",
        re.I,
    )

    lines = section.splitlines()
    for i, line in enumerate(lines):
        for name in names:
            m = re.match(
                rf"^\s*(?:-\s*)?{re.escape(name)}\s*:\s*(.*)$",
                line,
                re.I,
            )
            if not m:
                continue

            parts = []
            first = m.group(1).strip()
            if first:
                parts.append(first)

            for cont in lines[i + 1:]:
                if not cont.strip():
                    break
                if re.match(r"^\s*#{1,6}\s+", cont):
                    break
                if next_field_re.match(cont):
                    break
                if not re.match(r"^\s+\S", cont):
                    break
                parts.append(cont.strip())

            return " ".join(parts).strip()

    return ""

def strict_owned_artifact_paths(raw: str):
    """Parse the machine-owned artifact field.

    Canonical syntax is either exactly:
      none
    or:
      `project/relative/path`, `second/path`

    Descriptions belong in Outcome/Done when, never in this field.  Keeping the
    grammar deliberately small prevents prose such as "/PORT=" or "PASS/FAIL"
    from becoming supervisor-owned filesystem paths.
    """
    if not isinstance(raw, str):
        return [], "Owned artifacts must be a string"
    raw = raw.strip()
    if raw == "none":
        return [], ""
    if not raw:
        return [], "Owned artifacts is empty"

    values = re.findall(r"`([^`\r\n]+)`", raw)
    if not values:
        return [], (
            "Owned artifacts must be exact comma-separated backticked "
            "project-relative paths, or exact 'none'"
        )
    canonical = ", ".join(f"`{value}`" for value in values)
    if raw != canonical:
        return [], (
            "Owned artifacts must contain paths only: "
            "`path`, `path`; move all descriptions/parentheticals to Outcome or Done when"
        )

    out = []
    for value in values:
        if value != value.strip():
            return [], f"Owned artifact path has surrounding whitespace: {value!r}"
        if value.startswith(("/", "./", "~")):
            return [], f"Owned artifact must be project-relative: {value}"
        if "\\" in value or any(ch.isspace() for ch in value):
            return [], f"Owned artifact contains whitespace/backslash: {value}"
        if any(ch in value for ch in "*?[]{}|<>\"'`;"):
            return [], f"Owned artifact contains unsupported shell/path metacharacter: {value}"
        core = value[:-1] if value.endswith("/") else value
        if not core:
            return [], f"Owned artifact path is invalid: {value}"
        parts = core.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return [], f"Owned artifact contains an invalid path segment: {value}"
        if value in out:
            return [], f"Owned artifact is duplicated: {value}"
        out.append(value)
    return out, ""


def canonical_owned_artifacts(paths):
    return "none" if not paths else ", ".join(f"`{path}`" for path in paths)


def plan_artifact_paths(raw: str):
    """Return only deterministically validated ownership paths."""
    paths, error = strict_owned_artifact_paths(raw)
    return [] if error else paths


def referenced_paths(raw: str):
    """Project-looking file paths explicitly named in backticks."""
    out=[]
    for value in re.findall(r"`([^`]+)`",raw or ""):
        value=value.strip().rstrip(".,;:")
        if value.startswith("./"): value=value[2:]
        if not value or any(ch.isspace() for ch in value): continue
        if value.startswith(("http://","https://","/tmp/")): continue
        base=value.rstrip("/").rsplit("/",1)[-1]
        if "." not in base and not value.startswith(".opencode-v2/"):
            continue
        if value not in out: out.append(value)
    return out

def path_is_within(path: str, owned):
    return any(path==x or path.startswith(x.rstrip("/")+"/") for x in owned)

def parse_waves(text: str):
    # Accept normal and numbered Markdown headings, e.g.
    # "## Execution waves", "## Execution Waves", "## 5. Execution Waves".
    m = re.search(
        r"(?ims)^##\s+(?:\d+(?:\.\d+)*\.?\s+)?Execution\s+waves\s*$\n"
        r"(.*?)(?=^##\s+|\Z)",
        text,
    )
    if not m:
        return {}, ["missing Execution Waves section (numbered headings are allowed)"]
    waves = {}
    errors = []
    for line in m.group(1).splitlines():
        # Canonical machine payload is a comma-separated Dxxx list. Permit
        # harmless Markdown bullet/bold styling, but reject prose appended to
        # an assignment because it can smuggle misleading extra IDs/deps.
        wm = re.match(
            r"\s*[-*+]\s*(?:\*\*)?Wave\s+(\d+)(?:\*\*)?\s*:\s*"
            r"(D\d{3}(?:\s*,\s*D\d{3})*)\s*$",
            line,
            re.I,
        )
        if not wm:
            if re.search(r"\bWave\s+\d+\s*:", line, re.I):
                errors.append(f"non-canonical wave assignment: {line.strip()}")
            continue
        wave = int(wm.group(1))
        assignment = wm.group(2)
        for did in re.findall(r"\bD\d{3}\b", assignment):
            if did in waves:
                errors.append(f"{did}: appears in more than one wave")
            else:
                waves[did] = wave
    return waves, errors

def parse_plan(text: str):
    matches = list(re.finditer(r"(?m)^###\s+(D\d{3})\s+[—-]\s+(.+?)\s*$", text))
    leaves = {}
    errors = []
    if not matches:
        return {}, {}, ["no Dxxx deliverables found"]

    for i, m in enumerate(matches):
        did = m.group(1)
        name = m.group(2).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section = text[m.end():end]

        vals = {k: field(section, names) for k, names in ALIASES.items()}
        cm = re.search(r"\b(S|M|L|XL)\b", vals["complexity"], re.I)

        leaf = {
            "id": did,
            "name": name,
            "outcome": vals["outcome"],
            "owned_artifacts": vals["owned_artifacts"],
            "launch_deps": deps(vals["launch_deps"]),
            "contract_deps": deps(vals["contract_deps"]),
            "verify_deps": deps(vals["verify_deps"]),
            "verify_command": vals["verify_command"].strip().strip("`"),
            "complexity": cm.group(1).upper() if cm else "",
            "role": re.split(r"[\s,;]+", vals["role"])[0] if vals["role"] else "",
            "done_when": vals["done_when"],
            "acceptance_ids": re.findall(r"\bA\d{3}\b", vals["acceptance_ids"]),
            "parallel": vals["parallel"],
        }
        owned_paths, owned_error = strict_owned_artifact_paths(leaf["owned_artifacts"])
        leaf["owned_artifact_paths"] = owned_paths
        if owned_error:
            errors.append(f"{did}: {owned_error}")
        else:
            leaf["owned_artifacts"] = canonical_owned_artifacts(owned_paths)

        leaves[did] = leaf

        required = (
            ("Owned artifacts", leaf["owned_artifacts"]),
            ("Launch deps", vals["launch_deps"]),
            ("Contract deps", vals["contract_deps"]),
            ("Verify deps", vals["verify_deps"]),
            ("Verify command", leaf["verify_command"]),
            ("Done when", leaf["done_when"]),
        )
        for label, value in required:
            if not value:
                errors.append(f"{did}: missing {label}")

        if leaf["complexity"] not in ("S", "M"):
            errors.append(
                f"{did}: final complexity must be S/M, got {leaf['complexity'] or 'missing'}"
            )
        if not leaf["role"]:
            errors.append(f"{did}: missing Role")
        if not leaf["acceptance_ids"]:
            errors.append(f"{did}: missing Acceptance IDs")
        if not leaf["parallel"]:
            errors.append(f"{did}: missing Parallel-safe field")

        writes = bool(leaf.get("owned_artifact_paths"))
        if writes and leaf["role"] and not role_can_write(leaf["role"]):
            allowed = ", ".join(sorted(WRITE_ROLES))
            errors.append(
                f"{did}: role '{leaf['role']}' is not an allowed write-capable Role; "
                f"choose one of: {allowed}"
            )
        if "probe" in name.lower() and leaf["role"] != "probe-builder":
            errors.append(
                f"{did}: probe leaf must use exact Role 'probe-builder', got "
                f"'{leaf['role'] or 'missing'}'"
            )
        if leaf["verify_command"] in ("true", ":", "echo ok", "echo pass"):
            errors.append(f"{did}: Verify command is non-verifying")

    waves, wave_errors = parse_waves(text)
    errors.extend(wave_errors)

    ids = set(leaves)
    for did, leaf in leaves.items():
        for kind in ("launch_deps", "contract_deps", "verify_deps"):
            for dep in leaf[kind]:
                if dep not in ids:
                    errors.append(f"{did}: dangling {kind} reference {dep}")
        if did not in waves:
            errors.append(f"{did}: missing from execution waves")
        else:
            leaf["wave"] = waves[did]

    visiting, done = set(), set()
    def visit(node, stack):
        if node in done:
            return
        if node in visiting:
            errors.append("launch-dependency cycle: " + " -> ".join(stack + [node]))
            return
        visiting.add(node)
        for dep in leaves[node]["launch_deps"]:
            if dep in leaves:
                visit(dep, stack + [node])
        visiting.remove(node)
        done.add(node)

    for did in leaves:
        visit(did, [])

    for did, leaf in leaves.items():
        if did not in waves:
            continue
        for dep in leaf["launch_deps"]:
            if dep in waves and waves[dep] >= waves[did]:
                errors.append(
                    f"{did}: Launch dep {dep} wave {waves[dep]} is not earlier than wave {waves[did]}"
                )

    # plan path ownership consistency
    owners={}
    for owner_id, owner_leaf in leaves.items():
        for path in plan_artifact_paths(owner_leaf.get("owned_artifacts","")):
            owners.setdefault(path,[]).append(owner_id)

    def dependency_closure(did):
        seen=set(); stack=[]
        for kind in ("launch_deps","contract_deps","verify_deps"):
            stack.extend(leaves[did].get(kind,[]))
        while stack:
            dep=stack.pop()
            if dep in seen or dep not in leaves: continue
            seen.add(dep)
            for kind in ("launch_deps","contract_deps","verify_deps"):
                stack.extend(leaves[dep].get(kind,[]))
        return seen

    for did, leaf in leaves.items():
        own=plan_artifact_paths(leaf.get("owned_artifacts",""))
        depset=dependency_closure(did)
        # Done-when is a hard completion obligation. Verify commands are also
        # hard machine obligations. If either names another leaf's artifact,
        # that owner must be an explicit/transitive dependency.
        refs=[]
        refs += [("Done when",p) for p in referenced_paths(leaf.get("done_when",""))]
        refs += [("Verify command",p) for p in referenced_paths(leaf.get("verify_command",""))]
        for source,path in refs:
            if path_is_within(path,own): continue
            matching=[]
            for owned_path, owner_ids in owners.items():
                if path==owned_path or path.startswith(owned_path.rstrip("/")+"/"):
                    matching.extend(owner_ids)
            matching=[x for x in matching if x!=did]
            if matching and not any(x in depset for x in matching):
                errors.append(
                    f"{did}: {source} requires `{path}` owned by {','.join(sorted(set(matching)))}, "
                    "but that owner is not a declared dependency"
                )

    test_leaves = [
        x for x in leaves.values()
        if ".opencode-v2/TEST_CHECKS.json" in x.get("owned_artifact_paths", [])
    ]
    if not test_leaves:
        errors.append(
            "missing final test-manifest leaf owning .opencode-v2/TEST_CHECKS.json"
        )
    else:
        for leaf in test_leaves:
            if leaf["role"] not in ("tester", "test-builder"):
                errors.append(
                    f"{leaf['id']}: TEST_CHECKS leaf must use tester/test-builder"
                )
            if leaf["verify_command"] != RUN_CHECKS_COMMAND:
                errors.append(
                    f"{leaf['id']}: TEST_CHECKS Verify command must be exact "
                    f"{RUN_CHECKS_COMMAND}"
                )

    return leaves, waves, errors

def reference_policy(text: str):
    m = re.search(
        r"(?im)^\s*(?:#{1,6}\s*)?Reference policy\s*:\s*"
        r"(none|internal|external-required)\s*$",
        text,
    )
    return m.group(1).lower() if m else ""

def validate_acceptance(project: Path, finalize=False):
    ctrl = project / ".opencode-v2"
    path = ctrl / "ACCEPTANCE.md"
    ready = ctrl / "ACCEPTANCE.ready"
    err = ctrl / "ACCEPTANCE.guard-errors.txt"
    errors = []

    if not path.exists():
        errors.append("ACCEPTANCE.md missing")
    else:
        text = path.read_text(errors="replace")
        if final_nonempty_line(text) != ACC_MARKER:
            errors.append("final line is not exact ACCEPTANCE_COMPLETE marker")
        if reference_policy(text) == "internal" and INTERNAL_EXTERNAL_REFERENCE_RE.search(text):
            errors.append(
                "internal Reference policy cannot require named external astronomical/reference truth"
            )
        if not re.findall(r"\bA\d{3}\b", text):
            errors.append("no Axxx acceptance IDs found")
        if not reference_policy(text):
            errors.append("missing Reference policy: none|internal|external-required")

    if errors:
        atomic_write(err, "\n".join(errors) + "\n")
        return False, errors

    if err.exists():
        err.unlink()
    if finalize:
        atomic_write(
            ready,
            "status=complete\n"
            "protocol=V2.6.7c\n"
            "artifact=ACCEPTANCE.md\n"
            "marker=ACCEPTANCE_COMPLETE\n"
            "validated=deterministic-v2.6.7b\n",
        )
    return True, []

def merge_split_leaf_overlay(project: Path, leaves: dict):
    # Restore supervisor-created recursive split leaves after plan regeneration.
    path = project / ".opencode-v2" / "work" / "split-leaves.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        return leaves

    parents = data.get("parents") if isinstance(data, dict) else {}
    if not isinstance(parents, dict):
        return leaves

    for parent, entry in parents.items():
        if not isinstance(entry, dict):
            continue
        child_defs = entry.get("child_defs")
        if not isinstance(child_defs, dict):
            continue
        children = entry.get("children")
        if not isinstance(children, list):
            children = list(child_defs)
        if parent in leaves and isinstance(leaves[parent], dict):
            leaves[parent]["split_children"] = list(children)
        for did, child in child_defs.items():
            if isinstance(child, dict):
                leaves[did] = child
    return leaves


def validate_plan(project: Path, finalize=False):
    ctrl = project / ".opencode-v2"
    path = ctrl / "IMPLEMENTATION_PLAN.md"
    ready = ctrl / "IMPLEMENTATION_PLAN.ready"
    manifest_path = ctrl / "IMPLEMENTATION_PLAN.guard.json"
    err = ctrl / "IMPLEMENTATION_PLAN.guard-errors.txt"
    errors = []
    leaves, waves = {}, {}

    if not path.exists():
        errors.append("IMPLEMENTATION_PLAN.md missing")
    else:
        text = path.read_text(errors="replace")
        line_count = len(text.splitlines())
        if line_count > PLAN_MAX_LINES:
            errors.append(
                f"IMPLEMENTATION_PLAN.md has {line_count} lines; hard maximum is {PLAN_MAX_LINES}"
            )
        if final_nonempty_line(text) != PLAN_MARKER:
            errors.append("final line is not exact IMPLEMENTATION_PLAN_COMPLETE marker")
        if not re.search(r"(?m)^Status:\s*COMPLETE\s*$", text):
            errors.append("top-level plan Status must be COMPLETE before finalization")
        if not re.search(
            r"(?ms)^##\s+Planner checkpoint\s*\nStatus:\s*COMPLETE\s*$",
            text,
        ):
            errors.append("Planner checkpoint Status must be COMPLETE before finalization")
        leaves, waves, parse_errors = parse_plan(text)
        errors.extend(parse_errors)
        leaves = merge_split_leaf_overlay(project, leaves)
        acceptance = ctrl / "ACCEPTANCE.md"
        try:
            policy = reference_policy(acceptance.read_text(errors="replace"))
        except OSError:
            policy = ""
        if policy == "internal":
            for did, leaf in leaves.items():
                if leaf.get("role") != "probe-builder":
                    continue
                section = " ".join(str(leaf.get(k, "")) for k in (
                    "name", "owned_artifacts", "verify_command", "done_when"
                ))
                if INTERNAL_EXTERNAL_REFERENCE_RE.search(section):
                    errors.append(
                        f"{did}: internal Reference policy forbids an external astronomy/reference probe"
                    )

    if errors:
        atomic_write(err, "\n".join(errors) + "\n")
        return False, errors

    if err.exists():
        err.unlink()

    atomic_write(
        manifest_path,
        json.dumps(
            {
                "protocol": "V2.6.9",
                "recursive_split_protocol": "v2-recursive-split-v1",
                "project": str(project),
                "leaves": leaves,
                "waves": waves,
            },
            indent=2,
        )
        + "\n",
    )

    if finalize:
        atomic_write(
            ready,
            "status=complete\n"
            "protocol=V2.6.7c\n"
            "artifact=IMPLEMENTATION_PLAN.md\n"
            "marker=IMPLEMENTATION_PLAN_COMPLETE\n"
            "validated=deterministic-v2.6.7b\n",
        )
    return True, []

def selftest():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fake = td / "agents"
        fake.mkdir()
        for name, edit in (
            ("implementer", "allow"),
            ("tester", "allow"),
            ("probe-builder", "allow"),
            ("investigator", "deny"),
        ):
            (fake / f"{name}.md").write_text(
                f"---\nmode: subagent\npermission:\n  edit: {edit}\n---\n"
            )

        global AGENTS
        old = AGENTS
        AGENTS = fake
        try:
            valid = """# Plan
Status: COMPLETE
## Planner checkpoint
Status: COMPLETE
## Deliverables
### D001 — Scaffold
- Outcome: scaffold
- Owned artifacts / files: `app.js`
- Launch deps / depends_on: (none)
- Contract deps: (none)
- Verify deps: (none)
- Acceptance IDs / acceptance: A001
- Complexity / size: S
- Deep reasoning: no
- Role / role: implementer
- Parallel-safe with: D002
- Verify command / verify: `test -s app.js`
- Done when: app exists
### D002 — Tests
- Outcome: tests
- Owned artifacts / files: `.opencode-v2/TEST_CHECKS.json`
- Launch deps / depends_on: [D001]
- Contract deps: (none)
- Verify deps: [D001]
- Acceptance IDs / acceptance: A001
- Complexity / size: S
- Deep reasoning: no
- Role / role: tester
- Parallel-safe with: (none)
- Verify command / verify: `.opencode-v2/bin/run-checks`
- Done when: report passes
## 5. Execution Waves
- Wave 1: D001
- Wave 2: D002
<!-- IMPLEMENTATION_PLAN_COMPLETE -->
"""
            _, _, errors = parse_plan(valid)
            assert not errors, errors
            # MULTILINE_PLAN_FIELD_REGRESSION
            multiline = valid.replace(
                "- Owned artifacts / files: `app.js`",
                "- Owned artifacts / files: `app.js`,\n"
                "  `server/index.js`,\n"
                "  `public/index.html`",
                1,
            )
            multi_leaves, _, multi_errors = parse_plan(multiline)
            assert not multi_errors, multi_errors
            multi_owned = multi_leaves["D001"]["owned_artifacts"]
            assert "server/index.js" in multi_owned, multi_owned
            assert "public/index.html" in multi_owned, multi_owned
            assert reference_policy("## Reference policy: internal") == "internal"
            # PLAN_OWNERSHIP_DEP_REGRESSION
            contradiction = valid.replace(
                "- Done when: app exists",
                "- Done when: app exists and serves `tests.js`",
                1,
            ).replace(
                "- Owned artifacts / files: `.opencode-v2/TEST_CHECKS.json`",
                "- Owned artifacts / files: `.opencode-v2/TEST_CHECKS.json`, `tests.js`",
                1,
            )
            _, _, contradiction_errors = parse_plan(contradiction)
            assert any("not a declared dependency" in e for e in contradiction_errors), contradiction_errors

            styled = valid.replace("- Wave 1: D001", "* **Wave 1**: D001")
            _, _, styled_errors = parse_plan(styled)
            assert not styled_errors, styled_errors

            prose = valid.replace("- Wave 1: D001", "- Wave 1: D001 (root)")
            _, _, prose_errors = parse_plan(prose)
            assert any("non-canonical wave assignment" in e for e in prose_errors), prose_errors

            invalid_role = valid.replace(
                "- Role / role: implementer",
                "- Role / role: builder",
                1,
            )
            _, _, role_errors = parse_plan(invalid_role)
            assert any(
                "not an allowed write-capable Role" in e
                for e in role_errors
            ), role_errors
        finally:
            AGENTS = old
    print("control-guard selftest: OK")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project")
    ap.add_argument("--finalize-acceptance", action="store_true")
    ap.add_argument("--finalize-plan", action="store_true")
    ap.add_argument("--check-acceptance", action="store_true")
    ap.add_argument("--check-plan", action="store_true")
    ap.add_argument("--finalize-all", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if not args.project:
        raise SystemExit("--project required")

    project = Path(args.project).resolve()
    ok = True

    if args.finalize_all or args.finalize_acceptance or args.check_acceptance:
        aok, errors = validate_acceptance(
            project, finalize=(args.finalize_all or args.finalize_acceptance)
        )
        if not aok:
            ok = False
            print("ACCEPTANCE INVALID:")
            for e in errors:
                print(" -", e)

    if args.finalize_all or args.finalize_plan or args.check_plan:
        pok, errors = validate_plan(
            project, finalize=(args.finalize_all or args.finalize_plan)
        )
        if not pok:
            ok = False
            print("PLAN INVALID:")
            for e in errors:
                print(" -", e)

    raise SystemExit(0 if ok else 2)

if __name__ == "__main__":
    main()
