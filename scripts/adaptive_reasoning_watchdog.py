#!/usr/bin/env python3
"""Optional action-based reasoning watchdog; pure decisions, no OpenCode IO.

The ordinary SSE stall watchdog resets on every streamed token. This second
clock measures time since a tool action, not token liveness. Only opt-in
enforcement interrupts a real child; all other modes preserve current behavior.
"""
from __future__ import annotations

import re
from typing import Any

MODES = frozenset(("off", "observe", "enforce"))
# Character limits apply to *visible streamed reasoning*, not guessed hidden
# thinking tokens. Never trust total OpenCode message token usage here because
# those counters may include completed steps that preceded the last tool.
BUDGETS = {
    "TRIVIAL": {"reasoning_chars": 3600, "min_age": 45.0, "max_age": 90.0},
    "NORMAL": {"reasoning_chars": 6500, "min_age": 75.0, "max_age": 180.0},
    "COMPLEX": {"reasoning_chars": 14000, "min_age": 120.0, "max_age": 300.0},
    "RESEARCH": {"reasoning_chars": 18000, "min_age": 180.0, "max_age": 480.0},
}
IMPLEMENTATION_ROLES = frozenset((
    "implementer", "core-builder", "feature-builder", "reasoning-builder",
    "integrator", "test-builder", "probe-builder",
))
_REASONING_TAIL_LIMIT = 1440
_LOOP_BLOCK = 120


def profile(agent: str, leaf: dict|None) -> str:
    """Deterministic, fail-soft complexity selection from a guarded leaf."""
    if agent not in IMPLEMENTATION_ROLES:
        return ""
    if agent == "probe-builder":
        return "RESEARCH"
    leaf = leaf if isinstance(leaf, dict) else {}
    if leaf.get("deep_reasoning") is True or agent == "reasoning-builder":
        return "COMPLEX"
    complexity = str(leaf.get("complexity") or "").upper()
    repeated = leaf.get("repeated_operations")
    repeated = repeated if type(repeated) is int and repeated >= 0 else 1
    if complexity in ("L", "XL") or repeated >= 6:
        return "COMPLEX"
    if complexity == "M" or repeated >= 3:
        return "NORMAL"
    if complexity == "S" and repeated == 0:
        return "TRIVIAL"
    return "NORMAL"


def mode(raw: str|None) -> str:
    value = str(raw or "off").strip().lower()
    if value not in MODES:
        raise ValueError("V2_ADAPTIVE_REASONING_MODE must be off, observe or enforce")
    return value


def action_clock(
    state: dict,
    key: tuple,
    now: float,
    reasoning_chars: int,
    *,
    observable: bool,
    tool_running: bool,
) -> tuple[float,int]:
    """Reset on a *tool/step* change, never on streamed reasoning/text deltas.

    Keep this state separate from the preexisting SSE progress clock. A
    telemetry reconnect may lose a tool event; caller includes persisted
    message/last-tool IDs and the live tool-success sequence in the key.
    """
    if (
        not observable or tool_running
        or not isinstance(key, tuple)
        or not any(key[:2])
    ):
        state.clear()
        return 0.0, 0
    observed = max(0, int(reasoning_chars or 0))
    if state.get("key") != key or state.get("since") is None:
        state.clear()
        state.update(key=key, since=float(now), baseline=observed)
        return 0.0, 0
    baseline = max(0, int(state.get("baseline") or 0))
    if observed < baseline:  # SSE restart/compaction: fail open.
        state.clear()
        state.update(key=key, since=float(now), baseline=observed)
        return 0.0, 0
    return max(0.0, float(now)-float(state["since"])), observed-baseline


def repeated_reasoning_tail(text: str, block: int = _LOOP_BLOCK) -> bool:
    """Detect three exact, reasonably long repeated spans; no semantic guesses."""
    if not isinstance(text, str):
        return False
    normalized = re.sub(r"\s+", " ", text.strip()).lower()
    if len(normalized) < block * 3:
        return False
    sample = normalized[-block:]
    if len(set(sample)) < 5:  # repeated whitespace/punctuation is not a loop.
        return False
    return (
        normalized[-block * 3:-block * 2] == sample
        and normalized[-block * 2:-block] == sample
    )


def append_reasoning_tail(state: dict, delta: str) -> None:
    if not isinstance(delta, str) or not delta:
        return
    text = (str(state.get("reasoning_tail") or "") + delta)[-_REASONING_TAIL_LIMIT:]
    state["reasoning_tail"] = text
    if repeated_reasoning_tail(text):
        state["reasoning_loop_detected"] = True


def clear_reasoning_tail(state: dict) -> None:
    state.pop("reasoning_tail", None)
    state.pop("reasoning_loop_detected", None)


def decision(
    *,
    agent: str,
    leaf: dict|None,
    action_age: float,
    reasoning_chars: int,
    loop_detected: bool,
    observable: bool,
    tool_running: bool,
    compaction_active: bool,
    backend: dict|None = None,
) -> dict[str,Any]:
    """Pure decision; backend global activity alone never establishes a loop.

    Prefill or queueing without *visible* reasoning can never trip this
    watchdog. A running tool and active compaction always suppress it. The
    legacy hard cap and backend-aware no-SSE watchdog remain independently
    authoritative.
    """
    name = profile(agent, leaf)
    budget = BUDGETS.get(name)
    result: dict[str,Any] = {
        "abort": False, "reason": "", "profile": name,
        "action_age": round(max(0.0, float(action_age)), 2),
        "reasoning_chars_since_action": max(0, int(reasoning_chars or 0)),
        "budget": dict(budget) if budget else {},
    }
    if not budget or not observable or tool_running or compaction_active:
        result["gate"] = "ineligible"
        return result
    age = result["action_age"]
    chars = result["reasoning_chars_since_action"]
    if not chars:
        result["gate"] = "no-reasoning"
        return result
    cap = budget["reasoning_chars"]
    if loop_detected and age >= 30 and chars >= min(3000, cap // 2):
        result.update(abort=True, reason="repeated-reasoning-without-tool", gate="loop")
    elif age >= budget["min_age"] and chars >= cap:
        result.update(abort=True, reason="reasoning-budget-without-tool", gate="budget")
    elif age >= budget["max_age"] and chars >= cap // 2:
        result.update(abort=True, reason="action-timeout-with-reasoning", gate="age")
    else:
        result["gate"] = "within-budget"
    # Backend phase describes server-global activity. Report it for observability
    # only; shared token progress is NOT proof this exact session advanced.
    snapshot = backend if isinstance(backend, dict) else {}
    result["backend_running"] = snapshot.get("running")
    result["backend_generation_progress_age"] = snapshot.get("generation_progress_age")
    return result
