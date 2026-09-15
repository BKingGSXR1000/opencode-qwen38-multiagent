import http from "node:http";
import fs from "node:fs";

const LISTEN_PORT = Number(process.env.V2_PROXY_LISTEN_PORT || 18033);
const TARGET_PORT = Number(process.env.V2_PROXY_TARGET_PORT || 18030);

const LOG =
  process.env.V2_PROXY_LOG ||
  `${process.env.HOME}/AI/opencode-qwen38-multiagent-v2/logs/v2-wire.jsonl`;

const VALID_EFFORTS = new Set(["low", "medium", "high"]);
const MODE = "v2-role-aware-bounded-v1";
const GLOBAL_GENERATION_CEILING = 12288;
const COMPACTION_GENERATION_CAP = 8192;
const FALLBACK_GENERATION_CAPS = Object.freeze({
  off: 4096,
  low: 5120,
  medium: 8192,
  high: 12288,
  "thinking-on-default": 8192,
  unchanged: 8192,
});

// V2 reasoning budgets are hard ceilings, not targets.
// Keep role routing semantic (LOW/MEDIUM/HIGH), but prevent a single
// model call from spending tens of thousands of tokens in hidden reasoning.
const THINKING_BUDGETS = Object.freeze({
  low: 2048,
  medium: 4096,
  high: 8192,
});

function log(obj) {
  fs.appendFileSync(
    LOG,
    JSON.stringify({
      ts: new Date().toISOString(),
      ...obj,
    }) + "\n"
  );
}

function meta(j) {
  return {
    model: j.model ?? null,
    max_tokens: j.max_tokens ?? null,
    max_completion_tokens: j.max_completion_tokens ?? null,
    reasoning_effort: j.reasoning_effort ?? null,
    thinking_token_budget: j.thinking_token_budget ?? null,
    thinking_budget_tokens: j.thinking_budget_tokens ?? null,
    chat_template_kwargs: j.chat_template_kwargs ?? null,
    messages: Array.isArray(j.messages) ? j.messages.length : null,
    tools: Array.isArray(j.tools) ? j.tools.length : null,
    stream: j.stream ?? null,
    v2_max_tokens: j.v2_max_tokens ?? null,
  };
}

function chooseEffort(j, ctk) {
  if (VALID_EFFORTS.has(j.reasoning_effort)) {
    return j.reasoning_effort;
  }

  if (VALID_EFFORTS.has(ctk.reasoning_effort)) {
    return ctk.reasoning_effort;
  }

  return null;
}

