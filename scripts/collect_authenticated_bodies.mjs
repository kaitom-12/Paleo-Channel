#!/usr/bin/env node

/**
 * Capture D-Lab article-body HTML from an already authenticated Chrome
 * context.  The script deliberately fails closed: a login page, a redirect,
 * an empty body, or an identity mismatch is recorded as a failure and never
 * promoted to a full article export.
 *
 * The bundled workspace runtime provides Playwright.  Start Chrome with a
 * remote-debugging endpoint and keep the D-Lab account logged in before
 * running this collector, for example:
 *
 *   node scripts/collect_authenticated_bodies.mjs --cdp-url http://127.0.0.1:9222
 */

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import process from "node:process";
import { pathToFileURL } from "node:url";

let chromium;
for (const nodeModules of [
  process.env.PALEO_NODE_MODULES,
  path.resolve(process.cwd(), "node_modules"),
  path.resolve(path.dirname(process.execPath), "..", "node_modules"),
].filter(Boolean)) {
  try {
    const playwrightModule = await import(
      pathToFileURL(path.join(nodeModules, "playwright", "index.js")).href
    );
    chromium = playwrightModule.default?.chromium || playwrightModule.chromium;
    break;
  } catch {
    // Try the next workspace/runtime module location.
  }
}
if (!chromium) {
  throw new Error(
    "Playwrightが見つかりません。bundled Node runtimeか、PALEO_NODE_MODULESを指定してください",
  );
}

const DEFAULTS = {
  manifest: "export_state/inventory.csv",
  bodyCache: "export_state/authenticated_bodies.jsonl",
  assetCache: "export_state/assets.jsonl",
  failureCache: "export_state/collection_failures.jsonl",
  assetDir: "articles/assets",
  markdownRoot: "articles",
  cdpUrl: "http://127.0.0.1:9222",
  timeoutMs: 30_000,
  retries: 3,
  pauseMs: 200,
};

const BODY_SELECTORS = [
  '[data-testid="article-body"]',
  '[data-testid="blog-body"]',
  '[data-testid*="article-body"]',
  '[data-testid*="blog-body"]',
  ".article-body",
  ".blog-body",
  ".article-content",
  ".blog-content",
  ".post-content",
  ".content-body",
  ".editor-content",
  ".ql-editor",
  ".tiptap",
  ".jss226.jss260",
  ".jss226.jss261",
  ".jss226",
  "article",
  '[role="article"]',
];

function parseArgs(argv) {
  const options = { ...DEFAULTS, limit: null, refresh: false, selector: null };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--help" || token === "-h") {
      printHelp();
      process.exit(0);
    }
    if (token === "--refresh") {
      options.refresh = true;
      continue;
    }
    if (!token.startsWith("--")) {
      throw new Error(`unexpected argument: ${token}`);
    }
    const key = token.slice(2).replaceAll("-", "");
    const value = argv[index + 1];
    if (value == null || value.startsWith("--")) {
      throw new Error(`missing value for ${token}`);
    }
    index += 1;
    const keyMap = {
      manifest: "manifest",
      bodycache: "bodyCache",
      assetcache: "assetCache",
      failurecache: "failureCache",
      assetdir: "assetDir",
      markdownroot: "markdownRoot",
      cdpurl: "cdpUrl",
      timeout: "timeoutMs",
      retries: "retries",
      pause: "pauseMs",
      limit: "limit",
      selector: "selector",
    };
    const normalized = keyMap[key];
    if (!normalized) throw new Error(`unknown argument: ${token}`);
    if (["timeoutMs", "retries", "pauseMs", "limit"].includes(normalized)) {
      const number = Number(value);
      if (!Number.isInteger(number) || number < 0) {
        throw new Error(`${token} must be a non-negative integer`);
      }
      options[normalized] = number;
    } else {
      options[normalized] = value;
    }
  }
  return options;
}

