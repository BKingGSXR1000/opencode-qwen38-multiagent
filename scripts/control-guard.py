#!/usr/bin/env python3
import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from acceptance_contract import must_acceptance_ids
from control_policy import phase_ready_validator_id
from state_io import atomic_write_text as state_atomic_write_text
from leaf_contract import (
    WRITE_ROLES as SHARED_WRITE_ROLES,
    strict_owned_artifact_paths as shared_strict_owned_artifact_paths,
    canonical_owned_artifacts as shared_canonical_owned_artifacts,
    supervisor_reserved_owned_path as shared_supervisor_reserved_owned_path,
    ownership_overlap_errors as shared_ownership_overlap_errors,
    validate_leaf_contract,
    validate_verify_adequacy,
    validate_contract_challenge_reason,
)

ROOT = Path(os.environ.get("V2_ROOT", str(Path.home() / "AI" / "opencode-qwen38-multiagent-v2")))
AGENTS = ROOT / "xdg" / "config" / "opencode" / "agents"

PLAN_MARKER = "<!-- IMPLEMENTATION_PLAN_COMPLETE -->"
ACC_MARKER = "<!-- ACCEPTANCE_COMPLETE -->"
PLAN_MAX_LINES = 500
RUN_CHECKS_COMMAND = ".opencode-v2/bin/run-checks"
PHASE_READY_PROTOCOL = "V2.6.7c"
PHASE_READY_VALIDATOR = phase_ready_validator_id()
ACCEPTANCE_READY_VALIDATOR = phase_ready_validator_id("ACCEPTANCE.md")
TASK_SHAPE_OWNED_LIMIT = {"S": 2, "M": 3}
TASK_SHAPE_ACCEPTANCE_LIMIT = {"S": 2, "M": 4}
TASK_SHAPE_REPEAT_LIMIT = {"S": 4, "M": 6}
EXPLICIT_STAGE_WORD_RE = re.compile(
    r"(?<!-)\b(?:first|second|third)\b(?!-)|\b(?:then|followed\s+by)\b",
    re.I,
)
EXPLICIT_PAREN_STAGE_SEQUENCE_RE = re.compile(
    r"(?:\(1\).*?\(2\)|\(a\).*?\(b\))",
    re.I | re.S,
)
EXPLICIT_MIXED_STAGE_SEQUENCE_RE = re.compile(
    r"(?:\(1\)|\(a\)).*?\b(?:then|followed\s+by)\b",
    re.I | re.S,
)

def has_compound_stage_sequence(text):
    """Detect explicit staged work without treating arbitrary IDs as stages."""
    text=str(text or "")
    return bool(
        len(EXPLICIT_STAGE_WORD_RE.findall(text))>=2
        or EXPLICIT_PAREN_STAGE_SEQUENCE_RE.search(text)
        or EXPLICIT_MIXED_STAGE_SEQUENCE_RE.search(text)
    )
EXTERNAL_ACQUISITION_RE = re.compile(
    r"(?:\b(?:fetch(?:ed|ing)?|download(?:ed|ing)?|vendor(?:ed|ing)?|research(?:ed|ing)?)\b"
    r".{0,100}\b(?:https?://|external|public\s+source|authoritative|cdn|jpl|horizons|unpkg|jsdelivr)\b"
    r"|\b(?:curl|wget)\s+https?://)",
    re.I,
)
PREEXISTING_EXTERNAL_STATE_RE = re.compile(
    r"\b(?:already|previously|preexisting|pre-existing|pre)\s*[- ]?\s*"
    r"(?:fetched|downloaded|vendored|researched)\b",
    re.I,
)

def external_acquisition_match(text):
    # Explicitly pre-existing/frozen artifacts are inputs, not acquisition
    # performed by this leaf. Unqualified/active acquisition remains fail-closed.
    scrubbed=PREEXISTING_EXTERNAL_STATE_RE.sub("preexisting-artifact",str(text or ""))
    return EXTERNAL_ACQUISITION_RE.search(scrubbed)
HOST_REMEDIATION_RE = re.compile(
    r"\b(?:sudo|systemctl|service\s+[-\w.@:]+\s+(?:start|stop|restart|reload|force-reload)|"
    r"daemon-reload|apt(?:-get)?\s+install|dnf\s+install|yum\s+install|pacman\s+-S|"
    r"restart\s+(?:the\s+)?(?:service|daemon)|enable\s+(?:the\s+)?(?:service|daemon))\b",
    re.I,
)
INTERNAL_EXTERNAL_REFERENCE_RE = re.compile(
    r"\b(?:external(?:ly)?\s+(?:authoritative|maintained|scientific|reference)\s+"
    r"(?:data|source|truth|reference|interface)|authoritative\s+(?:external\s+)?"
    r"(?:data|source|reference)|official\s+(?:external\s+)?"
    r"(?:dataset|api|source|reference)|live\s+external\s+(?:api|service|data))\b",
    re.I,
)

