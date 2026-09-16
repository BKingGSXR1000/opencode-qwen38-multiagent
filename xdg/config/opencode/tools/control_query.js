import { tool } from "@opencode-ai/plugin";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const DID_RE = /^D\d{3}(?:-[AB](?:[12])?)?$/;
const QUERY_PROTOCOL = "v2-control-query-v1";
const MAX_RESULT_CHARS = 6000;

function cleanScheduler(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    max_concurrent_workers: Number(s.max_concurrent_workers || 0),
    active_workers: Number(s.active_workers || 0),
    reserved_workers: Number(s.reserved_workers || 0),
    available_worker_slots: Number(s.available_worker_slots || 0),
    active_deliverables: Array.isArray(s.active_deliverables) ? [...s.active_deliverables].sort() : [],
    error: String(s.error || ""),
  };
}

function cleanBlockers(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.filter((x) => x && typeof x === "object").map((x) => ({
    deliverable: String(x.deliverable || ""),
    reason: String(x.reason || ""),
    attempts: Number(x.attempts || 0),
    allowed_attempts: Number(x.allowed_attempts || 0),
    ...(x.detail ? { detail: String(x.detail).slice(0, 500) } : {}),
  }));
}

function cleanLeaf(did, raw) {
  const leaf = raw && typeof raw === "object" ? raw : {};
  return {
    deliverable: did,
    complete: Boolean(leaf.complete),
    eligible: Boolean(leaf.eligible),
    running: Boolean(leaf.running),
    attempts: Number(leaf.attempts || 0),
    total_dispatches: Number(leaf.total_dispatches || 0),
    automatic_attempts_consumed: Number(leaf.automatic_attempts_consumed || 0),
    automatic_limit: Number(leaf.automatic_limit || 0),
    allowed_attempts: Number(leaf.allowed_attempts || 0),
    attempt_limit_reached: Boolean(leaf.attempt_limit_reached),
    attempt_ledger_valid: leaf.attempt_ledger_valid !== false,
    unmaterialized_dispatch_reusable: Boolean(leaf.unmaterialized_dispatch_reusable),
    launch_deps_missing: Array.isArray(leaf.launch_deps_missing) ? leaf.launch_deps_missing : [],
    contract_deps_missing: Array.isArray(leaf.contract_deps_missing) ? leaf.contract_deps_missing : [],
    verify_deps_missing: Array.isArray(leaf.verify_deps_missing) ? leaf.verify_deps_missing : [],
    verification_pending: Boolean(leaf.verification_pending),
    split_required: Boolean(leaf.split_required),
    split_state: String(leaf.split_state || ""),
    split_generation: Number(leaf.split_generation || 0),
    split_children: Array.isArray(leaf.split_children) ? leaf.split_children : [],
    operator_grants_remaining: Number(leaf.operator_grants_remaining || 0),
    operator_infrastructure_blocked: Number(leaf.operator_infrastructure_blocked || 0),
    infrastructure_grants_remaining: Number(leaf.infrastructure_grants_remaining || 0),
  };
}

function ineligibilityReasons(leaf) {
  if (!leaf) return ["unknown_deliverable"];
  if (leaf.eligible) return [];
  const reasons = [];
  if (leaf.complete) reasons.push("complete");
  if (leaf.running) reasons.push("running");
  if (leaf.split_required) reasons.push(leaf.split_state || "split_required");
  if (leaf.verification_pending) reasons.push("verification_pending");
  if (!leaf.attempt_ledger_valid) reasons.push("attempt_ledger_invalid");
  if (leaf.attempt_limit_reached) reasons.push("attempt_limit_reached");
  if (leaf.operator_infrastructure_blocked) reasons.push("operator_infrastructure_blocked");
  if (leaf.launch_deps_missing.length) reasons.push("launch_deps_missing");
  if (leaf.contract_deps_missing.length) reasons.push("contract_deps_missing");
  if (!reasons.length) reasons.push("not_currently_eligible");
  return reasons;
}

