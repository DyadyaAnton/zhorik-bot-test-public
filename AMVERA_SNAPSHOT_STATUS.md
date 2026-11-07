# Amvera snapshot — public source transfer (2026-11-08)

Source: user-uploaded ZIP `budilnik-sandbox-code(35).zip` (contains Zhorik code). The original ZIP was backed up separately to the private Google Drive project archive under `01 — Жорик / 02 — Архив полных сборок`.

This public copy lives in `amvera_snapshot_2026-11-08/`. Source files were fetched from the original repository's `amvera-release` branch and published individually. A sample of key files was checked by Git blob SHA against the uploaded ZIP; **not every file has been independently compared against the ZIP**, so a complete byte-for-byte archive reconciliation is still pending.

Excluded from public transfer: `kbju_candidates_v1.csv`, `recipe01_external_menu_candidates.csv`, and six internal handoff/audit/TZ files: `DEVELOPER_LETTER.txt`, `MEGAPATCH16A_AUDIT_REPORT.txt`, `MINI_TZ.txt`, `PRICE07_MINI_TZ.txt`, `PRICE07_NEXT_STAGE_LOG.txt`, `TESTER_LETTER.txt`. No SQLite databases or .env files were present in the uploaded ZIP. A regex-based scan for common credential patterns found no matches; this is not a guarantee against all secret types.

The separate `pr6_changed_files/` folder holds five files from PR #6 commit `1c0607d2b36480d70ba2f08ca1e7e3ec68b05a18`. **These are separate snapshots, not a verified merged runnable build**. In particular, `menu_energy_snapshot.py` from PR #6 is not in the uploaded Amvera ZIP.

Status: **PUBLIC CODE REVIEW / NOT PRODUCTION APPROVED**. Do not deploy or treat this repository as a validated Amvera release without independent integration and QA.
