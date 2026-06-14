#!/usr/bin/env node
/**
 * Carbon Lint (JS) — static analysis for high-emission JavaScript patterns.
 *
 * A line-by-line pattern scanner that flags common anti-patterns with
 * estimated CO₂ costs. No build step or AST parser required.
 *
 * Usage:
 *   node carbon_lint.js src/
 *   node carbon_lint.js app.js --format json
 *   node carbon_lint.js . --format github
 */

const fs = require("fs");
const path = require("path");

const LARGE_MODELS = new Set([
  "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4-32k",
  "claude-opus-4-8", "claude-opus-4-7", "claude-3-opus-20240229",
  "gemini-ultra", "gemini-1.5-pro", "gemini-2.0-pro",
  "llama-3.1-405b", "mistral-large", "mistral-large-2",
  "command-r-plus",
]);

const RULES = {
  "POLL-001": {
    message: "setInterval with short interval — consider event-driven or WebSocket instead.",
    co2: "~100–500 g CO₂/hour extra vs. push-based alternatives",
    severity: "warning",
    hint: "Replace polling with a WebSocket, Server-Sent Event, or a webhook. If polling is unavoidable, use exponential backoff.",
  },
  "FETCH-001": {
    message: "fetch() inside a loop — N requests where batching might do.",
    co2: "Each redundant round-trip burns compute on client and server",
    severity: "warning",
    hint: "Batch IDs into a single request, or move the fetch outside the loop and cache the result.",
  },
  "PROMISE-001": {
    message: "Promise.all() with unbounded array — can spike concurrency and server load.",
    co2: "Concurrent inflight requests multiply server-side compute proportionally",
    severity: "info",
    hint: "Use a concurrency-limited helper (p-limit, bottleneck) to cap simultaneous requests.",
  },
  "MODEL-001": {
    message: "Large LLM selected. Verify task complexity justifies the model size.",
    co2: "GPT-4 / Opus-class models use ~10–50× more energy per token than smaller alternatives",
    severity: "warning",
    hint: "For classification, extraction, or short Q&A, try gpt-4o-mini, claude-haiku, or mistral-small first.",
  },
  "MODEL-002": {
    message: "LLM call without stream: true — full response buffered server-side.",
    co2: "Same token count, but non-streaming holds server KV-cache longer",
    severity: "info",
    hint: "Add stream: true and consume the async iterator to reduce server memory pressure.",
  },
  "CACHE-001": {
    message: "No Cache-Control header on GET request — response may be re-fetched on every call.",
    co2: "Redundant fetches repeat network transfer and origin compute unnecessarily",
    severity: "info",
    hint: "Add Cache-Control: max-age=N or use a service worker / edge cache.",
  },
};

/**
 * Scan a single file's lines for carbon anti-patterns.
 * Returns an array of finding objects.
 */
