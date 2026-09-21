#!/usr/bin/env python3
"""Project-scoped absolute-path aliases for OpenCode's pre-hook permission check.

The canonical permission contract stays in the agent files as project-relative
paths.  OpenCode v1.18.31 evaluates those permissions before plugin hooks can
normalize a tool argument, so a Stage-A server receives an ephemeral copied
config tree with only exact aliases for the active project's already-allowed
paths.  This is deliberately not a general absolute-path permission mechanism.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class PathPermissionError(RuntimeError):
    pass


GLOB_META = set("*?[]{}")


def project_root(project: Path) -> Path:
    root = project.resolve(strict=True)
    if not root.is_dir():
        raise PathPermissionError(f"project is not a directory: {project}")
    if any(char in str(root) for char in GLOB_META):
        raise PathPermissionError("project path contains glob metacharacters")
    return root


def opencode_worktree(project: Path) -> Path:
    """Predict v1.18.31's worktree: Git top-level, otherwise global root."""
    root = project_root(project)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return Path("/")
    worktree = Path(result.stdout.strip()).resolve(strict=True)
    try:
        root.relative_to(worktree)
    except ValueError as exc:
        raise PathPermissionError("Git worktree does not contain project") from exc
    return worktree


def effective_permission_pattern(project: Path, worktree: Path, canonical_pattern: str) -> str:
    """Translate one canonical owned path to OpenCode's evaluated spelling."""
    if not canonical_pattern or canonical_pattern.startswith("/"):
        raise PathPermissionError(f"non-relative canonical permission pattern: {canonical_pattern!r}")
    if any(part == ".." for part in Path(canonical_pattern).parts):
        raise PathPermissionError(f"escaping canonical permission pattern: {canonical_pattern!r}")
    target = project_root(project) / canonical_pattern
    value = os.path.relpath(target, worktree.resolve(strict=True)).replace(os.sep, "/")
    if value in {"", "."} or value == ".." or value.startswith("../"):
        raise PathPermissionError("effective permission pattern escapes worktree")
    return value


def normalize_tool_path(project: Path, raw: str) -> str:
    """Return a canonical relative path, denying escapes before rule matching."""
    root = project_root(project)
    if not isinstance(raw, str) or not raw.strip():
        raise PathPermissionError("tool path is empty")
    supplied = Path(raw.strip())
    candidate = supplied if supplied.is_absolute() else root / supplied
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise PathPermissionError(f"tool path escapes exact project: {raw}") from exc
    value = relative.as_posix()
    if value in {"", "."} or value.startswith("../"):
        raise PathPermissionError(f"tool path is not a project artifact: {raw}")
    return value


def permission_allows(rules: list[tuple[str, str]], relative: str) -> bool:
    """Apply the existing relative allow/deny rules by most-specific match."""
    matches = []
    for order, (pattern, decision) in enumerate(rules):
        if fnmatch.fnmatchcase(relative, pattern):
            specificity = len(pattern.replace("*", "").replace("?", ""))
            matches.append((specificity, order, decision))
    if not matches:
        return False
    return max(matches)[2] == "allow"


def parse_permission_rules(text: str) -> dict[str, list[tuple[str, str]]]:
    """Read the simple agent-frontmatter permission maps without YAML coercion."""
    lines = text.splitlines()
    try:
        start = lines.index("permission:")
    except ValueError:
        return {}
    result: dict[str, list[tuple[str, str]]] = {}
    tool = ""
    for line in lines[start + 1 :]:
        if line == "---" or (line and not line.startswith(" ")):
            break
        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
            tool = line.strip()[:-1]
            result.setdefault(tool, [])
            continue
        if not tool or not line.startswith("    "):
            continue
        body = line.strip()
        if ":" not in body:
            continue
        raw_pattern, decision = body.rsplit(":", 1)
        decision = decision.strip()
        if decision not in {"allow", "deny"}:
            continue
        try:
            pattern = json.loads(raw_pattern) if raw_pattern.startswith('"') else raw_pattern
        except json.JSONDecodeError as exc:
            raise PathPermissionError(f"invalid permission pattern: {raw_pattern}") from exc
        if not isinstance(pattern, str):
            raise PathPermissionError("permission pattern is not text")
        result[tool].append((pattern, decision))
    return result


def render_agent_with_projection(text: str, project: Path, worktree: Path) -> str:
    """Append only the v1 worktree-relative spelling of existing owned allows."""
    lines = text.splitlines(keepends=True)
    rules = parse_permission_rules(text)
    if not rules:
        return text
    output: list[str] = []
    active_tool = ""
    inserted: set[str] = set()

    def add_aliases(tool: str) -> None:
        if not tool or tool in inserted:
            return
        inserted.add(tool)
        for pattern, decision in rules.get(tool, []):
            if decision != "allow" or pattern == "*":
                continue
            effective = effective_permission_pattern(project, worktree, pattern)
            if effective != pattern:
                output.append(f"    {json.dumps(effective)}: allow\n")

    in_permission = False
    for line in lines:
        if line == "permission:\n":
            in_permission = True
            output.append(line)
            continue
        if in_permission and (line == "---\n" or (line and not line.startswith(" "))):
            add_aliases(active_tool)
            in_permission = False
            active_tool = ""
        if in_permission and line.startswith("  ") and not line.startswith("    "):
            add_aliases(active_tool)
            active_tool = line.strip()[:-1] if line.rstrip().endswith(":") else ""
        output.append(line)
    if in_permission:
        add_aliases(active_tool)
    return "".join(output)


