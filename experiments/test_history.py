"""Offline synthetic API responses. No network or Actions runner."""
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

import artifacts as app

A, B, C, P, V = ("A Prepare and Deploy", "B Prepare and Deploy", "C Deploy",
                  "C Prepare Pages", "Validate")
RUN, HEAD = 45321, "ab" * 20
# Independent oracle: do not derive expected membership from implementation.
MEMBERS = {1: (V, A, B, P, C), 2: (A,), 3: (B,), 4: (C,), 5: (P, C), 6: (V, A, B, P, C)}
def archive_bytes(text):
    buf = io.BytesIO()
    with ZipFile(buf, "w") as archive:
        archive.writestr(ZipInfo("site.txt"), text)
    return buf.getvalue()


DATA = {"validated": archive_bytes("synthetic validated file"), "c": archive_bytes("synthetic Pages payload")}


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def evidence(role, ident):
    return {"id": ident, "name": f"lab-{role}-{RUN}-1", "digest": sha(DATA[role]),
            "expired": False, "workflow_run": {"id": RUN, "head_sha": HEAD}}


def listings():
    endpoints = {}
    for number, names in MEMBERS.items():
        url = f"/actions/runs/{RUN}/attempts/{number}"
        endpoints[url] = {"id": RUN, "run_attempt": number, "head_sha": HEAD, "status": "completed"}
        rows = []
        for i, job in enumerate(names, 1):
            upload = "Upload validated archive" if job == V else "Prepare Pages archive"
            rows.append({"id": number * 100 + i, "run_id": RUN, "run_attempt": number,
                         "name": job, "head_sha": HEAD, "status": "completed",
                         "conclusion": "failure" if number == 1 and job in {A, B, C} else "success",
                         "started_at": "2026-01-01T00:00:00Z", "steps": [
                             {"name": upload, "status": "completed", "conclusion": "success"},
                             {"name": "Inject pre-send failure in attempt 1", "status": "completed",
                              "conclusion": "failure" if number == 1 else "skipped"},
                             {"name": "Report stopped repeat", "status": "completed",
                              "conclusion": "success" if number == 5 else "skipped"},
                             {"name": "Simulate publication", "status": "completed",
                              "conclusion": "skipped" if number in (1, 5) else "success"},
                         ]})
        endpoints[url + "/jobs?per_page=100"] = {"total_count": len(rows), "jobs": rows}
    artifacts = [evidence("validated", 777), evidence("c", 888)]
    endpoints[f"/actions/runs/{RUN}/artifacts?per_page=100"] = {
        "total_count": len(artifacts), "artifacts": [row.copy() for row in artifacts]}
    for row in artifacts:
        endpoints[f"/actions/artifacts/{row['id']}"] = row.copy()
    return endpoints