def internal_external_reference_violation(text: str) -> bool:
    """Ignore explicit negative statements while preserving positive requirements."""
    normalized=re.sub(r"\s+"," ",str(text or ""))
    for clause in re.split(r"[.;]+", normalized):
        match=INTERNAL_EXTERNAL_REFERENCE_RE.search(clause)
        if not match:
            continue
        prefix=clause[:match.start()]
        suffix=clause[match.end():]
        negatives=list(re.finditer(r"\b(?:no|without|not|never)\b",prefix,re.I))
        if negatives:
            tail=prefix[negatives[-1].end():]
            crossed_contrast=bool(re.search(r"\b(?:but|however|yet)\b",tail,re.I))
            direct_negative=bool(re.fullmatch(
                r"\s*(?:(?:a|an|any|the)\s+)?",tail,re.I
            ))
            negative_requirement=bool(re.search(
                r"\b(?:required|needed|used|consulted|relied\s+upon)\b",suffix,re.I
            ))
            negative_dependency=bool(re.search(
                r"\b(?:dependence|dependency|reliance|requirement|need)\s+(?:on|for)\b",
                tail,re.I,
            ))
            negative_action=bool(re.search(
                r"\b(?:require|requires|required\s+to|need|needs|needed\s+to|"
                r"use|uses|used\s+to|consult|consults|consulted|"
                r"rely\s+on|relies\s+on|relied\s+on|"
                r"depend\s+on|depends\s+on|depended\s+on)\s+"
                r"(?:(?:a|an|any|the)\s+)?$",
                tail,re.I,
            ))
            if not crossed_contrast and (
                direct_negative
                or negative_requirement
                or negative_dependency
                or negative_action
            ):
                continue
        return True
    return False

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

WRITE_ROLES = set(SHARED_WRITE_ROLES)

ALIASES = {
    "outcome": ("Outcome",),
    "owned_artifacts": ("Owned artifacts", "Owned artifacts / files", "Owned files"),
    "launch_deps": ("Launch deps", "Launch deps / depends_on", "Depends on"),
    "contract_deps": ("Contract deps",),
    "verify_deps": ("Verify deps",),
    "acceptance_ids": ("Acceptance IDs", "Acceptance IDs / acceptance", "Acceptance"),
    "complexity": ("Complexity", "Complexity / size"),
    "independent_stages": ("Independent stages",),
    "expected_compactions": ("Expected compactions",),
    "repeated_operations": ("Repeated operations",),
    "role": ("Role", "Role / role", "Preferred role"),
    "parallel": ("Parallel-safe with", "Parallel-safe"),
    "verify_command": ("Verify command", "Verify command / verify", "Verify"),
    "done_when": ("Done when",),
}

def atomic_write(path: Path, text: str):
    state_atomic_write_text(path,text)

def final_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""

def top_level_plan_complete(text: str) -> bool:
    lines=text.splitlines()
    return bool(
        len(lines)>=2
        and lines[0].strip()=="# Implementation Plan"
        and lines[1].strip()=="Status: COMPLETE"
    )

def phase_ready_text(artifact_path: Path, artifact: str, marker: str) -> str:
    digest=hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return (
        "status=complete\n"
        f"protocol={PHASE_READY_PROTOCOL}\n"
        f"artifact={artifact}\n"
        f"marker={marker}\n"
        f"validated={phase_ready_validator_id(artifact)}\n"
        f"artifact_sha256={digest}\n"
    )

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
    return shared_strict_owned_artifact_paths(raw)

def canonical_owned_artifacts(paths):
    return shared_canonical_owned_artifacts(paths)

def supervisor_reserved_owned_path(path: str):
    return shared_supervisor_reserved_owned_path(path)

def ownership_overlap_errors(leaves: dict):
    return shared_ownership_overlap_errors(leaves)


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


VERIFY_PROJECT_FILE_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:js|mjs|cjs|py|json|html|css|sh|md|txt|yaml|yml|toml)$",
    re.I,
)
VERIFY_PROJECT_GLOB_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.*?\[\]-]+\."
    r"(?:js|mjs|cjs|py|json|html|css|sh|md|txt|yaml|yml|toml)$",
    re.I,
)


def _normalize_verify_path_candidate(raw: str, *, allow_glob=False):
    if not isinstance(raw,str) or not raw:
        return ""
    value=raw.strip().strip("(),;")
    if value.startswith("./"):
        value=value[2:]
    if (
        not value
        or value.startswith(("http://","https://","/","$"))
        or ".." in Path(value).parts
        or any(mark in value for mark in ("$(", "${", ">", "<", "|", "&"))
        or "=" in value
    ):
        return ""
    if value==RUN_CHECKS_COMMAND or value.startswith(".opencode-v2/"):
        return value
    pattern=VERIFY_PROJECT_GLOB_RE if allow_glob else VERIFY_PROJECT_FILE_RE
    return value if pattern.fullmatch(value) else ""


