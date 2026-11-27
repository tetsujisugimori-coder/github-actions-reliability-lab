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


def checked_rows(response, key):
    rows = response[key]
    if response["total_count"] != len(rows) or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("incomplete or inconsistent " + key + " listing")
    return rows


VALIDATE = "Validate"
A = "A Prepare and Deploy"
B = "B Prepare and Deploy"
C_PREPARE = "C Prepare Pages"
C = "C Deploy"
ALL_JOBS = frozenset({VALIDATE, A, B, C_PREPARE, C})
RECOVERY = {
    1: ALL_JOBS, 2: frozenset({A}), 3: frozenset({B}),
    4: frozenset({C}), 5: frozenset({C_PREPARE, C}), 6: ALL_JOBS,
}
ROLES = {"validated": VALIDATE, "a": A, "b": B, "c": C_PREPARE}
PAGES_PRODUCERS = {A: "a", B: "b", C_PREPARE: "c", C: "c"}


def contract():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    label = event["label"]["name"]
    plans = {"run-rerun-experiment": RECOVERY, "run-rerun-baseline": {1: ALL_JOBS}}
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    job_name = os.environ["LAB_JOB"]
    head = event["pull_request"]["head"]["sha"]
    plan = plans.get(label)
    if run_id <= 0 or plan is None or attempt not in plan or job_name not in plan[attempt]:
        raise ValueError("unknown execution plan or job not scheduled")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid pull request head")
    return run_id, attempt, head, plan, job_name


def attempt_jobs(run_id, attempt, head, plan, *, completed=True):
    expected = plan.get(attempt)
    if not expected:
        raise ValueError("unknown attempt membership")
    endpoint = f"/actions/runs/{run_id}/attempts/{attempt}"
    receipt = get(endpoint)
    if (receipt["id"] != run_id or receipt.get("run_attempt") != attempt or receipt.get("head_sha") != head
            or (completed and receipt.get("status") != "completed")):
        raise ValueError("attempt receipt does not match run and attempt")
    rows = checked_rows(get(endpoint + "/jobs?per_page=100"), "jobs")
    jobs = {row["name"]: row for row in rows}
    if set(jobs) != expected or len(jobs) != len(rows):
        raise ValueError("unexpected, missing, or ambiguous job in attempt")
    for row in rows:
        if (type(row["id"]) is not int or row["id"] <= 0
                or row["run_id"] != run_id or row.get("run_attempt", attempt) != attempt
                or row.get("head_sha") != head
                or (completed and row["status"] != "completed")):
            raise ValueError("job identity or completion is inconsistent")
    return jobs


def step(job, name, conclusions):
    matches = [row for row in job["steps"] if row["name"] == name]
    if (len(matches) != 1 or matches[0]["status"] != "completed"
            or matches[0]["conclusion"] not in conclusions):
        raise ValueError("step evidence is missing or inconsistent: " + name)
    return matches[0]["conclusion"]


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
    run_id, attempt, head, plan, job_name = contract()
    if prefix not in {"BUILD", "PAGES"}:
        raise ValueError("unknown artifact prefix")
    name, artifact_id, digest = [os.environ.get(prefix + part, "") for part in ("_NAME", "_ID", "_DIGEST")]
    match = re.fullmatch(fr"lab-(validated|a|b|c)-{run_id}-([1-9][0-9]*)", name)
    role = "validated" if prefix == "BUILD" else PAGES_PRODUCERS.get(job_name)
    if not match or not role or match[1] != role or not re.fullmatch(r"[1-9][0-9]*", artifact_id):
        raise ValueError("invalid artifact identity or producer role")
    # Only the immediate producer records a newly uploaded Pages archive without
    # an independently supplied digest. Every consumer must have one.
    producer_attempt = int(match[2])
    recording = (prefix == "PAGES" and ROLES[role] == job_name and producer_attempt == attempt
                 and os.environ.get("LAB_RECORD_UPLOAD") == "1")
    if not recording and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("expected artifact digest is missing or invalid")
    eligible = [number for number in plan if number <= attempt and ROLES[role] in plan[number]]
    if not eligible or producer_attempt != max(eligible):
        raise ValueError("artifact is not from the expected producer attempt")
    jobs = attempt_jobs(run_id, producer_attempt, head, plan, completed=producer_attempt < attempt)
    producer = jobs[ROLES[role]]
    if not recording:
        if producer["status"] != "completed" or producer["conclusion"] != "success":
            raise ValueError("artifact producer job did not succeed")
        upload_step = "Upload validated archive" if role == "validated" else "Prepare Pages archive"
        step(producer, upload_step, {"success"})
    artifacts = checked_rows(get(f"/actions/runs/{run_id}/artifacts?per_page=100"), "artifacts")
    print(json.dumps({"attempt": attempt, "rest_artifacts": [
        {"id": row["id"], "name": row["name"], "digest": row.get("digest"), "expired": row["expired"]}
        for row in artifacts
    ]}))
    selected = select(artifacts, {"name": name, "id": int(artifact_id), "digest": digest})
    detail = get(f"/actions/artifacts/{artifact_id}")
    if (detail["id"] != int(artifact_id) or detail["digest"] != selected["digest"]
            or detail["name"] != name or detail["expired"]):
        raise ValueError("artifact listing and ID metadata disagree")
    producer_run = detail["workflow_run"]
    if producer_run["id"] != run_id or producer_run["head_sha"] != head:
        raise ValueError("artifact belongs to a different run or head")
    evidence = {"name": name, "id": int(artifact_id), "digest": selected["digest"],
                "producer_attempt": producer_attempt, "producer_job_id": producer["id"],
                "producer_job": producer["name"], "head": head, "run_id": run_id}
    print(json.dumps(evidence))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print("digest=" + selected["digest"], file=output)
    return evidence


