#!/usr/bin/env python3
"""Test ikatan review bundel (dua format) dan ikatan file siap-salin.

Latar: pada BL-DEMO-001 controller hanya mengikat SATU path per review. Dua
format (caption Instagram + copy landing page) dititipkan lewat bundel JSON,
tetapi controller tidak memeriksa isi bundel. Akibatnya satu format bisa
berubah setelah review tanpa ada yang menolak.

Yang dibuktikan di sini:
  1. review atas bundel memverifikasi SETIAP file: path, keberadaan, hash;
  2. satu format diubah / hilang / hash-nya salah -> review ditolak;
  3. approval menolak bila salah satu file yang direview berubah atau hilang;
  4. file siap-salin wajib diikat dan perubahannya membatalkan approval/gerbang;
  5. review satu file bentuk lama tetap didukung;
  6. sitasi faktual yang belum lengkap (S2) tidak bisa diberi PASS atau
     PASS_WITH_NOTES (rulebook v2 §0.2/§0.3, FACT-01).

SEMUA approval di suite ini berlabel TEST (`--test`) dan memakai data tiruan
bertanda TEST. Tidak ada persetujuan manusia, publikasi, atau pengiriman.

Jalankan:  python3 -m unittest discover -s tests -v
Tanpa dependency eksternal.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_gate import GateBase  # noqa: E402  (harness CLI yang sama)

CAPTION = (
    "# Caption Instagram (TEST)\n"
    "\n"
    "## Catatan perubahan\n"
    "- versi uji, bukan konten asli\n"
    "\n"
    "## Teks siap tempel\n"
    "Belajar Excel dari nol. Nama produk: Kursus Excel Dasar (TEST).\n"
)

LANDING = (
    "# Copy landing page (TEST)\n"
    "\n"
    "## Catatan perubahan\n"
    "- versi uji, bukan konten asli\n"
    "\n"
    "## Teks siap tempel\n"
    "Kursus Excel Dasar (TEST) — 12 video, akses seumur hidup.\n"
)


class BundleBase(GateBase):
    """Campaign TEST dengan dua format yang direview sebagai satu bundel."""

    def record_format(self, rel, text):
        self.write(rel, text)
        self.c(
            "record-artifact", "--kind", "draft", "--path", rel,
            "--actor", "writer", expect=0,
        )
        return rel

    def record_bundle(self, entries, tamper_index=None, rel=None):
        """Tulis review-unit.json dan catat PALING AKHIR agar jadi draft aktif."""
        files = []
        for role, path in entries:
            digest = self.sha(path)
            if tamper_index is not None and len(files) == tamper_index:
                digest = "0" * 64
            files.append(
                {
                    "role": role,
                    "path": path,
                    "sha256": digest,
                    "bytes": os.path.getsize(os.path.join(self.dir, path)),
                }
            )
        rel = rel or "drafts/round-1/review-unit.json"
        payload = {
            "review_unit_id": "T-BUNDLE-R1",
            "campaign": "T-GATE",
            "round": self.state()["round"],
            "status": "MOCK / FIKTIF — TEST",
            "files": files,
        }
        self.write(rel, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        self.c(
            "record-artifact", "--kind", "draft", "--path", rel,
            "--actor", "writer", expect=0,
        )
        return rel

    def two_formats(self):
        a = self.record_format("drafts/round-1/caption-ig.md", CAPTION)
        b = self.record_format("drafts/round-1/landing-copy.md", LANDING)
        return [("caption_instagram", a), ("landing_copy", b)]

    def start_bundle_round(self):
        self.planned()
        self.c("begin-round", "--actor", "planner", expect=0)
        entries = self.two_formats()
        return entries, self.record_bundle(entries)

    def bundle_review(self, bundle, verdict="PASS_WITH_NOTES", severity=(0, 0, 0, 0),
                      **overrides):
        return self.make_review(
            self.state()["round"], bundle, verdict=verdict, severity=severity,
            **overrides,
        )

    def submit_bundle(self, verdict, review_rel, expect=None):
        token = self.dispatch("reviewer")
        return self.submit(verdict, review_rel, token, expect=expect)

    def export_copy(self, source="drafts/round-1/caption-ig.md", out=None):
        args = ["export-copy", "--source", source]
        if out:
            args += ["--out", out]
        _, payload, _, _ = self.c(*args, expect=0)
        return payload["out"]


class TestBundleReviewVerification(BundleBase):
    """Syarat 1: setiap file yang dirujuk bundel diverifikasi saat review masuk."""

    def test_bundle_review_accepted_and_records_every_file(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "READY_FOR_APPROVAL")
        rec = self.state()["reviews"][-1]
        self.assertEqual(rec["draft_path"], bundle)
        paths = [e["path"] for e in rec["reviewed_files"]]
        self.assertEqual(
            sorted(paths),
            ["drafts/round-1/caption-ig.md", "drafts/round-1/landing-copy.md"],
        )
        for e in rec["reviewed_files"]:
            self.assertEqual(e["sha256"], self.sha(e["path"]))

    def test_bundle_review_refused_when_one_format_changed(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle)
        # Satu format diubah SETELAH bundel dicatat, SEBELUM review masuk.
        self.write("drafts/round-1/landing-copy.md", LANDING + "\nbaris tambahan (TEST)\n")
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-11")
        self.assertEqual(payload["detail"]["path"], "drafts/round-1/landing-copy.md")
        self.assertEqual(self.state()["status"], "DRAFTED")
        self.assertEqual(self.state()["reviews"], [])

    def test_bundle_review_refused_when_format_missing(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle)
        os.remove(os.path.join(self.dir, "drafts/round-1/caption-ig.md"))
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-11")
        self.assertIn("tidak ada", payload["message"])
        self.assertEqual(self.state()["status"], "DRAFTED")

    def test_bundle_review_refused_when_declared_hash_wrong(self):
        self.planned()
        self.c("begin-round", "--actor", "planner", expect=0)
        entries = self.two_formats()
        bundle = self.record_bundle(entries, tamper_index=1)
        rel = self.bundle_review(bundle)
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-11")
        self.assertEqual(payload["detail"]["path"], "drafts/round-1/landing-copy.md")
        self.assertEqual(payload["detail"]["declared"], "0" * 64)

    def test_bundle_review_refused_when_format_not_recorded(self):
        self.planned()
        self.c("begin-round", "--actor", "planner", expect=0)
        a = self.record_format("drafts/round-1/caption-ig.md", CAPTION)
        # File ada dan hash-nya benar, tetapi tidak pernah dicatat sebagai artefak.
        self.write("drafts/round-1/landing-copy.md", LANDING)
        bundle = self.record_bundle(
            [("caption_instagram", a), ("landing_copy", "drafts/round-1/landing-copy.md")]
        )
        rel = self.bundle_review(bundle)
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-12")
        self.assertEqual(payload["detail"]["path"], "drafts/round-1/landing-copy.md")

    def test_bundle_review_refused_when_reviewed_files_disagree(self):
        _, bundle = self.start_bundle_round()
        # Review menyebut daftar sendiri yang berbeda dari bundel.
        rel = self.bundle_review(
            bundle,
            drafts=[
                {"path": "drafts/round-1/caption-ig.md",
                 "sha256": self.sha("drafts/round-1/caption-ig.md")},
                {"path": "drafts/round-1/landing-copy.md", "sha256": "1" * 64},
            ],
        )
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-11")

    def test_single_file_review_still_supported(self):
        """Bentuk lama (satu draft, tanpa bundel) tidak boleh ikut rusak."""
        self.planned()
        self.c("begin-round", "--actor", "planner", expect=0)
        draft = self.record_draft()
        token = self.dispatch("reviewer")
        rel = self.make_review(1, draft, verdict="PASS_WITH_NOTES", severity=(0, 0, 0, 1))
        code, payload, _, _ = self.submit("PASS_WITH_NOTES", rel, token, expect=0)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "READY_FOR_APPROVAL")
        rec = self.state()["reviews"][-1]
        self.assertEqual(rec["reviewed_files"], [])

    def test_single_file_review_still_detects_changed_draft(self):
        self.planned()
        self.c("begin-round", "--actor", "planner", expect=0)
        draft = self.record_draft()
        token = self.dispatch("reviewer")
        rel = self.make_review(1, draft)
        self.write(draft, "# draft diubah setelah review dibuat (TEST)\n")
        code, payload, _, _ = self.submit("PASS_WITH_NOTES", rel, token, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-04")


class TestApprovalBindsEveryReviewedFile(BundleBase):
    """Syarat 2 & 3: approval dan gerbang memeriksa ulang semua file yang direview."""

    def ready_bundle(self, export=False):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        self.assertEqual(self.state()["status"], "READY_FOR_APPROVAL")
        copies = []
        if export:
            copies = [self.export_copy()]
        return bundle, copies

    def test_approval_refused_when_reviewed_format_changed(self):
        bundle, _ = self.ready_bundle()
        self.write("drafts/round-1/landing-copy.md", LANDING + "\nedit setelah review (TEST)\n")
        code, payload, _, _ = self.approve(expect=None, extra=["--draft", bundle])
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-APPROVAL-04")
        self.assertEqual(payload["detail"]["path"], "drafts/round-1/landing-copy.md")
        self.assertEqual(self.state()["status"], "READY_FOR_APPROVAL")
        self.assertEqual(self.state()["approvals"], [])

    def test_approval_refused_when_reviewed_format_deleted(self):
        bundle, _ = self.ready_bundle()
        os.remove(os.path.join(self.dir, "drafts/round-1/caption-ig.md"))
        code, payload, _, _ = self.approve(expect=None, extra=["--draft", bundle])
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-APPROVAL-04")
        self.assertIn("hilang", payload["message"])

    def test_approval_refuses_member_path_as_draft(self):
        """--draft harus path yang benar-benar direview, yaitu bundelnya."""
        bundle, _ = self.ready_bundle()
        code, payload, _, _ = self.approve(
            expect=None, extra=["--draft", "drafts/round-1/caption-ig.md"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-APPROVAL-04")
        self.assertIn("bundel", payload["message"])

    def test_approval_package_points_at_reviewed_bundle(self):
        bundle, _ = self.ready_bundle()
        code, pkg, _, _ = self.c(
            "approval-package",
            "--bind", "rules/rulebook.md",
            "--bind", "sources/product-sheet.md",
            expect=0,
        )
        self.assertEqual(code, 0)
        self.assertEqual(pkg["draft_path"], bundle)
        self.assertEqual(len(pkg["reviewed_files"]), 2)
        self.assertTrue(all(f["match"] for f in pkg["reviewed_files"]))
        self.assertIn(bundle, pkg["approval_command"])
        self.assertIn("--by", pkg["approval_command"])
        self.assertEqual(pkg["human_approval"], "not_granted")
        self.assertEqual(pkg["publication"], "none")

    def test_approval_package_flags_changed_format(self):
        bundle, _ = self.ready_bundle()
        self.write("drafts/round-1/caption-ig.md", CAPTION + "\nedit (TEST)\n")
        code, pkg, _, _ = self.c("approval-package", expect=None)
        self.assertEqual(code, 2)
        self.assertFalse(pkg["ok"])
        match = {f["path"]: f["match"] for f in pkg["reviewed_files"]}
        self.assertFalse(match["drafts/round-1/caption-ig.md"])

    def test_gate_and_recheck_refuse_after_reviewed_format_changes(self):
        bundle, _ = self.ready_bundle()
        self.approve(expect=0, extra=["--draft", bundle])
        self.assertEqual(self.state()["status"], "APPROVED")
        self.write("drafts/round-1/landing-copy.md", LANDING + "\nedit setelah approval (TEST)\n")

        code, payload, _, _ = self.c("check-gate", "--allow-test", expect=None)
        self.assertEqual(code, 2)
        codes = [p["code"] for p in payload["problems"]]
        self.assertIn("R-APPROVAL-01", codes)

        code, payload, _, _ = self.c("recheck", expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["invalidated"], 1)
        self.assertEqual(self.state()["status"], "REVIEWED")
        self.assertFalse(self.state()["approvals"][-1]["valid"])


class TestApprovalBindsCopyFiles(BundleBase):
    """Syarat 4: file siap-salin ikut diikat; perubahannya membatalkan approval."""

    def test_approval_requires_copy_binding_when_export_exists(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        self.export_copy()
        code, payload, _, _ = self.approve(expect=None, extra=["--draft", bundle])
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-APPROVAL-07")
        self.assertIn("copy/caption-ig.md.copy.txt", payload["detail"]["missing"])

    def test_approval_refused_when_copy_file_changed_after_export(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        out = self.export_copy()
        self.write(out, "teks siap-salin yang diubah (TEST)\n")
        code, payload, _, _ = self.approve(
            expect=None, extra=["--draft", bundle, "--copy", out]
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-APPROVAL-07")
        self.assertEqual(payload["detail"]["path"], out)

    def test_approval_records_copy_hash(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        out = self.export_copy()
        code, payload, _, _ = self.approve(
            expect=0, extra=["--draft", bundle, "--copy", out]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["copy_files"], 1)
        self.assertEqual(payload["reviewed_files"], 2)
        ap = self.state()["approvals"][-1]
        self.assertEqual(ap["copy_files"][0]["path"], out)
        self.assertEqual(ap["copy_files"][0]["sha256"], self.sha(out))
        self.assertEqual(len(ap["reviewed_files"]), 2)

    def test_gate_and_recheck_refuse_after_copy_file_changes(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        out = self.export_copy()
        self.approve(expect=0, extra=["--draft", bundle, "--copy", out])
        self.assertEqual(self.state()["status"], "APPROVED")
        self.c("check-gate", "--allow-test", expect=0)

        self.write(out, "teks siap-salin diubah setelah approval (TEST)\n")

        code, payload, _, _ = self.c("check-gate", "--allow-test", expect=None)
        self.assertEqual(code, 2)
        reasons = [p["reason"] for p in payload["problems"]]
        self.assertTrue(any("siap-salin" in r for r in reasons))

        code, payload, _, _ = self.c("recheck", expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(self.state()["status"], "REVIEWED")

    def test_clean_bundle_approval_passes_gate(self):
        """Kontrol positif: tanpa perubahan, gerbang lolos."""
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(bundle, severity=(0, 0, 0, 1))
        self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        out = self.export_copy()
        self.approve(expect=0, extra=["--draft", bundle, "--copy", out])
        code, payload, _, _ = self.c("check-gate", "--allow-test", expect=0)
        self.assertEqual(code, 0)
        self.assertEqual(payload["problems"], [])
        self.c("recheck", expect=0)


class TestCitationVerdictPrecondition(BundleBase):
    """Syarat 5: sitasi faktual tidak lengkap = S2, jadi PASS/PASS_WITH_NOTES batal.

    Controller tidak membaca prosa; ia menegakkan aritmetika severity terhadap
    verdict (rulebook v2 §0.2/§0.3). Karena itu Reviewer wajib menghitung S2
    untuk klaim tanpa sitasi lengkap — persis temuan R2-F01 pada BL-DEMO-001.
    """

    def issue_citation(self):
        return [
            {
                "id": "R2-F01",
                "severity": "S2",
                "rule": "FACT-01",
                "summary": "Nama produk disebut tanpa sitasi lengkap pada kedua format",
                "required_action": "Lengkapi sitasi [PS-01 v1 §...] atau hapus klaim",
            }
        ]

    def test_s2_citation_cannot_get_pass(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(
            bundle, verdict="PASS", severity=(0, 1, 0, 0), issues=self.issue_citation()
        )
        code, payload, _, _ = self.submit_bundle("PASS", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-06")
        self.assertEqual(self.state()["status"], "DRAFTED")

    def test_s2_citation_cannot_get_pass_with_notes(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(
            bundle, verdict="PASS_WITH_NOTES", severity=(0, 1, 0, 0),
            issues=self.issue_citation(),
        )
        code, payload, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=None)
        self.assertEqual(code, 2)
        self.assertEqual(payload["code"], "R-REVIEW-06")
        self.assertIn("S2=1", payload["message"])
        self.assertEqual(self.state()["status"], "DRAFTED")

    def test_s2_citation_must_be_revise(self):
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(
            bundle, verdict="REVISE", severity=(0, 1, 0, 0), issues=self.issue_citation()
        )
        code, payload, _, _ = self.submit_bundle("REVISE", rel, expect=0)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "REVIEWED")
        rec = self.state()["reviews"][-1]
        self.assertEqual(rec["severity_count"]["S2"], 1)

    def test_controller_enforces_arithmetic_not_prose(self):
        """Batas yang jujur: controller tidak bisa menilai prosa review.

        Bila Reviewer menulis temuan sitasi di `issues` tetapi menghitung S2=0,
        controller tidak punya cara mendeteksinya. Yang dijaga adalah angka yang
        dilaporkan, bukan kebenaran penilaian. Karena itu koreksi penilaian
        BL-DEMO-001 dicatat manual sebagai addendum, bukan diklaim otomatis.
        """
        _, bundle = self.start_bundle_round()
        rel = self.bundle_review(
            bundle, verdict="PASS_WITH_NOTES", severity=(0, 0, 0, 1),
            issues=self.issue_citation(),
        )
        code, _, _, _ = self.submit_bundle("PASS_WITH_NOTES", rel, expect=0)
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["status"], "READY_FOR_APPROVAL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