def _python_inline_verify_paths(code: str):
    try:
        tree=ast.parse(code)
    except (SyntaxError,ValueError):
        return []
    out=[]
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call) or not node.args:
            continue
        fn=node.func
        name=""
        if isinstance(fn,ast.Name):
            name=fn.id
        elif isinstance(fn,ast.Attribute):
            name=fn.attr
        if name not in {"open","Path","glob","iglob"}:
            continue
        arg=node.args[0]
        if not isinstance(arg,ast.Constant) or not isinstance(arg.value,str):
            continue
        candidate=_normalize_verify_path_candidate(
            arg.value,allow_glob=name in {"glob","iglob"}
        )
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def verify_referenced_paths(command: str):
    """Return explicit/literal project-relative file inputs from a shell Verify."""
    try:
        tokens=shlex.split(command or "",posix=True)
    except ValueError:
        return []
    out=[]
    for raw in tokens:
        if not isinstance(raw,str) or not raw:
            continue
        candidate=_normalize_verify_path_candidate(raw)
        if candidate and candidate not in out:
            out.append(candidate)

    # Inline Python is common in canonical Verify commands. Inspect only literal
    # arguments to known file/glob APIs; dynamic paths remain outside this
    # conservative static provenance check.
    for index,token in enumerate(tokens[:-2]):
        exe=Path(token).name.lower()
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?",exe) and tokens[index+1]=="-c":
            for candidate in _python_inline_verify_paths(tokens[index+2]):
                if candidate not in out:
                    out.append(candidate)
    return out


def _dependency_closure_ids(leaves: dict, did: str):
    seen=set(); stack=[]
    leaf=leaves.get(did)
    if not isinstance(leaf,dict):
        return seen
    for kind in ("launch_deps","contract_deps","verify_deps"):
        values=leaf.get(kind,[])
        if isinstance(values,list):
            stack.extend(x for x in values if isinstance(x,str))
    while stack:
        dep=stack.pop()
        if dep in seen or dep not in leaves:
            continue
        seen.add(dep)
        child=leaves.get(dep)
        if not isinstance(child,dict):
            continue
        for kind in ("launch_deps","contract_deps","verify_deps"):
            values=child.get(kind,[])
            if isinstance(values,list):
                stack.extend(x for x in values if isinstance(x,str))
    return seen


def _expanded_verify_paths(project: Path, path: str):
    if not any(mark in path for mark in ("*", "?", "[")):
        return [path]
    try:
        matches=[
            candidate.relative_to(project).as_posix()
            for candidate in sorted(project.glob(path))
            if candidate.is_file()
        ]
    except (OSError,ValueError):
        matches=[]
    return matches or [path]


