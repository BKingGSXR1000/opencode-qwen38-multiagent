#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ROOT = path.join(process.env.HOME, "AI/opencode-qwen38-multiagent-v2");
const PW = path.join(ROOT, "tools/acceptance-browser/node_modules/playwright");

function fail(msg) {
  console.error("ACCEPTANCE_BROWSER_ERROR:", msg);
  process.exit(2);
}

const url = process.argv[2];
const projectOut = process.argv[3] || ".opencode-v2";
if (!url) fail("Usage: acceptance-browser.mjs <URL> [project-output-dir]");
if (!fs.existsSync(PW)) {
  fail(`Playwright is not installed. Run: ${ROOT}/scripts/setup-acceptance-browser.sh`);
}

const { chromium } = require(PW);
const outRoot = path.resolve(projectOut);
const shotDir = path.join(outRoot, "acceptance");
fs.mkdirSync(shotDir, { recursive: true });

const screenshotPath = path.join(shotDir, "page.png");
const evidencePath = path.join(outRoot, "browser-evidence.json");

const consoleErrors = [];
const consoleWarnings = [];
const pageErrors = [];
const failedRequests = [];
const responseErrors = [];

async function imageStats(page, buffer) {
  const dataUrl = `data:image/png;base64,${buffer.toString("base64")}`;
  return await page.evaluate(async (src) => {
    const img = new Image();
    img.src = src;
    await img.decode();
    const c = document.createElement("canvas");
    const max = 96;
    const scale = Math.min(1, max / Math.max(img.width, img.height));
    c.width = Math.max(1, Math.round(img.width * scale));
    c.height = Math.max(1, Math.round(img.height * scale));
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, c.width, c.height);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;

    const colors = new Set();
    let min = 255, maxv = 0, sum = 0, n = 0, dark = 0, light = 0;
    for (let i = 0; i < d.length; i += 4) {
      const r=d[i], g=d[i+1], b=d[i+2];
      const lum=(r+g+b)/3;
      min=Math.min(min,r,g,b); maxv=Math.max(maxv,r,g,b);
      sum += lum; n++;
      if (lum < 12) dark++;
      if (lum > 243) light++;
      colors.add(`${r>>4},${g>>4},${b>>4}`);
    }
    return {
      width: img.width,
      height: img.height,
      sampled_pixels: n,
      quantized_unique_colors: colors.size,
      channel_range: maxv-min,
      mean_luminance: n ? sum/n : 0,
      dark_fraction: n ? dark/n : 0,
      light_fraction: n ? light/n : 0
    };
  }, dataUrl);
}

let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });

  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
    if (msg.type() === "warning") consoleWarnings.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));
  page.on("requestfailed", (req) => {
    failedRequests.push({
      url: req.url(),
      method: req.method(),
      failure: req.failure()?.errorText || "unknown"
    });
  });
  page.on("response", (resp) => {
    if (resp.status() >= 400) {
      responseErrors.push({ url: resp.url(), status: resp.status() });
    }
  });

  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 20000 });
  try { await page.waitForLoadState("networkidle", { timeout: 8000 }); } catch {}
  await page.waitForTimeout(1500);

  const pageState = await page.evaluate(() => {
    function safeState(v) {
      try { return JSON.parse(JSON.stringify(v)); }
      catch { return { __unserializable__: true, type: typeof v }; }
    }
    const canvases = [...document.querySelectorAll("canvas")].map((c, i) => {
      const r = c.getBoundingClientRect();
      let webgl = false;
      try {
        webgl = !!(c.getContext("webgl2") || c.getContext("webgl"));
      } catch {}
      return {
        index: i,
        width: c.width,
        height: c.height,
        clientWidth: r.width,
        clientHeight: r.height,
        visible: r.width > 1 && r.height > 1,
        webgl
      };
    });
    return {
      title: document.title,
      body_text_length: (document.body?.innerText || "").trim().length,
      body_html_length: (document.body?.innerHTML || "").length,
      canvases,
      app_test_state_present: "__APP_TEST_STATE__" in window,
      app_test_state: "__APP_TEST_STATE__" in window ? safeState(window.__APP_TEST_STATE__) : null
    };
  });

  const pagePng = await page.screenshot({ path: screenshotPath, fullPage: true });
  const screenshotStats = await imageStats(page, pagePng);

  const canvasEvidence = [];
  const canvasHandles = await page.$$("canvas");
  for (let i = 0; i < canvasHandles.length; i++) {
    const h = canvasHandles[i];
    try {
      const box = await h.boundingBox();
      if (!box || box.width < 2 || box.height < 2) continue;
      const p = path.join(shotDir, `canvas-${i}.png`);
      const b = await h.screenshot({ path: p });
      canvasEvidence.push({
        index: i,
        screenshot: path.relative(process.cwd(), p),
        stats: await imageStats(page, b)
      });
    } catch (e) {
      canvasEvidence.push({ index: i, error: String(e) });
    }
  }

  const evidence = {
    generated_at: new Date().toISOString(),
    url,
    http_status: response?.status() ?? null,
    final_url: page.url(),
    page: pageState,
    console_errors: consoleErrors,
    console_warnings: consoleWarnings,
    page_errors: pageErrors,
    failed_requests: failedRequests,
    response_errors: responseErrors,
    screenshot: path.relative(process.cwd(), screenshotPath),
    screenshot_stats: screenshotStats,
    canvas_evidence: canvasEvidence
  };

  fs.writeFileSync(evidencePath, JSON.stringify(evidence, null, 2) + "\n");
  console.log(`BROWSER_EVIDENCE=${evidencePath}`);
  console.log(JSON.stringify({
    http_status: evidence.http_status,
    console_errors: evidence.console_errors.length,
    page_errors: evidence.page_errors.length,
    canvases: evidence.page.canvases.length,
    screenshot_unique_colors: evidence.screenshot_stats.quantized_unique_colors,
    app_test_state_present: evidence.page.app_test_state_present
  }, null, 2));
} catch (e) {
  fail(e?.stack || String(e));
} finally {
  if (browser) await browser.close();
}
