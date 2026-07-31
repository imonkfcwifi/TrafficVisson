const NOTIFICATION_KIND = "question_review_ready";

function focusMainWindow(windowResolver) {
  const window = windowResolver();
  if (!window || window.isDestroyed?.()) return;
  if (window.isMinimized?.()) window.restore();
  window.show?.();
  window.focus?.();
  window.flashFrame?.(false);
}

export class UserActionNotificationService {
  constructor({
    NotificationCtor,
    windowResolver,
    maximumRemembered = 512,
  }) {
    if (
      typeof NotificationCtor !== "function" ||
      typeof windowResolver !== "function"
    ) {
      throw new TypeError("Native notification dependencies are invalid");
    }
    if (
      !Number.isInteger(maximumRemembered) ||
      maximumRemembered < 1 ||
      maximumRemembered > 10_000
    ) {
      throw new TypeError("Native notification memory bound is invalid");
    }
    this.NotificationCtor = NotificationCtor;
    this.windowResolver = windowResolver;
    this.maximumRemembered = maximumRemembered;
    this.shownKeys = new Set();
  }

  notifyQuestionReviewReady({
    projectId,
    pipelineJobId,
    queueRevision,
    questionCount,
  }) {
    const notificationKey = [
      NOTIFICATION_KIND,
      projectId,
      pipelineJobId,
      queueRevision,
    ].join(":");
    if (this.shownKeys.has(notificationKey)) {
      return Object.freeze({
        state: "duplicate",
        kind: NOTIFICATION_KIND,
      });
    }
    if (this.NotificationCtor.isSupported?.() === false) {
      return Object.freeze({
        state: "unsupported",
        kind: NOTIFICATION_KIND,
      });
    }

    const notification = new this.NotificationCtor({
      title: "질문 검토가 준비됐습니다",
      body: `도로 점검 후보 ${questionCount}건을 확인해 주세요.`,
      silent: false,
    });
    notification.on?.("click", () => {
      focusMainWindow(this.windowResolver);
    });
    notification.show();

    const window = this.windowResolver();
    if (
      window &&
      !window.isDestroyed?.() &&
      window.isFocused?.() === false
    ) {
      window.flashFrame?.(true);
    }

    this.shownKeys.add(notificationKey);
    while (this.shownKeys.size > this.maximumRemembered) {
      const oldestKey = this.shownKeys.values().next().value;
      this.shownKeys.delete(oldestKey);
    }
    return Object.freeze({
      state: "shown",
      kind: NOTIFICATION_KIND,
    });
  }
}
