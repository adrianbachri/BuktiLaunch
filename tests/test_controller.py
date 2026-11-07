#!/usr/bin/env python3
"""Test controller putaran BuktiLaunch (P-06 / P-12 sebagian).

Fokus: batas 3 putaran dan integritas penghitung benar-benar ditegakkan kode,
bukan prompt. Semua test memanggil `runs/controller.py` lewat subprocess, jadi
yang diuji adalah perilaku CLI yang nyata — bukan fungsi internal yang dimock.

Sejak controller v2 (schema STATE-01 v2), satu putaran hanya sah bila:
  begin-round -> record-artifact draft -> dispatch reviewer -> submit-review
dengan file review yang memuat sha256 draft tersebut. Helper `play_round`
di bawah melakukan urutan itu. Gerbang approval diuji terpisah di
`tests/test_gate.py`.

Tanpa dependency eksternal: unittest dari pustaka standar.
Jalankan:  python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER = os.path.join(REPO_ROOT, "runs", "controller.py")
PY = sys.executable

# Direktori kerja test dibuat di dalam repo: direktori temp sistem di mesin ini
# tidak selalu tersedia (lihat BUILD-STATUS.md §C catatan lingkungan).
TMP_BASE = os.path.join(REPO_ROOT, "tests", "tmp")


def new_workdir(prefix):
    os.makedirs(TMP_BASE, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=TMP_BASE)


def run(*args, expect=None):
    """Panggil controller. Kembalikan (exit_code, parsed_json_or_none, stdout, stderr)."""
    proc = subprocess.run(
        [PY, CONTROLLER, *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"exit {proc.returncode} != {expect} untuk {args}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc.returncode, payload, proc.stdout, proc.stderr


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class ControllerTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = new_workdir("bl-ctl-test-")
        if os.environ.get("BUKTILAUNCH_KEEP_TEST_ARTIFACTS") != "1":
            self.addCleanup(shutil.rmtree, self.dir, True)

    def init(self, campaign="T-TEST", max_rounds=3, force=False, expect=None):
        args = ["init", "--dir", self.dir, "--campaign", campaign,
                "--max-rounds", str(max_rounds)]
        if force:
            args.append("--force")
        return run(*args, expect=expect)

    def state(self):
        with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, rel, text):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        return full

    def play_round(self, verdict="REVISE", severity=None, review=None):
        """begin-round -> draft -> dispatch -> review -> submit; kembalikan payload submit."""
        if not os.path.exists(os.path.join(self.dir, "state.json")):
            self.init(expect=0)
        run("begin-round", "--dir", self.dir, "--actor", "planner", expect=0)
        rnd = self.state()["round"]
        draft_rel = f"drafts/draft-v{rnd}.md"
        self.write(draft_rel, f"# draft putaran {rnd}\n\nIsi uji.\n")
        run("record-artifact", "--dir", self.dir, "--kind", "draft",
            "--path", draft_rel, "--actor", "writer", expect=0)
        _, disp, _, _ = run("dispatch", "--dir", self.dir, "--role", "reviewer",
                            "--actor", "planner", expect=0)

        if severity is None:
            severity = (1, 0, 0, 0) if verdict in ("REVISE", "REJECT") else (0, 0, 0, 1)
        review_rel = review or f"reviews/review-v{rnd}.json"
        self.write(review_rel, json.dumps({
            "review_id": f"REVIEW-T-{rnd}",
            "campaign_id": "T-TEST",
            "round": rnd,
            "reviewer": "proof-reviewer (TEST)",
            "draft": draft_rel,
            "draft_sha256": sha256_file(os.path.join(self.dir, draft_rel)),
            "severity_count": {"S1": severity[0], "S2": severity[1],
                               "S3": severity[2], "S4": severity[3]},
            "issues": [],
            "verdict": verdict,
            "human_approval": "not_granted",
            "publication": "none",
        }, indent=2))
        _, payload, _, _ = run("submit-review", "--dir", self.dir,
                               "--verdict", verdict, "--review-path", review_rel,
                               "--dispatch-token", disp["dispatch_token"],
                               "--actor", "reviewer", expect=0)
        return payload


class TestStateLifecycle(ControllerTestBase):
    def test_init_creates_state_with_round_zero(self):
        code, payload, _, _ = self.init(expect=0)
        self.assertEqual(payload["round"], 0)
        st = self.state()
        self.assertEqual(st["schema"], "STATE-01")
        self.assertEqual(st["schema_version"], 2)
        self.assertEqual(st["status"], "INIT")
        self.assertEqual(st["round"], 0)
        self.assertEqual(st["max_rounds"], 3)
        self.assertEqual(st["campaign_id"], "T-TEST")
        for field in ("reviews", "dispatches", "approvals", "invalidations"):
            self.assertEqual(st[field], [])

    def test_init_refuses_overwrite_without_force(self):
        self.init(expect=0)
        code, payload, _, _ = self.init()
        self.assertEqual(code, 2)
        self.assertTrue(payload["refused"])
        self.assertEqual(payload["code"], "R-STATE-02")

    def test_begin_round_increments_and_sets_drafted(self):
        self.init(expect=0)
        code, payload, _, _ = run("begin-round", "--dir", self.dir,
                                  "--actor", "planner", expect=0)
        self.assertEqual(payload["round"], 1)
        self.assertEqual(payload["status"], "DRAFTED")
        self.assertEqual(self.state()["round"], 1)

    def test_illegal_transition_refused(self):
        self.init(expect=0)
        run("begin-round", "--dir", self.dir, "--actor", "planner", expect=0)
        code, payload, _, _ = run("set-status", "--dir", self.dir,
                                  "--status", "PLANNED", "--actor", "planner")
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-STATE-04")
        self.assertEqual(self.state()["status"], "DRAFTED")

    def test_review_requires_drafted_status(self):
        self.init(expect=0)
        # belum begin-round -> status INIT, review harus ditolak
        code, payload, _, _ = run("submit-review", "--dir", self.dir,
                                  "--verdict", "REVISE",
                                  "--review-path", "x.json", "--actor", "reviewer")
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-STATE-05")


class TestRoundLimit(ControllerTestBase):
    """Inti P-06: maksimum 3 putaran ditegakkan kode."""

    def test_three_rounds_allowed(self):
        self.init(expect=0)
        for expected in (1, 2):
            self.play_round("REVISE")
            self.assertEqual(self.state()["round"], expected)
        payload = self.play_round("PASS_WITH_NOTES")
        self.assertEqual(payload["round"], 3)
        self.assertEqual(self.state()["round"], 3)
        self.assertEqual(self.state()["status"], "READY_FOR_APPROVAL")

    def test_third_round_revise_escalates_immediately(self):
        self.init(expect=0)
        for _ in range(2):
            self.play_round("REVISE")
        payload = self.play_round("REVISE")
        self.assertTrue(payload["escalated"])

        st = self.state()
        self.assertEqual(st["round"], 3, "round tidak boleh naik ke 4")
        self.assertEqual(st["status"], "ESCALATED")
        self.assertIn("R-ROUND-03", [v["code"] for v in st["violations"]])
        self.assertEqual(st["history"][-1]["event"], "escalate-round-limit")

    def test_escalated_is_terminal(self):
        self.init(expect=0)
        for _ in range(3):
            self.play_round("REVISE")
        self.assertEqual(self.state()["status"], "ESCALATED")

        for args in (
            ("begin-round",),
            ("set-status", "--status", "DRAFTED"),
            ("dispatch", "--role", "reviewer"),
        ):
            code, payload, _, _ = run(*args, "--dir", self.dir, "--actor", "planner")
            self.assertEqual(code, 2)
            self.assertEqual(payload["code"], "R-STATE-01")
        self.assertEqual(self.state()["status"], "ESCALATED")

    def test_repeated_attempt_does_not_advance(self):
        self.init(expect=0)
        for _ in range(3):
            self.play_round("REVISE")
        for _ in range(3):
            code, _, _, _ = run("begin-round", "--dir", self.dir, "--actor", "planner")
            self.assertEqual(code, 2)
        self.assertEqual(self.state()["round"], 3)

    def test_pass_stops_loop_early(self):
        self.init(expect=0)
        self.play_round("PASS_WITH_NOTES")
        self.assertEqual(self.state()["status"], "READY_FOR_APPROVAL")
        self.assertEqual(self.state()["round"], 1)
        # loop selesai: tidak ada putaran baru
        code, _, _, _ = run("begin-round", "--dir", self.dir, "--actor", "planner")
        self.assertEqual(code, 2)


class TestCounterIntegrity(ControllerTestBase):
    """Planner/Reviewer tidak bisa menurunkan atau mereset penghitung."""

    def test_round_limit_cannot_exceed_three(self):
        code, payload, _, _ = self.init(max_rounds=4)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-ROUND-04")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "state.json")))

    def test_round_limit_must_be_positive(self):
        for limit in (0, -1):
            with self.subTest(limit=limit):
                code, payload, _, _ = self.init(max_rounds=limit)
                self.assertEqual(code, 2)
                self.assertEqual(payload["code"], "R-ROUND-04")

    def test_force_init_cannot_reset_existing_campaign(self):
        self._reach_round_two()
        code, payload, _, _ = self.init(force=True)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-STATE-02")
        self.assertEqual(self.state()["round"], 2)

    def test_invalid_persisted_limit_is_refused_without_change(self):
        self.init(expect=0)
        state = self.state()
        state["max_rounds"] = 4
        raw = json.dumps(state)
        self.write("state.json", raw)
        code, _, _, stderr = run("begin-round", "--dir", self.dir)
        self.assertEqual(code, 1)
        self.assertIn("max_rounds", stderr)
        with open(os.path.join(self.dir, "state.json")) as fh:
            self.assertEqual(fh.read(), raw)

    def test_shorter_limit_escalates_on_first_revise(self):
        self.init(max_rounds=1, expect=0)
        self.play_round("REVISE")
        self.assertEqual(self.state()["round"], 1)
        self.assertEqual(self.state()["status"], "ESCALATED")

    def _reach_round_two(self):
        self.init(expect=0)
        self.play_round("REVISE")
        run("begin-round", "--dir", self.dir, "--actor", "planner", expect=0)
        self.assertEqual(self.state()["round"], 2)

    def test_lowering_round_refused(self):
        self._reach_round_two()
        code, payload, _, _ = run("set-round", "--dir", self.dir,
                                  "--round", "1", "--actor", "planner")
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-ROUND-01")
        self.assertEqual(self.state()["round"], 2)
        self.assertEqual(self.state()["violations"][-1]["code"], "R-ROUND-01")

    def test_jumping_round_forward_refused(self):
        self._reach_round_two()
        code, payload, _, _ = run("set-round", "--dir", self.dir,
                                  "--round", "3", "--actor", "planner")
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-ROUND-01")
        self.assertEqual(self.state()["round"], 2)

    def test_reset_refused_even_from_reviewer(self):
        self._reach_round_two()
        code, payload, _, _ = run("reset", "--dir", self.dir,
                                  "--actor", "reviewer",
                                  "--reason", "anggap ini putaran 1 lagi")
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-ROUND-01")
        self.assertEqual(payload["round"], 2)
        st = self.state()
        self.assertEqual(st["round"], 2)
        self.assertEqual(st["violations"][-1]["actor"], "reviewer")


class TestArtifactHash(ControllerTestBase):
    def test_hash_matches_independent_sha256(self):
        self.init(expect=0)
        run("begin-round", "--dir", self.dir, "--actor", "planner", expect=0)
        os.makedirs(os.path.join(self.dir, "drafts"), exist_ok=True)
        target = os.path.join(self.dir, "drafts", "draft-v1.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("# Draft v1\n\nKlaim uji.\n")

        code, payload, _, _ = run("record-artifact", "--dir", self.dir,
                                  "--kind", "draft", "--path", "drafts/draft-v1.md",
                                  "--actor", "writer", expect=0)
        expected = sha256_file(target)
        self.assertEqual(payload["sha256"], expected)
        self.assertEqual(payload["bytes"], os.path.getsize(target))

        st = self.state()
        self.assertEqual(len(st["artifacts"]), 1)
        self.assertEqual(st["artifacts"][0]["sha256"], expected)
        self.assertEqual(st["artifacts"][0]["round"], 1)
        self.assertEqual(st["artifacts"][0]["kind"], "draft")

    def test_verify_passes_then_detects_tampering(self):
        self.init(expect=0)
        run("begin-round", "--dir", self.dir, "--actor", "planner", expect=0)
        target = os.path.join(self.dir, "artifact.md")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("isi awal\n")
        run("record-artifact", "--dir", self.dir, "--kind", "other",
            "--path", "artifact.md", "--actor", "writer", expect=0)

        code, payload, _, _ = run("verify", "--dir", self.dir, expect=0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mismatches"], [])

        with open(target, "a", encoding="utf-8") as fh:
            fh.write("tambahan tanpa izin\n")
        code, payload, _, _ = run("verify", "--dir", self.dir)
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["mismatches"][0]["problem"], "hash berubah")


class TestPersistence(ControllerTestBase):
    def test_state_survives_separate_processes(self):
        """round tersimpan di disk, bukan di konteks model."""
        self.init(expect=0)
        self.play_round("REVISE")
        run("begin-round", "--dir", self.dir, "--actor", "planner", expect=0)

        # proses baru membaca dari disk
        code, payload, stdout, _ = run("show", "--dir", self.dir, expect=0)
        st = json.loads(stdout)
        self.assertEqual(st["round"], 2)
        self.assertEqual(st["status"], "DRAFTED")

    def test_history_is_append_only_with_digests(self):
        self.init(expect=0)
        self.play_round("REVISE")
        st = self.state()
        seqs = [h["seq"] for h in st["history"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        for entry in st["history"]:
            self.assertEqual(len(entry["state_digest"]), 64)
            self.assertIn("at", entry)
            self.assertIn("actor", entry)


class TestSchemaMigration(ControllerTestBase):
    """State v1 lama tetap bisa dibaca tanpa ditulis ulang."""

    def test_v1_state_can_be_read_and_verified(self):
        self.init(expect=0)
        path = os.path.join(self.dir, "state.json")
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        for field in ("reviews", "dispatches", "approvals", "invalidations", "copy_exports"):
            st.pop(field, None)
        st["schema_version"] = 1
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=2)

        code, payload, _, _ = run("verify", "--dir", self.dir, expect=0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["approvals"], 0)
        # verify bersifat baca-saja: file tidak ditulis ulang
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["schema_version"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