export function buildControlQuery(snapshot, query, deliverable = "", stateVersion = "", sourceBytes = 0, roleMap = {}) {
  const data = snapshot && typeof snapshot === "object" ? snapshot : {};
  const base = {
    protocol: QUERY_PROTOCOL,
    state_version: stateVersion,
    source_bytes: sourceBytes,
    state_error: Boolean(data.state_error),
    resume_phase: String(data.resume_phase || "execution-blocked"),
  };

  if (data.state_error) {
    return {
      ...base,
      state_error_type: String(data.state_error_type || ""),
      state_error_message: String(data.state_error_message || "").slice(0, 500),
      execution_blockers: cleanBlockers(data.execution_blockers),
    };
  }

  const leaves = data.leaves && typeof data.leaves === "object" ? data.leaves : {};
  const scheduler = cleanScheduler(data.scheduler);
  const eligible = Object.entries(leaves)
    .filter(([, leaf]) => leaf && typeof leaf === "object" && leaf.eligible === true)
    .map(([did]) => did)
    .sort();
  const eligibleRoles = Object.fromEntries(
    eligible.map((did) => [did, String(roleMap?.[did] || "")])
  );
  const splitRequired = Object.entries(leaves)
    .filter(([, leaf]) => leaf && typeof leaf === "object" && leaf.split_required === true)
    .map(([did, leaf]) => ({
      deliverable: did,
      split_state: String(leaf.split_state || "split-required"),
      split_generation: Number(leaf.split_generation || 0),
    }))
    .sort((a, b) => a.deliverable.localeCompare(b.deliverable));

  if (query === "decision" || query === "next") {
    const plan = data.plan && typeof data.plan === "object" ? data.plan : {};
    return {
      ...base,
      acceptance_complete: Boolean(data.acceptance?.complete),
      plan: {
        complete: Boolean(plan.complete),
        blocked: Boolean(plan.blocked),
        planner_failures: Number(plan.planner_failures || 0),
      },
      scheduler,
      eligible,
      eligible_roles: eligibleRoles,
      split_required: splitRequired,
      execution_blockers: cleanBlockers(data.execution_blockers),
      tests_complete: Boolean(data.tests?.complete),
      acceptance_validation_complete: Boolean(data.acceptance_validation?.complete),
    };
  }

  if (query === "eligible") return { ...base, scheduler, eligible, eligible_roles: eligibleRoles };
  if (query === "blockers") return { ...base, scheduler, execution_blockers: cleanBlockers(data.execution_blockers) };
  if (query === "split") return { ...base, split_required: splitRequired };

  if (query === "state" || query === "is-eligible") {
    if (!DID_RE.test(String(deliverable || ""))) {
      return { ...base, query_error: "deliverable_required", deliverable: String(deliverable || "") };
    }
    const raw = leaves[deliverable];
    if (!raw || typeof raw !== "object") {
      return { ...base, deliverable, exists: false, eligible: false, ineligibility_reasons: ["unknown_deliverable"] };
    }
    const leaf = cleanLeaf(deliverable, raw);
    return {
      ...base,
      exists: true,
      ...leaf,
      role: String(roleMap?.[deliverable] || ""),
      ineligibility_reasons: ineligibilityReasons(leaf),
    };
  }

  return { ...base, query_error: "unsupported_query" };
}

function boundedJson(payload) {
  const rendered = JSON.stringify(payload);
  if (rendered.length <= MAX_RESULT_CHARS) return rendered;
  return JSON.stringify({
    protocol: QUERY_PROTOCOL,
    state_error: true,
    resume_phase: "execution-blocked",
    query_error: "bounded_result_exceeded",
    result_chars: rendered.length,
    max_result_chars: MAX_RESULT_CHARS,
  });
}

export default tool({
  description:
    "Read the latest supervisor-owned V2 scheduler snapshot outside model context and return only a bounded requested projection. Use decision for the normal root control loop; use state/is-eligible for one Dxxx.",
  args: {
    query: tool.schema
      .enum(["decision", "next", "eligible", "is-eligible", "state", "blockers", "split"])
      .describe("Small scheduler projection to return."),
    deliverable: tool.schema
      .string()
      .optional()
      .describe("Canonical Dxxx ID; required only for state or is-eligible."),
  },
  async execute(args, context) {
    const project = String(context?.directory || "");
    const statusPath = join(project, ".opencode-v2", "control-status.json");
    const manifestPath = join(project, ".opencode-v2", "IMPLEMENTATION_PLAN.guard.json");
    let raw = "";
    try {
      raw = readFileSync(statusPath, "utf8");
      const snapshot = JSON.parse(raw);
      if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
        throw new Error("control-status root is not an object");
      }
      if (snapshot.owner !== "supervisor") throw new Error("control-status owner is not supervisor");
      let roleMap = {};
      try {
        const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
        const leaves = manifest?.leaves && typeof manifest.leaves === "object" ? manifest.leaves : {};
        roleMap = Object.fromEntries(
          Object.entries(leaves)
            .filter(([, leaf]) => leaf && typeof leaf === "object")
            .map(([did, leaf]) => [did, String(leaf.role || "")])
        );
      } catch {
        roleMap = {};
      }
      const version = createHash("sha256").update(raw).digest("hex").slice(0, 16);
      return boundedJson(
        buildControlQuery(
          snapshot,
          args.query,
          String(args.deliverable || ""),
          version,
          Buffer.byteLength(raw, "utf8"),
          roleMap
        )
      );
    } catch (error) {
      return boundedJson({
        protocol: QUERY_PROTOCOL,
        state_version: raw ? createHash("sha256").update(raw).digest("hex").slice(0, 16) : "",
        source_bytes: Buffer.byteLength(raw, "utf8"),
        state_error: true,
        resume_phase: "execution-blocked",
        query_error: "control_status_unavailable",
        detail: String(error?.message || error).slice(0, 500),
      });
    }
  },
});