function positiveInt(value) {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function messageText(value, depth = 0) {
  // Provider adapters are allowed to nest text under content/parts/input_text
  // objects. New34 proved that inspecting only string/array `message.content`
  // can miss a real OpenCode v1.18.19 compaction request and incorrectly leave
  // the orchestrator's 2048-token role cap in force. Flatten only the messages
  // tree recursively; the anchored-summary markers below still provide the
  // semantic discriminator.
  if (depth > 16 || value == null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.map((item) => messageText(item, depth + 1)).filter(Boolean).join("\n");
  }
  if (typeof value !== "object") return "";
  return Object.values(value)
    .map((item) => messageText(item, depth + 1))
    .filter(Boolean)
    .join("\n");
}

function detectPurpose(j, explicitPurpose = "") {
  // Batch16E/New35: OpenCode V2 exposes request kind deterministically through
  // session.model.request. The plugin stamps only real compaction requests with
  // x-v2-request-purpose=compaction. Trust that source marker first and keep
  // prompt recognition only as a compatibility fallback.
  if (String(explicitPurpose).toLowerCase() === "compaction") {
    return "compaction";
  }

  const messages = Array.isArray(j.messages) ? j.messages : [];
  const text = messageText(messages);

  const anchored =
    text.includes("Create a new anchored summary") ||
    text.includes("Update the anchored summary") ||
    text.includes("Here is the summary of the conversation before the <conversation>");
  const envelope = text.includes("<conversation>") && text.includes("</conversation>");
  const legacyStructure =
    text.includes("## Objective") &&
    text.includes("## Work State") &&
    text.includes("## Relevant Files");
  const goalProgressStructure =
    text.includes("## Goal") &&
    text.includes("## Progress") &&
    text.includes("## Relevant Files");

  // OpenCode V2 beta-19242/current V2 local-summary family used in New35.
  const v2SummaryInstruction =
    text.includes("Summarize only what the user and the assistant said and did.") &&
    (
      text.includes("You MUST summarize the conversation above into a structured summary") ||
      text.includes("Update the existing checkpoint in the conversation above into one consolidated summary.")
    );
  const v2Structure =
    text.includes("## Objective") &&
    text.includes("## Requirements") &&
    text.includes("## Decisions") &&
    text.includes("## Work State") &&
    text.includes("## Relevant Files");

  return (
    (anchored && envelope && (legacyStructure || goalProgressStructure)) ||
    (v2SummaryInstruction && v2Structure)
  ) ? "compaction" : "normal";
}

function enforceGenerationCap(j, policy, purpose) {
  const roleCap = positiveInt(j.v2_max_tokens);
  delete j.v2_max_tokens;

  let cap;
  let source;
  if (purpose === "compaction") {
    // OpenCode beta's compaction request currently arrives without max_tokens.
    // New31 proved that the normal non-thinking 4096 clamp can truncate a
    // legitimate anchored summary and detach the visible parent session. Give
    // only recognized compaction prompts a larger, still-hard bounded budget.
    cap = Math.min(COMPACTION_GENERATION_CAP, GLOBAL_GENERATION_CEILING);
    source = "compaction-purpose";
  } else {
    const fallback = FALLBACK_GENERATION_CAPS[policy] || FALLBACK_GENERATION_CAPS.unchanged;
    const configured = roleCap || fallback;
    const existing = [positiveInt(j.max_tokens), positiveInt(j.max_completion_tokens)]
      .filter((x) => x !== null);
    cap = Math.min(configured, GLOBAL_GENERATION_CEILING, ...existing);
    source = roleCap ? "role-marker" : "fallback";
  }

  // vLLM's OpenAI-compatible chat endpoint consumes max_tokens. Avoid sending
  // a null/duplicate max_completion_tokens alongside it.
  j.max_tokens = cap;
  if (j.max_completion_tokens == null) {
    delete j.max_completion_tokens;
  } else {
    j.max_completion_tokens = cap;
  }
  return { cap, source };
}

function runSelfTest() {
  const current = {
    messages: [{ role: "user", content: [
      "Here is the conversation so far:",
      "<conversation>",
      "user: task",
      "</conversation>",
      "Create a new anchored summary from the conversation history in the <conversation> tags above so another coding agent can continue the work.",
      "## Goal",
      "## Progress",
      "## Relevant Files",
    ].join("\n") }],
    v2_max_tokens: 2048,
  };
  const update = {
    messages: [{ role: "user", content: [
      "<conversation>", "recent", "</conversation>",
      "Here is the summary of the conversation before the <conversation> above:",
      "Update the anchored summary below using the conversation history above.",
      "## Goal", "## Progress", "## Relevant Files",
    ].join("\n") }],
  };
  const legacy = {
    messages: [{ role: "user", content: [
      "<conversation>", "old", "</conversation>",
      "Create a new anchored summary from the conversation history.",
      "## Objective", "## Work State", "## Relevant Files",
    ].join("\n") }],
  };
  const normal = {
    messages: [{ role: "user", content: "Read control-status.json and continue.\n## Goal\n## Progress\n## Relevant Files" }],
  };
  // New34 production regression: the compaction Started row was durable but
  // the immediately-following provider request was classified as normal and
  // inherited v2_max_tokens=2048. The prompt can arrive under provider-specific
  // nested message objects; recursive extraction must still identify it.
  const nestedCompaction = {
    messages: [{
      role: "user",
      content: {
        parts: [{ type: "input_text", payload: { text: [
          "Here is the conversation so far:",
          "<conversation>",
          "assistant: prior work",
          "</conversation>",
          "Create a new anchored summary from the conversation history in the <conversation> tags above so another coding agent can continue the work.",
          "## Objective",
          "## Work State",
          "## Relevant Files",
        ].join("\n") } }],
      },
    }],
    tools: [{}, {}],
    v2_max_tokens: 2048,
  };
  const liveV2Compaction = {
    messages: [{ role: "user", content: [
      "You MUST summarize the conversation above into a structured summary that will be given to another agent to resume the work.",
      "Summarize only what the user and the assistant said and did.",
      "## Objective",
      "## Requirements",
      "## Decisions",
      "## Work State",
      "## Relevant Files",
    ].join("\n") }],
    v2_max_tokens: 2048,
  };
  const opaqueMarkedCompaction = {
    messages: [{ role: "user", content: "opaque provider-transformed request" }],
    v2_max_tokens: 2048,
  };
  if (detectPurpose(current) !== "compaction") throw new Error("current compaction prompt not detected");
  if (detectPurpose(update) !== "compaction") throw new Error("update compaction prompt not detected");
  if (detectPurpose(legacy) !== "compaction") throw new Error("legacy compaction prompt not detected");
  if (detectPurpose(normal) !== "normal") throw new Error("normal request misclassified as compaction");
  if (detectPurpose(nestedCompaction) !== "compaction") throw new Error("nested compaction request not detected");
  if (detectPurpose(liveV2Compaction) !== "compaction") throw new Error("OpenCode V2 compaction prompt not detected");
  if (detectPurpose(opaqueMarkedCompaction, "compaction") !== "compaction") throw new Error("source-marked compaction not detected");
  const nestedCapped = JSON.parse(JSON.stringify(nestedCompaction));
  const nestedResult = enforceGenerationCap(nestedCapped, "off", detectPurpose(nestedCapped));
  if (nestedResult.cap !== COMPACTION_GENERATION_CAP || nestedResult.source !== "compaction-purpose") {
    throw new Error("nested compaction did not override inherited role cap");
  }
  const marked = JSON.parse(JSON.stringify(opaqueMarkedCompaction));
  const markedResult = enforceGenerationCap(marked, "off", detectPurpose(marked, "compaction"));
  if (markedResult.cap !== COMPACTION_GENERATION_CAP || markedResult.source !== "compaction-purpose") {
    throw new Error("source-marked compaction did not override inherited role cap");
  }
  const v2Capped = JSON.parse(JSON.stringify(liveV2Compaction));
  const v2Result = enforceGenerationCap(v2Capped, "off", detectPurpose(v2Capped));
  if (v2Result.cap !== COMPACTION_GENERATION_CAP || v2Result.source !== "compaction-purpose") {
    throw new Error("OpenCode V2 compaction did not receive dedicated cap");
  }
  const capped = JSON.parse(JSON.stringify(current));
  const result = enforceGenerationCap(capped, "off", detectPurpose(capped));
  if (result.cap !== COMPACTION_GENERATION_CAP || result.source !== "compaction-purpose") {
    throw new Error(`compaction cap mismatch: ${JSON.stringify(result)}`);
  }
  console.log("v2-wire-proxy selftest: OK");
}

if (process.env.V2_PROXY_SELFTEST === "1") {
  runSelfTest();
  process.exit(0);
}

const server = http.createServer((req, res) => {
  if (req.url === "/__proxy_health") {
    res.writeHead(200, {
      "content-type": "application/json",
    });

    res.end(
      JSON.stringify({
        ok: true,
        mode: MODE,
        listen: LISTEN_PORT,
        target: TARGET_PORT,
        precedence:
          "explicit reasoning_effort low/medium/high > enable_thinking=false",
        hard_thinking_budget: THINKING_BUDGETS,
        hard_generation_ceiling: GLOBAL_GENERATION_CEILING,
        compaction_generation_cap: COMPACTION_GENERATION_CAP,
        fallback_generation_caps: FALLBACK_GENERATION_CAPS,
      })
    );

    return;
  }

  const chunks = [];

  req.on("data", (chunk) => chunks.push(chunk));

  req.on("end", () => {
    let body = Buffer.concat(chunks);

    const isChat =
      req.method === "POST" &&
      (
        req.url === "/v1/chat/completions" ||
        req.url === "/chat/completions"
      );

    if (isChat && body.length) {
      try {
        const j = JSON.parse(body.toString("utf8"));
        const before = meta(j);

        const ctk = {
          ...(j.chat_template_kwargs || {}),
        };

        const effort = chooseEffort(j, ctk);

        let policy = "unchanged";

        /*
         * IMPORTANT:
         *
         * An explicit LOW/MEDIUM/HIGH effort is more specific than a stale
         * enable_thinking:false inherited from an alias/provider translation.
         *
         * This fixes the observed request:
         *
         *   reasoning_effort:"medium"
         *   enable_thinking:false
         *
         * which must become MEDIUM, not OFF.
         */
        if (effort !== null) {
          j.reasoning_effort = effort;

          ctk.enable_thinking = true;
          ctk.reasoning_effort = effort;

          // vLLM/Qwen hard reasoning ceiling. This is deliberately top-level;
          // it is the field consumed by the current local backend.
          j.thinking_token_budget = THINKING_BUDGETS[effort];

          policy = effort;
        } else if (ctk.enable_thinking === false) {
          // Genuine non-thinking role. Remove any stale inherited budget too.
          delete j.reasoning_effort;
          delete j.thinking_token_budget;
          delete ctk.reasoning_effort;
          delete ctk.thinking_token_budget;

          ctk.enable_thinking = false;
          policy = "off";
        } else if (ctk.enable_thinking === true) {
          // Thinking explicitly enabled but no named effort: use the MEDIUM
          // ceiling rather than allowing an unbounded thinking request.
          j.thinking_token_budget = THINKING_BUDGETS.medium;
          policy = "thinking-on-default";
        }

        j.chat_template_kwargs = ctk;
        const explicitPurpose = String(req.headers["x-v2-request-purpose"] || "");
        const purpose = detectPurpose(j, explicitPurpose);
        const generation = enforceGenerationCap(j, policy, purpose);

        const after = meta(j);

        log({
          kind: "request",
          policy,
          purpose,
          purpose_source: explicitPurpose.toLowerCase() === "compaction" ? "source-header" : "prompt-fallback",
          generation,
          before,
          after,
        });

        body = Buffer.from(JSON.stringify(j));
      } catch (err) {
        log({
          kind: "parse-error",
          error: String(err),
        });
      }
    }

    const headers = {
      ...req.headers,
      host: `127.0.0.1:${TARGET_PORT}`,
    };

    delete headers["transfer-encoding"];
    // Private control-plane marker; the inference backend does not need it.
    delete headers["x-v2-request-purpose"];

    if (body.length) {
      headers["content-length"] = String(body.length);
    } else {
      delete headers["content-length"];
    }

    const upstream = http.request(
      {
        hostname: "127.0.0.1",
        port: TARGET_PORT,
        path: req.url,
        method: req.method,
        headers,
      },
      (upstreamRes) => {
        res.writeHead(
          upstreamRes.statusCode || 500,
          upstreamRes.headers
        );

        upstreamRes.pipe(res);
      }
    );

    upstream.on("error", (err) => {
      log({
        kind: "upstream-error",
        error: String(err),
      });

      if (!res.headersSent) {
        res.writeHead(502, {
          "content-type": "text/plain",
        });
      }

      res.end("V2 proxy upstream error\n");
    });

    if (body.length) {
      upstream.write(body);
    }

    upstream.end();
  });
});

server.listen(LISTEN_PORT, "127.0.0.1", () => {
  log({
    kind: "start",
    mode: MODE,
    listen: `http://127.0.0.1:${LISTEN_PORT}`,
    target: `http://127.0.0.1:${TARGET_PORT}`,
  });

  console.log(
    `V2 bounded role-aware proxy :${LISTEN_PORT} -> :${TARGET_PORT}`
  );
});
