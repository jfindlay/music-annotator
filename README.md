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
music-annotator --release-id <MBID> --src-dir <path> --dest-dir <path> [options]
```

### Required arguments

| Argument | Description |
|---|---|
| `--release-id MBID` | MusicBrainz release MBID (UUID) |
| `--src-dir DIR` | Directory containing source audio files |
| `--dest-dir DIR` | Root destination directory |

### Optional arguments

| Argument | Default | Description |
|---|---|---|
| `--user-agent STRING` | `MusicAnnotator/0.1 music-annotator@example.com` | MB API user-agent (`"AppName/Version contact"`) |
| `--dry-run` | off | Log planned operations without writing files |
| `--no-fetch-rels` | off | Skip per-recording lookups; produce minimal tags |
| `-v / --verbose` | off | Enable DEBUG-level logging |

### Examples

```sh
# Full annotation
music-annotator \
  --release-id 1c1e6a95-7b43-4a62-b2b9-2c2a3e0e8b0e \
  --src-dir ~/Music/source/beethoven-9 \
  --dest-dir ~/Music/tagged \
  --user-agent "MyTagger/1.0 me@example.com"

# Quick run — basic Picard tags only, no work lookups
music-annotator \
  --release-id 1c1e6a95-7b43-4a62-b2b9-2c2a3e0e8b0e \
  --src-dir ~/Music/source/beethoven-9 \
  --dest-dir ~/Music/tagged \
  --no-fetch-rels

# Preview what would happen without touching files
music-annotator \
  --release-id 1c1e6a95-7b43-4a62-b2b9-2c2a3e0e8b0e \
  --src-dir ~/Music/source/beethoven-9 \
  --dest-dir ~/Music/tagged \
  --dry-run --verbose
```

## Destination layout

```
<dest_dir>/
  <Composer last names> - <Conductor; Ensemble>/
    <Work title> (<work MBID>)/
      <nn> - <movement title>.<ext>
```

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
