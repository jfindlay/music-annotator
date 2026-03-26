# Agent Guide — music-annotator

This document describes the codebase conventions, tooling, and workflow that an AI coding agent must follow when working on this
project.  For outstanding tasks, open design questions, and the backlog see `PLAN.md`.

## Repository layout

```
music-annotator/
  src/music_annotator/
    __init__.py         ← thin re-export layer; defines __all__ and public API surface
    __main__.py         ← CLI entry point
    models.py           ← Pydantic models for MB API + tag output (~900 lines)
    py.typed            ← PEP 561 marker
    _console.py         ← shared rich Console instance and configure_color()
    _mb_api.py          ← MB API wrappers: _mb_retry, fetch_*, init_mb, _get_bottom_work, _WORK_CACHE
    _artists.py         ← artist helper functions and string constants (is_ensemble, artist_credit_phrase, …)
    _works.py           ← work hierarchy traversal and date/key extraction (build_work_hierarchy, …)
    _tags.py            ← tag-building logic (build_cea_performers, build_track_tags, build_dest_path, …)
    _tagger.py          ← mutagen tagging (apply_tags_flac, apply_tags_mp3, _MP3_STD_KEYS, _MP3_TXXX_MAP)
    _pipeline_io.py     ← filesystem I/O helpers (find_source_files, _sha256_file, _verify_copy, …)
    _pipeline.py        ← top-level run() pipeline; CollisionPolicy enum, _select_medium
    _discover.py        ← interactive discovery workflow; DiscoverUI Protocol, TerminalDiscoverUI, discover()
  tests/
    unit/
      test_annotator.py   ← pure-logic unit tests
      test_main.py        ← CLI tests
      test_mb_helpers.py  ← _mb_retry + fetch_* tests
      test_models.py      ← Pydantic model tests
      test_pipeline.py    ← build_cea_performers, build_track_tags, apply_tags_*, run()
      test_discover.py    ← discover(), search_releases_by_dir, parse_disc_*, _format_candidate
    integration/
      test_integration.py ← full-pipeline integration tests
  pyproject.toml      ← all config (mypy, pylint, ruff, tox, coverage)
  uv.lock             ← uv lockfile (not tracked for local dev)
```

## Tooling

All quality checks are driven by tox via the project-local venv:

```sh
~/.local/bin/tox -m analyze   # build + test + check_type + check_format + check_lint + check_upgrade
~/.local/bin/tox -e test      # tests + coverage only
~/.local/bin/tox -m edit      # auto-fix formatting in place
```

| Env | Command | Requirement |
|---|---|---|
| `build` | setuptools wheel | must succeed |
| `test` | pytest | 601 tests pass; **100% branch coverage** |
| `check_type` | mypy (strict) | **zero errors** |
| `check_format` | ruff check + ruff format --check | **zero warnings** |
| `check_lint` | pylint | **10.00/10** |
| `check_upgrade` | pyupgrade --py312-plus | **zero suggestions** |
| `fix_format` | pyupgrade + ruff check --fix + ruff format | auto-fix only |

Never skip the full `tox -m analyze` run before declaring a task done.

## Code conventions

### Language and style
- Python **3.12+** only — use `match/case`, PEP 695 `type` aliases, `ParamSpec`/`TypeVar`.
- Sphinx-style, PEP 257 docstrings on every named code block: package, module, class, method, and function
  - Omit type annotations in docstrings
  - Succinct summary of code block
  - A description including detail such as design motivation and considerations not obvious from reading the code.
  - For classes: Also list important attributes
  - For functions: List function parameters with `:param <param>:` and a short description, `:return <what>:`+description, and
    `:raises <WhichException>:`+description for each exception raised
- Line length: **128 characters** (pylint + ruff both configured for this).
  - Multiline statements, comments, and literal strings should not wrap before this line length
  - Other files with free-form lines, like MarkDown files should observe this line length.
- `__all__` is defined in `__init__.py` and must be kept up to date.

### Types
- **No `Any`** anywhere in source or tests — use the `JSON` type alias (`dict[str, JSON] | list[JSON] | str | float | int | bool
  | None`) for truly opaque MB API data; use typed Pydantic models everywhere else.
- **No `cast()`** in source — if a cast would be needed, the model is under-typed; fix the model instead.
- Mypy runs in strict mode (`disallow_untyped_defs`, `warn_return_any`, etc.).  Every file must be clean under `mypy src/
  tests/`.

### Models (`models.py`)
- All MusicBrainz API response types are Pydantic `BaseModel` subclasses.
- Additional models are based on the conventions in the Classical Extras Picard plugin.
- Hyphenated MB field names use `Field(alias="...")` + `model_config = {"populate_by_name": True}`.
- Every field defaults to `""` or `[]` — callers must never guard against `KeyError` or `AttributeError`.
- `MBArtistRelation.attribute_list` and `MBWork.attribute_list` are both `list[MBAttribute | str]` so Pydantic coerces raw
  `{"type":…, "value":…}` dicts from the MB API into `MBAttribute` objects automatically.
- `artist-credit` lists are `list[MBArtistCredit | str]` — the MB API can return bare join-phrase strings as list items.
- `JSON` type alias lives in `models.py` as a PEP 695 `type` statement.
- `MBAttribute` must be defined *before* any model that references it (`MBArtistRelation`, `MBWork`).

