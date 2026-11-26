"""Inspect real Actions metadata and exercise a local publish substitute."""

import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path


def get(path):
    url = os.environ["GITHUB_API_URL"] + "/repos/" + os.environ["GITHUB_REPOSITORY"] + path
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=12) as response:
        if 'rel="next"' in response.headers.get("Link", ""):
            raise ValueError("unexpected pagination")
        return json.load(response)


def select(artifacts, identity):
    matches = [artifact for artifact in artifacts if artifact["name"] == identity["name"]]
    if len(matches) != 1:
        raise ValueError("artifact name is absent or ambiguous")
    artifact = matches[0]
    if artifact["id"] != identity["id"] or artifact["expired"]:
        raise ValueError("artifact ID differs or artifact is expired")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest") or ""):
        raise ValueError("artifact digest is unavailable")
    if identity.get("digest") and artifact["digest"] != identity["digest"]:
        raise ValueError("artifact digest differs")
    return artifact


def check(prefix):
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    name, artifact_id, digest = [os.environ.get(prefix + part, "") for part in ("_NAME", "_ID", "_DIGEST")]
    if not re.fullmatch(fr"lab-[a-z]+-{run_id}-[1-9][0-9]*", name):
        raise ValueError("invalid artifact identity")
    producer_attempt = int(name.rsplit("-", 1)[1])
    if not 1 <= producer_attempt <= attempt:
        raise ValueError("artifact was not produced by an eligible attempt")
    identity = {"name": name, "id": int(artifact_id), "digest": digest}
    artifacts = get(f"/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
    print(json.dumps({"attempt": attempt, "rest_artifacts": [
        {"id": row["id"], "name": row["name"], "digest": row.get("digest"), "expired": row["expired"]}
        for row in artifacts
    ]}))
    selected = select(artifacts, identity)
    detail = get(f"/actions/artifacts/{artifact_id}")
    if detail["digest"] != selected["digest"] or detail["name"] != name or detail["expired"]:
        raise ValueError("artifact listing and ID metadata disagree")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    producer_run = detail["workflow_run"]
    if producer_run["id"] != run_id or producer_run["head_sha"] != event["pull_request"]["head"]["sha"]:
        raise ValueError("artifact belongs to a different run or head")
    print(json.dumps({"verified": name, "id": int(artifact_id), "producer_attempt": producer_attempt}))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print("digest=" + selected["digest"], file=output)


def history():
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    job_name = os.environ["PUBLICATION_JOB"]
    if not 1 <= attempt <= 6:
        raise ValueError("attempt cap exceeded")
    entered = False
    for previous in range(1, attempt):
        jobs = get(f"/actions/runs/{run_id}/attempts/{previous}/jobs?per_page=100")["jobs"]
        matches = [job for job in jobs if job["name"] == job_name]
        if len(matches) != 1 or matches[0]["status"] != "completed":
            raise ValueError("prior publication job is missing, ambiguous or incomplete")
        job = matches[0]
        steps = [step for step in job["steps"] if step["name"] == "Simulate publication"]
        if len(steps) != 1 or steps[0]["conclusion"] not in {"skipped", "success"}:
            raise ValueError("prior publication entry is unknown")
        print(json.dumps({"history_attempt": previous, "job_id": job["id"],
                          "job_started": job["started_at"], "publication": steps[0]["conclusion"]}))
        entered |= steps[0]["conclusion"] == "success"
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print("entered=" + str(entered).lower(), file=output)


def negative():
    identity = {"id": 12, "name": "lab-pages-1-1", "digest": "sha256:" + "a" * 64}
    good = {**identity, "expired": False}
    assert select([good], identity) == good
    for rows in ([], [good, good], [{**good, "id": 15}], [{**good, "expired": True}],
                 [{**good, "digest": None}], [{**good, "digest": "sha256:" + "b" * 64}]):
        try:
            select(rows, identity)
        except ValueError:
            continue
        raise AssertionError("unsafe metadata was accepted")
    print("6 negative metadata cases stopped")


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "check":
        check(sys.argv[2])
    elif action == "history":
        history()
    elif action == "negative":
        negative()
    elif action == "validate":
        Path("payload").mkdir()
        Path("payload/site.txt").write_text("Synthetic content for the Actions Reliability Lab.\n")
        print(hashlib.sha256(Path("payload/site.txt").read_bytes()).hexdigest())
    else:
        raise ValueError("unknown experiment command")
