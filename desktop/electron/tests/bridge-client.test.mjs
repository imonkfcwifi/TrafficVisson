import assert from "node:assert/strict";
import test from "node:test";

import {
  BridgeClient,
  MAX_REQUEST_TIMEOUT_MS,
} from "../bridge-client.mjs";

const TEST_TOKEN = "test-only-bridge-token-with-at-least-256-bits-000000";

test("bridge client only accepts explicit 127.0.0.1 HTTP endpoints", () => {
  assert.throws(
    () => new BridgeClient({ baseUrl: "http://localhost:9000", token: TEST_TOKEN }),
    /127\.0\.0\.1/,
  );
  assert.throws(
    () => new BridgeClient({ baseUrl: "https://127.0.0.1:9000", token: TEST_TOKEN }),
    /127\.0\.0\.1/,
  );
  assert.throws(
    () => new BridgeClient({ baseUrl: "http://127.0.0.1", token: TEST_TOKEN }),
    /port/,
  );
});

test("bridge client supplies auth internally and blocks redirects", async () => {
  let observed;
  const client = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    fetchImpl: async (url, options) => {
      observed = { url: String(url), options };
      return new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });

  const response = await client.request("GET", "/health", {
    query: { policy_mode: "quick_review" },
  });
  assert.equal(response.status, "ok");
  assert.equal(observed.url, "http://127.0.0.1:43123/health?policy_mode=quick_review");
  assert.equal(observed.options.redirect, "error");
  assert.equal(observed.options.credentials, "omit");
  assert.equal(observed.options.headers.Authorization, `Bearer ${TEST_TOKEN}`);
  await client.request("PUT", "/projects/11111111-1111-4111-8111-111111111111/pipeline/profile", {
    body: { review_mode: "precision" },
  });
  assert.equal(observed.options.method, "PUT");
  assert.equal(
    observed.url,
    "http://127.0.0.1:43123/projects/11111111-1111-4111-8111-111111111111/pipeline/profile",
  );
  await assert.rejects(() => client.request("DELETE", "/health"), /allowlisted/);
  await assert.rejects(() => client.request("PATCH", "/health"), /allowlisted/);
  await assert.rejects(() => client.request("GET", "//attacker.example"), /invalid/);
  await assert.rejects(() => client.request("GET", "/projects/../providers"), /invalid/);
});

test("bridge client accepts only a bounded main-process request timeout override", async () => {
  const client = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    timeoutMs: 1_000,
    fetchImpl: async (_url, options) =>
      new Promise((_resolve, reject) => {
        options.signal.addEventListener(
          "abort",
          () => {
            const error = new Error("aborted");
            error.name = "AbortError";
            reject(error);
          },
          { once: true },
        );
      }),
  });

  await assert.rejects(
    () => client.request("POST", "/exports", { body: {}, timeoutMs: 1 }),
    (error) => error?.code === "BRIDGE_TIMEOUT",
  );
  await assert.rejects(
    () => client.request("GET", "/health", { timeoutMs: 0 }),
    /timeout is invalid/,
  );
  await assert.rejects(
    () =>
      client.request("GET", "/health", {
        timeoutMs: MAX_REQUEST_TIMEOUT_MS + 1,
      }),
    /timeout is invalid/,
  );
  assert.throws(
    () =>
      new BridgeClient({
        baseUrl: "http://127.0.0.1:43123",
        token: TEST_TOKEN,
        timeoutMs: MAX_REQUEST_TIMEOUT_MS + 1,
      }),
    /timeout is invalid/,
  );
});

test("bridge client preserves bounded structured lifecycle error codes", async () => {
  const client = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          detail: {
            code: "fixed_validation_extension_insufficient",
            message:
              "new fixed-validation issue types require 30 confirmed positive samples each (sign_damage=29)",
          },
        }),
        {
          status: 409,
          headers: { "Content-Type": "application/json" },
        },
      ),
  });

  await assert.rejects(
    () => client.request("POST", "/projects/test/model-training-jobs"),
    /fixed_validation_extension_insufficient.*sign_damage=29/,
  );
});