function lintFile(filePath) {
  let source;
  try {
    source = fs.readFileSync(filePath, "utf8");
  } catch {
    return [];
  }

  const lines = source.split("\n");
  const findings = [];
  let inLoop = false; // rough heuristic: track for/forEach context

  lines.forEach((line, idx) => {
    const lineNo = idx + 1;
    const trimmed = line.trim();

    // Skip comments and empty lines
    if (trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed === "") return;

    // Track loop context (very simplified)
    if (/\b(for\s*\(|\.forEach\s*\(|\.map\s*\(|\.reduce\s*\()/.test(line)) {
      inLoop = true;
    }
    // Reset loop context on closing brace at low indent
    if (/^}/.test(trimmed)) inLoop = false;

    // POLL-001: setInterval with interval < 5000ms
    const intervalMatch = line.match(/setInterval\s*\([^,]+,\s*(\d+)\s*\)/);
    if (intervalMatch) {
      const ms = parseInt(intervalMatch[1], 10);
      if (ms < 5000) {
        findings.push(flag("POLL-001", filePath, lineNo, line.indexOf("setInterval")));
      }
    }

    // FETCH-001: fetch() inside a loop
    if (inLoop && /\bfetch\s*\(/.test(line)) {
      findings.push(flag("FETCH-001", filePath, lineNo, line.indexOf("fetch")));
    }

    // PROMISE-001: Promise.all() without obvious size constraint
    if (/Promise\.all\s*\(/.test(line) && !/\.slice\s*\(/.test(line)) {
      findings.push(flag("PROMISE-001", filePath, lineNo, line.indexOf("Promise.all")));
    }

    // MODEL-001 + MODEL-002: LLM API calls
    const modelMatch = line.match(/model\s*:\s*["']([^"']+)["']/);
    if (modelMatch && LARGE_MODELS.has(modelMatch[1].toLowerCase())) {
      findings.push(flag("MODEL-001", filePath, lineNo, line.indexOf("model")));
    }

    // Detect .create / .generate / .complete calls without stream: true nearby
    if (/\.(create|generate|complete)\s*\(/.test(line)) {
      // Look ahead up to 10 lines for stream: true
      const block = lines.slice(idx, idx + 10).join("\n");
      if (!(/stream\s*:\s*true/.test(block))) {
        findings.push(flag("MODEL-002", filePath, lineNo, line.search(/\.(create|generate|complete)/)));
      }
    }

    // CACHE-001: fetch GET without cache headers (simple heuristic)
    if (/fetch\s*\(/.test(line) && !/cache.*:/.test(line) && !inLoop) {
      // Check next few lines for cache options
      const block = lines.slice(idx, idx + 5).join("\n");
      if (!/cache\s*:/.test(block) && !/Cache-Control/.test(block)) {
        findings.push(flag("CACHE-001", filePath, lineNo, line.indexOf("fetch")));
      }
    }
  });

  return findings;
}

function flag(ruleId, filePath, line, col) {
  const rule = RULES[ruleId];
  return {
    rule: ruleId,
    severity: rule.severity,
    message: rule.message,
    co2: rule.co2,
    hint: rule.hint,
    file: filePath,
    line,
    col: Math.max(col, 0),
  };
}

function collectFiles(target) {
  const stat = fs.statSync(target);
  if (stat.isDirectory()) {
    return fs.readdirSync(target, { withFileTypes: true }).flatMap((entry) => {
      const full = path.join(target, entry.name);
      if (entry.isDirectory() && !entry.name.startsWith(".") && entry.name !== "node_modules") {
        return collectFiles(full);
      }
      if (entry.isFile() && (full.endsWith(".js") || full.endsWith(".ts") || full.endsWith(".mjs"))) {
        return [full];
      }
      return [];
    });
  }
  return [target];
}

function formatText(findings) {
  if (!findings.length) return "No carbon anti-patterns found.";
  const lines = findings.map((f) => {
    const icon = f.severity === "warning" ? "!" : "i";
    return [
      `${f.file}:${f.line}:${f.col}: [${f.rule}] (${icon}) ${f.message}`,
      `  CO2 impact : ${f.co2}`,
      `  Fix        : ${f.hint}`,
      "",
    ].join("\n");
  });
  const warnings = findings.filter((f) => f.severity === "warning").length;
  lines.push(`${findings.length} finding(s) — ${warnings} warning(s), ${findings.length - warnings} info`);
  return lines.join("\n");
}

function formatGithub(findings) {
  return findings.map((f) => {
    const level = f.severity === "warning" ? "warning" : "notice";
    return `::${level} file=${f.file},line=${f.line},col=${f.col},title=Carbon Lint [${f.rule}]::${f.message} | CO2: ${f.co2}`;
  }).join("\n");
}

// CLI
const args = process.argv.slice(2);
const formatIdx = args.indexOf("--format");
const format = formatIdx !== -1 ? args[formatIdx + 1] : "text";
const rulesIdx = args.indexOf("--rules");
const ruleFilter = rulesIdx !== -1 ? args.slice(rulesIdx + 1).filter((a) => !a.startsWith("--")) : null;
const exitZero = args.includes("--exit-zero");
const targets = args.filter((a) => !a.startsWith("--") && a !== format && (!ruleFilter || !ruleFilter.includes(a)));

if (!targets.length) {
  console.error("Usage: node carbon_lint.js <path> [--format text|json|github] [--rules RULE-001 ...] [--exit-zero]");
  process.exit(1);
}

const allFiles = targets.flatMap((t) => collectFiles(t));
let allFindings = allFiles.flatMap((f) => lintFile(f));

if (ruleFilter) {
  allFindings = allFindings.filter((f) => ruleFilter.includes(f.rule));
}

if (format === "json") {
  console.log(JSON.stringify(allFindings, null, 2));
} else if (format === "github") {
  console.log(formatGithub(allFindings));
} else {
  console.log(formatText(allFindings));
}

if (exitZero) process.exit(0);
const hasWarnings = allFindings.some((f) => f.severity === "warning");
process.exit(hasWarnings ? 1 : 0);
