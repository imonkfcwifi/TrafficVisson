const LOOPBACK_HOST = "127.0.0.1";
const DEFAULT_TIMEOUT_MS = 15_000;
export const MAX_REQUEST_TIMEOUT_MS = 30 * 60 * 1000;
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024;
export const MAX_BINARY_RESPONSE_BYTES = 128 * 1024 * 1024;
const MAX_ERROR_RESPONSE_BYTES = 16 * 1024;
const ALLOWED_JSON_METHODS = new Set(["GET", "POST", "PUT"]);
const BINARY_MEDIA_TYPES = new Set([
  "image/bmp",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
  "video/mp4",
]);

export class BridgeHttpError extends Error {
  constructor(message, { status = 0, code = "BRIDGE_REQUEST_FAILED" } = {}) {
    super(message);
    this.name = "BridgeHttpError";
    this.status = status;
    this.code = code;
  }
}

function assertLoopbackBaseUrl(baseUrl) {
  const parsed = new URL(baseUrl);
  if (parsed.protocol !== "http:" || parsed.hostname !== LOOPBACK_HOST || parsed.username || parsed.password) {
    throw new TypeError("Bridge base URL must be an unauthenticated 127.0.0.1 HTTP URL");
  }
  if (!parsed.port) throw new TypeError("Bridge base URL must include an explicit port");
  return parsed;
}

function safePath(pathname) {
  if (
    typeof pathname !== "string" ||
    !pathname.startsWith("/") ||
    pathname.startsWith("//") ||
    pathname.includes("\\") ||
    pathname.includes("\0") ||
    /%2e/i.test(pathname) ||
    pathname.split(/[/?#]/).some((segment) => segment === "..")
  ) {
    throw new TypeError("Bridge request path is invalid");
  }
  return pathname;
}

function boundedTimeoutMs(value) {
  if (
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > MAX_REQUEST_TIMEOUT_MS
  ) {
    throw new TypeError("Bridge request timeout is invalid");
  }
  return value;
}

function boundedBody(body, maximumBytes, abortController, onDone) {
  if (!body) {
    onDone();
    return null;
  }
  const reader = body.getReader();
  let consumed = 0;
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    onDone();
  };
  return new ReadableStream({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          finish();
          controller.close();
          return;
        }
        consumed += value.byteLength;
        if (consumed > maximumBytes) {
          abortController.abort();
          await reader.cancel("binary response exceeded limit");
          finish();
          controller.error(
            new BridgeHttpError("Bridge binary response exceeded the size limit", {
              code: "BRIDGE_RESPONSE_TOO_LARGE",
            }),
          );
          return;
        }
        controller.enqueue(value);
      } catch (error) {
        finish();
        controller.error(error);
      }
    },
    async cancel(reason) {
      abortController.abort();
      await reader.cancel(reason);
      finish();
    },
  });
}

async function readBoundedError(response) {
  const declaredLength = Number(response.headers.get("content-length") ?? "0");
  if (
    !Number.isFinite(declaredLength) ||
    declaredLength < 0 ||
    declaredLength > MAX_ERROR_RESPONSE_BYTES
  ) {
    return null;
  }
  const reader = response.body?.getReader();
  if (!reader) return null;
  const chunks = [];
  let consumed = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    consumed += value.byteLength;
    if (consumed > MAX_ERROR_RESPONSE_BYTES) {
      await reader.cancel("error response exceeded limit");
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(consumed);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const parsed = JSON.parse(new TextDecoder().decode(bytes));
    return typeof parsed?.detail === "string" && parsed.detail.length <= 300
      ? parsed.detail
      : null;
  } catch {
    return null;
  }
}

export class BridgeClient {
  #baseUrl;
  #token;
  #fetch;
  #timeoutMs;

  constructor({ baseUrl, token, fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS }) {
    if (typeof fetchImpl !== "function") throw new TypeError("fetch implementation is required");
    if (typeof token !== "string" || token.length < 43) {
      throw new TypeError("Bridge token must contain at least 256 bits of URL-safe entropy");
    }
    this.#baseUrl = assertLoopbackBaseUrl(baseUrl);
    this.#token = token;
    this.#fetch = fetchImpl;
    this.#timeoutMs = boundedTimeoutMs(timeoutMs);
  }

  async request(method, pathname, { body, query, timeoutMs } = {}) {
    if (!ALLOWED_JSON_METHODS.has(method)) {
      throw new TypeError("Only allowlisted bridge methods are supported");
    }
    const url = new URL(safePath(pathname), this.#baseUrl);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined && value !== null && value !== "") {
        if (!/^[a-z_][a-z0-9_]*$/.test(key)) throw new TypeError("Invalid query key");
        url.searchParams.set(key, String(value));
      }
    }

