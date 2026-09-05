#!/usr/bin/env node

/** Download images referenced by verified authenticated article-body HTML. */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const DEFAULTS = {
  manifest: "export_state/inventory.csv",
  bodyCache: "export_state/authenticated_bodies.jsonl",
  assetCache: "export_state/assets.jsonl",
  assetDir: "articles/assets",
  markdownRoot: "articles",
  retries: 3,
  pauseMs: 100,
  timeoutMs: 20_000,
  limit: null,
};

function parseArgs(argv) {
  const options = { ...DEFAULTS };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--help" || token === "-h") {
      console.log(`Usage: node scripts/download_article_assets.mjs [options]

Options:
  --manifest PATH       Frozen inventory CSV (default: ${DEFAULTS.manifest})
  --body-cache PATH     Authenticated HTML JSONL (default: ${DEFAULTS.bodyCache})
  --asset-cache PATH    Asset JSONL (default: ${DEFAULTS.assetCache})
  --asset-dir PATH      Local asset directory (default: ${DEFAULTS.assetDir})
  --markdown-root PATH  Root used for relative Markdown paths (default: ${DEFAULTS.markdownRoot})
  --retries N           Attempts per image (default: ${DEFAULTS.retries})
  --pause MS            Delay between images (default: ${DEFAULTS.pauseMs})
  --timeout MS          Timeout per image request (default: ${DEFAULTS.timeoutMs})
  --limit N             Capture at most N uncached images
`);
      process.exit(0);
    }
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const value = argv[index + 1];
    if (value == null || value.startsWith("--")) throw new Error(`missing value for ${token}`);
    index += 1;
    const key = token.slice(2).replaceAll("-", "");
    const keyMap = {
      manifest: "manifest",
      bodycache: "bodyCache",
      assetcache: "assetCache",
      assetdir: "assetDir",
      markdownroot: "markdownRoot",
      retries: "retries",
      pause: "pauseMs",
      timeout: "timeoutMs",
      limit: "limit",
    };
    const normalized = keyMap[key];
    if (!normalized) throw new Error(`unknown argument: ${token}`);
    if (["retries", "pauseMs", "timeoutMs", "limit"].includes(normalized)) {
      const number = Number(value);
      if (!Number.isInteger(number) || number < 0) throw new Error(`${token} must be a non-negative integer`);
      options[normalized] = number;
    } else {
      options[normalized] = value;
    }
  }
  return options;
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
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      const key = keyFunction(record);
      if (!key) continue;
      const previous = latest.get(key);
      if (previous?.status === "ok" && record.status !== "ok") continue;
      latest.set(key, record);
    } catch {
      // The verifier reports malformed historical records.
    }
  }
  return latest;
}

function ensureParent(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function appendJsonl(filePath, record) {
  ensureParent(filePath);
  fs.appendFileSync(filePath, `${JSON.stringify(record)}\n`, "utf8");
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function decodeHtmlEntities(value) {
  return String(value || "")
    .replaceAll("&amp;", "&")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">");
}

function assetAliasUrl(value) {
  const original = decodeHtmlEntities(value).trim();
  if (!original) return "";
  try {
    const parsed = new URL(original);
    if (parsed.hostname !== "firebasestorage.googleapis.com") return original;
    parsed.searchParams.delete("token");
    return parsed.href;
  } catch {
    return original;
  }
}

function assetKey(objectId, sourceUrl) {
  return `${objectId}\u0000${assetAliasUrl(sourceUrl)}`;
}

function assetCandidateUrls(sourceUrl) {
  const candidates = [sourceUrl];
  try {
    const parsed = new URL(sourceUrl);
    const match = parsed.pathname.match(/^\/v0\/b\/([^/]+)\/o\/(.+)$/);
    if (parsed.hostname !== "firebasestorage.googleapis.com" || !match) {
      return candidates;
    }
    const [, bucket, encodedObjectPath] = match;
    const noToken = new URL(parsed.href);
    noToken.searchParams.delete("token");
    candidates.push(noToken.href);
    candidates.push(
      `https://storage.googleapis.com/download/storage/v1/b/${bucket}/o/${encodedObjectPath}?alt=media`,
    );
    candidates.push(
      `https://storage.googleapis.com/${bucket}/${decodeURIComponent(encodedObjectPath)}`,
    );
  } catch {
    // Keep the original URL as the only candidate when it cannot be parsed.
  }
  return [...new Set(candidates)];
}

function extractImageUrls(bodyHtml, articleUrl, thumbnailUrl) {
  const thumbnail = decodeHtmlEntities(thumbnailUrl).trim();
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
          // Keep invalid URLs in raw HTML; do not fetch them.
        }
      }
    }
  }
  return urls;
}

