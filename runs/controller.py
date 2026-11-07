#!/usr/bin/env python3
"""Controller putaran + gerbang approval BuktiLaunch — P-06 / P-10.

Komponen di luar ketiga agen. Satu-satunya penulis field `round` pada
`state.json`. Spesifikasi: PROJECT.md §5.3, RULEBOOK-01 v2 §0.4 dan §11
(APPROVAL-01).

Tanpa dependency eksternal: hanya pustaka standar Python 3.

Perintah
--------
  init            buat state.json baru untuk satu campaign
  begin-round     mulai putaran draft->review (menaikkan round)
  set-status      HANYA INIT->PLANNED dan *->ESCALATED (status lain ditolak)
  dispatch        izin memanggil agen; token wajib untuk submit-review
  record-artifact catat artefak + sha256 ke history
  submit-review   terima verdict HANYA dari file review yang benar-benar ada,
                  terikat ke sha256 draft yang diperiksa
  approve         gerbang approval manusia, mengikat hash draft+sumber+rulebook
  check-gate      baca-saja: apakah gerbang approval terpenuhi sekarang
  recheck         hitung ulang ikatan approval; batalkan bila ada yang berubah
  export-copy     pisahkan blok siap-salin dari catatan perubahan
  set-round       SENGAJA ditolak bila menurunkan round (R-ROUND-01)
  reset           SENGAJA ditolak selalu (R-ROUND-01)
  show            tampilkan state
  verify          hitung ulang hash artefak, laporkan mismatch

Kode keluar
-----------
  0  berhasil
  2  ditolak oleh kebijakan controller (bukan error pemakaian)
  1  error pemakaian / state rusak

Kode penolakan
--------------
  R-STATE-01    status terminal
  R-STATE-02    state.json sudah ada
  R-STATE-03    begin-round dari status yang tidak sah
  R-STATE-04    transisi status tidak sah menurut tabel
  R-STATE-05    submit-review hanya saat DRAFTED
  R-STATE-06    status dicadangkan; hanya bisa dicapai lewat perintah khusus
  R-ROUND-01    percobaan menurunkan / melompatkan / mereset round
  R-ROUND-02    putaran melebihi batas (backstop)
  R-ROUND-03    review putaran terakhir masih REVISE -> langsung ESCALATED
  R-REVIEW-01   file review tidak ada
  R-REVIEW-02   file review bukan JSON / field wajib tidak lengkap
  R-REVIEW-03   verdict di file != verdict di CLI
  R-REVIEW-04   sha256 / path draft di file review tidak cocok
  R-REVIEW-05   nomor putaran di file review tidak cocok
  R-REVIEW-06   verdict tidak konsisten dengan severity_count
  R-REVIEW-07   file review mengklaim approval manusia / publikasi
  R-REVIEW-08   belum ada draft tercatat untuk putaran ini
  R-REVIEW-09   tidak ada token dispatch reviewer yang belum terpakai
  R-REVIEW-10   path keluar dari direktori campaign
  R-DISPATCH-01 status bukan DRAFTED
  R-DISPATCH-02 belum ada draft tercatat untuk putaran ini
  R-DISPATCH-03 round habis / melewati batas
  R-DISPATCH-04 review putaran ini sudah diterima
  R-APPROVAL-01 approval tidak lagi sah: hash berubah / file hilang
  R-APPROVAL-02 approve hanya dari READY_FOR_APPROVAL
  R-APPROVAL-03 identitas approval bukan manusia (nama agen/model)
  R-APPROVAL-04 draft berubah setelah review / draft tidak cocok
  R-APPROVAL-05 belum ada review yang diterima untuk putaran ini
  R-APPROVAL-06 ikatan (binding) tidak lengkap, termasuk rulebook
  R-GATE-01     gerbang belum terpenuhi (status / approval)
  R-GATE-02     approval berlabel TEST tidak memenuhi gerbang nyata
  R-COPY-01     blok siap-salin memuat string terlarang
  R-COPY-02     heading blok tidak ditemukan
  R-COPY-03     file tujuan sudah ada
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

SCHEMA_ID = "STATE-01"
SCHEMA_VERSION = 2
READABLE_VERSIONS = (1, 2)
MAX_ROUNDS_DEFAULT = 3

STATUSES = (
    "INIT",
    "PLANNED",
    "DRAFTED",
    "REVIEWED",
    "READY_FOR_APPROVAL",
    "ESCALATED",
    "APPROVED",
    "REJECTED",
)

TERMINAL = {"ESCALATED", "APPROVED", "REJECTED"}

# Transisi status yang sah. REVIEWED->DRAFTED hanya lewat begin-round.
TRANSITIONS = {
    "INIT": {"PLANNED", "ESCALATED"},
    "PLANNED": {"DRAFTED", "ESCALATED"},
    "DRAFTED": {"REVIEWED", "ESCALATED"},
    "REVIEWED": {"DRAFTED", "READY_FOR_APPROVAL", "ESCALATED"},
    "READY_FOR_APPROVAL": {"APPROVED", "REJECTED", "ESCALATED"},
}

# Status yang TIDAK boleh dicapai lewat `set-status`. Masing-masing punya
# perintah sendiri yang memeriksa prasyaratnya, sehingga satu perintah
# `set-status` tidak bisa melompati penghitung putaran, review, atau approval.
RESERVED_STATUS = {
    "DRAFTED": "begin-round (menaikkan round)",
    "REVIEWED": "submit-review (membaca file review)",
    "READY_FOR_APPROVAL": "submit-review dengan verdict PASS / PASS_WITH_NOTES",
    "APPROVED": "approve (gerbang approval manusia)",
    "REJECTED": "approve --reject (keputusan manusia)",
}

VERDICT_TO_STATUS = {
    "PASS": "READY_FOR_APPROVAL",
    "PASS_WITH_NOTES": "READY_FOR_APPROVAL",
    "REVISE": "REVIEWED",  # menunggu begin-round berikutnya
    "REJECT": "REVIEWED",  # alias REVISE (rulebook v2 §0.3.1)
    "ESCALATE": "ESCALATED",
}

# Verdict yang mengakhiri putaran tanpa lolos.
NON_PASS_VERDICTS = {"REVISE", "REJECT"}

# Token yang menandakan sebuah identitas bukan manusia. Dipakai gerbang
# approval: nama agen/model tidak boleh disangka sebagai persetujuan manusia.
AGENT_TOKENS = {
    "planner",
    "writer",
    "reviewer",
    "controller",
    "agent",
    "agents",
    "model",
    "assistant",
    "bot",
    "system",
    "llm",
    "gpt",
    "claude",
    "openai",
    "anthropic",
    "ai",
    "test",
    "fixture",
}

REVIEW_REQUIRED_FIELDS = (
    "verdict",
    "round",
    "draft",
    "draft_sha256",
    "severity_count",
    "human_approval",
    "publication",
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2


class Refused(Exception):
    """Penolakan kebijakan controller. Bukan crash."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def state_path(campaign_dir: str) -> str:
    return os.path.join(campaign_dir, "state.json")


