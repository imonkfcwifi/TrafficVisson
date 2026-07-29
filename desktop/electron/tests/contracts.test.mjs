import assert from "node:assert/strict";
import test from "node:test";

import {
  IPC_CHANNELS,
  IpcValidationError,
  toBridgePayload,
  validateIpcPayload,
} from "../contracts.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const QUESTION_ID = "44444444-4444-4444-8444-444444444441";
const ANALYSIS_JOB_ID = "22222222-2222-4222-8222-222222222222";

test("native video picker accepts no renderer-controlled payload", () => {
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.DIALOG_VIDEO_FILES, undefined),
    {},
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.DIALOG_VIDEO_FILES, {
        defaultPath: "C:\\secret",
      }),
    IpcValidationError,
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.DIALOG_HWP_DOCUMENT, undefined),
    {},
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.DIALOG_GPX_FILE, undefined),
    {},
  );
});

test("GPX IPC uses opaque selections and bounded synchronization offsets", () => {
  const selectionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.GPX_TRACK_IMPORT, {
      projectId: PROJECT_ID,
      tripId: ANALYSIS_JOB_ID,
      gpxSelectionId: selectionId,
      videoOffsetSeconds: -2.375,
    }),
    {
      project_id: PROJECT_ID,
      trip_id: ANALYSIS_JOB_ID,
      gpx_selection_id: selectionId,
      video_offset_seconds: -2.375,
    },
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.GPX_TRACK_GET, {
      projectId: PROJECT_ID,
      tripId: ANALYSIS_JOB_ID,
    }),
    {
      projectId: PROJECT_ID,
      tripId: ANALYSIS_JOB_ID,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.GPX_TRACK_IMPORT, {
        projectId: PROJECT_ID,
        tripId: ANALYSIS_JOB_ID,
        gpxSelectionId: selectionId,
        selectedPath: "C:\\secret\\route.gpx",
      }),
    /not allowed/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.GPX_TRACK_OFFSET, {
        projectId: PROJECT_ID,
        tripId: ANALYSIS_JOB_ID,
        videoOffsetSeconds: Number.POSITIVE_INFINITY,
      }),
    /finite/,
  );
});

test("source candidate catalogue accepts no renderer-controlled payload", () => {
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.SOURCE_CANDIDATES_LIST, undefined),
    {},
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.SOURCE_CANDIDATES_LIST, null),
    {},
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.SOURCE_CANDIDATES_LIST, {
        download: true,
      }),
    IpcValidationError,
  );
});

test("IPC contracts reject unknown fields and non-UUID project IDs", () => {
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.PROJECT_CREATE, {
        name: "project",
        arbitraryCommand: "calc.exe",
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.PROJECT_GET, {
        projectId: "../../etc/passwd",
      }),
    IpcValidationError,
  );
});

test("incident list IPC keeps project and analysis-job boundaries explicit", () => {
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.INCIDENT_LIST, {
      projectId: PROJECT_ID,
    }),
    {
      projectId: PROJECT_ID,
      analysisJobId: undefined,
      includeInactive: undefined,
    },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.INCIDENT_LIST, {
      projectId: PROJECT_ID,
      analysisJobId: ANALYSIS_JOB_ID,
      includeInactive: false,
    }),
    {
      project_id: PROJECT_ID,
      analysis_job_id: ANALYSIS_JOB_ID,
      include_inactive: false,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.INCIDENT_LIST, {
        projectId: PROJECT_ID,
        jobId: ANALYSIS_JOB_ID,
      }),
    /not allowed/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.INCIDENT_LIST, {
        projectId: PROJECT_ID,
        analysisJobId: "../../other-project",
      }),
    /UUID/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.INCIDENT_LIST, {
        projectId: PROJECT_ID,
        analysisJobId: ANALYSIS_JOB_ID,
        includeInactive: "false",
      }),
    /boolean/,
  );
});