def check_archive(directory, digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("expected archive digest is missing or invalid")
    entries = list(Path(directory).iterdir())
    if len(entries) != 1 or not entries[0].is_file() or entries[0].is_symlink():
        raise ValueError("expected exactly one regular archive file")
    with entries[0].open("rb") as archive:
        observed = "sha256:" + hashlib.file_digest(archive, "sha256").hexdigest()
    if observed != digest:
        raise ValueError("archive bytes differ from expected SHA-256")
    print(json.dumps({"archive_sha256": observed, "bytes": entries[0].stat().st_size}))


def history():
    run_id, attempt, head, plan, job_name = contract()
    if job_name not in {A, B, C}:
        raise ValueError("not a publication job")
    entered = False
    current = attempt_jobs(run_id, attempt, head, plan, completed=False)
    job_ids = {job["id"] for job in current.values()}
    for previous in range(1, attempt):
        jobs = attempt_jobs(run_id, previous, head, plan)
        ids = {row["id"] for row in jobs.values()}
        if job_ids & ids:
            raise ValueError("job ID unexpectedly reused across attempts")
        job_ids |= ids
        # Membership was verified on the full attempt, not inferred from
        # absence of the target row. Never ignore an incomplete listing.
        if job_name not in plan[previous]:
            print(json.dumps({"history_attempt": previous, "publication": "NOT_SCHEDULED"}))
            continue
        job = jobs[job_name]
        conclusion = step(job, "Simulate publication", {"skipped", "success"})
        if previous == 1:
            if job.get("conclusion") != "failure":
                raise ValueError("initial publication job did not fail as planned")
            step(job, "Inject pre-send failure in attempt 1", {"failure"})
            if conclusion != "skipped":
                raise ValueError("initial mock publication contradicts injected failure")
        elif entered:
            if job.get("conclusion") != "success":
                raise ValueError("repeat-stop job did not complete successfully")
            step(job, "Report stopped repeat", {"success"})
            if conclusion != "skipped":
                raise ValueError("mock publication was repeated after success")
        elif conclusion == "skipped":
            raise ValueError("mock publication skipped without prior success")
        elif job.get("conclusion") != "success":
            raise ValueError("mock publication succeeded in an inconsistent job")
        print(json.dumps({"history_attempt": previous, "job_id": job["id"],
                          "job_started": job["started_at"], "publication": conclusion}))
        # Success evidence can only block; it never authorizes a retry.
        entered |= conclusion == "success"
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        print("entered=" + str(entered).lower(), file=output)
    return entered


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
    assert checked_rows({"total_count": 1, "artifacts": [good]}, "artifacts") == [good]
    for response in ({"total_count": 2, "artifacts": [good]},
                     {"total_count": 2, "artifacts": [good, good]}):
        try:
            checked_rows(response, "artifacts")
        except ValueError:
            continue
        raise AssertionError("unsafe listing was accepted")
    print("8 negative metadata and listing cases stopped")


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "check":
        check(sys.argv[2])
    elif action == "archive":
        check_archive(sys.argv[2], os.environ["LAB_ARCHIVE_DIGEST"])
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