function printHelp() {
  console.log(`Usage: node scripts/collect_authenticated_bodies.mjs [options]

Required external setup:
  --cdp-url URL       Existing logged-in Chrome CDP endpoint (default: ${DEFAULTS.cdpUrl})

Options:
  --manifest PATH     Article inventory CSV (default: ${DEFAULTS.manifest})
  --body-cache PATH   Authenticated HTML JSONL (default: ${DEFAULTS.bodyCache})
  --asset-cache PATH  Body-image JSONL (default: ${DEFAULTS.assetCache})
  --failure-cache PATH Failure JSONL (default: ${DEFAULTS.failureCache})
  --asset-dir PATH    Local body-image directory (default: ${DEFAULTS.assetDir})
  --markdown-root PATH Root used for relative Markdown paths (default: ${DEFAULTS.markdownRoot})
  --timeout MS        Page/request timeout (default: ${DEFAULTS.timeoutMs})
  --retries N         Attempts per article (default: ${DEFAULTS.retries})
  --pause MS          Delay between articles (default: ${DEFAULTS.pauseMs})
  --limit N           Capture at most N uncached articles
  --selector CSS      Override the body selector for a site-specific layout
  --refresh           Recapture already cached bodies
`);
}

function ensureParent(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function appendJsonl(filePath, record) {
  ensureParent(filePath);
  fs.appendFileSync(filePath, `${JSON.stringify(record)}\n`, "utf8");
}

function sha256(bufferOrText) {
  return crypto.createHash("sha256").update(bufferOrText).digest("hex");
}

function parseCsvLine(line) {
  const values = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quoted) {
      if (character === '"' && line[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        value += character;
      }
    } else if (character === '"' && value.length === 0) {
      quoted = true;
    } else if (character === ",") {
      values.push(value);
      value = "";
    } else {
      value += character;
    }
  }
  if (quoted) throw new Error("unterminated quoted CSV field");
  values.push(value);
  return values;
}

function readCsv(filePath) {
  const text = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "");
  const lines = text.split(/\r?\n/).filter((line) => line.length > 0);
  if (!lines.length) return [];
  const headers = parseCsvLine(lines[0]);
  return lines.slice(1).map((line) => {
    const values = parseCsvLine(line);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
}

function readLatestJsonl(filePath, keyFunction) {
  const latest = new Map();
  if (!fs.existsSync(filePath)) return latest;
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  for (const [index, line] of lines.entries()) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      const key = keyFunction(record);
      if (key) latest.set(key, record);
    } catch (error) {
      console.warn(`warning: invalid JSONL ${filePath}:${index + 1}: ${error.message}`);
    }
  }
  return latest;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bodyUrl(row) {
  return row.url || `https://daigovideolab.jp/blog/${row.object_id}`;
}

function isTargetRow(row) {
  return String(row.object_id || "").trim() && !(row.origin || "").trim();
}