test("edit answers require bounded geometry", () => {
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.QUESTION_ANSWER, {
        projectId: PROJECT_ID,
        questionId: QUESTION_ID,
        answer: "edit",
      }),
    /finalGeometry is required/,
  );

  const value = validateIpcPayload(IPC_CHANNELS.QUESTION_ANSWER, {
    projectId: PROJECT_ID,
    questionId: QUESTION_ID,
    answer: "edit",
    finalGeometry: {
      kind: "box",
      points: [
        [10, 20],
        [40, 60],
      ],
    },
  });
  assert.equal(value.finalGeometry.kind, "box");
});

test("media intake IPC accepts bounded snapshots and rejects path-like filenames", () => {
  const localSelectionId =
    "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const localRegistration = toBridgePayload(
    IPC_CHANNELS.MEDIA_LOCAL_REGISTER,
    {
      projectId: PROJECT_ID,
      localSelectionId,
    },
  );
  assert.deepEqual(localRegistration, {
    project_id: PROJECT_ID,
    local_selection_id: localSelectionId,
  });
  assert.doesNotMatch(JSON.stringify(localRegistration), /(?:[A-Z]:\\|\\\\server)/);
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MEDIA_LOCAL_REGISTER, {
        projectId: PROJECT_ID,
        localSelectionId,
        selectedPath: "C:\\secret\\road.mp4",
      }),
    /not allowed/,
  );

  const snapshot = toBridgePayload(IPC_CHANNELS.MEDIA_SNAPSHOT, {
    projectId: PROJECT_ID,
    directory: "\\\\server\\share\\도로 영상",
  });
  assert.deepEqual(snapshot, {
    project_id: PROJECT_ID,
    directory: "\\\\server\\share\\도로 영상",
  });

  const snapshotIds = [
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3",
  ];
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.MEDIA_STABILITY, {
      projectId: PROJECT_ID,
      snapshotIds,
    }).snapshotIds,
    snapshotIds,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MEDIA_STABILITY, {
        projectId: PROJECT_ID,
        snapshotIds: [snapshotIds[0], snapshotIds[0], snapshotIds[2]],
      }),
    /distinct/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MEDIA_PROBE, {
        projectId: PROJECT_ID,
        snapshotId: snapshotIds[2],
        fileName: "..\\video.mp4",
      }),
    /path/,
  );

  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.MEDIA_HASH_START, {
      projectId: PROJECT_ID,
      snapshotId: snapshotIds[2],
      fileName: "천안논산 천안방향.mp4",
    }),
    {
      project_id: PROJECT_ID,
      snapshot_id: snapshotIds[2],
      file_name: "천안논산 천안방향.mp4",
    },
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.MEDIA_HASH_GET, {
      projectId: PROJECT_ID,
      jobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    }),
    {
      projectId: PROJECT_ID,
      jobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MEDIA_HASH_START, {
        projectId: PROJECT_ID,
        snapshotId: snapshotIds[2],
        fileName: "../video.mp4",
      }),
    /path/,
  );

  const analysisStart = toBridgePayload(IPC_CHANNELS.ANALYSIS_START, {
    projectId: PROJECT_ID,
    snapshotId: snapshotIds[2],
    hashJobId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    fileName: "천안논산 천안방향.mp4",
    sha256Lower: "a".repeat(64),
  });
  assert.equal(analysisStart.sha256_lower, "a".repeat(64));
  assert.equal(analysisStart.hash_job_id, "cccccccc-cccc-4ccc-8ccc-cccccccccccc");
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.ANALYSIS_LIST, {
      projectId: PROJECT_ID,
    }),
    { projectId: PROJECT_ID },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.ANALYSIS_LIST, {
        projectId: PROJECT_ID,
        limit: 999,
      }),
    /not allowed/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.ANALYSIS_START, {
        projectId: PROJECT_ID,
        snapshotId: snapshotIds[2],
        hashJobId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        fileName: "video.mp4",
        sha256Lower: "A".repeat(64),
      }),
    /lowercase/,
  );
});

