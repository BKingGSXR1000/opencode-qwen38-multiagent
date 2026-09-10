import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

export const HARD_MAX_CHILD_RESULT_CHARS = 2500;
export const TARGET_MAX_CHILD_RESULT_CHARS = 1500;

function exactDeliverable(args) {
  const prompt = typeof args?.prompt === "string" ? args.prompt : "";
  return prompt.match(/^DELIVERABLE:\s*(D\d{3})\s*$/m)?.[1] || "unknown";
}

function fileStatus(path) {
  try {
    return existsSync(path) ? "present" : "absent";
  } catch {
    return "unknown";
  }
}

function attemptFor(directory, did, childSessionID) {
  if (!/^D\d{3}$/.test(did)) return "unknown";
  try {
    const path = join(directory, ".opencode-v2", "work", "attempts.json");
    const entry = JSON.parse(readFileSync(path, "utf8"))?.deliverables?.[did];
    if (!entry || !Array.isArray(entry.sessions)) return "unknown";
    return entry.sessions.includes(childSessionID) ? String(entry.count) : "pending";
  } catch {
    return "unknown";
  }
}

function ownedArtifacts(directory, did) {
  if (!/^D\d{3}$/.test(did)) return ["  (unknown): unknown"];
  try {
    const path = join(directory, ".opencode-v2", "IMPLEMENTATION_PLAN.guard.json");
    const raw = JSON.parse(readFileSync(path, "utf8"))?.leaves?.[did]?.owned_artifacts;
    if (typeof raw !== "string") return ["  (unavailable): unknown"];
    const paths = raw.split(",").map((item) => item.trim().replace(/^`|`$/g, ""))
      .filter((item) => item && !["-", "—", "none", "n/a"].includes(item.toLowerCase()));
    if (!paths.length) return ["  (none): n/a"];
    return paths.slice(0, 20).map((item) => `  ${item}: ${fileStatus(join(directory, item))}`);
  } catch {
    return ["  (unavailable): unknown"];
  }
}

export function boundedChildResult({ directory, args = {}, metadata = {}, original = "" }) {
  if (args?.agent === "acceptance-validator" && /(?:^|\n)\s*ACCEPTANCE_PASS\s*(?:\n|<\/subagent>)/.test(original)) {
    return "ACCEPTANCE_PASS";
  }
  const did = exactDeliverable(args);
  const childSessionID = metadata?.sessionID || "unknown";
  const readyPath = /^D\d{3}$/.test(did)
    ? join(directory, ".opencode-v2", "work", `${did}.ready`)
    : "";
  const progressPath = /^D\d{3}$/.test(did)
    ? join(directory, ".opencode-v2", "work", `${did}.progress.md`)
    : "";
  const termination = metadata?.status || (original.length > TARGET_MAX_CHILD_RESULT_CHARS ? "output_limited" : "completed");
  const lines = [
    `AGENT: ${args?.agent || "unknown"}`,
    `DELIVERABLE: ${did}`,
    `SESSION: ${childSessionID}`,
    `ATTEMPT: ${attemptFor(directory, did, childSessionID)}`,
    `READY: ${readyPath ? fileStatus(readyPath) === "present" : false}`,
    `TERMINATION: ${termination}`,
    `PROGRESS_FILE: ${progressPath ? fileStatus(progressPath) : "unknown"}`,
    "OWNED_ARTIFACTS:",
    ...ownedArtifacts(directory, did),
    "SOURCE: authoritative filesystem; full child output remains in session history",
  ];
  let result = lines.join("\n");
  if (result.length > TARGET_MAX_CHILD_RESULT_CHARS) {
    result = result.slice(0, TARGET_MAX_CHILD_RESULT_CHARS - 33) + "\nOWNED_ARTIFACTS: (receipt clipped)";
  }
  if (result.length > HARD_MAX_CHILD_RESULT_CHARS) {
    throw new Error("bounded child receipt exceeded hard maximum");
  }
  return result;
}

export const V2BoundedSubagentPlugin = async ({ directory }) => ({
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "subagent" && input.tool !== "task") return;
    const agent = output.args?.agent;
    const prompt = output.args?.prompt;
    if (agent === "general") {
      throw new Error("DISPATCH_DENY general is not a canonical implementation role");
    }
    const hasDeliverable = typeof prompt === "string" && /^DELIVERABLE:\s*D\d{3}\s*$/m.test(prompt);
    const implementationAgents = new Set(["probe-builder", "implementer", "core-builder", "feature-builder", "reasoning-builder", "integrator", "tester", "test-builder"]);
    if (!implementationAgents.has(agent) && !hasDeliverable) return;
    // This deterministic supervisor claim occurs before OpenCode materializes
    // the child session or sends a provider request.
    execFileSync("python3", ["/home/bking/AI/opencode-qwen38-multiagent-v2/scripts/supervisor.py", "--project", directory, "--agent", String(agent || ""), "--prompt", String(prompt || ""), "--claim-dispatch", input.callID], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  },
  "tool.execute.after": async (input, output) => {
    if (input.tool !== "subagent" && input.tool !== "task") return;
    output.output = boundedChildResult({
      directory,
      args: input.args,
      metadata: output.metadata,
      original: output.output,
    });
    output.metadata = {
      ...output.metadata,
      parentResultBounded: true,
      parentResultChars: output.output.length,
      fullOutputStorage: "session_history",
    };
  },
});

export default V2BoundedSubagentPlugin;