async function inspectArticle(page, row, selectors) {
  const expectedId = String(row.object_id).trim();
  const expectedTitle = String(row.title || "").trim();
  return page.evaluate(
    ({ expectedId: id, expectedTitle: title, selectors: candidateSelectors }) => {
      const normalizeText = (value) => String(value || "").toLowerCase().replace(/\s+/g, "").trim();
      const pageUrl = window.location.href;
      const pageText = document.body?.innerText || "";
      const pageTitle = document.title || "";
      const urlMatch = new RegExp(`/blog/${id}(?:[/?#]|$)`).test(pageUrl);
      const loginLike = /(?:ログイン|login|会員登録|sign\s*in|sign\s*up)/i.test(pageText)
        && Boolean(document.querySelector('input[type="password"], form[action*="login" i]'));
      const titleKey = normalizeText(title).slice(0, 18);
      const titleMatch = !titleKey
        || normalizeText(pageTitle).includes(titleKey)
        || normalizeText(document.querySelector("h1")?.innerText).includes(titleKey)
        || normalizeText(pageText).includes(titleKey.slice(0, 10));

      const candidates = [];
      const seen = new Set();
      for (const selector of candidateSelectors) {
        for (const element of document.querySelectorAll(selector)) {
          if (seen.has(element)) continue;
          seen.add(element);
          if (element.closest("header, nav, footer, aside")) continue;
          const text = (element.innerText || "").trim();
          const html = element.innerHTML || "";
          if (!text && !html.trim()) continue;
          const paragraphs = element.querySelectorAll("p").length;
          const headings = element.querySelectorAll("h1,h2,h3,h4,h5,h6").length;
          const images = element.querySelectorAll("img").length;
          const embeds = element.querySelectorAll("iframe,input,video,audio").length;
          const controls = element.querySelectorAll("form,input[type=password]").length;
          const bodyClassMatch = selector === ".jss226"
            && [...element.classList].some((className) => /^jss26\d+$/.test(className));
          if (selector === ".jss226" && !bodyClassMatch) continue;
          const selectorWeight = selector.includes("body") ? 10000
            : selector === ".jss226" || selector.startsWith(".jss226.") ? 40000
            : selector === "article" ? 5000 : 0;
          const score = selectorWeight + Math.min(text.length, 30000)
            + paragraphs * 40 + headings * 100 + images * 30 + embeds * 50 - controls * 10000;
          candidates.push({ selector, html, textLength: text.length, childCount: element.children.length, score });
        }
      }
      candidates.sort((left, right) => right.score - left.score);
      const best = candidates[0] || null;
      return {
        currentUrl: pageUrl,
        pageTitle,
        urlMatch,
        titleMatch,
        loginLike,
        selector: best?.selector || "",
        html: best?.html || "",
        textLength: best?.textLength || 0,
        childCount: best?.childCount || 0,
        candidates: candidates.slice(0, 5).map(({ selector, textLength, childCount, score }) => ({
          selector, textLength, childCount, score,
        })),
      };
    },
    { expectedId, expectedTitle, selectors },
  );
}

function decodeHtmlEntities(value) {
  return String(value || "")
    .replaceAll("&amp;", "&")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">");
}

function extractImageUrls(bodyHtml, articleUrl, thumbnailUrl) {
  const thumbnail = decodeHtmlEntities(thumbnailUrl || "").trim();
  const urls = [];
  const seen = new Set();
  const imageTagPattern = /<img\b([^>]*)>/gi;
  let tagMatch;
  while ((tagMatch = imageTagPattern.exec(bodyHtml || "")) !== null) {
    const attributes = tagMatch[1];
    for (const attribute of ["src", "data-src", "srcset", "data-srcset"]) {
      const attributeMatch = new RegExp(`\\b${attribute}\\s*=\\s*([\\"'])(.*?)\\1`, "is").exec(attributes);
      if (!attributeMatch) continue;
      const values = attribute.includes("srcset")
        ? attributeMatch[2].split(",").map((value) => value.trim().split(/\s+/, 1)[0])
        : [attributeMatch[2]];
      for (const rawValue of values) {
        const candidate = decodeHtmlEntities(rawValue).trim();
        if (!candidate || candidate.startsWith("data:")) continue;
        try {
          const resolved = new URL(candidate, articleUrl).href;
          if (!/^https?:$/i.test(new URL(resolved).protocol)) continue;
          if (resolved === thumbnail || seen.has(resolved)) continue;
          seen.add(resolved);
          urls.push(resolved);
        } catch {
          // Invalid asset URL; leave it visible in the raw HTML but do not fetch it.
        }
      }
    }
  }
  return urls;
}

function fileExtension(url, contentType) {
  let extension = path.extname(new URL(url).pathname).toLowerCase();
  if (!/^[.]([a-z0-9]{1,8})$/i.test(extension)) {
    const match = String(contentType || "").toLowerCase().match(/image\/([a-z0-9.+-]+)/);
    extension = match ? `.${match[1].replace("svg+xml", "svg")}` : ".bin";
  }
  return extension.replace(/[^a-z0-9.]/gi, "").slice(0, 9) || ".bin";
}

