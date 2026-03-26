# music-annotator

Copy and tag a classical music album using [MusicBrainz](https://musicbrainz.org) metadata and [Classical
Extras](https://github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras) tag conventions.

Given a MusicBrainz release MBID and a directory of source audio files, `music-annotator` fetches the full release metadata,
resolves the work hierarchy for each recording (movement → symphony → collection), classifies performers into CEA roles
(conductor, soloist, ensemble, …), and writes rich `_cwp_*` / `_cea_*` tags into copies of the files placed in a structured
destination tree.

MusicBrainz API is expected to conform to the [MusicBrainz XML Metadata
Schema](https://github.com/metabrainz/mmd-schema/blob/master/schema/musicbrainz_mmd-2.0.rng) and music-annotator validates the
returned data through pydantic models that are based on this contract in `src/music_annotator/models.py`.  The MusicBrainz API
documentation is [here](https://musicbrainz.org/doc/MusicBrainz_API).

Supported formats: **FLAC** (Vorbis Comments) and **MP3** (ID3v2.4).

## Installation

Requires Python ≥ 3.12.

```
pip install music-annotator
```

## Usage

```
music-annotator apply  <src_dir> <dest_dir> --release-id <MBID> --user-agent-email <EMAIL> [options]
music-annotator search <src_dir> [<src_dir> ...] <dest_dir> --user-agent-email <EMAIL> [options]
music-annotator prune  <src_dir> [<src_dir> ...] <dest_dir> [-y]
```

### `apply` — copy and tag for a known release MBID

| Argument | Description |
|---|---|
| `src_dir` | Directory containing source audio files |
| `dest_dir` | Root destination directory |
| `--release-id MBID` | MusicBrainz release MBID (UUID) |
| `--user-agent-email EMAIL` | Contact e-mail for the MB API user-agent |
| `--user-agent-app STRING` | App token (`AppName/Version`, default: `MusicAnnotator/<version>`) |
| `--dry-run` | Log planned operations without writing files |
| `--no-fetch-rels` | Skip per-recording lookups; produce minimal tags |
| `-d / --delete` | After a successful copy, prompt to delete the source directory |
| `-v / --verbose` | Enable DEBUG-level logging (must come before the subcommand) |

### `search` — search MusicBrainz, confirm, and apply

Same options as `apply`, minus `--release-id`, plus `--limit N` (default 10). Accepts one or more
`src_dir` positionals; all are processed in sequence against the same `dest_dir`.

### `prune` — verify journal and delete annotated source directories

| Argument | Description |
|---|---|
| `src_dir [src_dir ...]` | One or more source directories to inspect and potentially delete |
| `dest_dir` | Root destination directory (journal is read from here) |
| `-y / --yes` | Skip confirmation prompt and delete immediately |

Reads `<dest_dir>/music_annotator_journal.json`, performs exact presence checks on source and destination
files, then offers to delete `src_dir`.

### Examples

```sh
# Annotate with a known MBID
music-annotator apply \
  ~/Music/source/beethoven-9 ~/Music/tagged \
  --release-id 1c1e6a95-7b43-4a62-b2b9-2c2a3e0e8b0e \
  --user-agent-email me@example.com

# Annotate and offer to delete the source when done
music-annotator apply \
  ~/Music/source/beethoven-9 ~/Music/tagged \
  --release-id 1c1e6a95-7b43-4a62-b2b9-2c2a3e0e8b0e \
  --user-agent-email me@example.com --delete

# Search MB for a matching release, confirm, and apply
music-annotator search \
  ~/Music/source/beethoven-9 ~/Music/tagged \
  --user-agent-email me@example.com

# Prune a source directory after confirming journal entries
music-annotator prune \
  ~/Music/source/beethoven-9 ~/Music/tagged

# Prune multiple source directories at once without interactive confirmation
music-annotator prune \
  ~/Music/source/beethoven-9 \
  ~/Music/source/brahms-1 \
  ~/Music/tagged --yes
```

# Destination directory layout

```
<dest_root>/
  <Composer lastnames> - <Conductor; Ensemble>/          ← Latin or native script
    <Work title> [rec YYYY] or [rel YYYY]/               ← recording or release year
      [nn - <Intermediate division>/]                    ← only when hierarchy depth ≥ 3
        [nn - <Sub-intermediate division>/]              ← only when hierarchy depth ≥ 4
          nn - <Movement title>.<ext>
```

- `nn`: zero-padded 2 digits (3 if >99 siblings), directory-scoped, derived from MB `ordering-key`
  → `MOVEMENTNUMBER` → `track.position`.
- `MOVEMENTNUMBER` in tag and title string: composer's global numbering across the whole work
  (e.g. No. 39 in the Handel Messiah).  Distinct from the directory-local `nn` prefix.
- Performer component: conductor + ensemble names only (soloists excluded for now).
- Collection/cycle wrappers (Ring cycle, symphony cycles): excluded from filesystem, deferred to
  playlist generation.
- `[rec YYYY]`: year from `recording.first-release-date` (when this audio was first commercially
  released).  Falls back to `[rel YYYY]` from `release_group.first_release_date` or `release.date`.
  Omitted when no date is known.

## How it works

1. **Fetch release** — full track list, artists, labels, disc structure, cover art.
2. **Select medium** — for a single-disc release the sole medium is used. For multi-disc releases the medium whose track count
   matches the number of source files is selected automatically; if several mediums tie, a disc-number hint in the directory
   name (e.g. `disc2`) breaks the tie. A total mismatch raises an error asking the caller to supply the correct
   `--release-id` for that disc. After selection, source files are sorted by name and zipped with tracks in medium order; a
   remaining count mismatch logs a warning but does not abort.
3. **Per-track** (skipped with `--no-fetch-rels`):
   - Fetch recording artist relations (conductor, soloists, ensembles, …).
   - Resolve the work linked via a `"performance"` relation.
   - Walk the parent work chain (movement → top-level work) using `"parts"` relations; cycle detection prevents infinite loops.
4. **Build tags** — combine release, recording, and work data into `TrackTags`.
5. **Movement numbers** — assigned after all tracks are processed by grouping under each top-level work MBID.
6. **Write files** — copy source to destination, apply tags, embed cover art, restore original atime/mtime.

### Rate limiting

All MusicBrainz API calls are wrapped with a 6-attempt exponential backoff (2ⁿ seconds) on HTTP 429, 503, and 500 responses,
plus a 1-second polite delay after each successful call.

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `mutagen` | 1.47 | FLAC / MP3 tag writing |
| `musicbrainzngs` | 0.7.1 | MusicBrainz API client |
| `pydantic` | 2.12.5 | Data validation and models |
| `structlog` | 25.5.0 | Structured logging |

## Development

```sh
git clone https://github.com/jfindlay/music-annotator
cd music-annotator
python -m venv venv
venv/bin/pip install tox tox-uv
```

### Running checks

```sh
# All checks (build, test, types, format, lint, upgrade)
venv/bin/tox -m analyze

# Tests only (with coverage)
venv/bin/tox -e test

# Auto-fix formatting
venv/bin/tox -m edit
```

### Tox environments

| Environment | Tool(s) | Purpose |
|---|---|---|
| `build` | setuptools | Build wheel |
| `test` | pytest + pytest-cov | Tests + 100% branch coverage |
| `check_type` | mypy (strict) | Static type checking |
| `check_format` | ruff | Import ordering + code formatting |
| `check_lint` | pylint | Lint (must score 10.00/10) |
| `check_upgrade` | pyupgrade | Enforce Python 3.12+ idioms |
| `fix_format` | pyupgrade + ruff | Auto-fix formatting in place |

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
