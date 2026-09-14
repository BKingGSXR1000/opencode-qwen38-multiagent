#!/usr/bin/env python3
"""Shared deterministic leaf-contract rules for initial plans and split children."""
import re
import shlex

IMPLEMENTATION_ROLES = frozenset({
    "probe-builder", "implementer", "core-builder", "feature-builder",
    "reasoning-builder", "integrator", "tester", "test-builder",
})
READ_ONLY_ROLES = frozenset({"tester"})
WRITE_ROLES = frozenset(IMPLEMENTATION_ROLES - READ_ONLY_ROLES)
NON_VERIFYING_COMMANDS = frozenset({"true", ":", "echo ok", "echo pass"})
VERIFY_MASKING_MESSAGES = {
    "or": "Verify command uses ||, which can mask a failed check",
    "trailing-success": "Verify command masks failure with trailing ; true/:",
    "set-plus-e": "Verify command disables fail-fast shell behavior",
    "exit-zero": "Verify command forces a successful exit",
}

def _shell_tokens(command: str):
    """Tokenize shell syntax while preserving operators outside quoted payloads.

    A JavaScript/Python expression such as `node -e "a || b"` must not be
    mistaken for shell-level `cmd || true`. Nested shell `sh -c` / `bash -c`
    payloads are validated recursively by validate_verify_command().
    """
    lexer=shlex.shlex(command,posix=True,punctuation_chars=";&|")
    lexer.whitespace_split=True
    lexer.commenters=""
    return list(lexer)

def _masking_errors_from_tokens(tokens):
    errors=[]
    if "||" in tokens:
        errors.append(VERIFY_MASKING_MESSAGES["or"])
    if len(tokens)>=2 and tokens[-2:]==[";","true"]:
        errors.append(VERIFY_MASKING_MESSAGES["trailing-success"])
    if len(tokens)>=2 and tokens[-2:]==[";",":"]:
        errors.append(VERIFY_MASKING_MESSAGES["trailing-success"])
    for i in range(len(tokens)-1):
        if tokens[i]=="set" and tokens[i+1]=="+e":
            errors.append(VERIFY_MASKING_MESSAGES["set-plus-e"])
        if tokens[i]=="exit" and tokens[i+1]=="0":
            errors.append(VERIFY_MASKING_MESSAGES["exit-zero"])
    return errors
SUPERVISOR_RESERVED_PREFIXES = (".opencode-v2/work/", ".opencode-v2/bin/")
SUPERVISOR_RESERVED_EXACT = frozenset({
    ".opencode-v2/control-status.json",
    ".opencode-v2/IMPLEMENTATION_PLAN.guard.json",
    ".opencode-v2/root-rollovers.json",
    ".opencode-v2/reference-gate.json",
    ".opencode-v2/reference-validation-gate.json",
    ".opencode-v2/ACCEPTANCE.ready",
    ".opencode-v2/IMPLEMENTATION_PLAN.ready",
})

def supervisor_reserved_owned_path(path: str):
    p=(path or "").rstrip("/")
    if not p:
        return False
    roots=tuple(prefix.rstrip("/") for prefix in SUPERVISOR_RESERVED_PREFIXES)
    reserved=roots+tuple(SUPERVISOR_RESERVED_EXACT)
    # Reject the reserved path itself, descendants, and an owned directory that
    # would recursively contain any reserved control path (e.g. .opencode-v2/).
    return any(
        p==item or p.startswith(item+"/") or item.startswith(p+"/")
        for item in reserved
    )