def verify_path_provenance_errors(project: Path, leaves: dict):
    owners={}
    for owner_id, owner_leaf in (leaves or {}).items():
        if not isinstance(owner_leaf,dict):
            continue
        for path in owner_leaf.get("owned_artifact_paths",[]) or []:
            if isinstance(path,str) and path:
                owners.setdefault(path,[]).append(owner_id)

    errors=[]
    project=project.resolve()
    for did,leaf in (leaves or {}).items():
        if not isinstance(leaf,dict):
            continue
        own=[x for x in leaf.get("owned_artifact_paths",[]) or [] if isinstance(x,str)]
        depset=_dependency_closure_ids(leaves,did)
        for spec in verify_referenced_paths(leaf.get("verify_command","")):
            for path in _expanded_verify_paths(project,spec):
                handoff_progress=(
                    f".opencode-v2/work/{did}.progress.md"
                    if leaf.get("split_handoff_only") is True else ""
                )
                if (
                    path==RUN_CHECKS_COMMAND
                    or path_is_within(path,own)
                    or (handoff_progress and path==handoff_progress)
                ):
                    continue
                matching=[]
                for owned_path,owner_ids in owners.items():
                    if path==owned_path or path.startswith(owned_path.rstrip("/")+"/"):
                        matching.extend(owner_ids)
                matching=[x for x in matching if x!=did]
                if matching:
                    if not any(x in depset for x in matching):
                        errors.append(
                            f"{did}: Verify command requires '{path}' owned by "
                            f"{','.join(sorted(set(matching)))}, but that owner is not a declared dependency"
                        )
                    continue
                candidate=(project/path).resolve(strict=False)
                try:
                    candidate.relative_to(project)
                except ValueError:
                    errors.append(f"{did}: Verify command path escapes project: '{path}'")
                    continue
                if candidate.is_file():
                    continue
                errors.append(
                    f"{did}: Verify command references missing/unowned project artifact '{path}'; "
                    "assign it to this leaf or a declared dependency"
                )
    return errors


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

        if did in leaves:
            errors.append(f"duplicate deliverable ID: {did}")
            continue

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
            "independent_stages": vals["independent_stages"].strip(),
            "expected_compactions": vals["expected_compactions"].strip(),
            "repeated_operations": vals["repeated_operations"].strip(),
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
            ("Independent stages", leaf["independent_stages"]),
            ("Expected compactions", leaf["expected_compactions"]),
            ("Repeated operations", leaf["repeated_operations"]),
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

        if leaf["independent_stages"] and leaf["independent_stages"] != "1":
            errors.append(
                f"{did}: Independent stages must be exactly 1; split compound work before execution"
            )
        if leaf["expected_compactions"] and leaf["expected_compactions"] != "0":
            errors.append(
                f"{did}: Expected compactions must be exactly 0; use a durable handoff and a fresh worker"
            )
        repeated=None
        if leaf["repeated_operations"]:
            if not re.fullmatch(r"\d+", leaf["repeated_operations"]):
                errors.append(f"{did}: Repeated operations must be a non-negative integer")
            else:
                repeated=int(leaf["repeated_operations"])
        if leaf["complexity"] in TASK_SHAPE_OWNED_LIMIT:
            owned_count=len(leaf.get("owned_artifact_paths",[]))
            owned_limit=TASK_SHAPE_OWNED_LIMIT[leaf["complexity"]]
            if owned_count>owned_limit:
                errors.append(
                    f"{did}: {leaf['complexity']} leaf owns {owned_count} artifact paths; "
                    f"maximum is {owned_limit}; split into smaller durable handoffs"
                )
            acceptance_limit=TASK_SHAPE_ACCEPTANCE_LIMIT[leaf["complexity"]]
            if len(leaf["acceptance_ids"])>acceptance_limit:
                errors.append(
                    f"{did}: {leaf['complexity']} leaf covers {len(leaf['acceptance_ids'])} Acceptance IDs; "
                    f"maximum is {acceptance_limit}; split independent acceptance work"
                )
            if repeated is not None:
                repeat_limit=TASK_SHAPE_REPEAT_LIMIT[leaf["complexity"]]
                if repeated>repeat_limit:
                    errors.append(
                        f"{did}: {leaf['complexity']} leaf declares {repeated} repeated operations; "
                        f"maximum is {repeat_limit}; split the batch into fresh-context leaves"
                    )

        shape_text=" ".join((leaf.get("outcome",""),leaf.get("done_when","")))
        # Two or more explicit stage markers are strong evidence of a bundled leaf.
        if has_compound_stage_sequence(shape_text):
            errors.append(
                f"{did}: Outcome/Done when describes multiple explicit stages; split them into separate leaves"
            )
        if leaf.get("role")!="probe-builder" and external_acquisition_match(leaf.get("outcome","")):
            errors.append(
                f"{did}: external acquisition/research is bundled into a non-probe leaf; "
                "freeze external evidence in a separate probe-builder leaf and hand it off durably"
            )
        if leaf.get("role")=="probe-builder" and HOST_REMEDIATION_RE.search(shape_text):
            errors.append(
                f"{did}: probe-builder may observe and record host prerequisites but must not remediate "
                "host services/packages; record the blocker and re-plan instead"
            )

        if not leaf["role"]:
            errors.append(f"{did}: missing Role")
        if not leaf["acceptance_ids"]:
            errors.append(f"{did}: missing Acceptance IDs")
        if not leaf["parallel"]:
            errors.append(f"{did}: missing Parallel-safe field")

        writes = bool(leaf.get("owned_artifact_paths"))
        for contract_error in validate_leaf_contract(
            leaf["role"], leaf.get("owned_artifact_paths", []), leaf["verify_command"]
        ):
            errors.append(f"{did}: {contract_error}")
        for adequacy_error in validate_verify_adequacy(
            leaf.get("done_when",""), leaf["verify_command"]
        ):
            errors.append(f"{did}: {adequacy_error}")
        if writes and leaf["role"] in WRITE_ROLES and not role_can_write(leaf["role"]):
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
            errors.append("dependency cycle: " + " -> ".join(stack + [node]))
            return
        visiting.add(node)
        for kind in ("launch_deps","contract_deps","verify_deps"):
            for dep in leaves[node][kind]:
                if dep in leaves:
                    visit(dep, stack + [node])
        visiting.remove(node)
        done.add(node)

    for did in leaves:
        visit(did, [])

    for did, leaf in leaves.items():
        if did not in waves:
            continue
        for dep_kind in ("launch_deps","contract_deps"):
            label="Launch" if dep_kind=="launch_deps" else "Contract"
            for dep in leaf[dep_kind]:
                if dep in waves and waves[dep] >= waves[did]:
                    errors.append(
                        f"{did}: {label} dep {dep} wave {waves[dep]} is not earlier than wave {waves[did]}"
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
            if leaf["role"] != "test-builder":
                errors.append(
                    f"{leaf['id']}: TEST_CHECKS leaf writes a manifest and must use test-builder"
                )
            if leaf["verify_command"] != RUN_CHECKS_COMMAND:
                errors.append(
                    f"{leaf['id']}: TEST_CHECKS Verify command must be exact "
                    f"{RUN_CHECKS_COMMAND}"
                )

    return leaves, waves, errors

REFERENCE_POLICY_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?Reference policy\s*:\s*"
    r"(none|internal|external-required)\s*$"
)