function extensionFor(url, contentType) {
  let extension = path.extname(new URL(url).pathname).toLowerCase();
  if (!/^[.]([a-z0-9]{1,8})$/i.test(extension)) {
    const match = String(contentType || "").toLowerCase().match(/image\/([a-z0-9.+-]+)/);
    extension = match ? `.${match[1].replace("svg+xml", "svg")}` : ".bin";
  }
  return extension.replace(/[^a-z0-9.]/gi, "").slice(0, 9) || ".bin";
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function fetchAsset(url, objectId, index, options, assetCache) {
  const key = assetKey(objectId, url);
  const cached = assetCache.get(key);
  if (cached?.status === "ok" && cached.local_file) {
    const absolute = path.resolve(options.markdownRoot, cached.local_file);
    if (fs.existsSync(absolute)) return { record: cached, reused: true };
  }
  let lastError = "";
  for (let attempt = 1; attempt <= options.retries; attempt += 1) {
    const candidateErrors = [];
    for (const candidateUrl of assetCandidateUrls(url)) {
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), options.timeoutMs);
        let response;
        try {
          response = await fetch(candidateUrl, { redirect: "follow", signal: controller.signal });
        } finally {
          clearTimeout(timeout);
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const contentType = response.headers.get("content-type") || "";
        if (!/^image\//i.test(contentType) && !/application\/octet-stream/i.test(contentType)) {
          throw new Error(`unexpected asset content type: ${contentType || "missing"}`);
        }
        const body = Buffer.from(await response.arrayBuffer());
        const extension = extensionFor(url, contentType);
        const localName = `${String(index).padStart(3, "0")}${extension}`;
        const absolute = path.resolve(options.assetDir, objectId, localName);
        fs.mkdirSync(path.dirname(absolute), { recursive: true });
        fs.writeFileSync(absolute, body);
        const localFile = path.relative(path.resolve(options.markdownRoot), absolute).replaceAll(path.sep, "/");
        const record = {
          object_id: objectId,
          source_url: url,
          download_url: candidateUrl === url ? undefined : candidateUrl,
          status: "ok",
          local_file: localFile,
          sha256: sha256(body),
          bytes: body.length,
          content_type: contentType,
          captured_at: new Date().toISOString(),
        };
        appendJsonl(options.assetCache, record);
        assetCache.set(key, record);
        return { record, reused: false };
      } catch (error) {
        candidateErrors.push(`${candidateUrl}: ${String(error.message || error)}`);
      }
    }
    lastError = candidateErrors.join("; ") || "unknown asset error";
    if (attempt < options.retries) await sleep(500 * attempt);
  }
  const record = {
    object_id: objectId,
    source_url: url,
    status: "failed",
    error: lastError || "unknown asset error",
    captured_at: new Date().toISOString(),
  };
  appendJsonl(options.assetCache, record);
  assetCache.set(key, record);
  return { record, reused: false };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const rows = readCsv(options.manifest).filter((row) => String(row.origin || "").trim() === "");
  const bodyCache = readLatestJsonl(options.bodyCache, (record) => (
    record.status === "ok" && record.object_id && typeof record.html === "string" && record.html.trim()
      ? String(record.object_id)
      : null
  ));
  const assetCache = readLatestJsonl(options.assetCache, (record) => (
    record.object_id && record.source_url ? assetKey(record.object_id, record.source_url) : null
  ));
  let imagesFound = 0;
  let downloaded = 0;
  let reused = 0;
  let failed = 0;
  let processedArticles = 0;
  let uncachedAttempts = 0;
  for (const row of rows) {
    const objectId = String(row.object_id || "").trim();
    const bodyRecord = bodyCache.get(objectId);
    if (!bodyRecord) continue;
    const urls = extractImageUrls(
      bodyRecord.html,
      String(row.url || `https://daigovideolab.jp/blog/${objectId}`),
      row.thumbnail_url,
    );
    imagesFound += urls.length;
    for (const [index, url] of urls.entries()) {
      const cached = assetCache.get(assetKey(objectId, url));
      const localExists = cached?.status === "ok" && cached.local_file
        && fs.existsSync(path.resolve(options.markdownRoot, cached.local_file));
      if (localExists) {
        reused += 1;
        continue;
      }
      if (options.limit != null && uncachedAttempts >= options.limit) {
        console.log(JSON.stringify({ rows: rows.length, processedArticles, imagesFound, downloaded, reused, failed, stopped: true }, null, 2));
        return;
      }
      uncachedAttempts += 1;
      const result = await fetchAsset(url, objectId, index, options, assetCache);
      if (result.reused) reused += 1;
      else if (result.record.status === "ok") downloaded += 1;
      else failed += 1;
      if (options.pauseMs) await sleep(options.pauseMs);
    }
    processedArticles += 1;
    if (processedArticles % 100 === 0) {
      console.log(`processed_articles=${processedArticles} images_found=${imagesFound} downloaded=${downloaded} reused=${reused} failed=${failed}`);
    }
  }
  console.log(JSON.stringify({ rows: rows.length, processedArticles, imagesFound, downloaded, reused, failed, stopped: false }, null, 2));
}

main().catch((error) => {
  console.error(`asset downloader error: ${error.message}`);
  process.exitCode = 1;
});
