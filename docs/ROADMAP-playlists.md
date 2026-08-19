# music-annotator — ROADMAP: playlist library (the reading-room lens)

**Chartered 2026-08-19** from the operator's design statement at the C-CLASS refutation (see
`docs/ROADMAP.md` Discoveries appendix, same date).  This is a **charter, not a session DAG** — the
arc's sub-tracks are not yet designed; they derive here once the naming-policy re-freeze lands.
Peer arc to the library-completion roadmap (`docs/ROADMAP.md`), which had ear-marked the playlist
library to graduate to its own roadmap when Act I neared completion; the operator's charter fired
that trigger early.

## Design intent (anchor — re-read at every sub-track boundary)

The library has two lenses (`docs/ROADMAP.md` design intent, 2026-07-17): **filesystem = catalog,
playlists = reading room**.  The catalog lens is uniform and fact-anchored — a universal top dir
over scholarship-stable components (composer, work, dates, performers).  **All editorial
organization lives in the playlist lens**, authored by deliberate, discriminating editorial
judgment — never inferred from MB's entropic free classification parameters.

**The epistemic criterion (operator, 2026-08-19).**  Defer to MB where variation is
*scholarship-driven* and converges under editorial pressure — composer identity, recording dates,
catalogue facts.  Never let MB's *free classification parameters* (release-group types,
is-classical predicates) define library topology: they are crowd-set, inconsistently applied, and
unanchored — entropy, not signal.  Playlists are where the discriminating judgment that MB cannot
supply gets authored.

**Coverage as a candidate invariant (to adjudicate at arc design):** the release-level strata
(1–3 below) should *cover* the library — every release reachable through at least one playlist —
the reading-room analogue of the catalog's full-inclusion principle.  The cross-release strata
(4–5) are curated views, not coverage obligations.

## The five playlist strata (the operator's taxonomy, 2026-08-19)

Release-level coverage strata:

1. **Canonical pop albums** — the album as the artist's authored artifact (e.g. "Weird Al"
   Yankovic — *Running with Scissors*).
2. **Regular albums** — single-release programmes, classical or otherwise (e.g. Karajan's 1987
   Neujahrskonzert).
3. **Multimedium releases** — box-scale sets experienced as one object: complete composer
   editions, complete symphony editions, complete recordings of a particular
   soloist/ensemble/conductor.

Cross-release strata:

4. **Metaplaylists** — constructed *above* the release level from the scholarship-stable axes:
   e.g. all of Karajan's Beethoven overtures as one cycle; all Rossini works in compositional
   order.  These are the lens's distinctive payoff — views no single release provides, built on
   exactly the MB data worth trusting (composer, work identity, dates).
5. **Purpose playlists** — functional programmes (ballet warmup, etc.); curated for use, not for
   catalog structure.

## Dependencies and sequencing

- **Gated on the naming-policy re-freeze** (`docs/ROADMAP.md`, J2 reopened 2026-08-19): playlists
  reference library files, so the catalog shape — universal top dir, C-INIT fate, class-dir
  migration — must settle first; the one-pass re-derivation (R6d) should land before bulk playlist
  generation so references are written once against final paths.
- **Consumes the catalog's embedded tags** (CWP/CEA work hierarchy, dates, performers) as the
  generation substrate for strata 1–4 candidates; the tags are the data, the operator judgment is
  the author.

## Open design questions (for arc design, not now)

- **Reference robustness** — playlists hold paths ("path is a handle") vs identifiers
  (MBID/AcoustID) with rendered paths derived; regeneratability after a repath.
- **Generation vs curation split** — strata 1–3 are heuristic-generatable candidates subject to
  operator ratification; strata 4–5 are authored.  Where the generated/authored boundary and the
  ratification workflow live.
- **Format and placement** — playlist format, storage location, naming conventions; how the
  reading-room is itself browsed.
- **Coverage audit** — whether/how `audit` gains a playlist-coverage pass (every release in ≥1
  release-level playlist).

## Out of scope

Catalog-lens policy (the peer roadmap owns it); MB-upstream edits; streaming/export integrations
until the local reading-room shape is proven.
