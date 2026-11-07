# BuktiLaunch

Prototipe untuk Challenge C — The Creator Agent, WorkBuddy Hackathon Indonesia 2026. BuktiLaunch membantu pemilik produk digital melacak setiap klaim pemasaran ke dokumen sumber dan meninjau versi konten yang sama sebelum menyetujui publikasi.

- `bukti-launch/` berisi konfigurasi Expert Team WorkBuddy serta definisi Planner, Writer, dan Reviewer.
- `runs/controller.py` mengelola status kampanye, batas tiga putaran review, dan persetujuan manusia yang terikat pada hash berkas.
- `rules/rulebook.md` mendefinisikan aturan sitasi dan verdict. Contoh di dalamnya fiktif.
- `ui/` dan `tools/build_review_snapshot.py` menampilkan hasil review dari run lokal.
- `tests/` berisi pengujian controller dengan data sintetis yang dibuat saat tes berjalan.

## Jalankan pengujian

Controller memakai pustaka standar Python 3.10+.

```sh
python3 runs/controller.py --help
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/scenario_round_limit.py
python3 tests/scenario_gate_chain.py
```

## Gunakan dengan WorkBuddy

Gunakan definisi agen di `bukti-launch/` dan siapkan brief, dokumen fakta serta ketentuan produk, manifest versi sumber, dan aturan gaya di workspace lokal. Planner menyusun ledger fakta. Writer membuat draft dari ledger. Reviewer mengevaluasi draft secara terpisah. Setelah review lolos, gunakan `approval-package` untuk memeriksa berkas yang akan disetujui dan `check-gate` untuk mengecek status persetujuan. Identitas yang dimasukkan lewat `--by` dicatat sebagai keterangan operator lokal.

Untuk membuka penampil, gunakan ID run lokal yang sudah memiliki state, review, sumber, dan sedikitnya dua hasil copy. Contoh perintah untuk run bernama `BL-LOCAL-001`.

```sh
python3 tools/build_review_snapshot.py BL-LOCAL-001
python3 -m http.server 8093 --bind 127.0.0.1
```

Buka `http://127.0.0.1:8093/ui/`. Simpan `ui/data.js` di workspace lokal karena snapshot memuat isi sumber dan konten dari run. Arsip sumber ini menyertakan definisi tim tanpa aset avatar.
