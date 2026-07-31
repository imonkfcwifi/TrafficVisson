import assert from "node:assert/strict";
import test from "node:test";

import {
  IPC_CHANNELS,
  IpcValidationError,
  validateIpcPayload,
} from "../contracts.mjs";
import { UserActionNotificationService } from "../user-action-notifications.mjs";

const payload = {
  projectId: "11111111-1111-4111-8111-111111111111",
  pipelineJobId: "22222222-2222-4222-8222-222222222222",
  queueRevision: "question-queue-a1",
  questionCount: 32,
};

test("notification IPC accepts only opaque review-queue identity and count", () => {
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.NOTIFICATION_QUESTION_REVIEW_READY,
      payload,
    ),
    payload,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.NOTIFICATION_QUESTION_REVIEW_READY,
        { ...payload, questionCount: 0 },
      ),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.NOTIFICATION_QUESTION_REVIEW_READY,
        {
          ...payload,
          body: "renderer-controlled message",
        },
      ),
    /not allowed/,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.NOTIFICATION_QUESTION_REVIEW_READY,
        {
          ...payload,
          queueRevision: "C:\\private\\queue.json",
        },
      ),
    IpcValidationError,
  );
});

class FakeNotification {
  static supported = true;
  static created = [];

  static isSupported() {
    return FakeNotification.supported;
  }

  constructor(options) {
    this.options = options;
    this.handlers = new Map();
    this.shown = false;
    FakeNotification.created.push(this);
  }

  on(eventName, callback) {
    this.handlers.set(eventName, callback);
  }

  show() {
    this.shown = true;
  }
}

function createWindow() {
  const calls = [];
  return {
    calls,
    flashFrame(value) {
      calls.push(["flashFrame", value]);
    },
    focus() {
      calls.push(["focus"]);
    },
    isDestroyed() {
      return false;
    },
    isFocused() {
      return false;
    },
    isMinimized() {
      return true;
    },
    restore() {
      calls.push(["restore"]);
    },
    show() {
      calls.push(["show"]);
    },
  };
}

test("question-ready notification is fixed-copy, audible and deduplicated", () => {
  FakeNotification.created = [];
  FakeNotification.supported = true;
  const window = createWindow();
  const service = new UserActionNotificationService({
    NotificationCtor: FakeNotification,
    windowResolver: () => window,
  });

  assert.deepEqual(service.notifyQuestionReviewReady(payload), {
    state: "shown",
    kind: "question_review_ready",
  });
  assert.equal(FakeNotification.created.length, 1);
  assert.deepEqual(FakeNotification.created[0].options, {
    title: "질문 검토가 준비됐습니다",
    body: "도로 점검 후보 32건을 확인해 주세요.",
    silent: false,
  });
  assert.equal(FakeNotification.created[0].shown, true);
  assert.deepEqual(window.calls, [["flashFrame", true]]);

  assert.deepEqual(service.notifyQuestionReviewReady(payload), {
    state: "duplicate",
    kind: "question_review_ready",
  });
  assert.equal(FakeNotification.created.length, 1);
});

test("notification click restores, focuses and clears taskbar attention", () => {
  FakeNotification.created = [];
  FakeNotification.supported = true;
  const window = createWindow();
  const service = new UserActionNotificationService({
    NotificationCtor: FakeNotification,
    windowResolver: () => window,
  });
  service.notifyQuestionReviewReady(payload);

  FakeNotification.created[0].handlers.get("click")();

  assert.deepEqual(window.calls, [
    ["flashFrame", true],
    ["restore"],
    ["show"],
    ["focus"],
    ["flashFrame", false],
  ]);
});

test("unsupported native notifications do not consume the dedupe key", () => {
  FakeNotification.created = [];
  FakeNotification.supported = false;
  const service = new UserActionNotificationService({
    NotificationCtor: FakeNotification,
    windowResolver: () => null,
  });

  assert.deepEqual(service.notifyQuestionReviewReady(payload), {
    state: "unsupported",
    kind: "question_review_ready",
  });
  FakeNotification.supported = true;
  assert.equal(
    service.notifyQuestionReviewReady(payload).state,
    "shown",
  );
});
