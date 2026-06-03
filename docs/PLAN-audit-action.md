# PLAN-audit-action.md — Structural-coherence audit: action plan

**Type:** sharded, **executable by `/run-plan`**.  Implements the proposals adjudicated at the
`PLAN-audit.md` A4 HALT (2026-06-03).  Each session is one commit-shaped deliverable ending green
(`~/.local/bin/tox -m analyze` clean: tests pass, **100% branch coverage**, mypy strict, ruff,
pylint 10.00, pyupgrade).

**Design intent (re-read at every track boundary):** make the app code, test code, and CLI
*coherent, logical, intuitive, and relentlessly minimal at every level of structure* — without
regressing the journal-provenance or defensive-download invariants.

**User rulings at A4 (frozen):**
1. **Full module splits** — Lane 2 goes deep: extract the shared primitive AND split
   `_pipeline.py` (maint ops) and `_pipeline_io.py` (high-level reads).
2. **Hoist shared test factories to `tests/conftest.py`** — over the existing
   self-containment choice.
3. Action plan sharded now (this file).

**Provenance:** findings + proposal IDs (`P-A1.a`, `P-A3.d`, …) live in `docs/PLAN-audit.md`'s
Findings register.  This file references them by ID rather than restating.

---

## Tracks and orthogonality

Three tracks.  **Track Q is independent** (ship first, lowest risk).  **Track S is a serial chain**
(substrate → splits → test reorg) plus two independent tail sessions.  **Track C is mostly
independent** of S, sequenced after Q on `__main__.py`.

```
Track Q (conformance+safety) ── independent ───────────────── ship first
Track S:  S1(substrate) ─→ S2 ─→ S3 ─→ S4
                       └─→ S5, S6 (independent of the S2–S4 chain)
Track C:  C1, C2  (after Q touches __main__.py)
                       C-consolidate (P-A3.g) ── stays in BACKLOG until S1 lands
```

Category (per `multi-session-planning.md`): **S1 is substrate** (over-specify the primitive's
signature; freezes C-PROV).  S2/S3 are refactor-moves.  S4 is test reorg.  S5/S6 are independent
refactors.  Q* and C* are self-contained algorithm-class sessions.

---

## Cross-session contracts

### C-PROV (frozen at S1 — prose + test enforced)
The transaction-journal provenance chain (AGENTS.md "Transaction journal" invariant).  A
`"copied"`/`"repathed"`/`"regrouped"`/`"unified"` journal entry is appended **only after** the file
passes: SHA-256(dest)==SHA-256(src) **and** `_verify_copy` (tag round-trip + cover bytes + mtime).
The extracted primitive `_move_verify_journal` (S1) is the **single site** that may append these
entries; the ordering inside it is the contract.  Every downstream session (S2, S3, and any future
maintenance command) consumes the primitive and MUST NOT append a move-entry by any other path.
**Test-enforced:** S1 adds a regression test asserting no journal entry exists after a forced
`_verify_copy` failure.

### C-DL (inherited, prose-enforced)
Defensive-download posture (AGENTS.md).  No network code is in this plan's scope; any Track C flag
change touching fetch paths preserves the `@_mb_retry` + `_mb_call` two-layer pattern.  Stated so a
`/run-plan` discovery touching it pages the juncture.

### C-MOVE (frozen at S1 — compiler-enforced)
The primitive's signature is the substrate interface S2/S3 sit on.  Provisional:
`_move_verify_journal(plan_pairs, *, journal_path, action, dest_root, now, release_id="") -> int`
returning the moved count; `_resolve_current_lib(journal) -> dict[Path, str]` for the lineage walk.
Over-specify at S1 (include `release_id` even though `repath` passes `""`) so S2 needn't widen it.

---

## Sessions

### Track Q — conformance + safety (independent; ship first)

**Q1 — Mechanical conformance sweep.**  Fix the A0 findings carrying no design choice.
- `_pipeline.py:1402` — add `# pragma: no cover` to the `match ext:` `case _:` arm.
- Tests: replace `Any` in `test_mb_helpers.py` (4 sites + import) with `MagicMock` (TYPE_CHECKING)
  / `JSON`; replace direct `unittest.mock.patch` for `sys.argv` (`test_main.py`) and `os.environ`
  (`test_mb_helpers.py`) with `mocker`/`monkeypatch`; fix the ~56 >128-char lines; parametrize the
  obvious in-body loops (`P-A2.c`).
