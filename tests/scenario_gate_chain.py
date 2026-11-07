#!/usr/bin/env python3
"""Skenario bukti: approval mengikat hash, dan batalnya memicu batas putaran.

Rantai yang dijalankan lewat CLI `runs/controller.py`:
  putaran 1 REVISE -> putaran 2 REVISE -> putaran 3 PASS_WITH_NOTES
  -> READY_FOR_APPROVAL -> approve (TEST) -> APPROVED
  -> isi draft diubah -> recheck -> approval batal, status REVIEWED
  -> begin-round -> R-ROUND-02 (batas) -> ESCALATED

Yang dibuktikan:
  - PASS Reviewer tidak otomatis menjadi APPROVED (butuh perintah approve);
  - approval berlabel TEST tidak memenuhi gerbang nyata;
  - approval batal begitu hash draft berubah (APPROVAL-01);
  - setelah batal pada putaran terakhir, percobaan putaran ke-4 ditolak
    controller dengan R-ROUND-02 dan status dipaksa ESCALATED.

Approval di sini **TEST**, bukan persetujuan manusia. Tidak ada publikasi.

Transkrip:
  python3 tests/scenario_gate_chain.py | tee tests/output/gate-chain-transcript.txt

Keluar 0 bila semua harapan terpenuhi, 1 bila ada yang meleset.
Tanpa dependency eksternal.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER = os.path.join(REPO_ROOT, "runs", "controller.py")
PY = sys.executable

# Direktori kerja dibuat di dalam repo: temp sistem di mesin ini tidak selalu
# tersedia (BUILD-STATUS.md §C catatan lingkungan).
TMP_BASE = os.path.join(REPO_ROOT, "tests", "tmp")

failures = []


def step(args):
    proc = subprocess.run(
        [PY, CONTROLLER, *args], capture_output=True, text=True, cwd=REPO_ROOT
    )
    out = proc.stdout.strip()
    shown = out if len(out) < 500 else out[:500] + " …"
    print(f"$ controller.py {' '.join(args)}")
    print(f"  -> exit={proc.returncode}  {shown}")
    if proc.stderr.strip():
        print(f"  stderr: {proc.stderr.strip()}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload


def expect(label, condition, detail):
    status = "OK  " if condition else "GAGAL"
    print(f"  [{status}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def play_round(workdir, n, verdict, severity):
    print(f"\n--- PUTARAN {n} ({verdict}) ---")
    code, payload = step(["begin-round", "--dir", workdir, "--actor", "planner"])
    expect(f"putaran {n} dimulai", code == 0 and payload["round"] == n,
           f"exit {code} payload {payload}")

    draft_rel = f"drafts/draft-v{n}.md"
    write(os.path.join(workdir, draft_rel), f"# Draft putaran {n} (TEST)\n")
    step(["record-artifact", "--dir", workdir, "--kind", "draft",
          "--path", draft_rel, "--actor", "writer"])
    code, disp = step(["dispatch", "--dir", workdir, "--role", "reviewer",
                       "--actor", "planner"])

    review_rel = f"reviews/review-v{n}.json"
    write(os.path.join(workdir, review_rel), json.dumps({
        "review_id": f"REVIEW-DEMO-R{n}",
        "campaign_id": "BL-GATE-DEMO",
        "round": n,
        "reviewer": "proof-reviewer (TEST FIXTURE)",
        "draft": draft_rel,
        "draft_sha256": sha256_file(os.path.join(workdir, draft_rel)),
        "severity_count": {"S1": severity[0], "S2": severity[1],
                           "S3": severity[2], "S4": severity[3]},
        "issues": [],
        "verdict": verdict,
        "verdict_reason": "fixture uji, bukan review agen",
        "human_approval": "not_granted",
        "publication": "none",
    }, indent=2))

    code, payload = step(["submit-review", "--dir", workdir, "--verdict", verdict,
                          "--review-path", review_rel,
                          "--dispatch-token", disp["dispatch_token"],
                          "--actor", "reviewer"])
    expect(f"review putaran {n} diterima", code == 0, f"exit {code} payload {payload}")
    return payload


def main():
    os.makedirs(TMP_BASE, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="bl-gate-chain-", dir=TMP_BASE)
    try:
        print("=" * 72)
        print("SKENARIO RANTAI APPROVAL — BuktiLaunch controller v2 (P-10)")
        print(f"campaign dir: {workdir}")
        print("=" * 72)

        code, _ = step(["init", "--dir", workdir, "--campaign", "BL-GATE-DEMO",
                        "--max-rounds", "3"])
        expect("init berhasil", code == 0, f"exit {code}")
        step(["set-status", "--dir", workdir, "--status", "PLANNED", "--actor", "planner"])

        write(os.path.join(workdir, "rules/rulebook.md"), "RULEBOOK-01 v2 (TEST)\n")
        write(os.path.join(workdir, "sources/product-sheet.md"), "PS-01 v1 (TEST)\n")

        play_round(workdir, 1, "REVISE", (1, 0, 0, 0))
        play_round(workdir, 2, "REVISE", (1, 0, 0, 0))
        payload = play_round(workdir, 3, "PASS_WITH_NOTES", (0, 0, 0, 2))

        print("\n--- PASS BUKAN APPROVAL ---")
        expect("status hanya READY_FOR_APPROVAL, bukan APPROVED",
               payload["status"] == "READY_FOR_APPROVAL", f"payload {payload}")

        code, payload = step(["check-gate", "--dir", workdir])
        expect("gerbang belum terpenuhi sebelum approve", code == 2, f"exit {code}")
        expect("alasan R-GATE-01",
               bool(payload and any(p["code"] == "R-GATE-01" for p in payload["problems"])),
               f"payload {payload}")

        print("\n--- APPROVE (TEST, bukan manusia) ---")
        code, payload = step(["approve", "--dir", workdir, "--by", "TEST-FIXTURE",
                              "--bind", "rules/rulebook.md",
                              "--bind", "sources/product-sheet.md",
                              "--test", "--note", "demo pengujian"])
        expect("approve tercatat", code == 0 and payload["status"] == "APPROVED",
               f"exit {code} payload {payload}")
        expect("ditandai TEST", bool(payload and payload["test"] is True), f"payload {payload}")

        code, payload = step(["check-gate", "--dir", workdir])
        expect("gerbang menolak approval TEST", code == 2, f"exit {code}")
        expect("alasan R-GATE-02",
               bool(payload and any(p["code"] == "R-GATE-02" for p in payload["problems"])),
               f"payload {payload}")

        code, payload = step(["check-gate", "--dir", workdir, "--allow-test"])
        expect("dengan --allow-test gerbang lolos (khusus pengujian)", code == 0,
               f"exit {code} payload {payload}")

        print("\n--- ISI DRAFT DIUBAH SETELAH APPROVAL ---")
        write(os.path.join(workdir, "drafts/draft-v3.md"),
              "# Draft putaran 3 (TEST) — diubah setelah approval\n")
        code, payload = step(["recheck", "--dir", workdir])
        expect("approval dibatalkan", code == 2, f"exit {code}")
        expect("kode R-APPROVAL-01",
               bool(payload and payload.get("code") == "R-APPROVAL-01"), f"payload {payload}")
        expect("status turun ke REVIEWED",
               bool(payload and payload.get("status") == "REVIEWED"), f"payload {payload}")

        code, payload = step(["check-gate", "--dir", workdir, "--allow-test"])
        expect("gerbang kembali gagal", code == 2, f"exit {code}")

        print("\n--- PERCOBAAN PUTARAN KE-4 SETELAH APPROVAL BATAL ---")
        code, payload = step(["begin-round", "--dir", workdir, "--actor", "planner"])
        expect("ditolak controller", code == 2, f"exit {code}")
        expect("kode R-ROUND-02 (batas putaran)",
               bool(payload and payload.get("code") == "R-ROUND-02"), f"payload {payload}")
        expect("status dipaksa ESCALATED",
               bool(payload and payload.get("status") == "ESCALATED"), f"payload {payload}")

        code, payload = step(["dispatch", "--dir", workdir, "--role", "reviewer",
                              "--actor", "planner"])
        expect("dispatch setelah eskalasi ditolak", code == 2, f"exit {code}")
        expect("kode R-STATE-01",
               bool(payload and payload.get("code") == "R-STATE-01"), f"payload {payload}")

        with open(os.path.join(workdir, "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)

        print("\n--- STATE AKHIR ---")
        print(json.dumps({
            "round": state["round"],
            "status": state["status"],
            "approvals": [{"valid": a["valid"], "test": a["test"],
                           "draft_sha256": a["draft_sha256"][:16] + "…"}
                          for a in state["approvals"]],
            "invalidations": [i["reason"] for i in state["invalidations"]],
            "violations": [v["code"] for v in state["violations"]],
        }, indent=2, ensure_ascii=False))

        expect("round tetap 3", state["round"] == 3, f"round={state['round']}")
        expect("approval tercatat tidak valid", state["approvals"][0]["valid"] is False,
               f"approvals={state['approvals']}")
        expect("satu invalidasi tercatat", len(state["invalidations"]) == 1,
               f"invalidations={len(state['invalidations'])}")
        expect("R-ROUND-02 tercatat",
               any(v["code"] == "R-ROUND-02" for v in state["violations"]),
               f"violations={[v['code'] for v in state['violations']]}")

        print("\n" + "=" * 72)
        if failures:
            print(f"HASIL: GAGAL — {len(failures)} harapan tidak terpenuhi")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("HASIL: LULUS — semua harapan terpenuhi")
        print("PASS != APPROVED; approval TEST ditolak gerbang; approval batal")
        print("setelah draft berubah; putaran ke-4 ditolak R-ROUND-02")
        print("=" * 72)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
