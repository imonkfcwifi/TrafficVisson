import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BridgeSupervisor,
  buildBridgeEnvironment,
  parseReadyLine,
  resolveFrozenBridgeExecutable,
} from "../bridge-supervisor.mjs";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const bridgeRoot = path.resolve(testDirectory, "..", "..", "bridge");

test("readiness records only accept an exact loopback payload", () => {
  const parsed = parseReadyLine(
    'TRAFFICVISSON_BRIDGE_READY {"host":"127.0.0.1","port":43123,"protocol":"http"}',
  );
  assert.deepEqual(parsed, {
    host: "127.0.0.1",
    port: 43123,
    protocol: "http",
  });
  assert.throws(
    () =>
      parseReadyLine(
        'TRAFFICVISSON_BRIDGE_READY {"host":"0.0.0.0","port":43123,"protocol":"http"}',
      ),
    /validation/,
  );
});

test("bridge child receives a minimal environment without parent credentials", () => {
  const environment = buildBridgeEnvironment(
    {
      PATH: "fixture-path",
      TEMP: "fixture-temp",
      OPENAI_API_KEY: "must-not-cross",
      ANTHROPIC_API_KEY: "must-not-cross",
      TRAFFICVISSON_RENDERER_URL: "must-not-cross",
      TRAFFICVISSON_COMMAND_PROPOSAL_RUNNER_ROOT: "must-not-cross",
      TRAFFICVISSON_VISION_MODEL_PACK:
        "C:\\model-pack\\trafficvisson-model-pack.json",
    },
    "fixture-token",
  );
  assert.equal(environment.PATH, "fixture-path");
  assert.equal(environment.TEMP, "fixture-temp");
  assert.equal(environment.TRAFFICVISSON_BRIDGE_TOKEN, "fixture-token");
  assert.equal(environment.PYTHONDONTWRITEBYTECODE, "1");
  assert.equal(
    environment.TRAFFICVISSON_VISION_MODEL_PACK,
    "C:\\model-pack\\trafficvisson-model-pack.json",
  );
  assert.equal(environment.OPENAI_API_KEY, undefined);
  assert.equal(environment.ANTHROPIC_API_KEY, undefined);
  assert.equal(environment.TRAFFICVISSON_RENDERER_URL, undefined);
  assert.equal(
    environment.TRAFFICVISSON_COMMAND_PROPOSAL_RUNNER_ROOT,
    undefined,
  );
});

test("packaged bridge resolution fails closed when the executable is absent", () => {
  assert.throws(
    () => resolveFrozenBridgeExecutable({ resourcesPath: testDirectory }),
    /missing/,
  );
});

test(
  "supervisor starts the authenticated Python bridge without exposing token or port",
  { timeout: 30_000 },
  async () => {
    const supervisor = new BridgeSupervisor({
      bridgeRoot,
      commandProposalRunnerRoot: bridgeRoot,
      pythonExecutable: process.env.TRAFFICVISSON_PYTHON || "python",
      startupTimeoutMs: 20_000,
    });
    try {
      const status = await supervisor.start();
      assert.equal(status.state, "ready");
      const health = await supervisor.request("GET", "/health");
      assert.equal(health.status, "ok");
      const projects = await supervisor.request("GET", "/projects");
      assert.equal(projects.projects.length >= 1, true);

      const serialized = JSON.stringify(supervisor.publicStatus());
      assert.doesNotMatch(serialized, /token/i);
      assert.doesNotMatch(serialized, /port/i);
      assert.doesNotMatch(serialized, /127\.0\.0\.1/);
      assert.doesNotMatch(serialized, /log/i);
    } finally {
      await supervisor.stop();
    }
    assert.equal(supervisor.publicStatus().state, "stopped");
  },
);