- **Files:** `_pipeline.py`, `tests/unit/test_main.py`, `tests/unit/test_mb_helpers.py`,
  `tests/unit/test_pipeline.py`, `tests/unit/test_annotator.py`.
- **Green gate, commit title:** `audit Q1 Conformance sweep (pragma, test Any/mock/line-length)`.

**Q2 — `repath` confirmation parity + CLI consistency** (point-items 3+5; `P-A3.a/b/c/h`).
- `_pipeline.repath`: add `yes: bool = False`; add a confirmation prompt mirroring `regroup`
  (`_pipeline.py:1961-1975`) before the move loop, after the dry-run return.
- `__main__.py`: add `-y/--yes` to `repath_parser` + thread `yes=args.yes` into the `case "repath"`
  arm; declare the `audit` `--enrich`/`--origin-time`/`--diff` `mutually_exclusive_group`; extract
  `_add_acoustid_arg` and call it from both `_add_common_args` and the `audit` parser; fix the
  module + `main()` docstring subcommand list (add `regroup`).
- **Tests:** `repath` `yes=True`/prompt-confirm/prompt-abort; argparse error when two `audit` modes
  combine; `--acoustid-key` still works on `audit`.
- **Files:** `_pipeline.py`, `__main__.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_main.py`.
- **Commit title:** `audit Q2 repath confirmation parity + audit-mode exclusion`.

### Track S — app-code structure (serial chain S1→S2→S3→S4; S5/S6 independent)

**S1 — SUBSTRATE: extract the move/verify/journal primitive + lineage walk** (`P-A1.a`; freezes
C-PROV, C-MOVE).
- Extract `_move_verify_journal(...)` (the SHA + `os.replace` + EXDEV cross-fs fallback +
  `_verify_copy` + journal-append + empty-dir cleanup block) and `_resolve_current_lib(journal)`.
- Refactor `repath`, `regroup`, `unify` to call the primitive; refactor `repath`/`regroup`/`enrich`
  to call `_resolve_current_lib`.  Behaviour-preserving — no path/journal output changes.
