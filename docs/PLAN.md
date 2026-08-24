<!-- Rolling action frame.  The previous sub-track (pre-R6d correctness fixes, four corrective
     sub-tracks, 8/8 rows done) closed 2026-08-24; its full plan, ledger, and discoveries live in
     this file's git history through commit 6ee245f.  This stub is rewritten when the next
     sub-track is derived. -->

# PLAN — no active sub-track

No agent-shardable sub-track is active.  Every code node in the library-completion arc is done and
green (`docs/ROADMAP.md` § "Current state").  The critical path is operator-paced:

1. **Repair turn (available now).**  Run the shipped diagnose-all dry run against live hades:
   `music-annotator preflight <library> --user-agent-email <email>` → review the consolidated
   report → run `repath` first (its extension-repair case must precede `regroup`/`unify`), then
   the other passes as the report indicates.  This repairs, under the move/verify/journal
   provenance chain, the live defects the closed sub-track fixed on fixtures: the extension-less
   FLACs, the empty `[]` collision-suffix dirs, and the album/edition-title top dirs.
2. **Drain `Original/`** (operator loop; exit = empty — Act I done).
3. **J3 go/no-go** on the destructive one-pass re-derivation, using the fresh preflight evidence.
4. **R6d one-pass re-derivation**, then the conventions spec (R6e).

Before opening any new sub-track, consult `docs/NOTES.md` § "Dormant decisions register" — the
consolidated list of deferred design paths and their firing triggers.