test("document plans allow only deterministic operations and opaque references", () => {
  const base = {
    projectId: PROJECT_ID,
    plan: {
      schemaVersion: "1.0",
      planId: "55555555-5555-4555-8555-555555555555",
      documentSnapshotId: "snapshot:demo",
      operations: [
        {
          operationId: "66666666-6666-4666-8666-666666666666",
          type: "fill_text",
          target: "bookmark:summary",
          value: "점검 결과",
          sourceRef: "draft:demo",
          stylePolicy: "preserve",
          preview: "요약을 채웁니다.",
        },
      ],
    },
  };
  assert.equal(
    validateIpcPayload(IPC_CHANNELS.REPORT_APPLY, base).plan.operations[0].type,
    "fill_text",
  );

  const arbitraryOperation = structuredClone(base);
  arbitraryOperation.plan.operations[0].type = "run_macro";
  assert.throws(
    () => validateIpcPayload(IPC_CHANNELS.REPORT_APPLY, arbitraryOperation),
    IpcValidationError,
  );

  const hwpPayload = validateIpcPayload(IPC_CHANNELS.HWP_PREVIEW, {
    projectId: PROJECT_ID,
    documentSelectionId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    plan: base.plan,
  });
  assert.equal(
    hwpPayload.documentSelectionId,
    "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  );
  const approvalToken = "a".repeat(64);
  const hwpApplyPayload = validateIpcPayload(IPC_CHANNELS.HWP_APPLY, {
    projectId: PROJECT_ID,
    documentSelectionId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    plan: base.plan,
    approvalToken,
  });
  assert.equal(hwpApplyPayload.approvalToken, approvalToken);
  assert.equal(
    toBridgePayload(IPC_CHANNELS.HWP_APPLY, {
      projectId: PROJECT_ID,
      documentSelectionId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
      plan: base.plan,
      approvalToken,
    }).approval_token,
    approvalToken,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.HWP_APPLY, {
        projectId: PROJECT_ID,
        documentSelectionId: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        plan: base.plan,
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.HWP_ROLLBACK, {
        projectId: PROJECT_ID,
        rollbackToken: "../unsafe",
      }),
    IpcValidationError,
  );

  const fileTarget = structuredClone(base);
  fileTarget.plan.operations[0].target = "C:\\secret\\document.hwpx";
  assert.throws(
    () => validateIpcPayload(IPC_CHANNELS.REPORT_APPLY, fileTarget),
    IpcValidationError,
  );
});

test("report generation sends only scoped incident IDs and analysis provenance", () => {
  const analysisJobId = "66666666-6666-4666-8666-666666666666";
  const incidentId = "77777777-7777-4777-8777-777777777777";
  const payload = {
    projectId: PROJECT_ID,
    analysisJobId,
    sourceFileSha256: "a".repeat(64),
    provider: "codex",
    consent: true,
    projectLabel: "천안논산 점검",
    incidentIds: [incidentId],
    documentStructureSummary: {
      documentSnapshotId: "selection:document-1",
      fieldTargets: ["field:inspection_date"],
      cellTargets: [],
      tableTargets: [],
      imageTargets: [],
      mapTargets: [],
      sectionHeadings: ["점검 결과"],
    },
    mapRefs: [],
  };
  const validated = validateIpcPayload(
    IPC_CHANNELS.REPORT_GENERATE,
    payload,
  );
  assert.deepEqual(validated.incidentIds, [incidentId]);
  assert.equal(validated.analysisJobId, analysisJobId);
  assert.equal(validated.sourceFileSha256, "a".repeat(64));
  assert.equal(
    toBridgePayload(IPC_CHANNELS.REPORT_GENERATE, payload)
      .document_structure_summary.document_snapshot_id,
    "selection:document-1",
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.REPORT_GENERATE, payload).incident_ids,
    [incidentId],
  );

  const pipelineBound = {
    ...payload,
    pipelineJobId: "88888888-8888-4888-8888-888888888888",
    pipelineInputFingerprint: "b".repeat(64),
    pipelineProfileSha256: "c".repeat(64),
  };
  const pipelineBridgePayload = toBridgePayload(
    IPC_CHANNELS.REPORT_GENERATE,
    pipelineBound,
  );
  assert.equal(
    pipelineBridgePayload.pipeline_job_id,
    pipelineBound.pipelineJobId,
  );
  assert.equal(
    pipelineBridgePayload.pipeline_input_fingerprint,
    "b".repeat(64),
  );

  const duplicate = structuredClone(payload);
  duplicate.incidentIds.push(incidentId);
  assert.throws(
    () => validateIpcPayload(IPC_CHANNELS.REPORT_GENERATE, duplicate),
    IpcValidationError,
  );

  const clientIncidentContent = structuredClone(payload);
  clientIncidentContent.approvedIncidents = [
    {
      incidentId,
      severity: "urgent",
      evidenceRefs: ["analysis:C:\\secret\\frame.jpg"],
    },
  ];
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.REPORT_GENERATE,
        clientIncidentContent,
      ),
    IpcValidationError,
  );

  const badDigest = structuredClone(payload);
  badDigest.sourceFileSha256 = "A".repeat(64);
  assert.throws(
    () => validateIpcPayload(IPC_CHANNELS.REPORT_GENERATE, badDigest),
    IpcValidationError,
  );

  const partialPipelineBinding = {
    ...payload,
    pipelineJobId: pipelineBound.pipelineJobId,
  };
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.REPORT_GENERATE,
        partialPipelineBinding,
      ),
    IpcValidationError,
  );
});