    const abortController = new AbortController();
    const requestTimeoutMs =
      timeoutMs === undefined
        ? this.#timeoutMs
        : boundedTimeoutMs(timeoutMs);
    const timer = setTimeout(() => abortController.abort(), requestTimeoutMs);
    try {
      const response = await this.#fetch(url, {
        method,
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${this.#token}`,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        signal: abortController.signal,
      });
      const declaredLength = Number(response.headers.get("content-length") ?? "0");
      if (declaredLength > MAX_RESPONSE_BYTES) {
        throw new BridgeHttpError("Bridge response exceeded the size limit", {
          status: response.status,
          code: "BRIDGE_RESPONSE_TOO_LARGE",
        });
      }
      const responseText = await response.text();
      if (Buffer.byteLength(responseText, "utf8") > MAX_RESPONSE_BYTES) {
        throw new BridgeHttpError("Bridge response exceeded the size limit", {
          status: response.status,
          code: "BRIDGE_RESPONSE_TOO_LARGE",
        });
      }
      let data = null;
      if (responseText) {
        try {
          data = JSON.parse(responseText);
        } catch {
          throw new BridgeHttpError("Bridge returned malformed JSON", {
            status: response.status,
            code: "BRIDGE_INVALID_JSON",
          });
        }
      }
      if (!response.ok) {
        const structuredDetail =
          data &&
          data.detail &&
          typeof data.detail === "object" &&
          !Array.isArray(data.detail) &&
          typeof data.detail.code === "string" &&
          /^[a-z][a-z0-9_]{0,127}$/.test(data.detail.code) &&
          typeof data.detail.message === "string" &&
          data.detail.message.length >= 1 &&
          data.detail.message.length <= 300
            ? data.detail
            : null;
        const safeMessage = structuredDetail
          ? `${structuredDetail.code}: ${structuredDetail.message}`
          : data &&
              typeof data.detail === "string" &&
              data.detail.length <= 300
            ? data.detail
            : "Bridge request was rejected";
        throw new BridgeHttpError(safeMessage, {
          status: response.status,
          code: "BRIDGE_HTTP_ERROR",
        });
      }
      return data;
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new BridgeHttpError("Bridge request timed out", {
          code: "BRIDGE_TIMEOUT",
        });
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  async requestBinary(
    pathname,
    { range, maximumBytes = MAX_BINARY_RESPONSE_BYTES } = {},
  ) {
    if (
      !Number.isSafeInteger(maximumBytes) ||
      maximumBytes < 1 ||
      maximumBytes > MAX_BINARY_RESPONSE_BYTES
    ) {
      throw new TypeError("Binary response limit is invalid");
    }
    if (
      range !== undefined &&
      (typeof range !== "string" ||
        range.length > 80 ||
        !/^bytes=(?:[0-9]+-[0-9]*|-[0-9]+)$/.test(range))
    ) {
      throw new TypeError("Binary byte range is invalid");
    }
    const url = new URL(safePath(pathname), this.#baseUrl);
    const abortController = new AbortController();
    const timer = setTimeout(() => abortController.abort(), this.#timeoutMs);
    let responseStreamOwnsTimer = false;
    try {
      const response = await this.#fetch(url, {
        method: "GET",
        headers: {
          Accept:
            "image/jpeg, image/png, image/bmp, image/gif, image/webp, video/mp4",
          Authorization: `Bearer ${this.#token}`,
          ...(range ? { Range: range } : {}),
        },
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        signal: abortController.signal,
      });
      if (!response.ok || ![200, 206].includes(response.status)) {
        const detail = await readBoundedError(response);
        throw new BridgeHttpError(detail ?? "Bridge binary request was rejected", {
          status: response.status,
          code: "BRIDGE_HTTP_ERROR",
        });
      }
      const mediaType = (response.headers.get("content-type") ?? "")
        .split(";", 1)[0]
        .trim()
        .toLowerCase();
      if (!BINARY_MEDIA_TYPES.has(mediaType)) {
        throw new BridgeHttpError("Bridge returned an unsupported binary media type", {
          status: response.status,
          code: "BRIDGE_INVALID_MEDIA_TYPE",
        });
      }
      const declaredLengthText = response.headers.get("content-length");
      const declaredLength =
        declaredLengthText === null ? null : Number(declaredLengthText);
      if (
        declaredLength !== null &&
        (!Number.isSafeInteger(declaredLength) ||
          declaredLength < 0 ||
          declaredLength > maximumBytes)
      ) {
        throw new BridgeHttpError("Bridge binary response exceeded the size limit", {
          status: response.status,
          code: "BRIDGE_RESPONSE_TOO_LARGE",
        });
      }
      const contentRange = response.headers.get("content-range");
      if (response.status === 206) {
        const match = /^bytes ([0-9]+)-([0-9]+)\/([0-9]+)$/.exec(
          contentRange ?? "",
        );
        const start = match ? Number(match[1]) : NaN;
        const end = match ? Number(match[2]) : NaN;
        const total = match ? Number(match[3]) : NaN;
        if (
          !match ||
          ![start, end, total].every(Number.isSafeInteger) ||
          start < 0 ||
          end < start ||
          end >= total ||
          total > maximumBytes ||
          (declaredLength !== null && declaredLength !== end - start + 1)
        ) {
          throw new BridgeHttpError("Bridge returned an invalid byte range", {
            status: response.status,
            code: "BRIDGE_INVALID_RANGE",
          });
        }
      }
      const headers = new Headers({
        "Content-Type": mediaType,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      });
      for (const name of [
        "accept-ranges",
        "content-length",
        "content-range",
        "etag",
      ]) {
        const value = response.headers.get(name);
        if (value) headers.set(name, value);
      }
      responseStreamOwnsTimer = true;
      return new Response(
        boundedBody(
          response.body,
          maximumBytes,
          abortController,
          () => clearTimeout(timer),
        ),
        {
          status: response.status,
          statusText: response.statusText,
          headers,
        },
      );
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new BridgeHttpError("Bridge binary request timed out", {
          code: "BRIDGE_TIMEOUT",
        });
      }
      throw error;
    } finally {
      if (!responseStreamOwnsTimer) clearTimeout(timer);
    }
  }
}
