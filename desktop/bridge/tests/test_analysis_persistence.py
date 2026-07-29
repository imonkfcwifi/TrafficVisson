import json

from app.store import DEMO_PROJECT_ID
from test_analysis_jobs import (
    _auth,
    _client,
    _complete_analysis,
    _make_video,
)


def test_completed_analysis_and_evidence_survive_bridge_restart(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as first_client:
        terminal = _complete_analysis(first_client, source)

    job_id = terminal["job_id"]
    candidate_id = terminal["result"]["candidates"][0]["candidate_id"]
    expected_image_sha256 = terminal["result"]["candidates"][0][
        "representative_image"
    ]["sha256"]

    with _client(tmp_path, ffmpeg, ffprobe) as restarted_client:
        listed = restarted_client.get(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs?limit=10",
            headers=_auth(),
        )
        assert listed.status_code == 200, listed.text
        assert [item["job_id"] for item in listed.json()["jobs"]] == [job_id]

        restored = restarted_client.get(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}",
            headers=_auth(),
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "completed"
        assert restored.json()["result"] == terminal["result"]

        questions = restarted_client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                "/label-questions"
            ),
            headers=_auth(),
        )
        assert questions.status_code == 200, questions.text
        assert questions.json()["questions"][0]["candidate_id"] == candidate_id

        image = restarted_client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                f"/candidates/{candidate_id}/image"
            ),
            headers=_auth(),
        )
        assert image.status_code == 200, image.text
        assert image.headers["etag"].strip('"') == expected_image_sha256


def test_tampered_persisted_analysis_record_is_not_rehydrated(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as first_client:
        terminal = _complete_analysis(first_client, source)

    job_id = terminal["job_id"]
    record_path = (
        tmp_path
        / "bridge data"
        / "projects"
        / str(DEMO_PROJECT_ID)
        / "analysis-job-records"
        / f"{job_id}.json"
    )
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["result"]["candidates"][0]["representative_image"]["sha256"] = (
        "0" * 64
    )
    record_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with _client(tmp_path, ffmpeg, ffprobe) as restarted_client:
        hidden = restarted_client.get(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}",
            headers=_auth(),
        )
        assert hidden.status_code == 404