async function captureAsset({ context, objectId, sourceUrl, index, thumbnailUrl, options, assetCache }) {
  const key = `${objectId}\u0000${sourceUrl}`;
  const cached = assetCache.get(key);
  if (cached?.status === "ok" && cached.local_file) {
    const absolute = path.resolve(options.markdownRoot, cached.local_file);
    if (fs.existsSync(absolute)) return cached;
  }
  if (sourceUrl === String(thumbnailUrl || "").trim()) return null;

  try {
    const response = await context.request.get(sourceUrl, { timeout: options.timeoutMs });
    if (!response.ok()) {
      throw new Error(`HTTP ${response.status()}`);
    }
    const contentType = response.headers()["content-type"] || "";
    if (contentType && !/^image\//i.test(contentType) && !/application\/octet-stream/i.test(contentType)) {
      throw new Error(`unexpected asset content type: ${contentType}`);
    }
    const body = await response.body();
    const extension = fileExtension(sourceUrl, contentType);
    const localName = `${String(index).padStart(3, "0")}${extension}`;
    const absolutePath = path.resolve(options.assetDir, objectId, localName);
    fs.mkdirSync(path.dirname(absolutePath), { recursive: true });
    fs.writeFileSync(absolutePath, body);
    const localFile = path.relative(path.resolve(options.markdownRoot), absolutePath).replaceAll(path.sep, "/");
    const record = {
      object_id: objectId,
      source_url: sourceUrl,
      status: "ok",
      local_file: localFile,
      sha256: sha256(body),
      bytes: body.length,
      content_type: contentType,
      captured_at: new Date().toISOString(),
    };
    appendJsonl(options.assetCache, record);
    assetCache.set(key, record);
    return record;
  } catch (error) {
    const record = {
      object_id: objectId,
      source_url: sourceUrl,
      status: "failed",
      error: String(error.message || error),
      captured_at: new Date().toISOString(),
    };
    appendJsonl(options.assetCache, record);
    assetCache.set(key, record);
    return record;
  }
}

async function captureAssets({ context, row, html, options, assetCache }) {
  const urls = extractImageUrls(html, bodyUrl(row), row.thumbnail_url);
  const replacements = {};
  const records = [];
  for (const [index, sourceUrl] of urls.entries()) {
    const record = await captureAsset({
      context,
      objectId: String(row.object_id),
      sourceUrl,
      index,
      thumbnailUrl: row.thumbnail_url,
      options,
      assetCache,
    });
    if (record) {
      records.push(record);
      if (record.status === "ok") replacements[sourceUrl] = record.local_file;
    }
  }
  return {
    urls,
    replacements,
    records,
    status: records.length === urls.length && records.every((record) => record.status === "ok")
      ? "complete"
      : urls.length === 0 ? "none" : "partial",
  };
}