def reference_policies(text: str):
    return [value.lower() for value in REFERENCE_POLICY_RE.findall(str(text or ""))]

def reference_policy(text: str):
    values=reference_policies(text)
    return values[0] if len(values)==1 else ""

def plan_acceptance_coverage_errors(leaves, acceptance_text: str):
    must_ids=must_acceptance_ids(acceptance_text)
    if not must_ids:
        return ["acceptance contract has no exact MUST Axxx IDs for plan coverage"]
    if len(must_ids)!=len(set(must_ids)):
        return ["acceptance contract has duplicate MUST Axxx IDs for plan coverage"]
    planned=set()
    for did,leaf in (leaves or {}).items():
        if not isinstance(leaf,dict):
            return [f"{did}: plan leaf is not an object for acceptance coverage"]
        ids=leaf.get("acceptance_ids") or []
        if not isinstance(ids,list):
            return [f"{did}: acceptance_ids is not an array for acceptance coverage"]
        planned.update(str(aid) for aid in ids)
    must=set(must_ids)
    missing=sorted(must-planned)
    extra=sorted(planned-must)
    if not missing and not extra:
        return []
    detail=[]
    if missing:
        detail.append("missing="+",".join(missing))
    if extra:
        detail.append("extra="+",".join(extra))
    return [
        "plan acceptance_ids must cover every and only MUST Axxx IDs; "
        + " ".join(detail)
    ]

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
        policies=reference_policies(text)
        policy=policies[0] if len(policies)==1 else ""
        if len(policies)!=1:
            errors.append(
                "Reference policy must appear exactly once: none|internal|external-required"
            )
        if policy == "internal" and internal_external_reference_violation(text):
            errors.append(
                "internal Reference policy cannot require externally authoritative/reference truth"
            )
        must_ids=must_acceptance_ids(text)
        if not must_ids:
            errors.append("no machine-readable MUST lines; use exact '- [ ] A001: description'")
        elif len(must_ids)!=len(set(must_ids)):
            errors.append("duplicate machine-readable MUST Axxx IDs")

    if errors:
        ready.unlink(missing_ok=True)
        atomic_write(err, "\n".join(errors) + "\n")
        return False, errors

    if err.exists():
        err.unlink()
    if finalize:
        atomic_write(
            ready,
            phase_ready_text(path,"ACCEPTANCE.md","ACCEPTANCE_COMPLETE"),
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


PLAN_REPAIR_PROTOCOL = "v2-structured-plan-repair-v1"
CONTROL_POLICY_REVALIDATION_SOURCE = "control-policy-revalidation"

def _structured_plan_key_map(ctrl: Path):
    path=ctrl/"IMPLEMENTATION_PLAN.structured-map.json"
    try:
        data=json.loads(path.read_text())
    except Exception:
        return {}
    mapping=data.get("id_to_key") if isinstance(data,dict) else {}
    return mapping if isinstance(mapping,dict) else {}

def _repair_leaf_baseline(ctrl: Path, affected):
    path=ctrl/"IMPLEMENTATION_PLAN.structured.json"
    try:
        raw=path.read_bytes()
        data=json.loads(raw)
    except Exception:
        return {"source_sha256":"","affected_leaf_sha256":{}}
    leaves=data.get("leaves") if isinstance(data,dict) else None
    hashes={}
    if isinstance(leaves,list):
        wanted=set(affected)
        for item in leaves:
            if not isinstance(item,dict):
                continue
            key=item.get("key")
            if key not in wanted:
                continue
            hashes[key]=hashlib.sha256(
                json.dumps(
                    item,sort_keys=True,separators=(",",":"),ensure_ascii=True
                ).encode()
            ).hexdigest()
    return {
        "source_sha256":hashlib.sha256(raw).hexdigest(),
        "affected_leaf_sha256":hashes,
    }


def _planner_restart_count_for_repair(ctrl: Path):
    path=ctrl/"work"/"planner-restarts.json"
    try:
        data=json.loads(path.read_text())
        count=int(data.get("count") or 0)
    except FileNotFoundError:
        return 0
    except Exception:
        return 0
    return max(0,count)


def write_plan_repair_packet(ctrl: Path, errors, source="control-guard"):
    mapping=_structured_plan_key_map(ctrl)
    affected=[]
    entries=[]
    for raw in errors:
        message=str(raw)
        dids=list(dict.fromkeys(re.findall(r"\bD\d{3}\b",message)))
        keys=[]
        for did in dids:
            key=mapping.get(did)
            if isinstance(key,str) and key and key not in keys:
                keys.append(key)
                if key not in affected:
                    affected.append(key)
        entries.append({
            "message":message,
            "deliverables":dids,
            "keys":keys,
        })

    # A repair is safely leaf-scoped only when every validation error maps to
    # at least one concrete structured leaf.  If any error is global (for
    # example missing MUST acceptance coverage), preserving only the keyed
    # subset would make the required correction inaccessible to the planner.
    # Match structured_plan.compile_plan(): global errors force whole-plan
    # repair while leaf keys/order remain protected by planner validation.
    if any(not entry["keys"] for entry in entries):
        affected=[]

    repair_path=ctrl/"IMPLEMENTATION_PLAN.repair.json"
    existing={}
    try:
        candidate=json.loads(repair_path.read_text())
        if isinstance(candidate,dict):
            existing=candidate
    except Exception:
        pass

    policy_source=(source==CONTROL_POLICY_REVALIDATION_SOURCE)
    if (
        not policy_source
        and affected
        and existing.get("source")==CONTROL_POLICY_REVALIDATION_SOURCE
        and set(existing.get("affected_keys") or [])==set(affected)
    ):
        source=CONTROL_POLICY_REVALIDATION_SOURCE
        policy_source=True

    payload={
        "protocol":PLAN_REPAIR_PROTOCOL,
        "source":source,
        "whole_plan":not bool(affected),
        "affected_keys":affected,
        "errors":entries,
    }
    if policy_source and affected:
        if existing.get("source")==CONTROL_POLICY_REVALIDATION_SOURCE:
            baseline=existing.get("baseline")
            planner_baseline=existing.get("planner_restart_baseline")
        else:
            baseline=None
            planner_baseline=None
        if not isinstance(baseline,dict):
            baseline=_repair_leaf_baseline(ctrl,affected)
        if not isinstance(planner_baseline,int) or planner_baseline<0:
            planner_baseline=_planner_restart_count_for_repair(ctrl)
        payload["baseline"]=baseline
        payload["planner_restart_baseline"]=planner_baseline

    atomic_write(
        repair_path,
        json.dumps(payload,indent=2,sort_keys=True)+"\n",
    )

def runtime_repair_change_errors(ctrl: Path):
    """Reject a syntactically valid no-op after a runtime contract repair."""
    repair_path=ctrl/"IMPLEMENTATION_PLAN.repair.json"
    try:
        repair=json.loads(repair_path.read_text())
    except FileNotFoundError:
        return []
    except Exception:
        return ["runtime repair packet is not valid JSON"]
    runtime_sources={
        "runtime-split-parent-contract",
        "runtime-leaf-contract-challenge",
        CONTROL_POLICY_REVALIDATION_SOURCE,
    }
    if (
        not isinstance(repair,dict)
        or repair.get("source") not in runtime_sources
    ):
        return []
    if repair.get("source")=="runtime-leaf-contract-challenge":
        challenge=repair.get("challenge")
        reason=challenge.get("reason") if isinstance(challenge,dict) else ""
        challenge_errors=validate_contract_challenge_reason(reason)
        if challenge_errors:
            return [
                "runtime leaf contract challenge invalid: "+challenge_errors[0]
            ]
    challenge=repair.get("challenge") if isinstance(repair.get("challenge"),dict) else {}
    nested=challenge.get("nested_repair")
    if repair.get("source")=="runtime-leaf-contract-challenge" and isinstance(nested,dict):
        if (
            nested.get("protocol")!="v2-split-contract-producer-repair-v1"
            or repair.get("affected_keys")!=[nested.get("producer_key")]
        ):
            return ["nested repair producer key or lineage invalid"]
        try:
            raw=json.loads((ctrl/"IMPLEMENTATION_PLAN.structured.json").read_text())
            by_key={x["key"]:x for x in raw["leaves"]}
            producer=by_key[nested["producer_key"]]
            parent=by_key[nested["root_key"]]
            producer_verify=str(producer.get("verify_command") or "")
            parent_verify=str(parent.get("verify_command") or "")
            if hashlib.sha256(producer_verify.encode()).hexdigest()==nested["producer_verify_sha256"]:
                return ["nested repair must strengthen the upstream producer Verify"]
            if hashlib.sha256(parent_verify.encode()).hexdigest()!=nested["root_verify_sha256"]:
                return ["nested repair cannot change the original final Verify"]
            if (
                sorted(producer.get("owned_artifacts") or [])!=nested["producer_owned_paths"]
                or sorted(parent.get("owned_artifacts") or [])!=nested["root_owned_paths"]
            ):
                return ["nested repair may not change producer or parent ownership"]
            # The original final Verify is structurally immutable. An independently
            # changed upstream exact Verify is positive repair evidence here;
            # the generic same-leaf baseline check would incorrectly demand a
            # second edit when salvaging a valid partial planner candidate.
            return []
        except (KeyError,TypeError,ValueError):
            return ["nested repair target structural fields invalid"]
    baseline=repair.get("baseline")
    expected=baseline.get("affected_leaf_sha256") if isinstance(baseline,dict) else None
    if not isinstance(expected,dict) or not expected:
        return ["runtime repair packet lacks affected leaf baselines"]
    try:
        raw=json.loads((ctrl/"IMPLEMENTATION_PLAN.structured.json").read_text())
    except Exception:
        return ["runtime repair structured source is unavailable"]
    leaves=raw.get("leaves") if isinstance(raw,dict) else None
    actual={
        str(item.get("key")):hashlib.sha256(
            json.dumps(item,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
        ).hexdigest()
        for item in leaves if isinstance(leaves,list) and isinstance(item,dict)
        and isinstance(item.get("key"),str)
    }
    if not any(actual.get(key)!=digest for key,digest in expected.items()):
        return ["runtime repair packet unresolved: no affected structured leaf changed"]
    return []

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
        if not top_level_plan_complete(text):
            errors.append("top-level plan Status must be COMPLETE before finalization")
        if not re.search(
            r"(?ms)^##\s+Planner checkpoint\s*\nStatus:\s*COMPLETE\s*$",
            text,
        ):
            errors.append("Planner checkpoint Status must be COMPLETE before finalization")
        leaves, waves, parse_errors = parse_plan(text)
        errors.extend(parse_errors)
        leaves = merge_split_leaf_overlay(project, leaves)
        errors.extend(ownership_overlap_errors(leaves))
        errors.extend(verify_path_provenance_errors(project, leaves))
        acceptance = ctrl / "ACCEPTANCE.md"
        try:
            acceptance_text = acceptance.read_text(errors="replace")
        except OSError as exc:
            acceptance_text = ""
            errors.append(f"ACCEPTANCE.md unavailable for plan coverage: {exc}")
        policy = reference_policy(acceptance_text)
        errors.extend(plan_acceptance_coverage_errors(leaves, acceptance_text))
        if policy == "internal":
            for did, leaf in leaves.items():
                if leaf.get("role") != "probe-builder":
                    continue
                section = " ".join(str(leaf.get(k, "")) for k in (
                    "name", "owned_artifacts", "verify_command", "done_when"
                ))
                if internal_external_reference_violation(section):
                    errors.append(
                        f"{did}: internal Reference policy forbids an externally authoritative/reference probe"
                    )

    runtime_repair_errors=runtime_repair_change_errors(ctrl)
    errors.extend(runtime_repair_errors)
    if errors:
        was_ready=ready.is_file() and manifest_path.is_file()
        ready.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        atomic_write(err, "\n".join(errors) + "\n")
        if not runtime_repair_errors:
            write_plan_repair_packet(
                ctrl,
                errors,
                source=(
                    CONTROL_POLICY_REVALIDATION_SOURCE
                    if was_ready else "control-guard"
                ),
            )
        return False, errors

    if err.exists():
        err.unlink()
    (ctrl/"IMPLEMENTATION_PLAN.repair.json").unlink(missing_ok=True)

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
            phase_ready_text(
                path,"IMPLEMENTATION_PLAN.md","IMPLEMENTATION_PLAN_COMPLETE"
            ),
        )
    return True, []

def selftest():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fake = td / "agents"
        fake.mkdir()
        for name, edit in (
            ("implementer", "allow"),
            ("tester", "deny"),
            ("test-builder", "allow"),
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
- Independent stages: 1
- Expected compactions: 0
- Repeated operations: 1
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
- Independent stages: 1
- Expected compactions: 0
- Repeated operations: 1
- Deep reasoning: no
- Role / role: test-builder
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
            ).replace(
                "- Complexity / size: S",
                "- Complexity / size: M",
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
                "unknown implementation Role" in e
                for e in role_errors
            ), role_errors

            duplicate = valid.replace(
                "## 5. Execution Waves",
                """### D001 — Duplicate
- Outcome: duplicate
- Owned artifacts / files: `other.js`
- Launch deps / depends_on: (none)
- Contract deps: (none)
- Verify deps: (none)
- Acceptance IDs / acceptance: A001
- Complexity / size: S
- Independent stages: 1
- Expected compactions: 0
- Repeated operations: 1
- Deep reasoning: no
- Role / role: implementer
- Parallel-safe with: (none)
- Verify command / verify: `test -s other.js`
- Done when: duplicate exists
## 5. Execution Waves""",
                1,
            )
            _, _, duplicate_errors = parse_plan(duplicate)
            assert any("duplicate deliverable ID: D001" in e for e in duplicate_errors), duplicate_errors

            tester_writer = valid.replace(
                "- Role / role: implementer",
                "- Role / role: tester",
                1,
            )
            _, _, tester_errors = parse_plan(tester_writer)
            assert any("read-only Role 'tester'" in e for e in tester_errors), tester_errors

            overlap_leaves={
                "D001":{"owned_artifact_paths":["public/"]},
                "D002":{"owned_artifact_paths":["public/index.html"]},
            }
            overlap_errors=ownership_overlap_errors(overlap_leaves)
            assert any("ownership overlap" in e for e in overlap_errors), overlap_errors

            reserved_paths,reserved_error=strict_owned_artifact_paths(
                "`.opencode-v2/work/D001.ready`"
            )
            assert not reserved_paths and "supervisor-reserved" in reserved_error, reserved_error

            masked = valid.replace(
                "`test -s app.js`",
                "`test -s app.js || true`",
                1,
            )
            _, _, masked_errors = parse_plan(masked)
            assert any("can mask a failed check" in e for e in masked_errors), masked_errors

            dependency_cycle = valid.replace(
                "- Verify deps: (none)",
                "- Verify deps: [D002]",
                1,
            )
            _, _, cycle_errors = parse_plan(dependency_cycle)
            assert any("dependency cycle" in e for e in cycle_errors), cycle_errors

            contract_wave = valid.replace(
                "- Launch deps / depends_on: [D001]",
                "- Launch deps / depends_on: (none)",
                1,
            ).replace(
                "- Contract deps: (none)",
                "- Contract deps: [D001]",
                2,
            ).replace(
                "- Wave 2: D002",
                "- Wave 1: D002",
                1,
            )
            _, _, contract_wave_errors = parse_plan(contract_wave)
            assert any("Contract dep D001" in e for e in contract_wave_errors), contract_wave_errors

            # V2.6.16 MACHINE-ENFORCED TASK-SHAPE REGRESSION
            compound = valid.replace(
                "- Outcome: scaffold",
                "- Outcome: (a) acquire source; (b) implement scaffold; (c) validate it",
                1,
            )
            _, _, compound_errors = parse_plan(compound)
            assert any("multiple explicit stages" in e for e in compound_errors), compound_errors

            compaction = valid.replace("- Expected compactions: 0", "- Expected compactions: 1", 1)
            _, _, compaction_errors = parse_plan(compaction)
            assert any("Expected compactions must be exactly 0" in e for e in compaction_errors), compaction_errors

            repeats = valid.replace("- Repeated operations: 1", "- Repeated operations: 9", 1)
            _, _, repeat_errors = parse_plan(repeats)
            assert any("repeated operations" in e for e in repeat_errors), repeat_errors

            too_many_s_paths = valid.replace(
                "- Owned artifacts / files: `app.js`",
                "- Owned artifacts / files: `app.js`, `app.css`, `app.html`",
                1,
            )
            _, _, path_errors = parse_plan(too_many_s_paths)
            assert any("S leaf owns 3 artifact paths" in e for e in path_errors), path_errors

            too_many_s_acceptance = valid.replace(
                "- Acceptance IDs / acceptance: A001",
                "- Acceptance IDs / acceptance: A001, A002, A003",
                1,
            )
            _, _, acceptance_errors = parse_plan(too_many_s_acceptance)
            assert any("S leaf covers 3 Acceptance IDs" in e for e in acceptance_errors), acceptance_errors

            external_bundle = valid.replace(
                "- Outcome: scaffold",
                "- Outcome: implement scaffold using coefficients fetched from a public source URL",
                1,
            )
            _, _, external_errors = parse_plan(external_bundle)
            assert any("external acquisition/research" in e for e in external_errors), external_errors

            probe_host_fix = valid.replace(
                "- Outcome: scaffold",
                "- Outcome: probe environment and systemctl restart snapd.apparmor if needed",
                1,
            ).replace(
                "- Role / role: implementer",
                "- Role / role: probe-builder",
                1,
            )
            _, _, probe_host_errors = parse_plan(probe_host_fix)
            assert any("must not remediate host" in e for e in probe_host_errors), probe_host_errors
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
