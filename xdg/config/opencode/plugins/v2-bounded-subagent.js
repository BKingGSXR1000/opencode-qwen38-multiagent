import { existsSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

export const HARD_MAX_CHILD_RESULT_CHARS = 2500;
export const TARGET_MAX_CHILD_RESULT_CHARS = 1500;

const WORKER_SANDBOX = "/home/bking/AI/opencode-qwen38-multiagent-v2/scripts/worker_sandbox.py";
const FINALIZE_ACCEPTANCE = "/home/bking/AI/opencode-qwen38-multiagent-v2/scripts/finalize-acceptance.py";
// V2.6.9 BATCH8 VERIFY-SANDBOX-LIFETIME-V3
const MUTATION_TOOLS = new Set(["edit", "write", "apply_patch", "patch", "multiedit", "bash", "shell", "execute"]);

function hookArgs(event, output) {
  if (output?.args && typeof output.args === "object") return output.args;
  if (event?.input && typeof event.input === "object") return event.input;
  if (event?.args && typeof event.args === "object") return event.args;
  return {};
}

function hookSessionID(event) {
  return String(
    event?.sessionID ||
    event?.sessionId ||
    event?.context?.sessionID ||
    event?.ctx?.sessionID ||
    ""
  );
}

function hookCallID(event) {
  return String(event?.callID || event?.callId || event?.id || "");
}

export function toolResultText(result) {
  if (!result || typeof result !== "object") return "";
  if (typeof result.output === "string" && result.output.length) return result.output;
  if (typeof result.content === "string") return result.content;
  if (Array.isArray(result.content)) {
    return result.content
      .map((item) => {
        if (typeof item === "string") return item;
        if (item?.type === "text" && typeof item.text === "string") return item.text;
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return typeof result.output === "string" ? result.output : "";
}

export function markRequestPurpose(event) {
  if (event?.kind !== "compaction") return false;
  if (!event.headers || typeof event.headers !== "object") return false;
  event.headers["x-v2-request-purpose"] = "compaction";
  return true;
}

function shellQuote(value) {
  return "'" + String(value).replaceAll("'", "'\"'\"'") + "'";
}

function guardWorkerMutation(directory, event, output) {
  const tool = String(event?.tool || "");
  if (!MUTATION_TOOLS.has(tool)) return;
  const args = hookArgs(event, output);
  const payload = Buffer.from(JSON.stringify(args), "utf8").toString("base64");
  const sessionID = hookSessionID(event);
  const callID = hookCallID(event);
  let raw;
  try {
    raw = execFileSync("python3", [
      WORKER_SANDBOX,
      "hook",
      "--project", directory,
      "--session", sessionID,
      "--call-id", callID,
      "--tool", tool,
      "--args-b64", payload,
    ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch (error) {
    const detail = String(error?.stderr || error?.message || error).trim();
    throw new Error(detail || `WORKER_FIREWALL_DENY ${tool}`);
  }
  let decision;
  try {
    decision = JSON.parse(raw);
  } catch {
    throw new Error(`WORKER_FIREWALL_DENY invalid guard response for ${tool}`);
  }
  if (decision?.action === "replace-bash" || decision?.action === "replace-validator-bash") {
    if (typeof decision.command !== "string" || !decision.command) {
      throw new Error("WORKER_FIREWALL_DENY missing sandbox command");
    }
    args.command = decision.command;
  }
}

function exactDeliverable(args) {
  const prompt = typeof args?.prompt === "string" ? args.prompt : "";
  return prompt.match(/^DELIVERABLE:\s*(D\d{3}(?:-[AB](?:[12])?)?)\s*$/m)?.[1] || "unknown";
}

function splitParent(args) {
  const prompt = typeof args?.prompt === "string" ? args.prompt : "";
  return prompt.match(/^\s*SPLIT_PARENT:\s*(D\d{3}(?:-[AB](?:[12])?)?)\s*$/)?.[1] || "";
}

function supervisor(directory, args) {
  return execFileSync("python3", [
    "/home/bking/AI/opencode-qwen38-multiagent-v2/scripts/supervisor.py",
    "--project", directory, ...args,
  ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
}

export function proposalFromOutput(output, parent, directory) {
  if (typeof output !== "string") return null;
  // The task-splitter is instructed to use its second tool turn for a write.
  // Some beta turns nevertheless reach their cap after returning a complete
  // fenced JSON proposal. Materialize only that exact structured contract;
  // never derive children, scopes, IDs, or control state from prose.
  const match = output.match(/```json\s*([\s\S]*?)\s*```/i);
  if (!match) return null;
  try {
    const proposal = JSON.parse(match[1]);
    const request = JSON.parse(readFileSync(join(directory, ".opencode-v2", "work", `${parent}.split-request.json`), "utf8"));
    if (proposal?.protocol !== "v2-task-split-proposal-v1" ||
        proposal?.parent_id !== parent || proposal?.depth !== request?.depth ||
        proposal?.generation !== request?.generation || !Array.isArray(proposal?.proposals) ||
        proposal.proposals.length !== 2) return null;
    return proposal;
  } catch {
    return null;
  }
}

function fileStatus(path) {
  try {
    return existsSync(path) ? "present" : "absent";
  } catch {
    return "unknown";
  }
}

function attemptFor(directory, did, childSessionID) {
  if (!/^D\d{3}(?:-[AB](?:[12])?)?$/.test(did)) return "unknown";
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
  if (!/^D\d{3}(?:-[AB](?:[12])?)?$/.test(did)) return ["  (unknown): unknown"];
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
  const did = exactDeliverable(args);
  const childSessionID = metadata?.sessionID || "unknown";
  const readyPath = /^D\d{3}(?:-[AB](?:[12])?)?$/.test(did)
    ? join(directory, ".opencode-v2", "work", `${did}.ready`)
    : "";
  const progressPath = /^D\d{3}(?:-[AB](?:[12])?)?$/.test(did)
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

export const V2BoundedSubagentPlugin = async ({ directory, api }) => {
  // Stamp OpenCode V2's deterministic request kind before provider dispatch.
  const modelRequest = await api.session.hook("model.request", async (event) => {
    markRequestPurpose(event);
  });

  const before = await api.tool.hook("execute.before", async (event, output) => {
    guardWorkerMutation(directory, event, output);
    if (event.tool !== "subagent" && event.tool !== "task") return;
    // Support both the beta combined event shape and the newer split
    // input/output hook shape.
    const args = hookArgs(event, output);
    const agent = args.agent;
    const prompt = args.prompt;
    if (agent === "acceptance-validator") {
      execFileSync("python3", [FINALIZE_ACCEPTANCE, "--prepare", directory], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    }
    if (agent === "task-splitter") {
      const parent = splitParent(args);
      if (!parent) throw new Error("SPLIT_DENY task-splitter requires exact SPLIT_PARENT prompt");
      // This durable preclaim prevents a restarted root from launching another
      // splitter for the same generation. It has no attempt/operator authority.
      supervisor(directory, ["--agent", "task-splitter", "--prompt", prompt, "--claim-splitter", hookCallID(event)]);
      return;
    }
    if (agent === "general") {
      throw new Error("DISPATCH_DENY general is not a canonical implementation role");
    }
    const hasDeliverable = typeof prompt === "string" && /^DELIVERABLE:\s*D\d{3}(?:-[AB](?:[12])?)?\s*$/m.test(prompt);
    const implementationAgents = new Set(["probe-builder", "implementer", "core-builder", "feature-builder", "reasoning-builder", "integrator", "tester", "test-builder"]);
    if (!implementationAgents.has(agent) && !hasDeliverable) return;
    // Render the exact post-preclaim runtime prompt before reserving an attempt.
    // Rendering is read-only; the canonical five-line parent prompt remains the
    // only text authorized to reserve an attempt.
    const did = exactDeliverable(args);
    const runtimePrompt = implementationAgents.has(agent) && did !== "unknown"
      ? supervisor(directory, ["--agent", String(agent || ""), "--render-runtime-prompt", did]).replace(/\r?\n$/, "")
      : null;

    // This deterministic supervisor claim occurs before OpenCode materializes
    // the child session or sends a provider request.
    supervisor(directory, ["--agent", String(agent || ""), "--prompt", String(prompt || ""), "--claim-dispatch", hookCallID(event)]);

    // Only after a successful canonical preclaim may deterministic supervisor
    // text replace the child-visible prompt. No model/root-authored suffix is
    // accepted here.
    if (runtimePrompt !== null) args.prompt = runtimePrompt;
  });
  const after = await api.tool.hook("execute.after", async (event, output) => {
    const result = event?.result || output;
    if ((event.tool !== "subagent" && event.tool !== "task") || !result) return;
    const args = hookArgs(event, output);
    const rawResult = toolResultText(result);
    const splitterParent = args?.agent === "task-splitter" ? splitParent(args) : "";
    if (splitterParent) {
      // The completion event is the deterministic validation trigger. The
      // supervisor reads only the splitter's durable proposal and records an
      // explicit failure if it is absent or invalid; no root cycle is needed.
      const session = String(result.metadata?.sessionID || "");
      const token = hookCallID(event);
      const encoded = Buffer.from(rawResult, "utf8").toString("base64");
      try {
        supervisor(directory, ["--complete-splitter", splitterParent, "--prompt", session, "--dispatch-token", token, "--splitter-output-b64", encoded]);
      } catch {
        // Status/lease state is durable; stale or invalid completions cannot commit.
      }
    }
    let receipt;
    if (args?.agent === "acceptance-validator") {
      const raw = rawResult.trim();
      const modelPass = /^ACCEPTANCE_PASS(?:\s*<\/subagent>)?$/.test(raw);
      if (!modelPass) {
        receipt = "ACCEPTANCE_FAIL\nMODEL_VERDICT_NOT_EXACT_PASS";
      } else {
        try {
          execFileSync("python3", [FINALIZE_ACCEPTANCE, directory], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
          receipt = "ACCEPTANCE_PASS";
        } catch (error) {
          const detail = String(error?.stderr || error?.message || error).trim().replace(/\s+/g, " ").slice(0, 1200);
          receipt = `ACCEPTANCE_FAIL\nDETERMINISTIC_GATE: ${detail || "failed"}`;
        }
      }
    } else {
      receipt = boundedChildResult({ directory, args, metadata: result.metadata, original: rawResult });
    }
    // The beta constructs the parent-visible tool response from `content`.
    // Updating `output` alone only changed hook metadata, not the text the root
    // receives, so replace both representations with the same bounded receipt.
    result.output = receipt;
    result.content = [{ type: "text", text: receipt }];
    result.metadata = {
      ...result.metadata,
      parentResultBounded: true,
      parentResultChars: receipt.length,
      fullOutputStorage: "session_history",
    };
    // Batch 8: supervisor Verify still needs this exact attempt's
    // ephemeral runtime environment. Terminal cleanup is supervisor-owned.
  });
  return () => {
    modelRequest.dispose();
    before.dispose();
    after.dispose();
  };
};

if (process.env.V2_BOUNDED_SUBAGENT_SELFTEST === "1") {
  const proposal = "{\"protocol\":\"v2-task-split-proposal-v1\",\"parent_id\":\"D009\"}";
  const contentOnly = { content: [{ type: "text", text: proposal }], metadata: { sessionID: "ses-test" } };
  if (toolResultText(contentOnly) !== proposal) throw new Error("content-only tool result was not extracted");
  if (toolResultText({ content: "ACCEPTANCE_PASS" }) !== "ACCEPTANCE_PASS") {
    throw new Error("string content tool result was not extracted");
  }
  const event = { kind: "compaction", headers: {} };
  if (!markRequestPurpose(event) || event.headers["x-v2-request-purpose"] !== "compaction") {
    throw new Error("compaction source marker was not stamped");
  }
  const primary = { kind: "primary", headers: {} };
  if (markRequestPurpose(primary) || primary.headers["x-v2-request-purpose"]) {
    throw new Error("normal request was marked as compaction");
  }
  console.log("v2-bounded-subagent selftest: OK");
}

export default {
  id: "v2-bounded-subagent",
  setup: async (api) => V2BoundedSubagentPlugin({ directory: api.location.directory, api }),
};