test("bridge client streams bounded evidence with internal auth and byte ranges", async () => {
  let observed;
  const jpeg = Uint8Array.from([0xff, 0xd8, 0xff, 0xe0]);
  const client = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    fetchImpl: async (url, options) => {
      observed = { url: String(url), options };
      return new Response(jpeg, {
        status: 206,
        headers: {
          "Accept-Ranges": "bytes",
          "Content-Length": String(jpeg.byteLength),
          "Content-Range": "bytes 0-3/400",
          "Content-Type": "image/jpeg",
          ETag: '"digest"',
        },
      });
    },
  });

  const response = await client.requestBinary(
    "/projects/11111111-1111-4111-8111-111111111111/analysis/jobs/" +
      "22222222-2222-4222-8222-222222222222/candidates/" +
      "0123456789abcdefabcd/image",
    { range: "bytes=0-3" },
  );
  assert.equal(response.status, 206);
  assert.deepEqual(
    new Uint8Array(await response.arrayBuffer()),
    jpeg,
  );
  assert.equal(observed.options.headers.Authorization, `Bearer ${TEST_TOKEN}`);
  assert.equal(observed.options.headers.Range, "bytes=0-3");
  assert.equal(observed.options.redirect, "error");
  assert.equal(response.headers.get("authorization"), null);
  assert.equal(response.headers.get("content-type"), "image/jpeg");
  assert.equal(response.headers.get("content-range"), "bytes 0-3/400");
});

test("bridge client accepts the bounded raster formats used by HWPX samples", async () => {
  for (const mediaType of [
    "image/bmp",
    "image/gif",
    "image/png",
    "image/webp",
  ]) {
    const body = Uint8Array.from([1, 2, 3, 4]);
    const client = new BridgeClient({
      baseUrl: "http://127.0.0.1:43123",
      token: TEST_TOKEN,
      fetchImpl: async () =>
        new Response(body, {
          status: 200,
          headers: {
            "Content-Length": String(body.byteLength),
            "Content-Type": mediaType,
          },
        }),
    });
    const response = await client.requestBinary("/reference-image", {
      maximumBytes: 64,
    });
    assert.equal(response.headers.get("content-type"), mediaType);
    assert.deepEqual(
      new Uint8Array(await response.arrayBuffer()),
      body,
    );
  }
});

test("bridge client rejects invalid or oversized binary responses", async () => {
  const oversizedDeclared = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    fetchImpl: async () =>
      new Response(Uint8Array.from([1]), {
        status: 200,
        headers: {
          "Content-Length": "129",
          "Content-Type": "video/mp4",
        },
      }),
  });
  await assert.rejects(
    () => oversizedDeclared.requestBinary("/evidence", { maximumBytes: 128 }),
    /size limit/,
  );
  await assert.rejects(
    () =>
      oversizedDeclared.requestBinary("/evidence", {
        range: "bytes=0-1,4-5",
      }),
    /range/,
  );

  const chunkedOversized = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    fetchImpl: async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(Uint8Array.from([1, 2, 3]));
            controller.enqueue(Uint8Array.from([4, 5, 6]));
            controller.close();
          },
        }),
        {
          status: 200,
          headers: { "Content-Type": "video/mp4" },
        },
      ),
  });
  const streamed = await chunkedOversized.requestBinary("/evidence", {
    maximumBytes: 4,
  });
  await assert.rejects(() => streamed.arrayBuffer(), /size limit/);

  const wrongType = new BridgeClient({
    baseUrl: "http://127.0.0.1:43123",
    token: TEST_TOKEN,
    fetchImpl: async () =>
      new Response("not evidence", {
        status: 200,
        headers: { "Content-Type": "text/html" },
      }),
  });
  await assert.rejects(
    () => wrongType.requestBinary("/evidence"),
    /unsupported binary media type/,
  );
});