- **C-PROV regression test** (the contract's test-enforcement): force a `_verify_copy` failure and
  assert no journal entry is appended.
- **Files:** `_pipeline.py`, `tests/unit/test_pipeline.py`, `tests/unit/test_main.py`.
- **Substrate pacing:** budget 1.5× — the signature is over-specified here for S2/S3.
- **Commit title:** `audit S1 Extract shared move/verify/journal primitive (C-PROV)`.

**S2 — Split maintenance ops into `_pipeline_maint.py`** (`P-A1.b`; consumes C-MOVE).
- Move `repath`, `regroup`, `unify`, `enrich` (+ `_move_verify_journal`, `_resolve_current_lib`,
  `_tags_from_file_dict`) to `_pipeline_maint.py`.  `_pipeline.py` keeps `run()` + ingest helpers.
- Update `__init__.py` re-exports and every test patch-target binding
  (`music_annotator._pipeline_maint.X`).
- **Files:** `_pipeline.py`, `_pipeline_maint.py` (new), `__init__.py`, test imports/patch strings.
- **Commit title:** `audit S2 Split maintenance ops into _pipeline_maint`.

**S3 — Split high-level read ops into `_audit.py`** (`P-A1.c`).
- Move `audit`, `diff_journal`, `detect_fragmented_releases`, the `_audit_*` helpers,
  `_journal_fragmentation_groups`, `_confirm_fragmentation` to `_audit.py`; `_pipeline_io.py` keeps
  raw I/O primitives + journal r/w + collision/corroboration.  Re-home `P-A1.e` strays
  (`parse_disc_*` → `_disc.py` or `_discover.py`; `_write_sidecars`/`_write_freedb_yaml` →
  `_pipeline_io.py`) as part of this seam if cheap; otherwise spin to a follow-up.
- Update `__init__.py` + test patch-targets.
- **Files:** `_pipeline_io.py`, `_audit.py` (new), `__init__.py`, test imports.
- **Commit title:** `audit S3 Split high-level read ops into _audit`.

**S4 — Test reorg tracking S2/S3 + conftest hoist** (`P-A2.a` + `P-A2.b` per ruling 2).
- Create `tests/conftest.py`; hoist `_w/_rec/_rel/_trk` and `_MINIMAL_FLAC/_MINIMAL_MP3`; drop the
  `# pylint: disable=duplicate-code` markers.
- Migrate `repath`/`regroup`/`unify`/`enrich` tests → `test_pipeline_maint.py`; `audit`/`diff` tests
  → `test_audit.py`, matching the S2/S3 module homes.  Pure moves; coverage stays 100%.
- **Files:** `tests/conftest.py` (new), `tests/unit/test_pipeline_maint.py` (new),
  `tests/unit/test_audit.py` (new), `test_pipeline.py`, `test_main.py`, `test_annotator.py`.
- **Commit title:** `audit S4 conftest hoist + test reorg tracking module splits`.

**S5 — `run()` named-pass decomposition** (`P-A1.d`; point-item 1; independent of S2–S4).
- Extract the work-group unification loop → `_apply_workgroup_unification(tags_map, all_media_pairs,
  release)` and the copy/tag/verify/journal loop → a named pass.  **No `WorkGroup` object** (A4
  ruling: sequential-accumulator passes don't benefit).  Behaviour-preserving.
- **Files:** `_pipeline.py`, `tests/unit/test_pipeline.py`.
- **Commit title:** `audit S5 Decompose run() into named passes`.

**S6 — `__init__.py` surface shrink** (`P-A1.f`; point-item 2; independent).
- Move test patch-targets to patch-at-binding-module convention; drop the `_reexports` tuple; trim
  `__all__` to the genuine public surface.
- **Files:** `__init__.py`, every test patch string still pointing at `music_annotator._X`.
- **Commit title:** `audit S6 Shrink __init__ public surface to real API`.

### Track C — CLI taxonomy (after Q on `__main__.py`)

**C1 — Split `audit` at the read/mutate boundary** (`P-A3.d`).
- Promote `enrich` (was `audit --enrich`), `diff` (was `audit --diff`), and a provenance-migration
  verb (was `audit --origin-time`) to top-level subcommands; bare `audit` stays read-only.  Move
  `--re-resolve`/`--acoustid-key` to the new `enrich` parser (via `_add_acoustid_arg`).
- **Files:** `__main__.py`, `tests/unit/test_main.py`.
- **Commit title:** `audit C1 Split audit verb at read/mutate boundary`.

**C2 — Dispatch helper + `rebuild --apply`** (`P-A3.e` + `P-A3.f`).
- Replace the 8× `try/except` dispatch arms with one helper (preserve `prune`'s continue-on-error
  and per-command log keys).  Rename `rebuild --write` → `--apply`; keep the inverted dry-run default
  and document it in the help + module docstring.
- **Files:** `__main__.py`, `tests/unit/test_main.py`.
- **Commit title:** `audit C2 Unify dispatch + rename rebuild --write to --apply`.

### Deferred (return to BACKLOG unless pulled in)
- **P-A1.g** — `models.py` split (cohesive; low priority).
- **P-A3.g** — merge `regroup`+`unify` → `consolidate --strategy`; **held until S1 lands**, then
  re-evaluate (high blast, loses single-strategy control).

---

## Progress ledger

| Session | Track | Depends on | State | Commit |
|---------|-------|-----------|-------|--------|
| Q1 | Q | — | done | 82e4456 |
| Q2 | Q | — | pending | |
| S1 | S | — (substrate) | pending | |
| S2 | S | S1 | pending | |
| S3 | S | S1 | pending | |
| S4 | S | S2, S3 | pending | |
| S5 | S | — (indep) | pending | |
| S6 | S | — (indep) | pending | |
| C1 | C | Q2 (`__main__.py`) | pending | |
| C2 | C | Q2, C1 | pending | |

## Action-frame digest (append discoveries here during `/run-plan`)

*(empty — populated by the chain at non-trivial iterations: C-PROV/C-MOVE flexes, coverage
surprises, module-seam discoveries that change a downstream session.)*