test("candidate answers enforce project identity and positive label details", () => {
  const payload = {
    projectId: PROJECT_ID,
    jobId: "88888888-8888-4888-8888-888888888888",
    candidateId: "0123456789abcdefabcd",
    answer: "edit",
    selectedIssueType: "road_debris",
    severity: "urgent",
    maintenanceAction: "긴급 제거",
    normalizedBox: { x: 0.2, y: 0.3, width: 0.25, height: 0.2 },
  };
  const validated = validateIpcPayload(
    IPC_CHANNELS.CANDIDATE_ANSWER,
    payload,
  );
  assert.equal(validated.selectedIssueType, "road_debris");
  assert.equal(
    toBridgePayload(IPC_CHANNELS.CANDIDATE_ANSWER, payload)
      .normalized_box.width,
    0.25,
  );

  const missingType = structuredClone(payload);
  delete missingType.selectedIssueType;
  assert.throws(
    () => validateIpcPayload(IPC_CHANNELS.CANDIDATE_ANSWER, missingType),
    IpcValidationError,
  );

  const negativeWithDetails = {
    ...payload,
    answer: "no",
  };
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.CANDIDATE_ANSWER,
        negativeWithDetails,
      ),
    IpcValidationError,
  );
});

test("auto-label commands are bounded and runs require explicit confirmation", () => {
  const compiled = toBridgePayload(IPC_CHANNELS.AUTO_LABEL_COMPILE, {
    projectId: PROJECT_ID,
    command: "표지판을 가린 수풀을 찾아줘",
  });
  assert.deepEqual(compiled, {
    project_id: PROJECT_ID,
    command: "표지판을 가린 수풀을 찾아줘",
  });

  const run = toBridgePayload(IPC_CHANNELS.AUTO_LABEL_RUN, {
    projectId: PROJECT_ID,
    jobId: "88888888-8888-4888-8888-888888888888",
    recipeId: "a".repeat(64),
    command: "표지판을 가린 수풀을 찾아줘",
    issueType: "sign_vegetation_occlusion",
    scope: "project",
    mode: "assisted_auto",
    userConfirmed: true,
  });
  assert.equal(run.recipe_id, "a".repeat(64));
  assert.equal(run.issue_type, "sign_vegetation_occlusion");
  assert.equal(run.user_confirmed, true);

  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.AUTO_LABEL_RUN, {
        ...run,
        projectId: PROJECT_ID,
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.AUTO_LABEL_RUN, {
        projectId: PROJECT_ID,
        recipeId: "A".repeat(64),
        command: "포트홀 찾아줘",
        issueType: "pothole",
        scope: "current_job",
        mode: "proposal_only",
        userConfirmed: false,
      }),
    IpcValidationError,
  );
});