def strict_owned_artifact_paths(raw: str):
    if not isinstance(raw,str): return [],"Owned artifacts must be a string"
    raw=raw.strip()
    if raw=="none": return [],""
    if not raw: return [],"Owned artifacts is empty"
    values=re.findall(r"`([^`\r\n]+)`",raw)
    if not values:
        return [],"Owned artifacts must be exact comma-separated backticked project-relative paths, or exact 'none'"
    canonical=", ".join(f"`{value}`" for value in values)
    if raw!=canonical:
        return [],"Owned artifacts must contain paths only: `path`, `path`; move descriptions/parentheticals to Outcome or Done when"
    out=[]
    for value in values:
        if value!=value.strip(): return [],f"Owned artifact path has surrounding whitespace: {value!r}"
        if value.startswith(("/","./","~")): return [],f"Owned artifact must be project-relative: {value}"
        if "\\" in value or any(ch.isspace() for ch in value): return [],f"Owned artifact contains whitespace/backslash: {value}"
        if any(ch in value for ch in "*?[]{}|<>\"'`; ".replace(" ","")): return [],f"Owned artifact contains unsupported shell/path metacharacter: {value}"
        core=value[:-1] if value.endswith("/") else value
        if not core: return [],f"Owned artifact path is invalid: {value}"
        parts=core.split("/")
        if any(part in ("",".","..") for part in parts): return [],f"Owned artifact contains an invalid path segment: {value}"
        if value in out: return [],f"Owned artifact is duplicated: {value}"
        if supervisor_reserved_owned_path(value): return [],f"Owned artifact is supervisor-reserved control state: {value}"
        out.append(value)
    return out,""

def canonical_owned_artifacts(paths):
    return "none" if not paths else ", ".join(f"`{path}`" for path in paths)

def validate_verify_command(verify_command: str):
    command=(verify_command or "").strip()
    errors=[]
    if not command:
        errors.append("missing Verify command")
        return errors
    if command.lower() in NON_VERIFYING_COMMANDS:
        errors.append("Verify command is non-verifying")
    try:
        tokens=_shell_tokens(command)
    except ValueError as exc:
        errors.append(f"Verify command has invalid shell quoting: {exc}")
        return errors

    errors.extend(_masking_errors_from_tokens(tokens))

    # A quoted node/python payload is data to the shell and may legitimately
    # contain ||. But a nested shell -c payload is shell syntax again, so
    # recursively apply the same policy.
    shell_names={"sh","bash","dash","zsh","ksh"}
    for i,tok in enumerate(tokens[:-2]):
        base=tok.rsplit("/",1)[-1]
        if base in shell_names and tokens[i+1] in ("-c","-lc"):
            nested=validate_verify_command(tokens[i+2])
            errors.extend(f"nested shell: {e}" for e in nested)
    # Stable de-duplication.
    return list(dict.fromkeys(errors))


def validate_leaf_contract(role: str, owned_paths, verify_command: str):
    errors=[]; role=(role or "").strip(); paths=list(owned_paths or [])
    if role not in IMPLEMENTATION_ROLES:
        errors.append(f"unknown implementation Role '{role or 'missing'}'")
    elif role in READ_ONLY_ROLES:
        if paths:
            errors.append(f"read-only Role '{role}' must use Owned artifacts: none; use test-builder when the leaf must create or modify a test artifact")
    elif not paths:
        errors.append(f"write-capable Role '{role}' must own at least one durable artifact")
    errors.extend(validate_verify_command(verify_command))
    return errors

def _split_ancestor(leaves,ancestor,descendant):
    seen=set(); current=descendant
    while current and current not in seen:
        seen.add(current); leaf=leaves.get(current)
        if not isinstance(leaf,dict): return False
        parent=leaf.get("parent")
        if parent==ancestor: return True
        current=parent if isinstance(parent,str) else ""
    return False

def ownership_overlap_errors(leaves: dict):
    entries=[]
    for did,leaf in (leaves or {}).items():
        if not isinstance(leaf,dict): continue
        for raw in leaf.get("owned_artifact_paths",[]) or []:
            p=str(raw).rstrip("/")
            if p: entries.append((did,p))
    errors=[]; seen=set()
    for i,(did_a,a) in enumerate(entries):
        for did_b,b in entries[i+1:]:
            if did_a!=did_b and (_split_ancestor(leaves,did_a,did_b) or _split_ancestor(leaves,did_b,did_a)):
                continue
            overlap=(a==b or a.startswith(b+"/") or b.startswith(a+"/"))
            if not overlap: continue
            key=tuple(sorted(((did_a,a),(did_b,b))))
            if key in seen: continue
            seen.add(key)
            if did_a==did_b: errors.append(f"{did_a}: overlapping owned artifact paths `{a}` and `{b}`")
            else: errors.append(f"ownership overlap: {did_a} owns `{a}` while {did_b} owns `{b}`")
    return errors
