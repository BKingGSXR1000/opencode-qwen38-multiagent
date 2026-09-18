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

export function implementationDispatchToken(event, did) {
  const callID = hookCallID(event);
  if (!callID) return "";
  const deliverable = String(did || "");
  return deliverable && deliverable !== "unknown"
    ? `${callID}:${deliverable}`
    : callID;
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

const earlyWriteSatisfiedSessions = new Set();

export async function interruptDeniedSession(api, sessionID) {
  if (!api?.session || typeof api.session.interrupt !== "function") {
    throw new Error("native session.interrupt API unavailable");
  }
  await api.session.interrupt({ sessionID, continue: false });
}

async function guardEarlyWrite(directory, event, output, api) {
  const tool = String(event?.tool || "");
  if (tool === "subagent" || tool === "task") return;
  const sessionID = hookSessionID(event);
  if (!sessionID || earlyWriteSatisfiedSessions.has(sessionID)) return;
  const args = hookArgs(event, output);
  const payload = Buffer.from(JSON.stringify(args), "utf8").toString("base64");
  let raw;
  try {
    raw = supervisor(directory, [
      "--early-write-check", sessionID,
      "--tool-name", tool,
      "--tool-args-b64", payload,
    ]);
  } catch (error) {
    const detail = String(error?.stderr || error?.message || error).trim();
    if (detail.includes("EARLY_WRITE_DENY")) {
      try {
        await interruptDeniedSession(api, sessionID);
        supervisor(directory, ["--confirm-plugin-interrupt", sessionID]);
      } catch (interruptError) {
        const interruptDetail = String(
          interruptError?.stderr || interruptError?.message || interruptError
        ).trim();
        throw new Error(
          `${detail} PLUGIN_NATIVE_INTERRUPT_FAILED ${interruptDetail || "unknown"}`
        );
      }
    }
    throw new Error(detail || `EARLY_WRITE_DENY session=${sessionID}`);
  }
  if (/^EARLY_WRITE_(?:NA|SATISFIED)\b/.test(String(raw || "").trim())) {
    earlyWriteSatisfiedSessions.add(sessionID);
  }
}

function guardSplitterToolBoundary(directory, event, output) {
  const sessionID = hookSessionID(event);
  if (!sessionID) return;
  const tool = String(event?.tool || "");
  if (tool === "subagent" || tool === "task") return;
  const args = hookArgs(event, output);
  const payload = Buffer.from(JSON.stringify(args), "utf8").toString("base64");
  try {
    supervisor(directory, [
      "--splitter-tool-check", sessionID,
      "--tool-name", tool,
      "--tool-args-b64", payload,
    ]);
  } catch (error) {
    const detail = String(error?.stderr || error?.message || error).trim();
    if (detail.includes("not-task-splitter")) return;
    if (detail.includes("SPLITTER_TOOL_DENY")) throw new Error(detail);
  }
}

function guardProgressHandoff(directory, event, output) {
  const sessionID = hookSessionID(event);
  if (!sessionID) return;
  const tool = String(event?.tool || "");
  if (tool === "subagent" || tool === "task") return;
  const args = hookArgs(event, output);
  const payload = Buffer.from(JSON.stringify(args), "utf8").toString("base64");
  try {
    supervisor(directory, [
      "--progress-handoff-tool-check", sessionID,
      "--tool-name", tool,
      "--tool-args-b64", payload,
    ]);
  } catch (error) {
    const detail = String(error?.stderr || error?.message || error).trim();
    if (detail.includes("not-probe-builder") || detail.includes("not-progress-handoff")) return;
    if (detail.includes("PROGRESS_HANDOFF_DENY")) throw new Error(detail);
  }
}

export function proposalFromOutput(output, parent, directory) {
  if (typeof output !== "string") return null;
  const raw = output.trim();
  if (!raw || raw.startsWith("```") || raw.endsWith("```")) return null;
  try {
    const proposal = JSON.parse(raw);
    const request = JSON.parse(readFileSync(join(directory, ".opencode-v2", "work", `${parent}.split-request.json`), "utf8"));
    if (proposal?.parent_id !== parent || proposal?.depth !== request?.depth ||
        proposal?.generation !== request?.generation) return null;
    if (proposal?.protocol === "v2-split-parent-contract-invalid-v1") {
      return proposal?.field === "verify_command" && typeof proposal?.reason === "string"
        ? proposal : null;
    }
    if (proposal?.protocol !== "v2-task-split-proposal-v2" ||
        !Array.isArray(proposal?.proposals) || proposal.proposals.length !== 2) return null;
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


// V2.6.9 CONTINUOUS IMPLEMENTATION BACKFILL BEGIN
const BACKFILL_INITIAL_SETTLE_MS = 600;
const BACKFILL_POLL_MS = 250;
const BACKFILL_STALE_LAUNCH_MS = 1500;
const BACKFILL_IMPLEMENTATION_AGENTS = new Set([
  "probe-builder", "implementer", "core-builder", "feature-builder",
  "reasoning-builder", "integrator", "tester", "test-builder",
]);

const nativeImplementationCalls = new Map();
const backfillPumps = new Map();
const backfillChildren = new Map();
const backfillFailedThisWave = new Map();
let backfillSerial = 0;

function nativeCallKey(event, did) {
  return `${hookCallID(event)}:${did}`;
}

function rootNativeCalls(rootSessionID, create = false) {
  let calls = nativeImplementationCalls.get(rootSessionID);
  if (!calls && create) {
    calls = new Map();
    nativeImplementationCalls.set(rootSessionID, calls);
  }
  return calls;
}

function trackNativeImplementationStart(event, did) {
  const rootSessionID = hookSessionID(event);
  if (!rootSessionID || !did || did === "unknown") return;
  rootNativeCalls(rootSessionID, true).set(nativeCallKey(event, did), did);
}

function trackNativeImplementationComplete(event, did) {
  const rootSessionID = hookSessionID(event);
  const calls = rootNativeCalls(rootSessionID, false);
  if (!calls) return 0;
  calls.delete(nativeCallKey(event, did));
  if (!calls.size) {
    nativeImplementationCalls.delete(rootSessionID);
    backfillFailedThisWave.delete(rootSessionID);
    return 0;
  }
  return calls.size;
}

function nativeActiveDids(rootSessionID) {
  return new Set(rootNativeCalls(rootSessionID, false)?.values() || []);
}

function canonicalImplementationPrompt(did) {
  return [
    `DELIVERABLE: ${did}`,
    `Read .opencode-v2/query/leaves/${did}-context.json exactly once for this session; it is the complete authoritative deliverable contract. Do not read .opencode-v2/IMPLEMENTATION_PLAN.md or a separate split scope.`,
    `Read .opencode-v2/work/${did}.progress.md if present.`,
    "Inspect your owned project artifacts as they currently exist.",
    "Continue from actual filesystem state and execute the deliverable.",
  ].join("\n");
}

function readDecision(directory) {
  try {
    const path = join(directory, ".opencode-v2", "query", "decision.json");
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function childSessionID(created) {
  return String(
    created?.id ||
    created?.sessionID ||
    created?.data?.id ||
    created?.data?.sessionID ||
    ""
  );
}

function reconcileBackfillChildren(rootSessionID, decision) {
  const active = new Set(
    Array.isArray(decision?.scheduler?.active_deliverables)
      ? decision.scheduler.active_deliverables
      : []
  );
  const now = Date.now();
  for (const [did, state] of [...backfillChildren.entries()]) {
    if (state.rootSessionID !== rootSessionID) continue;
    if (active.has(did)) {
      state.seenActive = true;
      continue;
    }
    if (state.seenActive || now - state.launchedAt >= BACKFILL_STALE_LAUNCH_MS) {
      backfillChildren.delete(did);
    }
  }
}

export function selectBackfillCandidates(decision, excluded = new Set()) {
  if (!decision || decision.state_error === true || decision.resume_phase !== "execution") {
    return [];
  }
  const scheduler = decision.scheduler || {};
  if (scheduler.error) return [];
  const slots = Math.max(0, Number(scheduler.available_worker_slots || 0));
  if (!slots) return [];
  const eligible = Array.isArray(decision.eligible) ? decision.eligible : [];
  const roles = decision.eligible_roles && typeof decision.eligible_roles === "object"
    ? decision.eligible_roles
    : {};
  const result = [];
  for (const did of eligible) {
    const role = roles[did];
    if (
      typeof did !== "string" ||
      excluded.has(did) ||
      !BACKFILL_IMPLEMENTATION_AGENTS.has(role)
    ) {
      continue;
    }
    result.push({ did, role });
    if (result.length >= slots) break;
  }
  return result;
}

async function launchBackfillWorker(directory, api, rootSessionID, did, agent) {
  const token = `backfill:${rootSessionID}:${did}:${++backfillSerial}`;
  const canonicalPrompt = canonicalImplementationPrompt(did);

  supervisor(directory, [
    "--agent", agent,
    "--prompt", canonicalPrompt,
    "--claim-dispatch", token,
  ]);

  const runtimePrompt = supervisor(directory, [
    "--agent", agent,
    "--render-runtime-prompt", did,
  ]).replace(/\r?\n$/, "");

  if (
    typeof api?.session?.create !== "function" ||
    typeof api?.session?.switchAgent !== "function" ||
    typeof api?.session?.prompt !== "function"
  ) {
    throw new Error("BACKFILL_API_UNAVAILABLE session.create/switchAgent/prompt");
  }

  const created = await api.session.create({
    parentID: rootSessionID,
    title: `V2 backfill ${did}`,
  });
  const sessionID = childSessionID(created);
  if (!sessionID) throw new Error(`BACKFILL_SESSION_ID_MISSING deliverable=${did}`);

  backfillChildren.set(did, {
    rootSessionID,
    sessionID,
    launchedAt: Date.now(),
    seenActive: false,
  });

  await api.session.switchAgent({ sessionID, agent });
  await api.session.prompt({ sessionID, text: runtimePrompt });
  console.error(
    `[V2_BACKFILL] launched deliverable=${did} agent=${agent} session=${sessionID}`
  );
  return sessionID;
}

async function runBackfillPump(directory, api, rootSessionID) {
  if (!rootSessionID || backfillPumps.has(rootSessionID)) return;

  const pump = (async () => {
    await sleepMs(BACKFILL_INITIAL_SETTLE_MS);
    while ((rootNativeCalls(rootSessionID, false)?.size || 0) > 0) {
      const decision = readDecision(directory);
      if (decision) reconcileBackfillChildren(rootSessionID, decision);

      const excluded = nativeActiveDids(rootSessionID);
      for (const did of backfillChildren.keys()) excluded.add(did);
      for (const did of backfillFailedThisWave.get(rootSessionID) || []) excluded.add(did);

      const candidates = selectBackfillCandidates(decision, excluded);
      for (const { did, role } of candidates) {
        if ((rootNativeCalls(rootSessionID, false)?.size || 0) <= 0) break;
        try {
          await launchBackfillWorker(directory, api, rootSessionID, did, role);
        } catch (error) {
          let failed = backfillFailedThisWave.get(rootSessionID);
          if (!failed) {
            failed = new Set();
            backfillFailedThisWave.set(rootSessionID, failed);
          }
          failed.add(did);
          const detail = String(error?.stderr || error?.message || error)
            .trim()
            .replace(/\s+/g, " ")
            .slice(0, 800);
          console.error(
            `[V2_BACKFILL] launch-denied deliverable=${did} detail=${detail || "unknown"}`
          );
        }
      }
      await sleepMs(BACKFILL_POLL_MS);
    }
  })().finally(() => {
    backfillPumps.delete(rootSessionID);
  });

  backfillPumps.set(rootSessionID, pump);
  void pump;
}
// V2.6.9 CONTINUOUS IMPLEMENTATION BACKFILL END


export const V2BoundedSubagentPlugin = async ({ directory, api }) => {
  // Stamp OpenCode V2's deterministic request kind before provider dispatch.
  const modelRequest = await api.session.hook("model.request", async (event) => {
    markRequestPurpose(event);
  });

  const before = await api.tool.hook("execute.before", async (event, output) => {
    guardSplitterToolBoundary(directory, event, output);
    guardProgressHandoff(directory, event, output);
    await guardEarlyWrite(directory, event, output, api);
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
    // OpenCode2 beta can expose one shared hook call ID to multiple subagent
    // calls emitted in the same assistant response. Namespace implementation
    // reservations by canonical Dxxx so C3 batch preclaims remain unique while
    // repeated delivery of the same call+Dxxx stays idempotent.
    const dispatchToken = implementationDispatchToken(event, did);
    supervisor(directory, ["--agent", String(agent || ""), "--prompt", String(prompt || ""), "--claim-dispatch", dispatchToken]);

    // Only after a successful canonical preclaim may deterministic supervisor
    // text replace the child-visible prompt. No model/root-authored suffix is
    // accepted here.
    if (runtimePrompt !== null) args.prompt = runtimePrompt;

    trackNativeImplementationStart(event, did);
  });
  const after = await api.tool.hook("execute.after", async (event, output) => {
    const result = event?.result || output;
    if ((event.tool !== "subagent" && event.tool !== "task") || !result) return;
    const args = hookArgs(event, output);
    const completedDid = exactDeliverable(args);
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

    if (completedDid !== "unknown") {
      const remaining = trackNativeImplementationComplete(event, completedDid);
      if (remaining > 0) void runBackfillPump(directory, api, hookSessionID(event));
    }
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

  // OpenCode2 beta may share one event id across a batch. Different
  // deliverables must still reserve distinct supervisor dispatch identities.
  const shared = { id: "call-shared" };
  const d1 = implementationDispatchToken(shared, "D001");
  const d2 = implementationDispatchToken(shared, "D002");
  if (d1 !== "call-shared:D001" || d2 !== "call-shared:D002" || d1 === d2) {
    throw new Error("implementation batch dispatch tokens are not Dxxx-namespaced");
  }
  if (implementationDispatchToken(shared, "D001") !== d1) {
    throw new Error("implementation dispatch token is not idempotent");
  }
  if (implementationDispatchToken({ callID: "split-call" }, "unknown") !== "split-call") {
    throw new Error("unknown-deliverable dispatch token fallback changed");
  }

  const canonical = canonicalImplementationPrompt("D042");
  if (canonical.split("\n").length !== 5 || !canonical.startsWith("DELIVERABLE: D042\n")) {
    throw new Error("backfill canonical implementation prompt shape changed");
  }
  const candidates = selectBackfillCandidates({
    resume_phase: "execution",
    state_error: false,
    eligible: ["D001", "D002", "D003"],
    eligible_roles: {
      D001: "implementer",
      D002: "tester",
      D003: "reference-researcher",
    },
    scheduler: { available_worker_slots: 2, error: "" },
  }, new Set(["D001"]));
  if (JSON.stringify(candidates) !== JSON.stringify([{ did: "D002", role: "tester" }])) {
    throw new Error("backfill candidate selection changed");
  }

  console.log("v2-bounded-subagent selftest: OK");
}

export default {
  id: "v2-bounded-subagent",
  setup: async (api) => V2BoundedSubagentPlugin({ directory: api.location.directory, api }),
};