### Retry decorator
- `_mb_retry` in `_mb_api.py` uses `@functools.wraps` + `ParamSpec`/`TypeVar` for a fully typed decorator — do not use untyped
  alternatives.
- All three MB API wrappers (`fetch_release`, `fetch_recording_detail`, `fetch_work_detail`) are decorated with `@_mb_retry`.

### `match/case`
- Prefer `match/case` to chained `if`/`elif`/`else` constructs.
- All `match/case` blocks that have an exhaustive union type must include a `case _: # pragma: no cover` arm to suppress
  unreachable-branch coverage warnings from coverage.py.

### Imports
- Import order is enforced by `ruff` (rule set `"I"`). Run `~/.local/bin/tox -m edit` to auto-fix; never hand-edit import order.
- `from __future__ import annotations` is present in every source file.

## Testing conventions

### Integration tests
- Integration tests in `tests/integration/` exercise the full public API end-to-end with all network and filesystem boundaries
  mocked (via `pytest-mock` and `pyfakefs`).  They do not patch internal helpers such as `apply_tags_flac` or `_verify_copy`,
  so the real mutagen write-and-read-back path executes.

### Helpers
Each test module defines typed factory helpers:

| Helper | Returns | Used in |
|---|---|---|
| `_w(d)` | `MBWork` | test_annotator.py, test_pipeline.py |
| `_rec(d)` | `MBRecording` | test_annotator.py, test_pipeline.py |
| `_rel(d)` | `MBRelease` | test_annotator.py, test_pipeline.py |
| `_trk(d)` | `MBTrack` | test_annotator.py |
| `_ac(items)` | `list[MBArtistCredit \| str]` | test_annotator.py |
| `_make_release(n_tracks)` | `MBRelease` | test_pipeline.py |
| `_make_multi_disc_release(tracks_per_disc)` | `MBRelease` | test_pipeline.py |

All mock return values for `fetch_release`, `fetch_recording_detail`, and `fetch_work_detail` must return typed model instances
(`MBRelease`, `MBRecording`, `MBWork`), not raw dicts.

### Coverage
- 100% branch coverage is **enforced** (`fail_under = 100`).
- Every new code path — including error branches, empty-list guards, and match/case arms — needs an explicit test.
- `pyfakefs` (`FakeFilesystem` from `pyfakefs.fake_filesystem`) is used for all filesystem operations. It is listed in deps for
  `test`, `check_type`, and `check_lint` environments.

### Test isolation
- Tests must not make real network calls. All `musicbrainzngs.*` functions, `fetch_*` functions, and
  `music_annotator._mb_api.urllib.request.urlopen` (used by `fetch_acoustid_id`) are mocked via `pytest-mock`.
- Patch targets must use the submodule where the name is bound, not where it originates. For example, patch
  `music_annotator._pipeline.apply_tags_flac` (where it is imported), not `music_annotator._tagger.apply_tags_flac`.
- Minimal real FLAC/MP3 byte sequences are embedded as constants in `test_pipeline.py` and `test_integration.py` for testing
  mutagen tagging without actual audio files.

## Invariants that must never be broken

### Transaction journal and user confirmation provenance

The `action="copied"` entries in `journal_entries` (and therefore in the on-disk journal) and the "Verified OK" console message
printed to the user at the end of `run()` **must always derive exclusively from in-memory program state that was populated only
after all verification checks passed** for that file.  The ordering in `_pipeline.py` is the definition of this invariant:

1. SHA-256 of source captured before copy.
2. `shutil.copy2` executed.
3. SHA-256 of destination checked equal to source — `raise RuntimeError` on mismatch (no journal entry).
4. `apply_tags_flac` / `apply_tags_mp3` executed.
5. `_verify_copy` checks tag round-trip, cover art bytes, and mtime — `raise RuntimeError` on any mismatch (no journal entry).
6. Only on reaching step 6 is `journal_entries.append(..., action="copied")` executed.

The user-facing confirmation is then derived by filtering `journal_entries` to `action == "copied"` — it is **not** a re-read
of the journal file, and it is **not** derived from any other source.  This guarantees that the message "It is safe to delete
the source directory" is backed by the same evidence as the journal record.

Any future change that touches the copy/tag/verify loop or the post-loop confirmation message **must preserve this provenance
chain**.  Specifically:
- Do not append a `"copied"` journal entry before `_verify_copy` returns successfully.
- Do not derive the confirmation message from the journal file on disk, from the plan list, or from any other source that is
  not gated on successful verification.
- Do not swallow `RuntimeError` from `_verify_copy` without removing or preventing the corresponding journal entry.

## Common pitfalls

- **`MBTrack.position`** is `int`, not `str` — test dicts must use integer values or rely on Pydantic coercion.
- **`artist_sort_names`** skips entries where `item.artist.id == ""` (same as `artist_ids`) to handle join-phrase-only
  `MBArtistCredit` objects.
- **`collect_work_dates` / `collect_work_tags_and_key`** use `isinstance(attr, MBAttribute)` — they depend on `attribute_list`
  being `list[MBAttribute | str]`, not `list[str | dict]`. Never regress this type.
- **Dead branches in `build_cea_performers`**: the `instr` extraction uses a single ternary (`first_attr.value if
  isinstance(first_attr, MBAttribute) else first_attr`) — any multi-branch `if/elif/else` here will produce a partial coverage
  failure because the `else` arm is unreachable given the type.
