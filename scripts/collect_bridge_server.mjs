#!/usr/bin/env node

/**
 * Local-only receiver for authenticated browser captures.
 *
 * The browser-use session can read the already-authenticated D-Lab page, but
 * it cannot write directly to the workspace filesystem. This small server is
 * bound to loopback only and accepts verified article-body records from that
 * browser session. It never forwards data outside the local machine.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import process from "node:process";

const DEFAULTS = {
  host: "127.0.0.1",
  port: 8765,
  manifest: "export_state/inventory.csv",
  bodyCache: "export_state/authenticated_bodies.jsonl",
  failureCache: "export_state/collection_failures.jsonl",
  assetCache: "export_state/assets.jsonl",
  assetDir: "articles/assets",
  maxBodyBytes: 20 * 1024 * 1024,
};

function parseArgs(argv) {
  const options = { ...DEFAULTS };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--help" || token === "-h") {
      console.log(`Usage: node scripts/collect_bridge_server.mjs [options]

Options:
  --host HOST             Loopback host (default: ${DEFAULTS.host})
  --port PORT             Local port (default: ${DEFAULTS.port})
  --manifest PATH         Frozen inventory CSV (default: ${DEFAULTS.manifest})
  --body-cache PATH       Authenticated HTML JSONL (default: ${DEFAULTS.bodyCache})
  --failure-cache PATH    Failure JSONL (default: ${DEFAULTS.failureCache})
  --asset-cache PATH      Asset JSONL (default: ${DEFAULTS.assetCache})
  --asset-dir PATH        Local asset directory (default: ${DEFAULTS.assetDir})
`);
      process.exit(0);
    }
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const value = argv[index + 1];
    if (value == null || value.startsWith("--")) throw new Error(`missing value for ${token}`);
    index += 1;
    const key = token.slice(2).replaceAll("-", "");
    const keyMap = {
      host: "host",
      port: "port",
      manifest: "manifest",
      bodycache: "bodyCache",
      failurecache: "failureCache",
      assetcache: "assetCache",
      assetdir: "assetDir",
    };
    const normalized = keyMap[key];
    if (!normalized) throw new Error(`unknown argument: ${token}`);
    if (normalized === "port") {
      const port = Number(value);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        throw new Error(`${token} must be a valid TCP port`);
      }
      options.port = port;
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

function readLatest(pathValue, keyFunction) {
  const latest = new Map();
  if (!fs.existsSync(pathValue)) return latest;
  const lines = fs.readFileSync(pathValue, "utf8").split(/\r?\n/);
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      const key = keyFunction(record);
      if (key) latest.set(key, record);
    } catch {
      // Invalid historical lines are reported by the normal verifier.
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

function sha256Text(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function articleUrl(objectId) {
  return `https://daigovideolab.jp/blog/${objectId}`;
}

function sendJson(response, statusCode, value) {
  const body = JSON.stringify(value);
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

async function readRequestBody(request, maxBytes) {
  const chunks = [];
  let total = 0;
  for await (const chunk of request) {
    total += chunk.length;
    if (total > maxBytes) throw new Error(`request body exceeds ${maxBytes} bytes`);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function fail(message) {
  const error = new Error(message);
  error.statusCode = 400;
  throw error;
}

function resolveFailure(objectId, failures, failureCache) {
  const previous = failures.get(objectId);
  if (previous?.status !== "failed") return;
  const resolved = {
    object_id: objectId,
    status: "resolved",
    reason: "",
    resolved_at: new Date().toISOString(),
  };
  appendJsonl(failureCache, resolved);
  failures.set(objectId, resolved);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.host !== "127.0.0.1" && options.host !== "localhost") {
    throw new Error("bridge server must be bound to loopback");
  }
  const rows = readCsv(options.manifest).filter((row) => String(row.origin || "").trim() === "");
  const byId = new Map();
  for (const row of rows) {
    const objectId = String(row.object_id || "").trim();
    if (!objectId || byId.has(objectId)) throw new Error(`invalid or duplicate object_id: ${objectId}`);
    byId.set(objectId, row);
  }
  const bodies = readLatest(options.bodyCache, (record) => (
    record.status === "ok" && record.object_id && typeof record.html === "string" && record.html.trim()
      ? String(record.object_id)
      : null
  ));
  const failures = readLatest(options.failureCache, (record) => (
    record.object_id ? String(record.object_id) : null
  ));

  const server = http.createServer(async (request, response) => {
    try {
      const requestUrl = new URL(request.url || "/", `http://${options.host}:${options.port}`);
      if (request.method === "OPTIONS") {
        response.writeHead(204, {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET, POST, OPTIONS",
          "access-control-allow-headers": "content-type",
        });
        response.end();
        return;
      }
      if (request.method === "GET" && requestUrl.pathname === "/health") {
        sendJson(response, 200, { ok: true, records: rows.length, bodies: bodies.size, failures: failures.size });
        return;
      }
      if (request.method === "GET" && requestUrl.pathname === "/manifest") {
        sendJson(response, 200, {
          records: rows.map((row) => ({
            object_id: String(row.object_id).trim(),
            title: String(row.title || "").trim(),
            date: String(row.date || "").trim(),
            url: String(row.url || articleUrl(row.object_id)).trim(),
            thumbnail_url: String(row.thumbnail_url || "").trim(),
          })),
        });
        return;
      }
      if (request.method === "GET" && requestUrl.pathname === "/status") {
        sendJson(response, 200, {
          bodies: [...bodies.keys()],
          failures: [...failures.entries()].map(([object_id, record]) => ({ object_id, reason: record.reason || "" })),
        });
        return;
      }
      if (request.method === "POST" && requestUrl.pathname === "/asset") {
        const parsed = JSON.parse(await readRequestBody(request, options.maxBodyBytes));
        const objectId = String(parsed.object_id || "").trim();
        if (!byId.has(objectId)) fail(`object_id is not in frozen inventory: ${objectId}`);
        const sourceUrl = String(parsed.source_url || "").trim();
        if (!/^https?:\/\//i.test(sourceUrl)) fail(`invalid asset URL: ${objectId}`);
        const base64 = String(parsed.bytes_base64 || "");
        if (!base64) fail(`empty asset body: ${objectId}`);
        const body = Buffer.from(base64, "base64");
        if (!body.length) fail(`invalid base64 asset body: ${objectId}`);
        const contentType = String(parsed.content_type || "application/octet-stream");
        const extensionFromType = contentType.match(/^image\/([a-z0-9.+-]+)/i)?.[1]
          ?.replace("svg+xml", "svg") || "bin";
        const requestedExtension = String(parsed.extension || extensionFromType)
          .replace(/[^a-z0-9]/gi, "").slice(0, 8) || "bin";
        const index = Number.isInteger(parsed.index) && parsed.index >= 0 ? parsed.index : 0;
        const absolute = path.resolve(options.assetDir, objectId, `${String(index).padStart(3, "0")}.${requestedExtension}`);
        fs.mkdirSync(path.dirname(absolute), { recursive: true });
        const digest = sha256Bytes(body);
        if (fs.existsSync(absolute)) {
          const existing = fs.readFileSync(absolute);
          if (!existing.equals(body)) fail(`asset path already contains different bytes: ${objectId} ${index}`);
        } else {
          fs.writeFileSync(absolute, body);
        }
        const localFile = path.relative(path.resolve(options.assetDir, ".."), absolute).replaceAll(path.sep, "/");
        const record = {
          object_id: objectId,
          source_url: sourceUrl,
          status: "ok",
          local_file: localFile,
          sha256: digest,
          bytes: body.length,
          content_type: contentType,
          source: "authenticated_browser",
          captured_at: parsed.captured_at || new Date().toISOString(),
        };
        appendJsonl(options.assetCache, record);
        sendJson(response, 201, { ok: true, status: "saved", object_id: objectId, source_url: sourceUrl, local_file: localFile, sha256: digest });
        return;
      }
      if (request.method !== "POST" || !["/capture", "/failure"].includes(requestUrl.pathname)) {
        sendJson(response, 404, { error: "not found" });
        return;
      }
      const parsed = JSON.parse(await readRequestBody(request, options.maxBodyBytes));
      const objectId = String(parsed.object_id || "").trim();
      const row = byId.get(objectId);
      if (!row) fail(`object_id is not in frozen inventory: ${objectId}`);

      if (requestUrl.pathname === "/capture") {
        const html = typeof parsed.html === "string" ? parsed.html : "";
        if (!html.trim()) fail(`empty HTML: ${objectId}`);
        if (String(parsed.page_id || objectId) !== objectId) fail(`page_id mismatch: ${objectId}`);
        const expectedUrl = String(row.url || articleUrl(objectId)).trim();
        if (String(parsed.url || expectedUrl).trim() !== expectedUrl) fail(`URL mismatch: ${objectId}`);
        const actualHash = sha256Text(html);
        if (parsed.html_sha256 && String(parsed.html_sha256) !== actualHash) fail(`HTML hash mismatch: ${objectId}`);
        const record = {
          ...parsed,
          object_id: objectId,
          page_id: objectId,
          url: expectedUrl,
          status: "ok",
          kind: "exact",
          html_sha256: actualHash,
          captured_at: parsed.captured_at || new Date().toISOString(),
        };
        const previous = bodies.get(objectId);
        if (previous?.html_sha256 === actualHash || (previous?.html && sha256Text(previous.html) === actualHash)) {
          resolveFailure(objectId, failures, options.failureCache);
          sendJson(response, 200, { ok: true, status: "already_cached", object_id: objectId, html_sha256: actualHash });
          return;
        }
        appendJsonl(options.bodyCache, record);
        bodies.set(objectId, record);
        resolveFailure(objectId, failures, options.failureCache);
        sendJson(response, 201, { ok: true, status: "saved", object_id: objectId, html_sha256: actualHash });
        return;
      }

      const record = {
        object_id: objectId,
        url: String(parsed.url || row.url || articleUrl(objectId)).trim(),
        status: "failed",
        reason: String(parsed.reason || "unknown capture error"),
        attempts: Number.isInteger(parsed.attempts) ? parsed.attempts : 1,
        captured_at: parsed.captured_at || new Date().toISOString(),
      };
      appendJsonl(options.failureCache, record);
      failures.set(objectId, record);
      sendJson(response, 201, { ok: true, status: "saved", object_id: objectId });
    } catch (error) {
      const statusCode = Number.isInteger(error.statusCode) ? error.statusCode : 500;
      sendJson(response, statusCode, { ok: false, error: String(error.message || error) });
    }
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port, options.host, () => {
      console.log(`bridge listening on http://${options.host}:${options.port}`);
      console.log(`inventory=${rows.length} cached_bodies=${bodies.size} recorded_failures=${failures.size}`);
      resolve();
    });
  });
}

main().catch((error) => {
  console.error(`bridge error: ${error.message}`);
  process.exitCode = 1;
});