class History(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.event = self.dir / "event.json"
        self.event.write_text(json.dumps({"label": {"name": "run-rerun-experiment"},
                                          "pull_request": {"head": {"sha": HEAD}}}))
        self.output = self.dir / "output"
        self.responses = listings()
        self.env = {"GITHUB_EVENT_PATH": str(self.event), "GITHUB_OUTPUT": str(self.output),
                    "GITHUB_RUN_ID": str(RUN)}
        self.capture = io.StringIO()
        self.enterContext(patch.dict(os.environ, self.env, clear=True))
        self.enterContext(patch.object(app, "get", side_effect=self.responses.__getitem__))
        self.enterContext(redirect_stdout(self.capture))

    def job(self, attempt, name):
        os.environ.update(GITHUB_RUN_ATTEMPT=str(attempt), LAB_JOB=name)

    def rows(self, i):
        return self.responses[f"/actions/runs/{RUN}/attempts/{i}/jobs?per_page=100"]

    def artifact(self, role, prefix, ident, expected_job):
        rec = evidence(role, ident)
        for key in ("name", "id", "digest"):
            os.environ[f"{prefix}_{key.upper()}"] = str(rec[key])
        got = app.check(prefix)
        self.assertEqual((got["producer_attempt"], got["producer_job_id"], got["head"],
                          got["producer_job"], got["id"], got["digest"]),
                         (1, expected_job, HEAD, V if role == "validated" else P, ident, rec["digest"]))
        archive = self.dir / "archive"
        archive.mkdir()
        (archive / "receipt.zip").write_bytes(DATA[role])
        app.check_archive(archive, rec["digest"])
        return archive, rec["digest"]

    def test_a2_inherits_validate(self):
        self.job(2, A)
        self.artifact("validated", "BUILD", 777, 101)
        self.assertFalse(app.history())
        self.assertIn("entered=false", self.output.read_text())

    def test_b3_inherits_validate_with_a_only_history(self):
        self.job(3, B)
        self.artifact("validated", "BUILD", 777, 101)
        self.assertFalse(app.history())
        self.assertIn('"publication": "NOT_SCHEDULED"', self.capture.getvalue())

    def test_c4_inherits_prepare_with_a_and_b_only_history(self):
        self.job(4, C)
        self.artifact("c", "PAGES", 888, 104)
        self.assertFalse(app.history())
        self.assertEqual(self.capture.getvalue().count('"publication": "NOT_SCHEDULED"'), 2)

    def test_required_job_missing(self):
        self.job(3, B)
        row = self.rows(1)
        row["jobs"] = [job for job in row["jobs"] if job["name"] != B]
        row["total_count"] -= 1
        with self.assertRaisesRegex(ValueError, "missing"):
            app.history()

    def test_wrong_membership_for_an_expected_omission(self):
        self.job(3, B)
        self.rows(2)["jobs"][0]["name"] = C
        with self.assertRaisesRegex(ValueError, "unexpected"):
            app.history()

    def test_incomplete_listing_cannot_prove_omission(self):
        self.job(3, B)
        self.rows(2)["total_count"] = 2
        with self.assertRaisesRegex(ValueError, "incomplete"):
            app.history()

    def test_wrong_attempt_receipt(self):
        self.job(3, B)
        self.responses[f"/actions/runs/{RUN}/attempts/2"]["run_attempt"] = 1
        with self.assertRaisesRegex(ValueError, "receipt"):
            app.history()

    def test_explicit_conflicting_job_attempt(self):
        self.job(3, B)
        self.rows(2)["jobs"][0]["run_attempt"] = 3
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            app.history()

    def test_duplicate_job_id_across_attempts(self):
        self.job(3, B)
        self.rows(2)["jobs"][0]["id"] = 101
        with self.assertRaisesRegex(ValueError, "reused"):
            app.history()

    def test_conflicting_publication_steps(self):
        self.job(3, B)
        row = self.rows(1)["jobs"][2]
        row["steps"].append({"name": "Simulate publication", "status": "completed", "conclusion": "success"})
        with self.assertRaisesRegex(ValueError, "step evidence"):
            app.history()

    def test_unknown_plan(self):
        self.job(7, A)
        with self.assertRaisesRegex(ValueError, "unknown execution plan"):
            app.history()

    def test_unplanned_current_job(self):
        self.job(2, B)
        with self.assertRaisesRegex(ValueError, "not scheduled"):
            app.history()

    def test_already_published_c5_and_c6(self):
        for attempt in (5, 6):
            with self.subTest(attempt=attempt):
                self.job(attempt, C)
                self.assertTrue(app.history())
                self.assertIn("entered=true", self.output.read_text())

    def test_already_published_a_b_at_6(self):
        for name in (A, B):
            with self.subTest(job=name):
                self.job(6, name)
                self.assertTrue(app.history())

    def test_artifact_bad_id_digest_head_and_producer(self):
        self.job(4, C)
        rec = evidence("c", 888)
        os.environ.update(PAGES_NAME=rec["name"], PAGES_ID="888", PAGES_DIGEST=rec["digest"])
        row = self.responses["/actions/artifacts/888"]
        for key, bad in {"id": 889, "digest": "sha256:" + "0" * 64,
                         "workflow_run": {"id": RUN, "head_sha": "de" * 20}}.items():
            with self.subTest(field=key):
                before = row[key]
                row[key] = bad
                with self.assertRaises(ValueError):
                    app.check("PAGES")
                row[key] = before
        self.rows(1)["jobs"][3]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "producer job did not succeed"):
            app.check("PAGES")

    def test_prior_artifact_must_match_expected_attempt(self):
        self.job(4, C)
        os.environ.update(PAGES_NAME=f"lab-c-{RUN}-2", PAGES_ID="888", PAGES_DIGEST=sha(DATA["c"]))
        with self.assertRaisesRegex(ValueError, "expected producer attempt"):
            app.check("PAGES")

    def test_archive_bytes_mutation(self):
        self.job(4, C)
        archive, digest = self.artifact("c", "PAGES", 888, 104)
        (archive / "receipt.zip").write_bytes(b"changed bytes")
        with self.assertRaisesRegex(ValueError, "archive bytes differ"):
            app.check_archive(archive, digest)

    def test_consumer_must_have_digest(self):
        self.job(4, C)
        os.environ.update(PAGES_NAME=f"lab-c-{RUN}-1", PAGES_ID="888", PAGES_DIGEST="")
        with self.assertRaisesRegex(ValueError, "digest is missing"):
            app.check("PAGES")

    def test_current_membership_must_be_exact(self):
        self.job(2, A)
        self.rows(2)["jobs"][0]["name"] = B
        with self.assertRaisesRegex(ValueError, "unexpected"):
            app.history()

    def test_job_from_different_head(self):
        self.job(3, B)
        self.rows(1)["jobs"][2]["head_sha"] = "cd" * 20
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            app.history()

    def test_unrelated_initial_failure_does_not_prove_pre_send(self):
        self.job(3, B)
        self.rows(1)["jobs"][2]["steps"][1]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ValueError, "step evidence"):
            app.history()

    def test_later_skip_without_success_stops(self):
        self.job(6, A)
        self.rows(2)["jobs"][0]["steps"][3]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ValueError, "skipped without prior success"):
            app.history()

    def test_repeated_publication_after_success_conflicts(self):
        self.job(6, C)
        self.rows(5)["jobs"][1]["steps"][3]["conclusion"] = "success"
        with self.assertRaisesRegex(ValueError, "repeated after success"):
            app.history()

    def test_metadata_sample_without_row_level_attempt(self):
        self.job(3, B)
        del self.rows(2)["jobs"][0]["run_attempt"]
        self.assertFalse(app.history())


if __name__ == "__main__":
    unittest.main(verbosity=2)
