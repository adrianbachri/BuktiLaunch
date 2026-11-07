#!/usr/bin/env python3
"""Skenario bukti: putaran terakhir yang masih REVISE langsung ESCALATED.

Menjalankan campaign sekali pakai lewat CLI `runs/controller.py`:
  putaran 1 -> REVISE, putaran 2 -> REVISE, putaran 3 -> REVISE.

Yang dibuktikan (controller v2):
  - review putaran 3 yang masih REVISE langsung mengubah status menjadi
    ESCALATED tanpa menunggu percobaan putaran ke-4;
  - `round` tetap 3, tidak pernah menjadi 4;
  - pelanggaran R-ROUND-03 tercatat;
  - dispatch berikutnya ditolak SEBELUM agen dipanggil (status terminal);
  - begin-round berikutnya juga ditolak.

Semua review di sini adalah fixture TEST, bukan review agen sungguhan.

Transkrip:
  python3 tests/scenario_round_limit.py | tee tests/output/round-limit-transcript-v2.txt

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
    shown = out if len(out) < 400 else out[:400] + " …"
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


def play_round(workdir, n, verdict="REVISE"):
    """Satu putaran sah: draft tercatat, dispatch reviewer, review terikat hash."""
    print(f"\n--- PUTARAN {n} ---")
    code, payload = step(["begin-round", "--dir", workdir, "--actor", "planner"])
    expect(f"putaran {n} dimulai", code == 0 and payload["round"] == n,
           f"exit {code} payload {payload}")

    draft_rel = f"drafts/draft-v{n}.md"
    write(os.path.join(workdir, draft_rel), f"# Draft putaran {n} (TEST)\n")
    step(["record-artifact", "--dir", workdir, "--kind", "draft",
          "--path", draft_rel, "--actor", "writer"])

    code, disp = step(["dispatch", "--dir", workdir, "--role", "reviewer",
                       "--actor", "planner"])
    expect("dispatch reviewer diterbitkan", code == 0 and bool(disp.get("dispatch_token")),
           f"exit {code} payload {disp}")

    review_rel = f"reviews/review-v{n}.json"
    write(os.path.join(workdir, review_rel), json.dumps({
        "review_id": f"REVIEW-DEMO-R{n}",
        "campaign_id": "BL-ROUNDLIMIT-DEMO",
        "round": n,
        "reviewer": "proof-reviewer (TEST FIXTURE)",
        "draft": draft_rel,
        "draft_sha256": sha256_file(os.path.join(workdir, draft_rel)),
        "severity_count": {"S1": 1, "S2": 0, "S3": 0, "S4": 0},
        "issues": [{"id": f"R{n}-01", "severity": "S1", "ruleID": "REFUND-01"}],
        "verdict": verdict,
        "verdict_reason": "fixture uji, bukan review agen",
        "human_approval": "not_granted",
        "publication": "none",
    }, indent=2))

    code, payload = step(["submit-review", "--dir", workdir, "--verdict", verdict,
                          "--review-path", review_rel,
                          "--dispatch-token", disp["dispatch_token"],
                          "--actor", "reviewer"])
    expect(f"review putaran {n} diterima", code == 0 and payload["verdict"] == verdict,
           f"exit {code} payload {payload}")
    return payload


def main():
    os.makedirs(TMP_BASE, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="bl-roundlimit-", dir=TMP_BASE)
    try:
        print("=" * 72)
        print("SKENARIO BATAS PUTARAN — BuktiLaunch controller v2 (P-06)")
        print(f"campaign dir: {workdir}")
        print("=" * 72)

        code, _ = step(["init", "--dir", workdir, "--campaign",
                        "BL-ROUNDLIMIT-DEMO", "--max-rounds", "3"])
        expect("init berhasil", code == 0, f"exit {code}")
        step(["set-status", "--dir", workdir, "--status", "PLANNED", "--actor", "planner"])

        for n in (1, 2):
            payload = play_round(workdir, n, "REVISE")
            expect(f"setelah putaran {n} status REVIEWED (belum eskalasi)",
                   payload["status"] == "REVIEWED" and not payload["escalated"],
                   f"payload {payload}")

        payload = play_round(workdir, 3, "REVISE")

        print("\n--- REVIEW PUTARAN 3 MASIH REVISE ---")
        expect("langsung ESCALATED tanpa menunggu putaran ke-4",
               payload["status"] == "ESCALATED" and payload["escalated"] is True,
               f"payload {payload}")

        with open(os.path.join(workdir, "state.json"), encoding="utf-8") as fh:
            state = json.load(fh)

        print("\n--- DISPATCH SETELAH ESKALASI (harus ditolak sebelum agen dipanggil) ---")
        code, payload = step(["dispatch", "--dir", workdir, "--role", "reviewer",
                              "--actor", "planner"])
        expect("dispatch ditolak", code == 2, f"exit {code}")
        expect("kode R-STATE-01 (status terminal)",
               bool(payload and payload.get("code") == "R-STATE-01"),
               f"payload {payload}")

        print("\n--- PERCOBAAN PUTARAN KE-4 ---")
        code, payload = step(["begin-round", "--dir", workdir, "--actor", "planner"])
        expect("exit code 2 (ditolak)", code == 2, f"exit {code}")
        expect("kode R-STATE-01 (status terminal)",
               bool(payload and payload.get("code") == "R-STATE-01"),
               f"payload {payload}")

        print("\n--- STATE AKHIR ---")
        print(json.dumps(
            {
                "round": state["round"],
                "max_rounds": state["max_rounds"],
                "status": state["status"],
                "violations": [v["code"] for v in state["violations"]],
                "history_events": [h["event"] for h in state["history"]],
            },
            indent=2, ensure_ascii=False,
        ))

        expect("round tetap 3, tidak naik ke 4", state["round"] == 3,
               f"round={state['round']}")
        expect("max_rounds = 3", state["max_rounds"] == 3, f"max_rounds={state['max_rounds']}")
        expect("status ESCALATED", state["status"] == "ESCALATED",
               f"status={state['status']}")
        expect("pelanggaran R-ROUND-03 tercatat",
               any(v["code"] == "R-ROUND-03" for v in state["violations"]),
               f"violations={[v['code'] for v in state['violations']]}")
        expect("tiga review tercatat dengan hash draft",
               len(state["reviews"]) == 3 and all(r["draft_sha256"] for r in state["reviews"]),
               f"reviews={len(state['reviews'])}")

        print("\n" + "=" * 72)
        if failures:
            print(f"HASIL: GAGAL — {len(failures)} harapan tidak terpenuhi")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("HASIL: LULUS — semua harapan terpenuhi")
        print("REVISE pada putaran 3 langsung ESCALATED; dispatch & putaran ke-4 ditolak")
        print("=" * 72)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