def create_overlay(project: Path, source_config_home: Path, overlay_root: Path | None = None) -> dict:
    root = project_root(project)
    worktree = opencode_worktree(root)
    source = source_config_home.resolve(strict=True)
    agents = source / "opencode" / "agents"
    if not agents.is_dir():
        raise PathPermissionError(f"canonical agent config is missing: {agents}")
    target = overlay_root or Path(tempfile.mkdtemp(prefix="stage-a-opencode-config-"))
    if target.exists() and any(target.iterdir()):
        raise PathPermissionError(f"overlay root is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    rendered = []
    for agent in sorted((target / "opencode" / "agents").glob("*.md")):
        original = agent.read_text(encoding="utf-8")
        updated = render_agent_with_projection(original, root, worktree)
        if updated != original:
            agent.write_text(updated, encoding="utf-8")
            rendered.append(agent.name)
    marker = {
        "protocol": "v2-stage-a-exact-project-permission-overlay-v1",
        "canonical_config_home": str(source),
        "project": str(root),
        "worktree": str(worktree),
        "agents": rendered,
    }
    (target / "opencode" / ".stage-a-permission-overlay.json").write_text(
        json.dumps(marker, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    validate_overlay(root, target, source)
    return {"config_home": str(target), **marker}


def validate_overlay(project: Path, config_home: Path, canonical_config_home: Path) -> None:
    marker_path = config_home / "opencode" / ".stage-a-permission-overlay.json"
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PathPermissionError("exact-project permission overlay marker is invalid") from exc
    if marker.get("protocol") != "v2-stage-a-exact-project-permission-overlay-v1":
        raise PathPermissionError("permission overlay protocol mismatch")
    if marker.get("project") != str(project_root(project)):
        raise PathPermissionError("permission overlay project mismatch")
    if marker.get("worktree") != str(opencode_worktree(project)):
        raise PathPermissionError("permission overlay worktree mismatch")
    if marker.get("canonical_config_home") != str(canonical_config_home.resolve()):
        raise PathPermissionError("permission overlay canonical config mismatch")


def selftest() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        project = base / "project"
        sibling = base / "project-sibling"
        other = base / "other"
        for path in (project, sibling, other):
            path.mkdir()
        owned = ".opencode-v2/ACCEPTANCE.md"
        rules = [("*", "deny"), (owned, "allow")]
        assert permission_allows(rules, normalize_tool_path(project, owned))
        assert permission_allows(rules, normalize_tool_path(project, str(project / owned)))
        assert not permission_allows(rules, normalize_tool_path(project, ".opencode-v2/OTHER.md"))
        for raw in ("../other/x", str(other / owned), str(sibling / owned)):
            try:
                normalize_tool_path(project, raw)
            except PathPermissionError:
                pass
            else:
                raise AssertionError(f"escape was accepted: {raw}")
        (project / "escape").symlink_to(other, target_is_directory=True)
        try:
            normalize_tool_path(project, "escape/owned.txt")
        except PathPermissionError:
            pass
        else:
            raise AssertionError("symlink escape was accepted")
        # Non-Git projects use / as the v1.18.31 worktree; aliases therefore
        # have no leading slash and retain no /PROJECT/... representation.
        assert opencode_worktree(project) == Path("/")
        assert effective_permission_pattern(project, Path("/"), owned) == str(project / owned).lstrip("/")
        config = base / "config" / "opencode" / "agents"
        config.mkdir(parents=True)
        agent = "---\npermission:\n  edit:\n    \"*\": deny\n    \".opencode-v2/ACCEPTANCE.md\": allow\n---\n"
        (config / "acceptance-planner.md").write_text(agent, encoding="utf-8")
        overlay = Path(base / "overlay")
        receipt = create_overlay(project, base / "config", overlay)
        validate_overlay(project, overlay, base / "config")
        rendered = (overlay / "opencode" / "agents" / "acceptance-planner.md").read_text()
        assert str(project / owned).lstrip("/") in rendered
        assert str(project / owned) not in rendered and str(sibling / owned) not in rendered
        assert receipt["project"] == str(project.resolve())
        assert receipt["worktree"] == "/"

        # A project which is its Git worktree needs no duplicate projection.
        git_project = base / "git-project"
        git_project.mkdir()
        subprocess.run(["git", "init", "-q", str(git_project)], check=True)
        assert opencode_worktree(git_project) == git_project.resolve()
        assert effective_permission_pattern(git_project, git_project, owned) == owned

        # A nested project projects only its owned path below the enclosing Git root.
        nested = git_project / "nested"
        nested.mkdir()
        assert opencode_worktree(nested) == git_project.resolve()
        assert effective_permission_pattern(nested, git_project, owned) == f"nested/{owned}"
    print("stage-a path permission selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an exact-project OpenCode permission overlay.")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--config-home", type=Path)
    parser.add_argument("--overlay-root", type=Path)
    parser.add_argument("--selftest", action="store_true")
    ns = parser.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or ns.config_home is None:
        parser.error("--project and --config-home are required unless --selftest is used")
    print(json.dumps(create_overlay(ns.project, ns.config_home, ns.overlay_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PathPermissionError as exc:
        raise SystemExit(f"ERROR: {exc}")
