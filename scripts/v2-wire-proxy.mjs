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

function enforceGenerationCap(j, policy) {
  const roleCap = positiveInt(j.v2_max_tokens);
  delete j.v2_max_tokens;

  const fallback = FALLBACK_GENERATION_CAPS[policy] || FALLBACK_GENERATION_CAPS.unchanged;
  const configured = roleCap || fallback;
  const existing = [positiveInt(j.max_tokens), positiveInt(j.max_completion_tokens)]
    .filter((x) => x !== null);
  const cap = Math.min(configured, GLOBAL_GENERATION_CEILING, ...existing);

  // vLLM's OpenAI-compatible chat endpoint consumes max_tokens. Avoid sending
  // a null/duplicate max_completion_tokens alongside it.
  j.max_tokens = cap;
  if (j.max_completion_tokens == null) {
    delete j.max_completion_tokens;
  } else {
    j.max_completion_tokens = cap;
  }
  return { cap, source: roleCap ? "role-marker" : "fallback" };
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
        const generation = enforceGenerationCap(j, policy);

        const after = meta(j);

        log({
          kind: "request",
          policy,
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
