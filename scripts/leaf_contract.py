#!/usr/bin/env python3
"""Shared deterministic leaf-contract rules for initial plans and split children."""
import ast
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

IMPLEMENTATION_ROLES = frozenset({
    "probe-builder", "implementer", "core-builder", "feature-builder",
    "reasoning-builder", "integrator", "tester", "test-builder",
})
READ_ONLY_ROLES = frozenset({"tester"})
WRITE_ROLES = frozenset(IMPLEMENTATION_ROLES - READ_ONLY_ROLES)
NON_VERIFYING_COMMANDS = frozenset({"true", ":", "echo ok", "echo pass"})
CONTRACT_CHALLENGE_NEGATION_RE = re.compile(
    r"\b(?:no|none|not|without)\b.{0,48}\b(?:contradiction|conflict|mismatch|incompatib)",
    re.I,
)
CONTRACT_CHALLENGE_CONFLICT_RE = re.compile(
    r"\b(?:contradict(?:s|ion|ory)?|conflict(?:s|ing)?|mismatch|incompatib(?:le|ility)|"
    r"requires?\b.{0,96}\b(?:but|while|whereas)\b|"
    r"(?:cannot|can't)\s+both\s+(?:hold|be\s+true|be\s+satisfied)|"
    r"(?:cannot|can't|can\s+never|could\s+never)\b.{0,96}\b(?:pass|satisfy|succeed)\b|"
    r"no\s+implementation\b.{0,96}\bcan\s+(?:ever\s+)?(?:pass|satisfy|succeed)\b|"
    r"impossible\s+to\s+(?:pass|satisfy|succeed)|unsatisfiable)",
    re.I,
)
CONTRACT_CHALLENGE_AUTHORITY_RE = re.compile(
    r"\b(?:acceptance|done[- ]?when|contract|verify[_ -]?command|A\d{3})\b",
    re.I,
)

VERIFY_MASKING_MESSAGES = {
    "or": "Verify command uses ||, which can mask a failed check",
    "trailing-success": "Verify command masks failure with trailing ; true/:",
    "set-plus-e": "Verify command disables fail-fast shell behavior",
    "exit-zero": "Verify command forces a successful exit",
}

def validate_contract_challenge_reason(reason: str):
    """Validate a fail-closed worker claim that canonical Verify contradicts contract."""
    text=" ".join(str(reason or "").split())
    errors=[]
    if len(text)<20 or len(text)>800:
        errors.append("contract challenge reason must be 20..800 chars")
        return errors
    lower=text.lower()
    if "verify" not in lower:
        errors.append("contract challenge must name canonical Verify")
    if not CONTRACT_CHALLENGE_AUTHORITY_RE.search(text):
        errors.append(
            "contract challenge must name Acceptance, Done-when, contract, verify_command, or Axxx authority"
        )
    if CONTRACT_CHALLENGE_NEGATION_RE.search(text):
        errors.append("contract challenge explicitly negates a contract contradiction")
    if not CONTRACT_CHALLENGE_CONFLICT_RE.search(text):
        errors.append("contract challenge must positively assert a conflict or contradiction")
    return errors


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