def sha256_file(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_state_json(path: str, payload: dict) -> None:
    """Tulis state.json di tempat.

    Catatan lingkungan: penulisan atomik lewat `os.replace()` dari file sementara
    diblokir oleh file-policy host di mesin ini (brokered rename ditolak), jadi
    controller menulis langsung + fsync. Konsekuensinya state.json tidak
    di-swap secara atomik; karena hanya satu proses yang menulis pada satu
    waktu, ini diterima dan dicatat di RUN.md.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def load_state(campaign_dir: str) -> dict:
    path = state_path(campaign_dir)
    if not os.path.isfile(path):
        raise SystemExit(
            f"error: state.json tidak ditemukan di {campaign_dir}. Jalankan `init` dulu."
        )
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        raise SystemExit("error: state harus berupa objek JSON")
    if state.get("schema") != SCHEMA_ID:
        raise SystemExit(f"error: schema tidak dikenal: {state.get('schema')!r}")
    version = state.get("schema_version", 1)
    if version not in READABLE_VERSIONS:
        raise SystemExit(f"error: schema_version tidak didukung: {version!r}")
    limit = state.get("max_rounds")
    current = state.get("round")
    if type(limit) is not int or not 1 <= limit <= MAX_ROUNDS_DEFAULT:
        raise SystemExit("error: max_rounds state harus 1 sampai 3; state tidak diubah")
    if type(current) is not int or not 0 <= current <= limit:
        raise SystemExit("error: round state tidak sah; state tidak diubah")
    return state


def ensure_schema(state: dict) -> dict:
    """Tambahkan field v2 pada state v1 yang dibaca.

    Campaign lama (BL-SMOKE-001, BL-REVISION-001) tetap bisa dibaca dan
    di-`verify` tanpa ditulis ulang. Migrasi hanya menambah field kosong;
    tidak mengubah round, status, maupun artefak yang sudah tercatat.
    """
    state.setdefault("reviews", [])
    state.setdefault("dispatches", [])
    state.setdefault("approvals", [])
    state.setdefault("invalidations", [])
    state.setdefault("copy_exports", [])
    state["schema_version"] = SCHEMA_VERSION
    return state


def save_state(campaign_dir: str, state: dict, event: str, actor: str, **detail) -> dict:
    """Tulis state + tambah satu entri history. Digest state ikut dicatat."""
    state["updated_at"] = now_iso()
    digest = sha256_bytes(
        json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    entry = {
        "seq": len(state["history"]) + 1,
        "at": state["updated_at"],
        "event": event,
        "actor": actor,
        "round": state["round"],
        "status": state["status"],
        "state_digest": digest,
    }
    if detail:
        entry["detail"] = detail
    state["history"].append(entry)
    write_state_json(state_path(campaign_dir), state)
    return entry


def require_mutable(state: dict) -> None:
    if state["status"] in TERMINAL:
        raise Refused(
            "R-STATE-01",
            f"status {state['status']} bersifat terminal; tidak ada mutasi lagi",
        )


def record_violation(state: dict, code: str, message: str, actor: str) -> None:
    state.setdefault("violations", []).append(
        {"at": now_iso(), "code": code, "message": message, "actor": actor}
    )


# --------------------------------------------------------------------------
# Helper: path, draft, dispatch, ikatan hash
# --------------------------------------------------------------------------


def resolve_path(campaign_dir: str, path: str) -> str:
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(campaign_dir, path))


def rel_to_campaign(campaign_dir: str, path: str, code: str = "R-REVIEW-10") -> str:
    """Pastikan path berada di dalam campaign; kembalikan path relatif."""
    root = os.path.realpath(campaign_dir)
    target = os.path.realpath(path)
    try:
        common = os.path.commonpath([root, target])
    except ValueError:  # beda drive di Windows
        common = ""
    if common != root:
        raise Refused(code, f"path {path} keluar dari direktori campaign")
    return os.path.relpath(target, root)


def current_draft(state: dict, campaign_dir: str) -> dict | None:
    """Artefak draft terakhir untuk putaran berjalan, bila filenya masih ada."""
    for art in reversed(state.get("artifacts", [])):
        if art.get("kind") == "draft" and art.get("round") == state["round"]:
            full = os.path.join(campaign_dir, art["path"])
            if os.path.isfile(full):
                digest, size = sha256_file(full)
                rec = dict(art)
                rec["sha256_now"] = digest
                rec["bytes_now"] = size
                rec["changed_since_record"] = digest != art["sha256"]
                return rec
    return None


def reviewer_dispatches(state: dict) -> list:
    return [
        d
        for d in state.get("dispatches", [])
        if d.get("role") == "reviewer" and d.get("round") == state["round"]
    ]


def consumed_tokens(state: dict) -> set:
    return {r.get("dispatch_token") for r in state.get("reviews", []) if r.get("dispatch_token")}


def unconsumed_reviewer_dispatches(state: dict) -> list:
    used = consumed_tokens(state)
    return [d for d in reviewer_dispatches(state) if d["token"] not in used]


def last_accepted_review(state: dict, round_no: int | None = None) -> dict | None:
    rnd = state["round"] if round_no is None else round_no
    for rec in reversed(state.get("reviews", [])):
        if rec.get("round") == rnd:
            return rec
    return None


def looks_like_agent(identity: str) -> bool:
    tokens = [t for t in re.split(r"[^a-z0-9]+", identity.lower()) if t]
    return any(t in AGENT_TOKENS for t in tokens)


# --------------------------------------------------------------------------
# Perintah
# --------------------------------------------------------------------------


def cmd_init(args) -> int:
    path = state_path(args.dir)
    if not 1 <= args.max_rounds <= MAX_ROUNDS_DEFAULT:
        raise Refused("R-ROUND-04", "max_rounds harus 1 sampai 3; batas keras Challenge C")
    if os.path.exists(path):
        raise Refused(
            "R-STATE-02",
            f"state.json sudah ada di {args.dir}; campaign baru butuh campaign_id baru",
        )
    os.makedirs(args.dir, exist_ok=True)
    state = {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "campaign_id": args.campaign,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "round": 0,
        "max_rounds": args.max_rounds,
        "status": "INIT",
        "artifacts": [],
        "verdicts": [],
        "reviews": [],
        "dispatches": [],
        "approvals": [],
        "invalidations": [],
        "copy_exports": [],
        "violations": [],
        "history": [],
    }
    save_state(args.dir, state, "init", args.actor, campaign_id=args.campaign)
    print(json.dumps({"ok": True, "campaign": args.campaign, "round": 0}, ensure_ascii=False))
    return EXIT_OK


def cmd_begin_round(args) -> int:
    state = ensure_schema(load_state(args.dir))
    require_mutable(state)

    # Putaran baru hanya sah dari INIT/PLANNED (putaran 1) atau REVIEWED (setelah REVISE).
    if state["status"] not in ("INIT", "PLANNED", "REVIEWED"):
        raise Refused(
            "R-STATE-03",
            "begin-round hanya dari INIT, PLANNED, atau REVIEWED; "
            f"status sekarang {state['status']}",
        )

    if state["round"] >= state["max_rounds"]:
        # Backstop. Jalur normal sudah dieskalasi saat submit-review putaran
        # terakhir (R-ROUND-03); ini menangkap sisa jalur seperti approval yang
        # batal pada putaran terakhir.
        record_violation(
            state,
            "R-ROUND-02",
            f"percobaan putaran ke-{state['round'] + 1} melebihi batas {state['max_rounds']}",
            args.actor,
        )
        state["status"] = "ESCALATED"
        save_state(
            args.dir,
            state,
            "begin-round-refused",
            args.actor,
            attempted_round=state["round"] + 1,
            max_rounds=state["max_rounds"],
            reason="round_limit",
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "refused": True,
                    "code": "R-ROUND-02",
                    "round": state["round"],
                    "status": state["status"],
                    "message": f"putaran ke-{state['round'] + 1} ditolak; batas {state['max_rounds']}",
                },
                ensure_ascii=False,
            )
        )
        return EXIT_REFUSED

    state["round"] += 1
    state["status"] = "DRAFTED"
    save_state(args.dir, state, "begin-round", args.actor, round_started=state["round"])
    print(
        json.dumps(
            {"ok": True, "round": state["round"], "status": state["status"]},
            ensure_ascii=False,
        )
    )
    return EXIT_OK


def cmd_set_status(args) -> int:
    state = ensure_schema(load_state(args.dir))
    require_mutable(state)
    target = args.status
    if target not in STATUSES:
        raise SystemExit(f"error: status tidak dikenal: {target}")
    allowed = TRANSITIONS.get(state["status"], set())
    if target not in allowed:
        raise Refused(
            "R-STATE-04",
            f"transisi {state['status']} -> {target} tidak sah; yang sah: {sorted(allowed) or 'tidak ada'}",
        )
    # Guard tambahan: transisi yang sah menurut tabel tetapi punya prasyarat
    # (review, hash, approval) tidak boleh dicapai lewat satu perintah status.
    if target in RESERVED_STATUS:
        raise Refused(
            "R-STATE-06",
            f"status {target} tidak boleh diset langsung; gunakan {RESERVED_STATUS[target]}",
            target=target,
            use=RESERVED_STATUS[target],
        )
    state["status"] = target
    save_state(args.dir, state, "set-status", args.actor, to=target)
    print(json.dumps({"ok": True, "status": target}, ensure_ascii=False))
    return EXIT_OK


def cmd_dispatch(args) -> int:
    """Izin memanggil agen. Dicatat dengan token; submit-review menuntut token.

    Ini yang membuat "dispatch berikutnya ditolak sebelum agen dipanggil"
    mungkin: begitu putaran habis atau status terminal, tidak ada token baru
    yang bisa diterbitkan, dan review tanpa token tidak akan diterima.
    """
    state = ensure_schema(load_state(args.dir))
    require_mutable(state)

    if state["round"] > state["max_rounds"]:
        raise Refused(
            "R-DISPATCH-03",
            f"round {state['round']} melewati batas {state['max_rounds']}",
        )
    if state["round"] < 1:
        raise Refused("R-DISPATCH-01", "belum ada putaran; jalankan begin-round dulu")
    if state["status"] != "DRAFTED":
        raise Refused(
            "R-DISPATCH-01",
            f"dispatch hanya saat status DRAFTED; status sekarang {state['status']}",
        )

    role = args.role
    if role == "writer":
        if last_accepted_review(state) is not None:
            raise Refused(
                "R-DISPATCH-04",
                f"review putaran {state['round']} sudah diterima; writer tidak dipanggil lagi",
            )
        draft = None
    else:  # reviewer
        draft = current_draft(state, args.dir)
        if draft is None:
            raise Refused(
                "R-DISPATCH-02",
                f"belum ada artefak draft tercatat untuk putaran {state['round']}",
            )

    token = uuid.uuid4().hex
    rec = {
        "at": now_iso(),
        "role": role,
        "round": state["round"],
        "token": token,
        "actor": args.actor,
        "draft_path": draft["path"] if draft else None,
        "draft_sha256": draft["sha256_now"] if draft else None,
    }
    state["dispatches"].append(rec)
    save_state(
        args.dir,
        state,
        "dispatch",
        args.actor,
        role=role,
        token=token,
        draft_path=rec["draft_path"],
        draft_sha256=rec["draft_sha256"],
    )
    print(
        json.dumps(
            {
                "ok": True,
                "role": role,
                "round": state["round"],
                "dispatch_token": token,
                "draft_path": rec["draft_path"],
                "draft_sha256": rec["draft_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return EXIT_OK


def cmd_record_artifact(args) -> int:
    state = ensure_schema(load_state(args.dir))
    require_mutable(state)
    path = resolve_path(args.dir, args.path)
    if not os.path.isfile(path):
        raise SystemExit(f"error: artefak tidak ditemukan: {path}")
    digest, size = sha256_file(path)
    rel = rel_to_campaign(args.dir, path, code="R-REVIEW-10")
    rec = {
        "at": now_iso(),
        "kind": args.kind,
        "actor": args.actor,
        "round": state["round"],
        "path": rel,
        "sha256": digest,
        "bytes": size,
    }
    state["artifacts"].append(rec)
    save_state(
        args.dir,
        state,
        "record-artifact",
        args.actor,
        kind=args.kind,
        path=rel,
        sha256=digest,
        bytes=size,
    )
    print(json.dumps({"ok": True, "sha256": digest, "bytes": size, "path": rel}, ensure_ascii=False))
    return EXIT_OK


def _read_json_file(path: str):
    """Baca JSON; kembalikan None bila bukan JSON yang sah. Tidak melempar."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def _bundle_entries(campaign_dir: str, draft: dict):
    """Bila draft yang diikat adalah bundel JSON berisi 'files', kembalikan entrinya."""
    payload = _read_json_file(os.path.join(campaign_dir, draft["path"]))
    if not isinstance(payload, dict):
        return None
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return None
    return files


def _collect_reviewed_files(campaign_dir: str, payload: dict, draft: dict) -> list:
    """Kumpulkan SEMUA file yang dirujuk review.

    Dua bentuk didukung:
      - field `drafts`: [{path, sha256}, ...] pada file review (bentuk eksplisit)
      - bundel: draft yang diikat berisi {"files": [{path, sha256}, ...]}

    Kosong berarti review satu file (bentuk lama) — tetap sah.
    """
    entries, source = [], None
    raw = payload.get("drafts")
    if isinstance(raw, list) and raw:
        entries, source = raw, "drafts"
    else:
        bundle = _bundle_entries(campaign_dir, draft)
        if bundle is not None:
            entries, source = bundle, "bundle"

    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise Refused("R-REVIEW-11", f"entri {source}[{i}] bukan objek JSON")
        path = str(e.get("path", "")).replace("\\", "/").lstrip("./")
        digest = str(e.get("sha256", "")).strip().lower()
        if not path:
            raise Refused("R-REVIEW-11", f"entri {source}[{i}] tidak menyebut path")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refused(
                "R-REVIEW-11",
                f"entri {source}[{i}] ({path}) tidak menyebut sha256 64 heksadesimal",
                path=path,
            )
        out.append({"path": path, "sha256": digest, "source": source})

    # Bila review juga memuat reviewed_files, isinya harus sama dengan sumbernya.
    declared = payload.get("reviewed_files")
    if isinstance(declared, list) and declared and out:
        dmap = {}
        for e in declared:
            if isinstance(e, dict) and e.get("path"):
                key = str(e["path"]).replace("\\", "/").lstrip("./")
                dmap[key] = str(e.get("sha256", "")).strip().lower()
        for e in out:
            if e["path"] not in dmap:
                raise Refused(
                    "R-REVIEW-11",
                    f"reviewed_files tidak menyebut {e['path']} yang ada di {e['source']}",
                    path=e["path"],
                )
            if dmap[e["path"]] != e["sha256"]:
                raise Refused(
                    "R-REVIEW-11",
                    f"sha256 {e['path']} berbeda antara reviewed_files dan {e['source']}",
                    path=e["path"],
                    reviewed_files=dmap[e["path"]],
                    source_sha=e["sha256"],
                )
    return out


def _verify_reviewed_files(state: dict, campaign_dir: str, reviewed: list) -> list:
    """Setiap file yang dirujuk harus ada, hash-nya cocok, dan tercatat sebagai artefak."""
    recorded = {}
    for art in state.get("artifacts", []):
        recorded.setdefault(art.get("path"), []).append(art)

    checked = []
    for e in reviewed:
        full = os.path.join(campaign_dir, e["path"])
        if not os.path.isfile(full):
            raise Refused(
                "R-REVIEW-11",
                f"file yang dirujuk review tidak ada: {e['path']}",
                path=e["path"],
            )
        actual, size = sha256_file(full)
        if actual != e["sha256"]:
            raise Refused(
                "R-REVIEW-11",
                "sha256 file yang dirujuk review tidak cocok dengan file sekarang "
                "(review untuk versi lain)",
                path=e["path"],
                declared=e["sha256"],
                actual=actual,
            )
        arts = recorded.get(e["path"], [])
        if not arts:
            raise Refused(
                "R-REVIEW-12",
                f"file yang dirujuk review belum dicatat sebagai artefak: {e['path']}",
                path=e["path"],
            )
        if arts[-1].get("sha256") != actual:
            raise Refused(
                "R-REVIEW-12",
                "sha256 file tercatat berbeda dari file sekarang; catat ulang sebelum review",
                path=e["path"],
                recorded=arts[-1].get("sha256"),
                actual=actual,
            )
        checked.append(
            {"path": e["path"], "sha256": actual, "bytes": size, "source": e["source"]}
        )
    return checked


def _load_review_file(campaign_dir: str, review_path: str) -> tuple[dict, str, str]:
    """Baca file review. Kembalikan (payload, path_relatif, sha256)."""
    full = resolve_path(campaign_dir, review_path)
    if not os.path.isfile(full):
        raise Refused(
            "R-REVIEW-01",
            f"file review tidak ada: {review_path}; verdict tidak diterima tanpa file",
        )
    try:
        with open(full, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Refused("R-REVIEW-02", f"file review bukan JSON yang sah: {exc}") from exc
    if not isinstance(payload, dict):
        raise Refused("R-REVIEW-02", "file review harus berupa objek JSON")
    missing = [f for f in REVIEW_REQUIRED_FIELDS if f not in payload]
    if missing:
        raise Refused(
            "R-REVIEW-02",
            "file review tidak memuat field wajib: " + ", ".join(missing),
            missing=missing,
        )
    digest, _ = sha256_file(full)
    return payload, rel_to_campaign(campaign_dir, full), digest


def cmd_submit_review(args) -> int:
    state = ensure_schema(load_state(args.dir))
    require_mutable(state)
    if state["status"] != "DRAFTED":
        raise Refused(
            "R-STATE-05",
            f"review hanya boleh masuk saat status DRAFTED; status sekarang {state['status']}",
        )
    verdict = args.verdict
    if verdict not in VERDICT_TO_STATUS:
        raise SystemExit(f"error: verdict tidak dikenal: {verdict}")

    # 1. Harus ada draft tercatat untuk putaran ini.
    draft = current_draft(state, args.dir)
    if draft is None:
        raise Refused(
            "R-REVIEW-08",
            f"belum ada artefak draft tercatat untuk putaran {state['round']}",
        )

    # 2. Harus ada izin dispatch reviewer yang belum terpakai.
    pending = unconsumed_reviewer_dispatches(state)
    if not pending:
        raise Refused(
            "R-REVIEW-09",
            "tidak ada token dispatch reviewer yang belum terpakai untuk putaran ini",
        )
    if args.dispatch_token:
        match = [d for d in pending if d["token"] == args.dispatch_token]
        if not match:
            raise Refused(
                "R-REVIEW-09",
                "token dispatch tidak dikenal atau sudah terpakai",
                token=args.dispatch_token,
            )
        dispatch = match[0]
    else:
        dispatch = pending[-1]

    # 3. File review harus benar-benar ada dan bisa dibaca.
    payload, review_rel, review_sha = _load_review_file(args.dir, args.review_path)

    # 4. Verdict di file harus sama dengan verdict di CLI.
    file_verdict = str(payload["verdict"]).strip().upper()
    if file_verdict == "REJECT" and verdict == "REVISE":
        pass  # alias resmi (rulebook v2 §0.3.1)
    elif file_verdict != verdict:
        raise Refused(
            "R-REVIEW-03",
            f"verdict di CLI ({verdict}) != verdict di file ({file_verdict}); "
            "verdict hanya sah bila tertulis di file review",
            cli=verdict,
            file=file_verdict,
        )

    # 5. Putaran di file harus sama dengan putaran berjalan.
    try:
        file_round = int(payload["round"])
    except (TypeError, ValueError) as exc:
        raise Refused("R-REVIEW-02", "field round pada file review bukan angka") from exc
    if file_round != state["round"]:
        raise Refused(
            "R-REVIEW-05",
            f"file review untuk putaran {file_round}, campaign di putaran {state['round']}",
            file_round=file_round,
            state_round=state["round"],
        )

    # 6. File review harus menunjuk draft yang sama, dengan hash yang cocok.
    file_draft = str(payload["draft"]).replace("\\", "/").lstrip("./")
    if file_draft != draft["path"]:
        raise Refused(
            "R-REVIEW-04",
            f"file review menunjuk draft {file_draft}, draft putaran ini {draft['path']}",
            file_draft=file_draft,
            expected=draft["path"],
        )
    declared = str(payload["draft_sha256"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", declared):
        raise Refused(
            "R-REVIEW-04",
            "field draft_sha256 wajib berisi sha256 64 heksadesimal",
            declared=declared,
        )
    if declared != draft["sha256_now"]:
        raise Refused(
            "R-REVIEW-04",
            "sha256 draft di file review tidak cocok dengan file draft sekarang "
            "(review untuk versi lain)",
            declared=declared,
            actual=draft["sha256_now"],
        )
    if declared != draft["sha256"]:
        raise Refused(
            "R-REVIEW-04",
            "sha256 draft di file review tidak cocok dengan yang tercatat saat record-artifact",
            declared=declared,
            recorded=draft["sha256"],
        )

    # 7. File review tidak boleh mengklaim approval manusia atau publikasi.
    if str(payload["human_approval"]).strip().lower() != "not_granted":
        raise Refused(
            "R-REVIEW-07",
            "file review mengklaim human_approval selain not_granted; "
            "approval bukan bagian dari review",
            human_approval=payload["human_approval"],
        )
    if str(payload["publication"]).strip().lower() != "none":
        raise Refused(
            "R-REVIEW-07",
            "file review mengklaim publikasi; review tidak boleh menerbitkan apa pun",
            publication=payload["publication"],
        )

    # 8. Verdict harus konsisten dengan severity_count (rulebook §0.2/§0.3).
    sc = payload["severity_count"]
    if not isinstance(sc, dict):
        raise Refused("R-REVIEW-02", "severity_count harus objek {S1..S4}")
    try:
        s1 = int(sc.get("S1", 0))
        s2 = int(sc.get("S2", 0))
        s3 = int(sc.get("S3", 0))
    except (TypeError, ValueError) as exc:
        raise Refused("R-REVIEW-02", "severity_count harus berisi angka") from exc
    if verdict == "PASS" and (s1 or s2 or s3):
        raise Refused(
            "R-REVIEW-06",
            f"PASS tidak sah dengan S1={s1} S2={s2} S3={s3}",
            severity=sc,
        )
    if verdict == "PASS_WITH_NOTES" and (s1 or s2):
        raise Refused(
            "R-REVIEW-06",
            f"PASS_WITH_NOTES tidak sah selama ada S1/S2 (S1={s1}, S2={s2})",
            severity=sc,
        )

    # 8b. Setiap file yang dirujuk review harus diverifikasi: path, keberadaan,
    #     hash aktual, dan tercatat sebagai artefak. Ini yang menutup celah
    #     "satu review untuk banyak file" — bundel tidak lagi dipercaya begitu saja.
    reviewed = _collect_reviewed_files(args.dir, payload, draft)
    verified = _verify_reviewed_files(state, args.dir, reviewed)

    # 9. Terima.
    target = VERDICT_TO_STATUS[verdict]
    rec = {
        "at": now_iso(),
        "round": state["round"],
        "verdict": verdict,
        "review_path": review_rel,
        "review_sha256": review_sha,
        "draft_path": draft["path"],
        "draft_sha256": draft["sha256_now"],
        "reviewed_files": verified,
        "severity_count": {"S1": s1, "S2": s2, "S3": s3, "S4": int(sc.get("S4", 0))},
        "dispatch_token": dispatch["token"],
        "actor": args.actor,
    }
    state["reviews"].append(rec)
    state["verdicts"].append(
        {
            "at": rec["at"],
            "round": state["round"],
            "verdict": verdict,
            "review_path": review_rel,
            "actor": args.actor,
        }
    )
    state["status"] = target
    save_state(
        args.dir,
        state,
        "submit-review",
        args.actor,
        verdict=verdict,
        review_path=review_rel,
        review_sha256=review_sha,
        draft_sha256=rec["draft_sha256"],
        dispatch_token=dispatch["token"],
        status_after=target,
    )

    # 10. Putaran terakhir masih REVISE -> langsung ESCALATED, tanpa menunggu
    #     percobaan putaran berikutnya.
    escalated = False
    if verdict in NON_PASS_VERDICTS and state["round"] >= state["max_rounds"]:
        record_violation(
            state,
            "R-ROUND-03",
            f"review putaran {state['round']} masih {verdict}; batas {state['max_rounds']} habis",
            args.actor,
        )
        state["status"] = "ESCALATED"
        save_state(
            args.dir,
            state,
            "escalate-round-limit",
            "controller",
            round=state["round"],
            max_rounds=state["max_rounds"],
            verdict=verdict,
        )
        escalated = True

    print(
        json.dumps(
            {
                "ok": True,
                "verdict": verdict,
                "round": state["round"],
                "status": state["status"],
                "escalated": escalated,
                "review_sha256": review_sha,
                "draft_sha256": rec["draft_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return EXIT_OK


def cmd_approve(args) -> int:
    """Gerbang approval manusia. Mengikat versi persis, bukan versi umum."""
    state = ensure_schema(load_state(args.dir))
    require_mutable(state)

    if state["status"] != "READY_FOR_APPROVAL":
        raise Refused(
            "R-APPROVAL-02",
            f"approve hanya dari READY_FOR_APPROVAL; status sekarang {state['status']}",
        )

    is_test = bool(args.test)
    identity = (args.by or "").strip()
    if len(identity) < 2:
        raise Refused("R-APPROVAL-03", "identitas approval wajib diisi")
    # Approval TEST sudah ditandai bukan manusia, jadi penjaga identitas hanya
    # berlaku untuk approval yang mengaku sebagai persetujuan manusia.
    if not is_test and looks_like_agent(identity):
        raise Refused(
            "R-APPROVAL-03",
            f"identitas '{identity}' terbaca sebagai agen/model, bukan manusia; "
            "PASS Reviewer bukan approval",
            identity=identity,
        )

    draft = current_draft(state, args.dir)
    if draft is None:
        raise Refused("R-APPROVAL-04", "tidak ada draft tercatat untuk putaran ini")
    review = last_accepted_review(state)
    if review is None:
        raise Refused(
            "R-APPROVAL-05",
            f"belum ada review yang diterima untuk putaran {state['round']}",
        )
    if review["draft_sha256"] != draft["sha256_now"]:
        raise Refused(
            "R-APPROVAL-04",
            "draft berubah setelah review; ajukan review ulang sebelum approval",
            reviewed=review["draft_sha256"],
            actual=draft["sha256_now"],
        )
    if args.draft:
        wanted = args.draft.replace("\\", "/").lstrip("./")
        if wanted != draft["path"]:
            raise Refused(
                "R-APPROVAL-04",
                f"draft yang diminta {wanted} bukan draft yang di-review {draft['path']}"
                + (
                    " (yang direview adalah bundel; sebut path bundel, bukan salah satu isinya: "
                    + ", ".join(e["path"] for e in review.get("reviewed_files", []))
                    + ")"
                    if review.get("reviewed_files")
                    else ""
                ),
                wanted=wanted,
                reviewed=draft["path"],
            )

    # 4b. Setiap file yang dirujuk review harus masih ada dan hash-nya sama.
    #     Tanpa ini, satu format bisa diubah setelah review tanpa terdeteksi.
    reviewed = review.get("reviewed_files") or []
    for e in reviewed:
        full = os.path.join(args.dir, e["path"])
        if not os.path.isfile(full):
            raise Refused(
                "R-APPROVAL-04",
                f"file yang direview hilang: {e['path']}; approval tidak sah",
                path=e["path"],
            )
        actual, _ = sha256_file(full)
        if actual != e["sha256"]:
            raise Refused(
                "R-APPROVAL-04",
                "file yang direview berubah setelah review; ajukan review ulang",
                path=e["path"],
                reviewed=e["sha256"],
                actual=actual,
            )

    if not args.bind:
        raise Refused(
            "R-APPROVAL-06",
            "approval wajib mengikat minimal satu sumber dan rulebook (--bind)",
        )
    bindings = []
    for raw in args.bind:
        full = resolve_path(args.dir, raw)
        if not os.path.isfile(full):
            raise Refused("R-APPROVAL-06", f"file ikatan tidak ada: {raw}")
        digest, size = sha256_file(full)
        bindings.append(
            {
                "path": raw.replace("\\", "/"),
                "sha256": digest,
                "bytes": size,
            }
        )
    if not any("rulebook" in b["path"].lower() for b in bindings):
        raise Refused(
            "R-APPROVAL-06",
            "approval wajib mengikat file rulebook (APPROVAL-01: rulebook yang berubah "
            "membatalkan approval)",
        )

    # 5b. File siap-salin yang disetujui harus ikut diikat hash-nya. Kalau putaran
    #     ini punya hasil export-copy, semuanya wajib disebut lewat --copy supaya
    #     perubahan pada teks yang akan ditempel benar-benar membatalkan approval.
    round_copies = [
        c for c in state.get("copy_exports", []) if c.get("round") == state["round"]
    ]
    wanted_copies = [p.replace("\\", "/").lstrip("./") for p in (args.copy or [])]
    missing_copy = [c["out"] for c in round_copies if c["out"] not in wanted_copies]
    if missing_copy:
        raise Refused(
            "R-APPROVAL-07",
            "approval wajib mengikat file siap-salin putaran ini lewat --copy: "
            + ", ".join(missing_copy),
            missing=missing_copy,
        )
    by_out = {c["out"]: c for c in round_copies}
    copy_files = []
    for rel in wanted_copies:
        full = os.path.join(args.dir, rel)
        if not os.path.isfile(full):
            raise Refused("R-APPROVAL-07", f"file siap-salin tidak ada: {rel}", path=rel)
        digest, size = sha256_file(full)
        rec_copy = by_out.get(rel)
        if rec_copy is None:
            raise Refused(
                "R-APPROVAL-07",
                f"file siap-salin tidak tercatat lewat export-copy putaran ini: {rel}",
                path=rel,
            )
        if rec_copy["out_sha256"] != digest:
            raise Refused(
                "R-APPROVAL-07",
                "file siap-salin berubah sejak export-copy; ekspor ulang sebelum approval",
                path=rel,
                exported=rec_copy["out_sha256"],
                actual=digest,
            )
        copy_files.append({"path": rel, "sha256": digest, "bytes": size})

    review_full = os.path.join(args.dir, review["review_path"])
    if not os.path.isfile(review_full):
        raise Refused("R-APPROVAL-01", f"file review hilang: {review['review_path']}")
    review_sha_now, _ = sha256_file(review_full)

    rec = {
        "at": now_iso(),
        "by": identity,
        "human": not is_test,
        "test": is_test,
        "label": "TEST — bukan persetujuan manusia" if is_test else "HUMAN",
        "round": state["round"],
        "draft_path": draft["path"],
        "draft_sha256": draft["sha256_now"],
        "review_path": review["review_path"],
        "review_sha256": review_sha_now,
        "reviewed_files": [
            {"path": e["path"], "sha256": e["sha256"]} for e in reviewed
        ],
        "copy_files": copy_files,
        "bindings": bindings,
        "note": args.note or "",
        "valid": True,
    }
    state["approvals"].append(rec)
    state["status"] = "APPROVED" if not args.reject else "REJECTED"
    save_state(
        args.dir,
        state,
        "approve" if not args.reject else "reject",
        identity,
        by=identity,
        test=is_test,
        draft_path=draft["path"],
        draft_sha256=draft["sha256_now"],
        review_sha256=review_sha_now,
        reviewed_files=[{"path": e["path"], "sha256": e["sha256"]} for e in reviewed],
        copy_files=copy_files,
        bindings=[{"path": b["path"], "sha256": b["sha256"]} for b in bindings],
        status_after=state["status"],
    )
    print(
        json.dumps(
            {
                "ok": True,
                "status": state["status"],
                "by": identity,
                "test": is_test,
                "label": rec["label"],
                "draft_path": draft["path"],
                "draft_sha256": draft["sha256_now"],
                "reviewed_files": len(reviewed),
                "copy_files": len(copy_files),
                "bindings": len(bindings),
            },
            ensure_ascii=False,
        )
    )
    return EXIT_OK


def _approval_problems(state: dict, campaign_dir: str) -> list:
    """Hitung ulang semua ikatan approval. Baca-saja."""
    problems = []
    for idx, ap in enumerate(state.get("approvals", [])):
        if not ap.get("valid", True):
            problems.append(
                {"approval": idx, "code": "R-APPROVAL-01", "reason": "sudah dibatalkan"}
            )
            continue
        draft_full = os.path.join(campaign_dir, ap["draft_path"])
        if not os.path.isfile(draft_full):
            problems.append(
                {
                    "approval": idx,
                    "code": "R-APPROVAL-01",
                    "reason": f"draft hilang: {ap['draft_path']}",
                }
            )
            continue
        digest, _ = sha256_file(draft_full)
        if digest != ap["draft_sha256"]:
            problems.append(
                {
                    "approval": idx,
                    "code": "R-APPROVAL-01",
                    "reason": "isi draft berubah setelah approval",
                    "path": ap["draft_path"],
                    "approved": ap["draft_sha256"],
                    "actual": digest,
                }
            )
            continue

        # Setiap file yang dirujuk review dan setiap file siap-salin yang ikut
        # disetujui diperiksa ulang. Approval lama (sebelum field ini ada) tetap
        # sah: daftar kosong berarti tidak ada yang perlu dihitung ulang.
        changed = None
        for e in ap.get("reviewed_files", []) or []:
            full = os.path.join(campaign_dir, e["path"])
            if not os.path.isfile(full):
                changed = {
                    "approval": idx,
                    "code": "R-APPROVAL-01",
                    "reason": f"file yang direview hilang: {e['path']}",
                    "path": e["path"],
                }
                break
            actual, _ = sha256_file(full)
            if actual != e["sha256"]:
                changed = {
                    "approval": idx,
                    "code": "R-APPROVAL-01",
                    "reason": "file yang direview berubah setelah approval",
                    "path": e["path"],
                    "approved": e["sha256"],
                    "actual": actual,
                }
                break
        if changed is None:
            for c in ap.get("copy_files", []) or []:
                full = os.path.join(campaign_dir, c["path"])
                if not os.path.isfile(full):
                    changed = {
                        "approval": idx,
                        "code": "R-APPROVAL-01",
                        "reason": f"file siap-salin hilang: {c['path']}",
                        "path": c["path"],
                    }
                    break
                actual, _ = sha256_file(full)
                if actual != c["sha256"]:
                    changed = {
                        "approval": idx,
                        "code": "R-APPROVAL-01",
                        "reason": "file siap-salin berubah setelah approval",
                        "path": c["path"],
                        "approved": c["sha256"],
                        "actual": actual,
                    }
                    break
        if changed is not None:
            problems.append(changed)
            continue
        review_full = os.path.join(campaign_dir, ap["review_path"])
        if not os.path.isfile(review_full):
            problems.append(
                {
                    "approval": idx,
                    "code": "R-APPROVAL-01",
                    "reason": f"file review hilang: {ap['review_path']}",
                }
            )
            continue
        r_digest, _ = sha256_file(review_full)
        if r_digest != ap["review_sha256"]:
            problems.append(
                {
                    "approval": idx,
                    "code": "R-APPROVAL-01",
                    "reason": "file review berubah setelah approval",
                    "path": ap["review_path"],
                }
            )
            continue
        for b in ap.get("bindings", []):
            full = resolve_path(campaign_dir, b["path"])
            if not os.path.isfile(full):
                problems.append(
                    {
                        "approval": idx,
                        "code": "R-APPROVAL-01",
                        "reason": f"file ikatan hilang: {b['path']}",
                    }
                )
                break
            b_digest, _ = sha256_file(full)
            if b_digest != b["sha256"]:
                problems.append(
                    {
                        "approval": idx,
                        "code": "R-APPROVAL-01",
                        "reason": "sumber/rulebook berubah setelah approval",
                        "path": b["path"],
                        "approved": b["sha256"],
                        "actual": b_digest,
                    }
                )
                break
    return problems


def cmd_check_gate(args) -> int:
    """Baca-saja. Dipakai sebelum apa pun masuk submission/."""
    state = ensure_schema(load_state(args.dir))
    problems = []
    if state["status"] != "APPROVED":
        problems.append(
            {
                "code": "R-GATE-01",
                "reason": f"status {state['status']} bukan APPROVED",
            }
        )
    approvals = state.get("approvals", [])
    live = [a for a in approvals if a.get("valid", True)]
    if not live:
        problems.append({"code": "R-GATE-01", "reason": "tidak ada approval yang sah"})
    else:
        ap = live[-1]
        if ap.get("test") and not args.allow_test:
            problems.append(
                {
                    "code": "R-GATE-02",
                    "reason": "approval terakhir berlabel TEST, bukan persetujuan manusia",
                }
            )
        problems.extend(_approval_problems(state, args.dir))
    ok = not problems
    print(
        json.dumps(
            {
                "ok": ok,
                "campaign": state.get("campaign_id"),
                "round": state["round"],
                "status": state["status"],
                "approvals": len(approvals),
                "problems": problems,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK if ok else EXIT_REFUSED


def cmd_approval_package(args) -> int:
    """Baca-saja. Tampilkan persis apa yang akan disetujui, bukan ringkasannya.

    Tujuannya menutup satu jenis kesalahan: petunjuk approval yang menunjuk
    draft berbeda dari yang benar-benar direview controller. Semua path di sini
    berasal dari state, bukan dari teks yang diketik ulang.
    """
    state = ensure_schema(load_state(args.dir))
    draft = current_draft(state, args.dir)
    review = last_accepted_review(state)
    problems = _approval_problems(state, args.dir)

    pkg = {
        "campaign": state.get("campaign_id"),
        "round": state["round"],
        "max_rounds": state["max_rounds"],
        "status": state["status"],
        "human_approval": "not_granted",
        "publication": "none",
        "draft_path": draft["path"] if draft else None,
        "draft_sha256": draft["sha256_now"] if draft else None,
        "review_path": review["review_path"] if review else None,
        "review_sha256": review["review_sha256"] if review else None,
        "verdict": review["verdict"] if review else None,
        "reviewed_files": [],
        "copy_files": [],
        "bindings": [],
        "problems": problems,
    }

    if draft and review:
        by_path = {e["path"]: e for e in review.get("reviewed_files", []) or []}
        entries = by_path or {draft["path"]: {"path": draft["path"], "sha256": draft["sha256"]}}
        for path, e in entries.items():
            full = os.path.join(args.dir, path)
            exists = os.path.isfile(full)
            actual, size = sha256_file(full) if exists else (None, None)
            pkg["reviewed_files"].append(
                {
                    "path": path,
                    "reviewed_sha256": e["sha256"],
                    "sha256_now": actual,
                    "bytes": size,
                    "exists": exists,
                    "match": actual == e["sha256"],
                }
            )

    # Teks siap-salin ditampilkan apa adanya supaya yang disetujui adalah teks
    # itu, bukan deskripsi tentangnya.
    for c in state.get("copy_exports", []):
        if c.get("round") != state["round"]:
            continue
        full = os.path.join(args.dir, c["out"])
        exists = os.path.isfile(full)
        actual, size = sha256_file(full) if exists else (None, None)
        text = ""
        if exists:
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
        pkg["copy_files"].append(
            {
                "path": c["out"],
                "source": c["source"],
                "exported_sha256": c["out_sha256"],
                "sha256_now": actual,
                "bytes": size,
                "exists": exists,
                "match": actual == c["out_sha256"],
                "text": text,
            }
        )

    for raw in args.bind or []:
        full = resolve_path(args.dir, raw)
        exists = os.path.isfile(full)
        digest, size = sha256_file(full) if exists else (None, None)
        pkg["bindings"].append(
            {
                "path": raw.replace("\\", "/"),
                "sha256": digest,
                "bytes": size,
                "exists": exists,
            }
        )

    cmd = [
        "python3",
        "runs/controller.py",
        "approve",
        "--dir",
        args.dir,
        "--by",
        "<NAMA ANDA>",
    ]
    if draft:
        cmd += ["--draft", draft["path"]]
    for b in pkg["bindings"]:
        cmd += ["--bind", b["path"]]
    for c in pkg["copy_files"]:
        cmd += ["--copy", c["path"]]
    cmd += ["--note", "<alasan singkat persetujuan>"]
    pkg["approval_command"] = " ".join(
        f'"{c}"' if " " in c else c for c in cmd
    )
    pkg["approval_by"] = (
        "Perintah di atas harus dijalankan dan ditandatangani oleh Anda sendiri. "
        "Model tidak boleh mengisi --by atas nama Anda, dan verdict PASS dari "
        "Reviewer bukan approval manusia."
    )
    pkg["ok"] = (
        not problems
        and state["status"] == "READY_FOR_APPROVAL"
        and all(f["match"] for f in pkg["reviewed_files"])
        and all(f["match"] for f in pkg["copy_files"])
    )
    print(json.dumps(pkg, ensure_ascii=False, indent=2))
    return EXIT_OK if pkg["ok"] else EXIT_REFUSED


def cmd_recheck(args) -> int:
    """Batalkan approval yang ikatannya tidak lagi cocok.

    Ini satu-satunya mutasi yang sah dari status terminal APPROVED, karena
    bukan transisi yang dipilih agen melainkan konsekuensi hash yang berubah
    (APPROVAL-01). Status turun ke REVIEWED supaya konten harus di-review ulang.
    """
    state = ensure_schema(load_state(args.dir))
    problems = _approval_problems(state, args.dir)
    if not problems:
        print(
            json.dumps(
                {"ok": True, "invalidated": 0, "status": state["status"]},
                ensure_ascii=False,
            )
        )
        return EXIT_OK

    touched = {p["approval"] for p in problems}
    for idx in sorted(touched):
        ap = state["approvals"][idx]
        if not ap.get("valid", True):
            continue
        ap["valid"] = False
        ap["invalidated_at"] = now_iso()
        reason = next(p["reason"] for p in problems if p["approval"] == idx)
        state["invalidations"].append(
            {
                "at": ap["invalidated_at"],
                "approval": idx,
                "code": "R-APPROVAL-01",
                "reason": reason,
                "by": args.actor,
            }
        )

    demoted = False
    if state["status"] in ("APPROVED", "REJECTED"):
        state["status"] = "REVIEWED"
        demoted = True

    save_state(
        args.dir,
        state,
        "approval-invalidated",
        args.actor,
        invalidated=len(touched),
        demoted=demoted,
        reasons=problems,
    )
    print(
        json.dumps(
            {
                "ok": False,
                "refused": True,
                "code": "R-APPROVAL-01",
                "invalidated": len(touched),
                "status": state["status"],
                "problems": problems,
                "message": "approval dibatalkan karena hash berubah; ajukan review + approval ulang",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_REFUSED


def cmd_export_copy(args) -> int:
    """Ambil hanya blok siap-salin; catatan perubahan tetap di file versi."""
    state = ensure_schema(load_state(args.dir))
    source_full = resolve_path(args.dir, args.source)
    if not os.path.isfile(source_full):
        raise SystemExit(f"error: sumber tidak ditemukan: {source_full}")
    with open(source_full, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    heading = args.heading
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading.lower() in line.lower():
            start = i
            break
    if start is None:
        raise Refused(
            "R-COPY-02",
            f"heading blok '{heading}' tidak ditemukan di {args.source}",
        )
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    block = "\n".join(lines[start + 1 : end]).strip()
    block = re.sub(r"\n+-{3,}\s*$", "", block).strip()

    forbidden = [t.strip() for t in (args.forbid or "").split(",") if t.strip()]
    hits = [t for t in forbidden if t.lower() in block.lower()]
    if hits:
        raise Refused(
            "R-COPY-01",
            "blok siap-salin masih memuat string terlarang: " + ", ".join(hits),
            hits=hits,
        )

    out_rel = args.out or os.path.join("copy", os.path.basename(args.source) + ".copy.txt")
    out_full = resolve_path(args.dir, out_rel)
    if os.path.exists(out_full) and not args.force:
        raise Refused("R-COPY-03", f"file tujuan sudah ada: {out_rel}")
    os.makedirs(os.path.dirname(out_full) or ".", exist_ok=True)
    with open(out_full, "w", encoding="utf-8") as fh:
        fh.write(block + "\n")
        fh.flush()
        os.fsync(fh.fileno())

    src_sha, _ = sha256_file(source_full)
    out_sha, out_size = sha256_file(out_full)
    out_rel_norm = rel_to_campaign(args.dir, out_full, code="R-COPY-03")

    if not args.no_record:
        state["copy_exports"].append(
            {
                "at": now_iso(),
                "source": rel_to_campaign(args.dir, source_full),
                "source_sha256": src_sha,
                "out": out_rel_norm,
                "out_sha256": out_sha,
                "bytes": out_size,
                "forbid": forbidden,
                "round": state["round"],
            }
        )
        state["artifacts"].append(
            {
                "at": now_iso(),
                "kind": "copy",
                "actor": args.actor,
                "round": state["round"],
                "path": out_rel_norm,
                "sha256": out_sha,
                "bytes": out_size,
            }
        )
        save_state(
            args.dir,
            state,
            "export-copy",
            args.actor,
            source=rel_to_campaign(args.dir, source_full),
            source_sha256=src_sha,
            out=out_rel_norm,
            out_sha256=out_sha,
        )

    print(
        json.dumps(
            {
                "ok": True,
                "out": out_rel_norm,
                "bytes": out_size,
                "sha256": out_sha,
                "source_sha256": src_sha,
                "forbidden_checked": forbidden,
                "recorded": not args.no_record,
            },
            ensure_ascii=False,
        )
    )
    return EXIT_OK


def cmd_set_round(args) -> int:
    """Ada hanya untuk membuktikan penolakan. Tidak pernah menurunkan round."""
    state = ensure_schema(load_state(args.dir))
    requested = args.round
    if requested == state["round"]:
        print(json.dumps({"ok": True, "round": state["round"], "noop": True}, ensure_ascii=False))
        return EXIT_OK
    if requested < state["round"]:
        record_violation(
            state,
            "R-ROUND-01",
            f"percobaan menurunkan round {state['round']} -> {requested}",
            args.actor,
        )
        save_state(
            args.dir,
            state,
            "set-round-refused",
            args.actor,
            requested=requested,
            current=state["round"],
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "refused": True,
                    "code": "R-ROUND-01",
                    "round": state["round"],
                    "message": "round tidak boleh turun atau direset",
                },
                ensure_ascii=False,
            )
        )
        return EXIT_REFUSED
    record_violation(
        state,
        "R-ROUND-01",
        f"percobaan melompatkan round {state['round']} -> {requested}",
        args.actor,
    )
    save_state(
        args.dir,
        state,
        "set-round-refused",
        args.actor,
        requested=requested,
        current=state["round"],
    )
    print(
        json.dumps(
            {
                "ok": False,
                "refused": True,
                "code": "R-ROUND-01",
                "round": state["round"],
                "message": "round hanya naik lewat begin-round",
            },
            ensure_ascii=False,
        )
    )
    return EXIT_REFUSED


def cmd_reset(args) -> int:
    state = ensure_schema(load_state(args.dir))
    record_violation(
        state,
        "R-ROUND-01",
        f"permintaan reset dari {args.actor}: {args.reason or 'tanpa alasan'}",
        args.actor,
    )
    save_state(args.dir, state, "reset-refused", args.actor, reason=args.reason or "")
    print(
        json.dumps(
            {
                "ok": False,
                "refused": True,
                "code": "R-ROUND-01",
                "round": state["round"],
                "message": "reset hanya sah lewat campaign_id baru",
            },
            ensure_ascii=False,
        )
    )
    return EXIT_REFUSED


def cmd_show(args) -> int:
    state = load_state(args.dir)
    print(json.dumps(state, indent=2, ensure_ascii=False))
    return EXIT_OK


def cmd_verify(args) -> int:
    state = ensure_schema(load_state(args.dir))
    bad = []
    for art in state["artifacts"]:
        path = os.path.join(args.dir, art["path"])
        if not os.path.isfile(path):
            bad.append({"path": art["path"], "problem": "hilang"})
            continue
        digest, size = sha256_file(path)
        if digest != art["sha256"]:
            bad.append(
                {
                    "path": art["path"],
                    "problem": "hash berubah",
                    "recorded": art["sha256"],
                    "actual": digest,
                }
            )
        elif size != art["bytes"]:
            bad.append(
                {
                    "path": art["path"],
                    "problem": "ukuran berubah",
                    "recorded": art["bytes"],
                    "actual": size,
                }
            )
    approval_problems = _approval_problems(state, args.dir)
    print(
        json.dumps(
            {
                "ok": not bad,
                "checked": len(state["artifacts"]),
                "mismatches": bad,
                "approvals": len(state.get("approvals", [])),
                "approval_problems": approval_problems,
                "round": state["round"],
                "status": state["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK if not bad else EXIT_REFUSED


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="controller.py",
        description="Controller putaran + gerbang approval BuktiLaunch. Tanpa dependency eksternal.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--dir", required=True, help="direktori campaign (berisi state.json)")
        sp.add_argument("--actor", default="controller", help="siapa yang memanggil")
        sp.set_defaults(func=fn)
        return sp

    sp = add("init", cmd_init, "buat state.json untuk campaign baru")
    sp.add_argument("--campaign", required=True)
    sp.add_argument("--max-rounds", type=int, default=MAX_ROUNDS_DEFAULT)
    sp.add_argument("--force", action="store_true", help="legacy; tetap menolak menimpa campaign yang ada")

    add("begin-round", cmd_begin_round, "mulai putaran draft->review")

    sp = add("set-status", cmd_set_status, "transisi status yang sah")
    sp.add_argument("--status", required=True)

    sp = add("dispatch", cmd_dispatch, "terbitkan izin memanggil agen")
    sp.add_argument("--role", required=True, choices=["writer", "reviewer"])

    sp = add("record-artifact", cmd_record_artifact, "catat artefak + sha256")
    sp.add_argument("--kind", required=True, help="plan|draft|review|diff|copy|other")
    sp.add_argument("--path", required=True)

    sp = add("submit-review", cmd_submit_review, "masukkan verdict dari file review")
    sp.add_argument("--verdict", required=True, choices=sorted(VERDICT_TO_STATUS))
    sp.add_argument("--review-path", required=True)
    sp.add_argument("--dispatch-token", default=None)

    sp = add("approve", cmd_approve, "gerbang approval manusia (mengikat hash)")
    sp.add_argument("--by", required=True, help="identitas manusia yang menyetujui")
    sp.add_argument("--draft", default=None, help="draft yang disetujui (opsional, harus cocok)")
    sp.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="PATH",
        help="file yang ikut diikat (sumber, rulebook); boleh berulang",
    )
    sp.add_argument("--note", default="")
    sp.add_argument("--reject", action="store_true", help="catat penolakan manusia")
    sp.add_argument(
        "--copy",
        action="append",
        default=[],
        metavar="PATH",
        help="file siap-salin yang ikut disetujui (wajib bila putaran ini punya export-copy)",
    )
    sp.add_argument(
        "--test",
        action="store_true",
        help="tandai approval sebagai TEST (bukan persetujuan manusia)",
    )

    sp = add(
        "approval-package",
        cmd_approval_package,
        "tampilkan paket yang akan disetujui (baca-saja)",
    )
    sp.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="PATH",
        help="sumber/rulebook yang akan diikat, untuk menyusun perintah approval",
    )

    sp = add("check-gate", cmd_check_gate, "periksa apakah gerbang approval terpenuhi")
    sp.add_argument(
        "--allow-test",
        action="store_true",
        help="izinkan approval TEST memenuhi gerbang (hanya untuk pengujian)",
    )

    add("recheck", cmd_recheck, "batalkan approval yang ikatannya berubah")

    sp = add("export-copy", cmd_export_copy, "pisahkan blok siap-salin")
    sp.add_argument("--source", required=True)
    sp.add_argument("--out", default=None)
    sp.add_argument("--heading", default="siap tempel")
    sp.add_argument("--forbid", default="", help="daftar string terlarang, pisah koma")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--no-record", action="store_true", help="jangan tulis ke state")

    sp = add("set-round", cmd_set_round, "uji penolakan penurunan round")
    sp.add_argument("--round", type=int, required=True)

    sp = add("reset", cmd_reset, "uji penolakan reset")
    sp.add_argument("--reason", default="")

    add("show", cmd_show, "tampilkan state.json")
    add("verify", cmd_verify, "hitung ulang hash artefak")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Refused as exc:
        log_refusal(args, exc)
        payload = {
            "ok": False,
            "refused": True,
            "code": exc.code,
            "message": exc.message,
        }
        if exc.detail:
            payload["detail"] = exc.detail
        print(json.dumps(payload, ensure_ascii=False))
        return EXIT_REFUSED


def log_refusal(args, exc: "Refused") -> None:
    """Catat penolakan guard ke history supaya jejaknya tidak hilang.

    Tidak mengubah `round` maupun `status`; hanya menambah entri audit.
    Penolakan yang sudah menulis sendiri (batas putaran, set-round, reset)
    tidak lewat jalur ini, jadi tidak tercatat dua kali.
    """
    campaign_dir = getattr(args, "dir", None)
    if not campaign_dir:
        return
    try:
        state = load_state(campaign_dir)
    except SystemExit:
        return
    ensure_schema(state)
    try:
        save_state(
            campaign_dir,
            state,
            "refused",
            getattr(args, "actor", "controller"),
            code=exc.code,
            message=exc.message,
            command=getattr(args, "command", None),
        )
    except OSError:
        # Jangan menutupi penolakan aslinya hanya karena audit gagal ditulis.
        pass


if __name__ == "__main__":
    sys.exit(main())
