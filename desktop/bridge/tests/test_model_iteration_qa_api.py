def test_project_model_iteration_ledger_is_project_scoped_and_empty(
    client,
    auth,
):
    created = client.post(
        "/projects",
        headers=auth,
        json={"name": "모델 반복 QA API"},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    response = client.get(
        f"/projects/{project_id}/model-iterations",
        headers=auth,
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "trafficvisson.model-iteration-ledger.v1",
        "project_id": project_id,
        "survey_qa_count": 0,
        "iteration_count": 0,
        "survey_qa": [],
        "iterations": [],
    }


def test_project_model_iteration_ledger_rejects_unknown_project(
    client,
    auth,
):
    response = client.get(
        (
            "/projects/"
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/"
            "model-iterations"
        ),
        headers=auth,
    )
    assert response.status_code == 404