test("answer and auto-label IPC share the complete canonical twelve-type contract", () => {
  const issueTypes = [
    "pothole",
    "road_surface_damage",
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "lane_marking_faded",
    "colored_guidance_faded",
  ];
  for (const issueType of issueTypes) {
    const answer = toBridgePayload(IPC_CHANNELS.CANDIDATE_ANSWER, {
      projectId: PROJECT_ID,
      jobId: "88888888-8888-4888-8888-888888888888",
      candidateId: "0123456789abcdefabcd",
      answer: "yes",
      selectedIssueType: issueType,
    });
    assert.equal(answer.selected_issue_type, issueType);

    const compile = toBridgePayload(IPC_CHANNELS.AUTO_LABEL_COMPILE, {
      projectId: PROJECT_ID,
      command: "도로 유지관리 검토 후보를 찾아줘",
      issueType,
    });
    assert.equal(compile.issue_type, issueType);

    const run = toBridgePayload(IPC_CHANNELS.AUTO_LABEL_RUN, {
      projectId: PROJECT_ID,
      recipeId: "a".repeat(64),
      command: "도로 유지관리 검토 후보를 찾아줘",
      issueType,
      scope: "project",
      mode: "proposal_only",
      userConfirmed: true,
    });
    assert.equal(run.issue_type, issueType);
  }

  for (const [legacy, canonical] of [
    ["sign_identity", "guide_sign_identity"],
    ["color_guidance_line_faded", "colored_guidance_faded"],
  ]) {
    const compile = toBridgePayload(IPC_CHANNELS.AUTO_LABEL_COMPILE, {
      projectId: PROJECT_ID,
      command: "호환 입력을 정규화해줘",
      issueType: legacy,
    });
    assert.equal(compile.issue_type, canonical);
    assert.notEqual(compile.issue_type, legacy);
  }
});

test("normalized label stats and dataset snapshots remain project scoped", () => {
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.LABEL_STATS, {
      projectId: PROJECT_ID,
    }),
    { projectId: PROJECT_ID },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.DATASET_CREATE, {
      projectId: PROJECT_ID,
      userConfirmed: true,
    }),
    { project_id: PROJECT_ID, user_confirmed: true },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.DATASET_CREATE, {
        projectId: PROJECT_ID,
        userConfirmed: false,
      }),
    IpcValidationError,
  );
});

test("bridge payload conversion is snake_case and export names are allowlisted", () => {
  const payload = toBridgePayload(IPC_CHANNELS.QUESTION_ANSWER, {
    projectId: PROJECT_ID,
    questionId: QUESTION_ID,
    answer: "yes",
    selectedSeverity: "major",
    includeInTraining: true,
  });
  assert.deepEqual(payload, {
    project_id: PROJECT_ID,
    question_id: QUESTION_ID,
    answer: "yes",
    selected_severity: "major",
    include_in_training: true,
  });

  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.EXPORT_CREATE, {
        projectId: PROJECT_ID,
        analysisJobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        artifacts: ["raw_detections", "arbitrary_file"],
      }),
    IpcValidationError,
  );

  const opaqueExport = toBridgePayload(IPC_CHANNELS.EXPORT_CREATE, {
    projectId: PROJECT_ID,
    analysisJobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    artifacts: ["raw_detections", "manifest"],
    destinationSelectionId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  });
  assert.deepEqual(opaqueExport, {
    project_id: PROJECT_ID,
    analysis_job_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    artifacts: ["raw_detections", "manifest"],
    destination_selection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  });
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.EXPORT_CREATE, {
        projectId: PROJECT_ID,
        analysisJobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        artifacts: ["raw_detections", "manifest"],
        destinationPath: "C:\\Users\\person\\Desktop\\exports",
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.EXPORT_CREATE, {
        projectId: PROJECT_ID,
        analysisJobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        artifacts: ["raw_detections", "manifest"],
        destination_path: "C:\\Users\\person\\Desktop\\exports",
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.EXPORT_CREATE, {
        projectId: PROJECT_ID,
        analysisJobId: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        artifacts: ["raw_detections", "manifest"],
        timeoutMs: 30 * 60 * 1000,
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.EXPORT_CREATE, {
        projectId: PROJECT_ID,
        artifacts: ["raw_detections", "manifest"],
      }),
    IpcValidationError,
  );
});
