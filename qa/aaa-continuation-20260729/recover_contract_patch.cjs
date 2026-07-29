const fs = require("node:fs");
const path = require("node:path");

const sessionRoots = [
  "C:/Users/devim/.codex/sessions/2026/07/27",
  "C:/Users/devim/.codex/sessions/2026/07/28",
  "C:/Users/devim/.codex/sessions/2026/07/29",
];
const calls = new Map();
const outputs = new Map();
for (const root of sessionRoots) {
  if (!fs.existsSync(root)) continue;
  for (const name of fs.readdirSync(root)) {
    if (!name.endsWith(".jsonl")) continue;
    const file = path.join(root, name);
    for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      if (!line) continue;
      let row;
      try {
        row = JSON.parse(line);
      } catch {
        continue;
      }
      const payload = row.payload ?? {};
      if (
        (payload.type === "custom_tool_call_output" ||
          payload.type === "function_call_output") &&
        payload.call_id
      ) {
        outputs.set(
          payload.call_id,
          typeof payload.output === "string"
            ? payload.output
            : JSON.stringify(payload.output),
        );
      }
      if (
        (payload.type === "custom_tool_call" ||
          payload.type === "function_call") &&
        (payload.name === "apply_patch" ||
          payload.name === "functions.apply_patch") &&
        payload.call_id
      ) {
        const input = payload.input ?? payload.arguments;
        if (
          typeof input === "string" &&
          input.includes("contracts.py") &&
          input.includes("*** ")
        ) {
          calls.set(payload.call_id, {
            input,
            timestamp: row.timestamp ?? "",
          });
        }
      }
    }
  }
}

function contractSection(input) {
  const headers = [
    ...input.matchAll(
    /^\*\*\* (Add|Update|Delete) File: ([^\r\n]+)$/gm,
    ),
  ];
  const contractHeaderIndex = headers.findIndex((match) =>
    /(?:^|[\\/])contracts\.py$/i.test(match[2].trim()),
  );
  if (contractHeaderIndex < 0) return null;
  const header = headers[contractHeaderIndex];
  const nextHeader = headers[contractHeaderIndex + 1];
  const endPatchIndex = input.indexOf("*** End Patch", header.index);
  return {
    action: header[1],
    body: input
      .slice(
        header.index + header[0].length,
        nextHeader?.index ?? endPatchIndex,
      )
      .replace(/^\r?\n/, "")
      .replace(/\s+$/, ""),
  };
}

function findSequence(haystack, needle, start) {
  if (needle.length === 0) return start;
  for (let index = Math.max(0, start); index <= haystack.length - needle.length; index += 1) {
    let matches = true;
    for (let offset = 0; offset < needle.length; offset += 1) {
      if (haystack[index + offset] !== needle[offset]) {
        matches = false;
        break;
      }
    }
    if (matches) return index;
  }
  return -1;
}

function applyUpdate(current, body, callId) {
  let lines = current.split("\n");
  const patchLines = body.split(/\r?\n/);
  const hunks = [];
  let active = null;
  for (const line of patchLines) {
    if (line.startsWith("@@")) {
      active = [];
      hunks.push(active);
    } else if (active) {
      active.push(line);
    }
  }
  if (hunks.length === 0) {
    throw new Error(`update patch has no hunks: ${callId}`);
  }
  let cursor = 0;
  for (const hunk of hunks) {
    const oldLines = [];
    const newLines = [];
    for (const line of hunk) {
      if (line === "*** End of File") continue;
      const prefix = line[0];
      const value = line.slice(1);
      if (prefix === " " || prefix === "-") oldLines.push(value);
      if (prefix === " " || prefix === "+") newLines.push(value);
    }
    let found = findSequence(lines, oldLines, cursor);
    if (found < 0) found = findSequence(lines, oldLines, 0);
    if (found < 0) {
      throw new Error(
        `cannot apply ${callId}; missing context: ${JSON.stringify(oldLines.slice(0, 4))}`,
      );
    }
    lines.splice(found, oldLines.length, ...newLines);
    cursor = found + newLines.length;
  }
  return lines.join("\n");
}

function reconstruct() {
  const historicalCutoff = "2026-07-28T21:20:00.000Z";
  const successful = [...calls.entries()]
    .filter(([callId]) =>
      /Success\. Updated the following files:/i.test(outputs.get(callId) ?? ""),
    )
    .map(([callId, record]) => ({
      callId,
      ...record,
      section: contractSection(record.input),
    }))
    .filter((record) => record.timestamp < historicalCutoff)
    .filter((record) => record.section)
    .sort((left, right) => left.timestamp.localeCompare(right.timestamp));
  let current = null;
  for (const record of successful) {
    const { action, body } = record.section;
    if (action === "Add") {
      if (current !== null) {
        throw new Error(`duplicate contracts.py add: ${record.callId}`);
      }
      current = body
        .split(/\r?\n/)
        .map((line) => {
          if (!line.startsWith("+")) {
            throw new Error(`invalid add line in ${record.callId}`);
          }
          return line.slice(1);
        })
        .join("\n");
    } else if (action === "Update") {
      if (current === null) {
        throw new Error(`update before add: ${record.callId}`);
      }
      current = applyUpdate(current, body, record.callId);
    } else if (action === "Delete") {
      current = null;
    }
  }
  if (current === null) throw new Error("contracts.py reconstruction is empty");
  return current;
}

if (
  process.argv[2] === "--reconstruct" ||
  process.argv[2] === "--reconstruct-raw" ||
  process.argv[2] === "--reconstruct-json"
) {
  const chunkIndex = Number(process.argv[3]);
  const chunkCount = Number(process.argv[4]);
  if (
    !Number.isSafeInteger(chunkIndex) ||
    !Number.isSafeInteger(chunkCount) ||
    chunkIndex < 0 ||
    chunkIndex >= chunkCount ||
    chunkCount < 1 ||
    chunkCount > 32
  ) {
    throw new Error("invalid reconstruction chunk request");
  }
  const payload =
    process.argv[2] === "--reconstruct"
      ? Buffer.from(reconstruct(), "utf8").toString("base64")
      : reconstruct();
  const chunkSize = Math.ceil(payload.length / chunkCount);
  const chunk = payload.slice(
    chunkIndex * chunkSize,
    (chunkIndex + 1) * chunkSize,
  );
  process.stdout.write(
    process.argv[2] === "--reconstruct-json"
      ? JSON.stringify(chunk)
      : chunk,
  );
  process.exit(0);
}

const requestedCallId = process.argv[2];
if (!requestedCallId) {
  throw new Error(
    "usage: node recover_contract_patch.cjs <call_id> | --reconstruct <chunk> <count>",
  );
}
const record = calls.get(requestedCallId);
const output = outputs.get(requestedCallId) ?? "";
if (!record) throw new Error(`patch call not found: ${requestedCallId}`);
if (!/Success\. Updated the following files:/i.test(output)) {
  throw new Error(`patch call did not succeed: ${requestedCallId}`);
}
const section = contractSection(record.input);
if (!section) {
  throw new Error(`contracts.py section not found: ${requestedCallId}`);
}
process.stdout.write(
  `PATCH_BEGIN\n*** Begin Patch\n*** ${section.action} File: desktop/bridge/app/contracts.py\n${section.body}\n*** End Patch\nPATCH_END`,
);