async function captureBody({ page, row, options }) {
  const targetUrl = bodyUrl(row);
  let lastError = null;
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    try {
      await page.goto(targetUrl, { waitUntil: "domcontentloaded", timeout: options.timeoutMs });
      await page.waitForTimeout(700);
      const selectors = options.selector ? [options.selector] : BODY_SELECTORS;
      await page.waitForFunction(
        (candidateSelectors) => candidateSelectors.some((selector) => document.querySelector(selector)),
        selectors,
        { timeout: Math.min(options.timeoutMs, 10_000) },
      );
      const result = await inspectArticle(page, row, selectors);
      if (!result.urlMatch) throw new Error(`URL mismatch: ${result.currentUrl}`);
      if (result.loginLike) throw new Error("login page detected");
      if (!result.titleMatch) throw new Error(`title mismatch: ${result.pageTitle}`);
      if (!result.html.trim()) throw new Error("empty article body");
      if (result.textLength === 0 && !/<(?:img|iframe|table|input|video|audio)\b/i.test(result.html)) {
        throw new Error("article body has no text or content element");
      }
      if (result.textLength < 30 && !/<(?:img|iframe|table|input|video|audio)\b/i.test(result.html)) {
        throw new Error(`article body is suspiciously short: ${result.textLength} chars`);
      }
      return {
        status: "ok",
        kind: "exact",
        object_id: String(row.object_id),
        page_id: String(row.object_id),
        url: targetUrl,
        selector: result.selector,
        title_match: result.titleMatch,
        html: result.html,
        html_sha256: sha256(result.html),
        text_length: result.textLength,
        html_length: result.html.length,
        child_count: result.childCount,
        captured_at: new Date().toISOString(),
      };
    } catch (error) {
      lastError = error;
      if (attempt < options.retries) await sleep(500 * attempt);
    }
  }
  const message = String(lastError?.message || lastError || "unknown capture error");
  const failure = {
    object_id: String(row.object_id),
    url: targetUrl,
    status: "failed",
    reason: message,
    attempts: options.retries,
    captured_at: new Date().toISOString(),
  };
  appendJsonl(options.failureCache, failure);
  return failure;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  let manifestPath = options.manifest;
  if (!fs.existsSync(manifestPath) && manifestPath === DEFAULTS.manifest && fs.existsSync("export_state/manifest.csv")) {
    manifestPath = "export_state/manifest.csv";
    console.warn(`warning: ${DEFAULTS.manifest} not found; using legacy ${manifestPath}`);
  }
  const rows = readCsv(manifestPath).filter(isTargetRow);
  const ids = rows.map((row) => String(row.object_id).trim());
  if (new Set(ids).size !== ids.length) throw new Error("manifest contains duplicate target object_id values");

  const bodyCache = readLatestJsonl(options.bodyCache, (record) => {
    if (record.status !== "ok" || !record.object_id || typeof record.html !== "string" || !record.html.trim()) return null;
    return String(record.object_id);
  });
  const assetCache = readLatestJsonl(options.assetCache, (record) => {
    if (!record.object_id || !record.source_url) return null;
    return `${record.object_id}\u0000${record.source_url}`;
  });

  let browser;
  try {
    browser = await chromium.connectOverCDP(options.cdpUrl);
  } catch (error) {
    throw new Error(
      `Chrome CDP接続に失敗: ${error.message}. `
      + `ログイン済みChromeをremote-debugging endpoint ${options.cdpUrl}で起動してから再実行してください`,
    );
  }
  const context = browser.contexts()[0];
  if (!context) throw new Error("Chrome CDPにブラウザコンテキストがありません");
  const page = await context.newPage();
  let processed = 0;
  let full = 0;
  let skipped = 0;
  let failed = 0;
  let assetFailed = 0;
  let captureAttempts = 0;

  try {
    for (const row of rows) {
      const objectId = String(row.object_id).trim();
      let bodyRecord = bodyCache.get(objectId);
      if (
        bodyRecord
        && ((bodyRecord.url && bodyRecord.url !== bodyUrl(row))
          || (bodyRecord.page_id && String(bodyRecord.page_id) !== objectId))
      ) {
        bodyRecord = null;
      }
      const needsBodyCapture = !bodyRecord || options.refresh;
      if (needsBodyCapture && options.limit != null && captureAttempts >= options.limit) break;
      if (bodyRecord && !options.refresh) {
        skipped += 1;
      } else {
        captureAttempts += 1;
        bodyRecord = await captureBody({ page, row, options });
        if (bodyRecord.status === "ok") {
          appendJsonl(options.bodyCache, bodyRecord);
          bodyCache.set(objectId, bodyRecord);
        } else {
          failed += 1;
          processed += 1;
          continue;
        }
      }

      const assetResult = await captureAssets({
        context,
        row,
        html: bodyRecord.html,
        options,
        assetCache,
      });
      if (assetResult.status === "partial") assetFailed += 1;
      else full += 1;
      processed += 1;
      if (processed % 50 === 0) {
        console.log(`processed=${processed} full=${full} skipped=${skipped} failed=${failed} asset_failed=${assetFailed}`);
      }
      if (options.pauseMs) await sleep(options.pauseMs);
    }
  } finally {
    await page.close();
    // For a CDP connection Playwright disconnects; it does not launch or
    // terminate the user's Chrome process.
    await browser.close();
  }
  console.log(JSON.stringify({
    records: rows.length,
    processed,
    full,
    skipped,
    failed,
    asset_failed: assetFailed,
    body_cache: options.bodyCache,
    asset_cache: options.assetCache,
    failure_cache: options.failureCache,
  }, null, 2));
}

main().catch((error) => {
  console.error(`collector error: ${error.message}`);
  process.exitCode = 1;
});