def _embedded_interpreter_syntax_errors(tokens):
    """Syntax-check quoted node/python -e/-c payloads without executing them."""
    errors=[]
    for i in range(len(tokens)-2):
        executable=tokens[i]
        base=executable.rsplit("/",1)[-1]
        mode=tokens[i+1]
        payload=tokens[i+2]

        if base in {"python","python3"} and mode=="-c":
            try:
                compile(payload,"<verify-python-c>","exec")
            except SyntaxError as exc:
                errors.append(
                    "Verify python -c payload has invalid syntax: "
                    f"{exc.msg} line={exc.lineno}"
                )

        if base in {"node","nodejs"} and mode in {"-e","--eval"}:
            node=shutil.which(executable) or shutil.which(base)
            if not node:
                continue
            temp_path=""
            try:
                with tempfile.NamedTemporaryFile(
                    "w",suffix=".js",encoding="utf-8",delete=False
                ) as handle:
                    handle.write(payload)
                    temp_path=handle.name
                proc=subprocess.run(
                    [node,"--check",temp_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if proc.returncode:
                    detail=" ".join(
                        line.strip() for line in proc.stderr.splitlines()
                        if line.strip() and not line.strip().startswith("Node.js ")
                    )
                    errors.append(
                        "Verify node -e payload has invalid JavaScript syntax"
                        + (f": {detail[:300]}" if detail else "")
                    )
            except (OSError,subprocess.SubprocessError):
                pass
            finally:
                if temp_path:
                    try:
                        Path(temp_path).unlink()
                    except OSError:
                        pass
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

def _embedded_python_subprocess_errors(tokens):
    """Reject Python verifier code that reads unavailable subprocess stdout."""
    errors=[]
    for i in range(len(tokens)-2):
        base=tokens[i].rsplit("/",1)[-1]
        if base not in {"python","python3"} or tokens[i+1]!="-c":
            continue
        payload=tokens[i+2]
        try:
            tree=ast.parse(payload)
        except SyntaxError:
            continue

        runs={}
        for node in ast.walk(tree):
            if not isinstance(node,ast.Assign):
                continue
            call=node.value
            if not (
                isinstance(call,ast.Call)
                and isinstance(call.func,ast.Attribute)
                and call.func.attr=="run"
                and isinstance(call.func.value,ast.Name)
                and call.func.value.id=="subprocess"
            ):
                continue
            kws={kw.arg:kw.value for kw in call.keywords if kw.arg}
            capture=(
                isinstance(kws.get("capture_output"),ast.Constant)
                and kws["capture_output"].value is True
            )
            stdout=kws.get("stdout")
            pipe=(
                isinstance(stdout,ast.Attribute) and stdout.attr=="PIPE"
                or isinstance(stdout,ast.Name) and stdout.id=="PIPE"
            )
            text_mode=(
                isinstance(kws.get("text"),ast.Constant)
                and kws["text"].value is True
            ) or (
                isinstance(kws.get("universal_newlines"),ast.Constant)
                and kws["universal_newlines"].value is True
            ) or any(
                name in kws
                and not (
                    isinstance(kws[name],ast.Constant)
                    and kws[name].value is None
                )
                for name in ("encoding","errors")
            )
            for target in node.targets:
                if isinstance(target,ast.Name):
                    runs[target.id]=(capture or pipe,text_mode)

        for node in ast.walk(tree):
            if (
                isinstance(node,ast.Attribute)
                and node.attr=="stdout"
                and isinstance(node.value,ast.Name)
                and node.value.id in runs
                and not runs[node.value.id][0]
            ):
                errors.append(
                    "Verify Python reads subprocess.run().stdout without "
                    "capture_output=True or stdout=subprocess.PIPE"
                )
            if (
                isinstance(node,ast.Call)
                and isinstance(node.func,ast.Attribute)
                and node.func.attr=="decode"
                and isinstance(node.func.value,ast.Attribute)
                and node.func.value.attr=="stdout"
                and isinstance(node.func.value.value,ast.Name)
                and node.func.value.value.id in runs
                and runs[node.func.value.value.id][1]
            ):
                errors.append(
                    "Verify Python decodes subprocess stdout even though text mode "
                    "already returns str"
                )
    return errors


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
    errors.extend(_embedded_interpreter_syntax_errors(tokens))
    errors.extend(_embedded_python_subprocess_errors(tokens))

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


BEHAVIORAL_DONE_WHEN_RE = re.compile(
    r"\b(?:assert(?:s|ions?)?|compar(?:e|es|ing)|driv(?:e|es|ing)|"
    r"launch(?:es|ed|ing)?|serv(?:e|es|ed|ing)|render(?:s|ed|ing)?|"
    r"hid(?:e|es|ing)|position(?:s|ed|ing)?|wir(?:e|es|ed|ing)|"
    r"expos(?:e|es|ed|ing)|returns?|prints?|play|pause|"
    r"correct(?:ly)?|matches?)\b",
    re.I,
)
SERVICE_DONE_WHEN_RE = re.compile(
    r"\b(?:launch(?:es|ed|ing)?|start(?:s|ed|ing)?)\b.{0,80}"
    r"\b(?:server|localhost|http)\b|"
    r"\bserv(?:e|es|ing)\b.{0,80}\b(?:http|localhost|app|index\.html)\b",
    re.I | re.S,
)
RUNTIME_VERIFY_RE = re.compile(
    r"(?:^|[;&|]\s*|\$\(\s*)"
    r"(?:"
    r"(?:node|nodejs)\s+(?!--check\b)(?:-e\b|--eval\b|[^;&|\n]+\.js\b)|"
    r"python3?\s+(?:-c\b|-m\s+(?!(?:py_compile|compileall)\b)[A-Za-z_][\w.]*\b|(?!-m\s+(?:py_compile|compileall)\b)[^;&|\n]+\.py\b(?!\s+--help\b))|"
    r"(?:npm|pnpm|yarn)\s+(?:test\b|start\b|run\s+[^;&|\n]+)|"
    r"(?:curl|wget|playwright|puppeteer)\b|"
    r"\.opencode-v2/bin/run-checks\b"
    r")",
    re.I,
)
SERVICE_VERIFY_RE = re.compile(
    r"\b(?:npm|pnpm|yarn)\s+start\b|"
    r"\bnode(?:js)?\s+(?!--check\b)(?!-e\b|--eval\b)[^;&|\n]+\.js\b|"
    r"\bpython3?\s+(?!-m\s+py_compile\b)[^;&|\n]+\.py\b(?!\s+--help\b)|"
    r"\b(?:curl|wget|playwright|puppeteer)\b|"
    r"\.opencode-v2/bin/run-checks\b",
    re.I,
)


TEST_RUNNER_HUMAN_SUMMARY_RE = re.compile(
    r"(?:unittest(?:\W|$)|pytest(?:\W|$)|npm\s+test|pnpm\s+test|yarn\s+test)",
    re.I,
)
HUMAN_SUMMARY_PARSE_RE = re.compile(
    r"(?:"
    r"(?:stdout|stderr)\s*\.\s*(?:endswith|startswith|split|find|index|count)\s*\(|"
    r"(?:stdout|stderr)\s*(?:==|!=|\bin\b)|"
    r"['\"](?:OK|FAILED|Ran(?:\s+\d+\s+tests?)?|passed|failed)['\"]\s+in\s+"
    r"(?:\w+\.)?(?:stdout|stderr)"
    r")",
    re.I,
)


def _human_test_runner_summary_contract_error(done_when: str, command: str):
    """Reject brittle success criteria based on a runner's presentation text.

    Exit status and behavioral assertions are stable verification signals.
    Human-readable runner summaries vary by version/configuration and must only
    become contract data when Done-when explicitly requires that presentation.
    """
    if not (
        TEST_RUNNER_HUMAN_SUMMARY_RE.search(command)
        and HUMAN_SUMMARY_PARSE_RE.search(command)
    ):
        return ""
    done=str(done_when or "")
    presentation_terms=(
        "stdout","stderr","summary line","test count","tests run",
        "passed tests","failed tests","runner output",
    )
    if any(term in done.lower() for term in presentation_terms):
        return ""
    return (
        "Verify command depends on human-readable test-runner summary formatting; "
        "use runner exit status and behavioral assertions unless Done when explicitly "
        "requires that output format"
    )


EXPLICIT_DONE_WHEN_COMMAND_RE = re.compile(
    r"\x60((?:(?:python3?|pytest|node|nodejs|npm|pnpm|yarn|bash|sh)\s+|\.opencode-v2/bin/)[^\x60\n]+)\x60",
    re.I,
)


def explicit_done_when_commands(done_when: str):
    """Return literal executable obligations deliberately embedded in Done-when."""
    seen=[]
    for match in EXPLICIT_DONE_WHEN_COMMAND_RE.finditer(str(done_when or "")):
        command=match.group(1).strip()
        if command and command not in seen:
            seen.append(command)
    return seen


def validate_verify_adequacy(done_when: str, verify_command: str):
    """Reject obvious static-proxy Verifies for behavioral completion contracts.

    This is deliberately conservative: it does not claim semantic proof. It only
    blocks the known-unsafe case where Done-when requires observable behavior but
    Verify performs syntax/existence/static-text checks only.
    """
    done=str(done_when or "").strip()
    command=str(verify_command or "").strip()
    if not done or not command:
        return []
    errors=[]
    summary_error=_human_test_runner_summary_contract_error(done,command)
    if summary_error:
        errors.append(summary_error)
    if command != ".opencode-v2/bin/run-checks":
        for required in explicit_done_when_commands(done):
            if required not in command:
                errors.append(
                    "Done-when explicitly requires command "
                    + chr(96) + required + chr(96)
                    + " but verify_command does not execute it"
                )
    if SERVICE_DONE_WHEN_RE.search(done) and not SERVICE_VERIFY_RE.search(command):
        errors.append(
            "Verify command does not exercise the server/service behavior required by Done when"
        )
    if (
        BEHAVIORAL_DONE_WHEN_RE.search(done)
        and not RUNTIME_VERIFY_RE.search(command)
    ):
        errors.append(
            "Verify command is static-proxy-only for behavioral Done when; "
            "execute the relevant behavior or use the canonical test runner"
        )
    return errors


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
