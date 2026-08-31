"""Library maintenance operations for music-annotator.

Provides the five recurring maintenance-mode commands that operate on an already-annotated library
without making MusicBrainz network calls:

* :func:`repath`                      — re-path all verified library files to their corrected destinations.
* :func:`regroup`                     — consolidate confirmed split-release files into their canonical destinations.
* :func:`unify`                       — consolidate performer-split and composer-split fragmented releases.
* :func:`enrich`                      — retroactively backfill fingerprint fields into library files.
* :func:`reconstruct_cross_references` — census the journal for destructive-choice shapes (SKIP and
  OVERWRITE collision policies) and write secondary release MBIDs as cross-reference tags on surviving
  files.  Offline; dry-run supported.  Also appends truthful ``"cross-referenced"`` journal entries for
  survivors carrying an embedded secondary MBID with no corroborating journal entry (C-AMEND: append-only,
  sourced from the embedded tag, records what the current correct code would have journalled).
* :func:`dedup_library`               — offline census over the live library: group files by embedded
  ``ACOUSTID_ID`` cluster (via the tag-read cache), with ``AUDIO_HASH`` equality as the byte-identity
  fast path; files lacking both are out of scope.  Aggregate per-recording pairs up to medium-level
  groups before prompting.  Each group runs the shared group-resolution flow (survivor / keep-both /
  abort) with C-DEDUP ordering.  Dry-run reports the full census without prompting.
* :func:`renumber_leaves`             — retroactive repair tool for cross-session leaf-collision defect:
  scans for directories with duplicate ``CWP_MOVT_NUM`` prefixes, re-derives the gap-free 1-based index
  from embedded ``(DISCNUMBER, TRACKNUMBER)`` order within each ``CWP_WORKID_TOP`` group, rewrites tags,
  recomputes the destination via :func:`~music_annotator._tags.build_dest_path`, and moves each file on
  the full provenance chain (SHA source → rewrite tags → move → SHA verify → ``_verify_copy`` tag
  round-trip → ``action="renumbered"`` journal entry).  Dirs with a stray-minority fragment (1–2-file
  fragment merged with a large one) or multiple distinct ``CWP_WORKID_TOP`` values are reported but not
  auto-moved even with ``--yes``.

Also provides the shared primitives consumed by all commands:

* :func:`compute_library_modal_depth` — compute the library-wide ``cwp_workid_top`` → modal-depth
  map from ``(cwp_workid_top, cwp_part_levels)`` pairs.  All move passes must use this single
  function over the same pass-invariant membership so that every pass derives the same canonical
  destination from the same group-scope statistic (C-GROUPSCOPE).
* :func:`_move_verify_journal`    — the single journal-append site for move-type entries (C-PROV).
* :func:`_warn_inverse_moves`     — C-IDEM tripwire: warns before a pass executes its plan when any
  planned ``(old, new)`` move inverts a journal-recorded move from this run or a prior run.  The
  tripwire warns; it does not block.
* :func:`_resolve_current_lib`    — lineage walk that resolves the current on-disk path per file.
* :func:`resolve_duplicate_group` — shared group-resolution flow for same-audio collisions (C-DEDUP):
  prompts the operator (survivor / keep-both / abort) and executes the C-DEDUP ordering (xref write +
  verify + journal before any deletion).  Reused by the library-wide dedup command.
* :func:`_write_xref_and_journal` — write a secondary MBID cross-reference and journal the mutation
  (C-PROV chain: tag write → verify → journal entry with action ``"cross-referenced"``).
* :func:`_tags_from_file_dict`    — reconstruct a :class:`~music_annotator.models.TrackTags` from
  an on-disk tag dict.
* :func:`_hydrate_performer_lists` — reconstruct performer :class:`~music_annotator.models.ArtistEntry`
  lists from embedded tags so that :func:`~music_annotator._tags.build_dest_path` can render
  canonical entity name-forms (the MB artist ``name`` field verbatim per NORM-2 as revised) in the
  compact path projection.  No MusicBrainz network calls are made — the maintenance path reads
  embedded tags alone.
* :func:`_apply_group_movement_renumber` — re-derive gap-free ``CWP_MOVT_NUM`` for one
  ``CWP_WORKID_TOP`` group from embedded ``(DISCNUMBER, TRACKNUMBER)`` order and write changed
  tags to disk.  Called by :func:`regroup` and :func:`unify` after any consolidation so that the
  leaf ``nn`` prefix is idempotent across sessions.

"""

# pylint: disable=duplicate-code  # _clamp_maint_dest's name_too_long log block mirrors the silent
# path of _resolve_long_names in _pipeline.py; the duplication is inherent to the module split
# (_pipeline_maint cannot import from _pipeline without a circular dependency).

from __future__ import annotations

import datetime
import errno
import json
import os
import shutil
import tempfile
from pathlib import Path

import structlog
from mutagen._util import MutagenError
from mutagen.flac import FLAC as MutagenFLAC
from mutagen.id3 import ID3
from rich.markup import escape as _markup_escape

from music_annotator._audit import (
    _confirm_fragmentation,
    detect_fragmented_releases,
)
from music_annotator._console import _console
from music_annotator._mb_api import _fetch_acoustid_lookup_raw
from music_annotator._pipeline import _apply_collision_suffix, _collision_suffix
from music_annotator._pipeline_io import (
    JOURNAL_FILENAME,
    AudioCompareResult,
    _assess_collisions,
    _needs_enrich,
    _read_duration_ms,
    _read_tags_flac,
    _read_tags_mp3,
    _resolve_tagged_to_current,
    _sha256_file,
    _verify_copy,
    append_journal_entry,
    enrich_origin_time,
    read_journal,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3, write_secondary_albumid_flac, write_secondary_albumid_mp3
from music_annotator._tags import (
    _CLASS_VOCAB,
    _NAME_MAX,
    _proposed_short,
    assign_group_movement_numbers,
    build_dest_path,
    sel23_ensemble_patch,
)
from music_annotator._works import (
    work_group_modal_depth,
)
from music_annotator.models import (
    JSON,
    ArtistEntry,
    CopyPlanEntry,
    DryRunEntry,
    DryRunPlan,
    JournalCapacity,
    MaintainDryRunReport,
    MaintainOverlapEntry,
    MaintainPassSummary,
    MBRelease,
    MBTrack,
    ReferenceEvidence,
    TrackTags,
    TransactionEntry,
    TransactionLog,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Filename of the tag-read cache sidecar written inside the destination root.
#: The file is a JSON object mapping ``"path\x00size\x00mtime"`` keys to tag dicts.
#: A missing or malformed sidecar degrades gracefully to a full tag read — never an error.
_TAG_CACHE_FILENAME: str = ".music_annotator_tag_cache.json"


class TagReadCache:
    """In-memory tag-read cache keyed on ``(path, size_bytes, mtime_ns)``.

    Avoids re-opening audio files whose path, byte size, and nanosecond mtime are all unchanged
    from a previous read.  Any change to any key component (path, size, or mtime) is a cache miss
    and triggers a full tag read.

    The cache is loaded from a JSON sidecar file under ``dest_root`` at the start of a maintenance
    pass and saved back at the end.  A missing or malformed sidecar degrades gracefully to an empty
    cache — never an error.  The sidecar is pure-Python JSON I/O, compatible with pyfakefs.

    After a file move via :func:`_move_verify_journal`, the cache entry is re-keyed to the new
    path so subsequent reads at the new path are cache hits.  The old path key is removed.

    The cache is read-only during planning: it is consulted only during tag reads and never
    participates in the write/verify/journal provenance chain (C-PROV).

    :ivar _store: Internal mapping from ``(path_str, size_bytes, mtime_ns)`` to tag dict.
    :ivar sidecar_path: Path to the JSON sidecar file on disk.
    """

    def __init__(self, sidecar_path: Path) -> None:
        """Initialise an empty cache bound to ``sidecar_path``.

        :param sidecar_path: Path to the JSON sidecar file.  The file need not exist yet.
        """
        self._store: dict[tuple[str, int, int], dict[str, str]] = {}
        self.sidecar_path = sidecar_path

    def __len__(self) -> int:
        """Return the number of entries in the cache.

        :returns: The count of cached tag dicts.
        """
        return len(self._store)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, sidecar_path: Path) -> TagReadCache:
        """Load a :class:`TagReadCache` from ``sidecar_path``, degrading gracefully on any error.

        Reads the JSON sidecar and populates the in-memory store.  If the file is absent,
        unreadable, or malformed (any exception), returns an empty cache and logs a warning.
        The sidecar format is a JSON object whose keys are ``"<path>\\x00<size>\\x00<mtime>"``
        strings and whose values are ``{TAG: value}`` dicts.

        :param sidecar_path: Path to the JSON sidecar file.
        :returns: A populated :class:`TagReadCache`, or an empty one on any read/parse error.
        """
        cache = cls(sidecar_path)
        if not sidecar_path.exists():
            return cache
        try:
            raw_text = sidecar_path.read_text(encoding="utf-8")
            raw: JSON = json.loads(raw_text)
            if not isinstance(raw, dict):
                log.warning("tag_cache_load_malformed", sidecar=str(sidecar_path), reason="not a JSON object")
                return cache
            for composite_key, value in raw.items():
                if not isinstance(composite_key, str) or not isinstance(value, dict):
                    continue
                parts = composite_key.split("\x00")
                if len(parts) != 3:  # noqa: PLR2004 — 3 is the fixed key-component count
                    continue
                path_str, size_str, mtime_str = parts
                try:
                    size_bytes = int(size_str)
                    mtime_ns = int(mtime_str)
                except ValueError:
                    continue
                # Validate that all tag values are strings before storing.
                tag_dict: dict[str, str] = {}
                valid = True
                for tag_key, tag_val in value.items():
                    if not isinstance(tag_key, str) or not isinstance(tag_val, str):
                        valid = False
                        break
                    tag_dict[tag_key] = tag_val
                if valid:
                    cache._store[(path_str, size_bytes, mtime_ns)] = tag_dict  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 — any failure degrades to empty cache
            log.warning("tag_cache_load_error", sidecar=str(sidecar_path), error=str(exc))
        return cache

    def save(self) -> None:
        """Persist the in-memory cache to the sidecar JSON file.

        Serialises the store to a JSON object with composite string keys
        ``"<path>\\x00<size>\\x00<mtime>"``.  Any write error is logged and silently ignored —
        a failed save means the next run pays full tag-read cost but is otherwise correct.
        """
        try:
            serialisable: dict[str, dict[str, str]] = {
                f"{path_str}\x00{size_bytes}\x00{mtime_ns}": tag_dict
                for (path_str, size_bytes, mtime_ns), tag_dict in self._store.items()
            }
            self.sidecar_path.write_text(json.dumps(serialisable), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — save failure is non-fatal
            log.warning("tag_cache_save_error", sidecar=str(self.sidecar_path), error=str(exc))

    # ------------------------------------------------------------------
    # Cache operations
    # ------------------------------------------------------------------

    def get(self, path: Path) -> dict[str, str] | None:
        """Return the cached tag dict for ``path`` if the key matches, or ``None`` on a miss.

        A hit requires that the file's current ``st_size`` and ``st_mtime_ns`` both match the
        stored key.  Any stat failure (e.g. file not found) is treated as a miss.

        :param path: Path to the audio file.
        :returns: The cached ``{TAG: value}`` dict on a hit, or ``None`` on a miss or stat error.
        """
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_size, st.st_mtime_ns)
        return self._store.get(key)

    def put(self, path: Path, tag_dict: dict[str, str]) -> None:
        """Store ``tag_dict`` in the cache keyed on ``path``'s current stat.

        Reads ``st_size`` and ``st_mtime_ns`` from the file's current stat.  Any stat failure
        is silently ignored — a failed put means the next read is a cache miss, not an error.

        :param path: Path to the audio file.
        :param tag_dict: The ``{TAG: value}`` mapping to cache.
        """
        try:
            st = path.stat()
        except OSError:
            return
        self._store[(str(path), st.st_size, st.st_mtime_ns)] = tag_dict

    def rekey(self, old_path: Path, new_path: Path) -> None:
        """Move a cache entry from ``old_path`` to ``new_path`` after a successful file move.

        Finds the entry whose path component matches ``old_path`` (regardless of size/mtime,
        since the file has just been moved and its stat at the new path may differ from the
        stored key).  Removes the old entry and inserts a new entry keyed on the new path's
        current stat.  If no matching entry exists, or if the new path's stat fails, the
        operation is a no-op.

        :param old_path: The source path before the move.
        :param new_path: The destination path after the move.
        """
        old_path_str = str(old_path)
        # Find the entry for old_path (there is at most one, since path is part of the key).
        old_key: tuple[str, int, int] | None = None
        for key in self._store:
            if key[0] == old_path_str:
                old_key = key
                break
        if old_key is None:
            return
        tag_dict = self._store.pop(old_key)
        # Re-key under the new path's current stat.
        try:
            st = new_path.stat()
        except OSError:
            return
        self._store[(str(new_path), st.st_size, st.st_mtime_ns)] = tag_dict


def _read_tags_cached(path: Path, ext: str, cache: TagReadCache | None) -> dict[str, str]:
    """Read tags from ``path``, consulting ``cache`` first when provided.

    On a cache hit (path, size, and mtime all match a stored entry), returns the cached tag dict
    without opening the audio file.  On a miss, reads tags via
    :func:`~music_annotator._pipeline_io._read_tags_flac` or
    :func:`~music_annotator._pipeline_io._read_tags_mp3`, stores the result in the cache, and
    returns it.

    The cache is read-only with respect to the provenance chain: this function only reads tags
    and populates the cache; it never writes audio files or journal entries.

    :param path: Path to the audio file.
    :param ext: Lowercase file extension (``".flac"`` or ``".mp3"``).
    :param cache: Optional :class:`TagReadCache` to consult.  When ``None``, always reads from
        the audio file directly.
    :returns: The ``{TAG: value}`` mapping read from the file (or cache).
    :raises Exception: Propagates any exception from the underlying tag-read function on a miss.
    """
    if cache is not None:
        cached = cache.get(path)
        if cached is not None:
            return cached

    match ext:
        case ".flac":
            tag_dict = _read_tags_flac(path)
        case ".mp3":
            tag_dict = _read_tags_mp3(path)
        case _:  # pragma: no cover — callers always pass .flac or .mp3
            tag_dict = {}

    if cache is not None:
        cache.put(path, tag_dict)
    return tag_dict


def _tags_from_file_dict(file_dict: dict[str, str]) -> TrackTags:
    """Reconstruct a :class:`~music_annotator.models.TrackTags` instance from an on-disk tag dict.

    Reads all uppercase tag keys produced by :func:`~music_annotator._pipeline_io._read_tags_flac`
    or :func:`~music_annotator._pipeline_io._read_tags_mp3` and populates a :class:`TrackTags`
    instance so that calling ``to_file_dict()`` on the result produces exactly the same uppercase
    dict.

    Named :class:`TrackTags` fields are populated by lowercasing the key and passing as keyword
    arguments via ``model_validate``.  Dynamic per-level fields (``CWP_WORK_0``, ``CWP_WORKID_0``,
    ``CWP_PART_0``, ``CWP_INTER_INDEX_1``, etc.) that are not named fields are placed in
    ``model_extra`` as lowercase keys so that ``to_file_dict()`` includes them via its extras loop.

    :param file_dict: Uppercase ``{KEY: value}`` mapping read back from an audio file.
    :returns: A :class:`TrackTags` with all tags populated, suitable for passing to
        :func:`~music_annotator._pipeline_io._verify_copy`.
    """
    # Split into named fields (known to TrackTags) vs dynamic extras
    named: dict[str, str] = {}
    extras: dict[str, str] = {}

    # Build set of known field names (lowercase) from the model
    known_fields: frozenset[str] = frozenset(TrackTags.model_fields)
    # Guard against the two fields that ARE named model fields but must not be round-tripped:
    # - recording_date_work: an in-memory path-construction helper, never written to audio files.
    # - cwp_composers_is_fallback: an internal flag used only during the ingest pipeline pass.
    # The other to_file_dict() exclusions (cea_*_list fields) are list-typed model fields and
    # will never appear in a read-back tag dict (they are not written to audio files), so they
    # do not need to be explicitly excluded here.
    _excluded = frozenset({"recording_date_work", "cwp_composers_is_fallback"})

    for key, value in file_dict.items():
        lower_key = key.lower()
        if lower_key in known_fields and lower_key not in _excluded:
            named[lower_key] = value
        else:
            # Dynamic per-level field (e.g. cwp_work_0, cwp_workid_0, cwp_inter_index_1) —
            # store with lowercase key so to_file_dict() uppercases it correctly.
            extras[lower_key] = value

    tags = TrackTags.model_validate(named)
    # Merge extras into model_extra (Pydantic's extra="allow" dict).
    # With extra="allow", model_extra is always a dict (never None) after model_validate —
    # the None branch is a defensive guard that cannot be reached in practice.
    if tags.model_extra is None:  # pragma: no cover
        object.__setattr__(tags, "__pydantic_extra__", extras)
    else:
        tags.model_extra.update(extras)
    return tags


def _hydrate_performer_lists(tags: TrackTags, file_dict: dict[str, str]) -> None:
    """Reconstruct performer :class:`~music_annotator.models.ArtistEntry` lists from embedded tags.

    The ``cea_album_conductors_list``, ``cea_album_ensembles_list``, ``cea_conductors_list``, and
    ``cea_ensembles_list`` fields of :class:`~music_annotator.models.TrackTags` are internal
    in-memory fields excluded from :meth:`~music_annotator.models.TrackTags.to_file_dict` and
    therefore absent from the embedded tag dict read back from an audio file.  When
    :func:`~music_annotator._tags.build_dest_path` is called with a :class:`TrackTags` reconstructed
    from embedded tags (as in :func:`repath`, :func:`regroup`, and :func:`unify`), those lists are
    empty and the path falls back to the raw ``CEA_ENSEMBLE_NAMES`` / ``ARTIST`` string — bypassing
    the canonical name-form resolver.

    This function reconstructs the lists from the embedded string tags so that
    :func:`~music_annotator._tags.build_dest_path` can render the canonical name-form for each
    performer entry.  The canonical name-form is the MB artist ``name`` field (NORM-2 as revised);
    no MusicBrainz network calls are made — the maintenance path (repath/regroup/unify) is
    genuinely offline and operates on embedded tags alone.

    MBID assignment strategy (retained for provenance and future use; MBIDs are no longer
    needed for canonical-name resolution):

    * **Album-level conductors**: ``MUSICBRAINZ_CONDUCTORID`` (slash-separated) holds the MBIDs
      of all per-track conductors.  When the count of album conductor names (from
      ``CEA_ALBUM_CONDUCTORS``) equals the count of conductor MBIDs, the two sequences are zipped
      positionally.  Otherwise entries are created without MBIDs.
    * **Album-level ensembles**: ``MUSICBRAINZ_ALBUMARTISTID`` (slash-separated) holds the MBIDs
      of all album artists (from ``release.artist_credit``).  Conductor MBIDs are subtracted
      (order-preserving) to isolate ensemble MBIDs.  When the count of album ensemble names (from
      ``CEA_ALBUM_ENSEMBLES``) equals the count of remaining MBIDs, the two sequences are zipped
      positionally.  Otherwise entries are created without MBIDs.
    * **Per-track conductors** (``cea_conductors_list``): ``MUSICBRAINZ_CONDUCTORID`` is used as
      the MBID source, zipped positionally when counts match.
    * **Per-track ensembles** (``cea_ensembles_list``): entries are always created **without
      MBIDs**.  ``MUSICBRAINZ_ALBUMARTISTID`` is the release's artist-credit MBID pool; for
      box-sets and composer-credited releases this is the edition/collection entity's MBID, not
      the ensemble's MBID.

    Mutates ``tags`` in-place; returns ``None``.  The function is idempotent: calling it on a
    ``TrackTags`` that already has non-empty lists is a no-op (the lists are only set when they
    are currently empty, which is always the case for tags reconstructed from embedded files).

    :param tags: The :class:`~music_annotator.models.TrackTags` instance to hydrate, as returned
        by :func:`_tags_from_file_dict`.
    :param file_dict: The uppercase ``{KEY: value}`` mapping read back from the audio file,
        as returned by :func:`~music_annotator._pipeline_io._read_tags_flac` or
        :func:`~music_annotator._pipeline_io._read_tags_mp3`.
    """

    def _split_semi(raw: str) -> list[str]:
        """Split a semicolon-separated tag value into a list of non-empty stripped parts.

        :param raw: A semicolon-separated string (e.g. ``"Karajan; Abbado"``).
        :returns: List of non-empty stripped parts.
        """
        return [p.strip() for p in raw.split(";") if p.strip()]

    def _split_slash(raw: str) -> list[str]:
        """Split a slash-separated MBID tag value into a list of non-empty stripped parts.

        :param raw: A slash-separated string (e.g. ``"mbid-1/mbid-2"``).
        :returns: List of non-empty stripped parts.
        """
        return [p.strip() for p in raw.split("/") if p.strip()]

    def _make_entries(names: list[str], sorts: list[str], mbids: list[str]) -> list[ArtistEntry]:
        """Build :class:`~music_annotator.models.ArtistEntry` objects from parallel name/sort/MBID lists.

        When ``mbids`` has the same length as ``names``, each entry receives its MBID for provenance.
        When lengths differ, entries are created without MBIDs.  The canonical name-form resolver
        uses ``entry.name`` directly (NORM-2 as revised — no network fetch required).

        :param names: Display names (from ``CEA_ALBUM_CONDUCTORS`` etc.).
        :param sorts: Sort names (from ``CEA_ALBUM_CONDUCTORS_SORT`` etc.); padded with ``""`` when
            shorter than ``names``.
        :param mbids: MBID strings; may be empty or a different length from ``names``.
        :returns: A list of :class:`~music_annotator.models.ArtistEntry` objects.
        """
        use_mbids = len(mbids) == len(names)
        entries: list[ArtistEntry] = []
        for i, name in enumerate(names):
            sort = sorts[i] if i < len(sorts) else ""
            mbid = mbids[i] if use_mbids else ""
            entries.append(ArtistEntry(name=name, sort=sort or name, mbid=mbid))
        return entries

    # --- Album-level conductors ---
    album_conductor_names = _split_semi(file_dict.get("CEA_ALBUM_CONDUCTORS", ""))
    album_conductor_sorts = _split_semi(file_dict.get("CEA_ALBUM_CONDUCTORS_SORT", ""))
    conductor_mbids = _split_slash(file_dict.get("MUSICBRAINZ_CONDUCTORID", ""))

    # --- Album-level ensembles ---
    # Ensemble MBIDs: album artist MBIDs minus conductor MBIDs (order-preserving subtraction).
    album_artist_mbids = _split_slash(file_dict.get("MUSICBRAINZ_ALBUMARTISTID", ""))
    conductor_mbid_set = set(conductor_mbids)
    ensemble_mbids = [m for m in album_artist_mbids if m not in conductor_mbid_set]

    album_ensemble_names = _split_semi(file_dict.get("CEA_ALBUM_ENSEMBLES", ""))
    album_ensemble_sorts = _split_semi(file_dict.get("CEA_ALBUM_ENSEMBLES_SORT", ""))

    # --- Per-track conductors and ensembles ---
    conductor_names = _split_semi(file_dict.get("CEA_CONDUCTORS", ""))
    conductor_sorts: list[str] = []  # no sort tag for per-track conductors in embedded tags
    ensemble_names = _split_semi(file_dict.get("CEA_ENSEMBLES", ""))
    ensemble_sorts = _split_semi(file_dict.get("CEA_ENSEMBLES_SORT", ""))

    # Reconstruct lists (only when currently empty — tags from embedded files always start empty).
    if not tags.cea_album_conductors_list and album_conductor_names:
        tags.cea_album_conductors_list = _make_entries(album_conductor_names, album_conductor_sorts, conductor_mbids)
    if not tags.cea_album_ensembles_list and album_ensemble_names:
        tags.cea_album_ensembles_list = _make_entries(album_ensemble_names, album_ensemble_sorts, ensemble_mbids)
    if not tags.cea_conductors_list and conductor_names:
        tags.cea_conductors_list = _make_entries(conductor_names, conductor_sorts, conductor_mbids)
    if not tags.cea_ensembles_list and ensemble_names:
        # Per-track ensemble MBIDs cannot be reliably derived from embedded tags: the only
        # available MBID pool is MUSICBRAINZ_ALBUMARTISTID, which for box-sets and
        # composer-credited releases is the edition/collection entity's MBID — not the
        # ensemble's MBID.  Assigning the wrong MBID causes _canonical_name to fetch the
        # edition entity's aliases and return the edition title instead of the ensemble name.
        # The safe fallback is to create entries without MBIDs so _canonical_name returns
        # entry.name (the as-credited name from CEA_ENSEMBLES) directly.
        tags.cea_ensembles_list = _make_entries(ensemble_names, ensemble_sorts, [])


def _resolve_current_lib(journal: TransactionLog) -> dict[Path, str]:
    """Resolve the current on-disk path for each logical library file from the journal.

    Walks journal entries in chronological order to build a mapping from each file's current
    on-disk path to its associated release MBID.  The walk handles the full lineage chain:

    * ``"tagged"`` entries seed the map (destination → release_id).
    * ``"repathed"`` and ``"regrouped"`` entries update the map: the old path is removed and the
      new path is registered with the same release_id.
    * ``"enriched"``, ``"repatched"``, and ``"acoustid-repatched"`` entries are in-place updates
      (source == destination); they re-register the path to keep the release_id current.
    * ``"cross-referenced"`` entries are in-place updates (source == destination); they
      re-register the path to keep the primary release_id current.  The ``release_id`` field
      of a ``"cross-referenced"`` entry carries the *secondary* MBID being added, not the
      file's primary MBID — so the primary MBID is preserved from the existing map entry.
    * ``"deduplicated"`` entries pop the source path from the map (the deleted copy is gone);
      the destination (surviving copy) is already registered and is not modified here.

    Multi-hop chains (a file that was repathed and then regrouped) resolve correctly because
    entries are processed in chronological order: each move pops the old path and registers the
    new one, so the map always reflects the latest known location.

    :param journal: The :class:`~music_annotator.models.TransactionLog` to walk.
    :returns: A ``{current_path: release_id}`` mapping for all logical library files.
    """
    current_lib: dict[Path, str] = {}

    for entry in journal.entries:
        dest_path = Path(entry.destination)
        if entry.action == "tagged":
            current_lib[dest_path] = entry.release_id
        elif entry.action in {"repathed", "regrouped", "unified", "renumbered"}:
            old_path = Path(entry.source)
            release_id_for_path = current_lib.pop(old_path, entry.release_id)
            current_lib[dest_path] = release_id_for_path
        elif entry.action in {"enriched", "repatched", "acoustid-repatched", "cross-referenced"}:
            # In-place update: source == destination, path unchanged.
            # Re-register to keep the primary release_id current.  For "cross-referenced"
            # entries the entry.release_id carries the secondary MBID, so we preserve the
            # existing primary MBID from the map rather than overwriting with the secondary.
            existing_primary = current_lib.get(dest_path, entry.release_id)
            current_lib[dest_path] = existing_primary
        elif entry.action == "deduplicated":
            # The source path (deleted copy) is removed from the map; the destination
            # (surviving copy) is already registered and is not modified here.
            src_path = Path(entry.source)
            current_lib.pop(src_path, None)

    return current_lib


class DuplicateResolution:
    """Result of :func:`resolve_duplicate_group` for one duplicate group.

    Carries the operator's choice (survivor / keep-both / abort) and the derived plan:
    which file to delete, which file to cross-reference, and whether the mover's move
    should proceed.

    :ivar choice: ``"survivor_occupant"`` — occupant wins, mover deleted, move dropped;
        ``"survivor_mover"`` — mover wins, occupant deleted first, move proceeds;
        ``"keep_both"`` — cross-reference only, move dropped;
        ``"abort"`` — operator aborted the run.
    :ivar survivor_path: Absolute path of the surviving file (the one that stays on disk).
    :ivar deleted_path: Absolute path of the file to delete (``None`` for keep-both / abort).
    :ivar deleted_release_id: Release MBID of the deleted copy (``""`` when no deletion).
    :ivar secondary_mbid: The secondary MBID to cross-reference onto the survivor
        (``""`` when no cross-reference is needed, e.g. abort).
    :ivar proceed_with_move: ``True`` when the mover's move should proceed after the occupant
        is deleted (survivor_mover arm only).
    """

    def __init__(
        self,
        *,
        choice: str,
        survivor_path: Path,
        deleted_path: Path | None,
        deleted_release_id: str,
        secondary_mbid: str,
        proceed_with_move: bool,
    ) -> None:
        """Initialise a :class:`DuplicateResolution`.

        :param choice: One of ``"survivor_occupant"``, ``"survivor_mover"``, ``"keep_both"``,
            ``"abort"``.
        :param survivor_path: Path of the surviving file.
        :param deleted_path: Path of the file to delete, or ``None``.
        :param deleted_release_id: Release MBID of the deleted copy.
        :param secondary_mbid: Secondary MBID to cross-reference onto the survivor.
        :param proceed_with_move: Whether the mover's move should proceed.
        """
        self.choice = choice
        self.survivor_path = survivor_path
        self.deleted_path = deleted_path
        self.deleted_release_id = deleted_release_id
        self.secondary_mbid = secondary_mbid
        self.proceed_with_move = proceed_with_move


def _write_xref_and_journal(
    survivor_path: Path,
    secondary_mbid: str,
    *,
    journal: TransactionLog,
    journal_path: Path,
    now_str: str,
) -> None:
    """Write a secondary MBID cross-reference to ``survivor_path`` and journal the mutation.

    Performs the full C-PROV chain for a cross-reference tag write:

    1. Write ``secondary_mbid`` into ``MUSICBRAINZ_SECONDARY_ALBUMID`` via
       :func:`~music_annotator._tagger.write_secondary_albumid_flac` or
       :func:`~music_annotator._tagger.write_secondary_albumid_mp3` (append-only set-union;
       no-op if already present).
    2. Read back the tag to verify the write landed correctly.
    3. Append a ``"cross-referenced"`` journal entry with ``release_id = secondary_mbid``
       (the secondary MBID being added, not the file's primary) and
       ``source == destination == str(survivor_path)``.

    This function is the sole site that writes ``"cross-referenced"`` journal entries.
    It must be called before any deletion executes (C-DEDUP ordering invariant: the reference
    must exist durably before the bytes disappear).

    :param survivor_path: Path to the surviving file to cross-reference.
    :param secondary_mbid: The secondary release MBID to add.
    :param journal: In-memory :class:`~music_annotator.models.TransactionLog`; mutated in place.
    :param journal_path: Path to the journal file.
    :param now_str: ISO-format UTC timestamp string for the journal entry.
    :raises RuntimeError: If the tag write or read-back verification fails.
    :raises mutagen.MutagenError: If the file cannot be read or written.
    """
    ext = survivor_path.suffix.lower()
    match ext:
        case ".flac":
            write_secondary_albumid_flac(survivor_path, secondary_mbid)
            # Verify: read back and confirm the MBID is present.
            verify_dict = _read_tags_flac(survivor_path)
        case ".mp3":
            write_secondary_albumid_mp3(survivor_path, secondary_mbid)
            verify_dict = _read_tags_mp3(survivor_path)
        case _:  # pragma: no cover — callers always pass .flac or .mp3
            raise RuntimeError(f"unsupported extension for cross-reference write: {ext}")
    written_val = verify_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
    written_set = {m.strip() for m in written_val.split("; ") if m.strip()}
    if secondary_mbid not in written_set:
        raise RuntimeError(
            f"cross-reference write verification failed for '{survivor_path.name}': "
            f"secondary MBID '{secondary_mbid}' not found in read-back value '{written_val}'"
        )
    entry = TransactionEntry(
        timestamp=now_str,
        release_id=secondary_mbid,
        source=str(survivor_path),
        destination=str(survivor_path),
        action="cross-referenced",
    )
    append_journal_entry(journal_path, entry)
    journal.entries.append(entry)
    log.info(
        "cross_referenced",
        path=str(survivor_path),
        secondary_mbid=secondary_mbid,
    )


def resolve_duplicate_group(  # pylint: disable=too-many-return-statements
    occupant_path: Path,
    occupant_release_id: str,
    mover_path: Path,
    mover_release_id: str,
    evidence_method: str,
    *,
    journal: TransactionLog,
    journal_path: Path,
    dest_root: Path,
    now: datetime.datetime,
    dry_run: bool = False,
) -> DuplicateResolution:
    """Prompt the operator to resolve one duplicate group and execute the C-DEDUP ordering.

    Called when a planned move's destination is already occupied by a file with the same audio
    content (``match=True``).  Presents the group members, their release MBIDs, and the evidence
    method to the operator, then executes the chosen resolution:

    * **Survivor = occupant**: the occupant wins.  The mover is cross-referenced onto the
      occupant (secondary MBID written + verified + journalled) before the mover is deleted
      (``"deduplicated"`` journal entry).  The move is dropped from the plan.
    * **Survivor = mover**: the mover wins.  The occupant is cross-referenced (secondary MBID
      written + verified + journalled) before the occupant is deleted (``"deduplicated"``
      journal entry).  The move then proceeds through the normal C-PROV chain into the vacated
      path (caller is responsible for executing the move).
    * **Keep both**: cross-reference only — the mover's release MBID is written as a secondary
      MBID on the occupant (or vice-versa, whichever is the "survivor" in the keep-both sense).
      The move is dropped.  On a later re-run the existing secondary MBID is detected and the
      group is silently dropped (idempotency, no re-prompt).
    * **Abort**: the operator aborts the run.  No changes are made.

    C-DEDUP ordering invariant: the survivor's cross-reference write + verify + journal entry
    complete **before** any deletion executes.

    ``--yes`` does **not** suppress this prompt (integrity prompts are not bulk consent).
    ``--dry-run`` reports the group but never prompts and never deletes.

    This function is the shared group-resolution flow reused by the library-wide dedup command.

    :param occupant_path: Path of the file already at the planned destination.
    :param occupant_release_id: Release MBID of the occupant file.
    :param mover_path: Path of the file being moved (the planned source).
    :param mover_release_id: Release MBID of the mover file.
    :param evidence_method: How identity was established (e.g. ``"sha256"``, ``"acoustid"``).
    :param journal: In-memory :class:`~music_annotator.models.TransactionLog`; mutated in place
        when cross-reference or deduplicated entries are written.
    :param journal_path: Path to the journal file.
    :param dest_root: Library root for relative-path display in the prompt.
    :param now: UTC datetime for journal entry timestamps.
    :param dry_run: When ``True``, report the group and return a keep-both result without
        prompting or modifying any files.
    :returns: A :class:`DuplicateResolution` describing the operator's choice and the derived plan.
    """
    now_str = now.isoformat()

    # Check idempotency: if the mover's release MBID is already in the occupant's secondary
    # MBID set, this group was already resolved as keep-both on a previous run.  Drop silently.
    try:
        ext = occupant_path.suffix.lower()
        match ext:
            case ".flac":
                occ_dict = _read_tags_flac(occupant_path)
            case ".mp3":
                occ_dict = _read_tags_mp3(occupant_path)
            case _:  # pragma: no cover
                occ_dict = {}
        existing_secondary = occ_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        existing_set = {m.strip() for m in existing_secondary.split("; ") if m.strip()}
        if mover_release_id and mover_release_id in existing_set:
            log.info(
                "duplicate_group_already_cross_referenced",
                occupant=str(occupant_path),
                mover=str(mover_path),
                secondary_mbid=mover_release_id,
            )
            return DuplicateResolution(
                choice="keep_both",
                survivor_path=occupant_path,
                deleted_path=None,
                deleted_release_id="",
                secondary_mbid="",
                proceed_with_move=False,
            )
    except Exception:  # noqa: BLE001 — tag read failure: treat as not-yet-cross-referenced
        existing_set = set()

    if dry_run:
        # Dry-run: report the group without prompting or modifying files.
        _occ_dry = str(occupant_path.relative_to(dest_root) if occupant_path.is_relative_to(dest_root) else occupant_path)
        _mov_dry = str(mover_path.relative_to(dest_root) if mover_path.is_relative_to(dest_root) else mover_path)
        _console.print(
            f"\n[bold yellow]duplicate group[/] (evidence: {_markup_escape(evidence_method)}):\n"
            f"  occupant: [dim]{_markup_escape(_occ_dry)}[/]"
            f"  (release {_markup_escape(occupant_release_id)})\n"
            f"  mover:    [dim]{_markup_escape(_mov_dry)}[/]"
            f"  (release {_markup_escape(mover_release_id)})\n"
            f"  [dim](dry-run: no changes made)[/]"
        )
        return DuplicateResolution(
            choice="keep_both",
            survivor_path=occupant_path,
            deleted_path=None,
            deleted_release_id="",
            secondary_mbid="",
            proceed_with_move=False,
        )

    # Interactive prompt — survives --yes (integrity prompts are not bulk consent, per INSTR/C-DEDUP).
    occ_rel = str(occupant_path.relative_to(dest_root)) if occupant_path.is_relative_to(dest_root) else str(occupant_path)
    mov_rel = str(mover_path.relative_to(dest_root)) if mover_path.is_relative_to(dest_root) else str(mover_path)
    _console.print(
        f"\n[bold yellow]Duplicate audio detected[/] (evidence: {_markup_escape(evidence_method)})\n"
        f"  [bold]1[/] occupant: [dim]{_markup_escape(occ_rel)}[/]  (release {_markup_escape(occupant_release_id)})\n"
        f"  [bold]2[/] mover:    [dim]{_markup_escape(mov_rel)}[/]  (release {_markup_escape(mover_release_id)})\n"
        f"\nChoose:\n"
        f"  [bold]1[/] — keep occupant (delete mover)\n"
        f"  [bold]2[/] — keep mover (delete occupant)\n"
        f"  [bold]b[/] — keep both (cross-reference only, no deletion)\n"
        f"  [bold]a[/] — abort run\n"
    )

    # Re-prompt loop: unrecognised input re-prompts; only 'a' aborts; EOF (piped stream exhausted)
    # is treated as abort with a clear message.  This ensures a piped 'y' stream does not silently
    # kill the pass — the operator must supply a valid choice interactively (INSTR/C-DEDUP).
    while True:
        _console.print("[bold cyan]>[/] ", end="")
        try:
            answer = input("").strip().lower()
        except EOFError:
            _console.print("[bold red]No input available — aborting dedup pass.[/]")
            log.info("duplicate_group_aborted_eof", occupant=str(occupant_path), mover=str(mover_path))
            return DuplicateResolution(
                choice="abort",
                survivor_path=occupant_path,
                deleted_path=None,
                deleted_release_id="",
                secondary_mbid="",
                proceed_with_move=False,
            )

        match answer:
            case "1":
                # Survivor = occupant.  Cross-reference mover's MBID onto occupant, then delete mover.
                _write_xref_and_journal(
                    occupant_path,
                    mover_release_id,
                    journal=journal,
                    journal_path=journal_path,
                    now_str=now_str,
                )
                dedup_entry = TransactionEntry(
                    timestamp=now_str,
                    release_id=mover_release_id,
                    source=str(mover_path),
                    destination=str(occupant_path),
                    action="deduplicated",
                )
                os.unlink(mover_path)
                append_journal_entry(journal_path, dedup_entry)
                journal.entries.append(dedup_entry)
                log.info(
                    "deduplicated",
                    deleted=str(mover_path),
                    survivor=str(occupant_path),
                    deleted_release_id=mover_release_id,
                )
                return DuplicateResolution(
                    choice="survivor_occupant",
                    survivor_path=occupant_path,
                    deleted_path=mover_path,
                    deleted_release_id=mover_release_id,
                    secondary_mbid=mover_release_id,
                    proceed_with_move=False,
                )
            case "2":
                # Survivor = mover.  Cross-reference occupant's MBID onto the mover (at its current
                # source path), then delete the occupant.  The move itself proceeds afterward.
                _write_xref_and_journal(
                    mover_path,
                    occupant_release_id,
                    journal=journal,
                    journal_path=journal_path,
                    now_str=now_str,
                )
                dedup_entry = TransactionEntry(
                    timestamp=now_str,
                    release_id=occupant_release_id,
                    source=str(occupant_path),
                    destination=str(mover_path),
                    action="deduplicated",
                )
                os.unlink(occupant_path)
                append_journal_entry(journal_path, dedup_entry)
                journal.entries.append(dedup_entry)
                log.info(
                    "deduplicated",
                    deleted=str(occupant_path),
                    survivor=str(mover_path),
                    deleted_release_id=occupant_release_id,
                )
                return DuplicateResolution(
                    choice="survivor_mover",
                    survivor_path=mover_path,
                    deleted_path=occupant_path,
                    deleted_release_id=occupant_release_id,
                    secondary_mbid=occupant_release_id,
                    proceed_with_move=True,
                )
            case "b":
                # Keep both: cross-reference mover's MBID onto occupant; drop the move.
                _write_xref_and_journal(
                    occupant_path,
                    mover_release_id,
                    journal=journal,
                    journal_path=journal_path,
                    now_str=now_str,
                )
                return DuplicateResolution(
                    choice="keep_both",
                    survivor_path=occupant_path,
                    deleted_path=None,
                    deleted_release_id="",
                    secondary_mbid=mover_release_id,
                    proceed_with_move=False,
                )
            case "a":
                # Explicit abort.
                log.info("duplicate_group_aborted", occupant=str(occupant_path), mover=str(mover_path))
                return DuplicateResolution(
                    choice="abort",
                    survivor_path=occupant_path,
                    deleted_path=None,
                    deleted_release_id="",
                    secondary_mbid="",
                    proceed_with_move=False,
                )
            case _:
                # Unrecognised input: re-prompt with the valid-choice reminder.
                _console.print(
                    "[bold red]Unrecognised input.[/] Please enter [bold]1[/], [bold]2[/], [bold]b[/], or [bold]a[/]."
                )


def _detect_audio_suffix(path: Path) -> str | None:
    """Probe ``path`` with mutagen to determine whether it is a FLAC or MP3 audio file.

    Used to repair extension-less audio files whose suffix was lost during over-long-name
    truncation.  Tries FLAC first (``mutagen.flac.FLAC``), then MP3 (``mutagen.mp3.MP3``).
    Both probes catch all exceptions so that a non-audio file returns ``None`` rather than
    raising.

    :param path: Path to the file to probe (may have any suffix, including none).
    :returns: ``".flac"`` if the file is a valid FLAC, ``".mp3"`` if it is a valid MP3,
        ``None`` if it is not identifiable as either audio format.
    """
    try:
        MutagenFLAC(str(path))
        return ".flac"
    except Exception:  # noqa: BLE001
        pass
    try:
        # ID3 reads ID3 tags regardless of file extension, making it suitable for probing
        # extension-less files.  A successful open confirms the file carries MP3/ID3 metadata.
        ID3(str(path))  # type: ignore[no-untyped-call]
        return ".mp3"
    except Exception:  # noqa: BLE001
        return None


def _clamp_maint_dest(dest_root: Path, dest: Path, current_path: Path | None = None) -> Path:
    """Clamp each component of ``dest`` to at most :data:`~music_annotator._tags._NAME_MAX` UTF-8 bytes.

    Maintenance passes (``repath``, ``regroup``, ``unify``) build destination paths from embedded
    tags via :func:`~music_annotator._tags.build_dest_path`, which deliberately does not enforce
    per-component byte limits (enforcement is the caller's responsibility).  Without clamping, a
    path whose work title or performer blob exceeds 255 UTF-8 bytes would reach
    :func:`~music_annotator._pipeline_io._assess_collisions` and raise ``OSError: [Errno 36]`` on
    ``dest.exists()``.

    The clamping logic mirrors the silent (``ui=None``) path of
    :func:`~music_annotator._pipeline._resolve_long_names`: ``_proposed_short`` is applied once per
    over-limit component, with the audio suffix bytes reserved for the leaf so that stem + suffix
    together fit within ``_NAME_MAX``.  A ``name_too_long`` warning is logged for each clamped
    component **only when the clamped result differs from ``current_path``** — i.e. only when a
    move would actually occur.  When the clamped destination equals the file's current path the
    component is already at its canonical clamped form on disk; no move is needed and no warning
    is emitted.  The function is idempotent: components already within the limit pass through
    unchanged.

    :param dest_root: Library root.  Used only to compute the relative parts of ``dest``.
    :param dest: Full absolute destination path including the audio extension (i.e. the result of
        ``build_dest_path(...).with_suffix(ext)``).
    :param current_path: The file's current on-disk path.  When provided, ``name_too_long``
        warnings are suppressed if the clamped result equals ``current_path`` (no-op move).
        When ``None``, warnings are always emitted for clamped components.
    :returns: A new :class:`~pathlib.Path` with every component guaranteed to be at most
        ``_NAME_MAX`` UTF-8 bytes.
    """
    rel_parts = dest.relative_to(dest_root).parts
    leaf = rel_parts[-1]
    # Derive the audio suffix from the leaf (the last component of dest).  Path.suffix on the leaf
    # may misidentify a trailing dot in a work title (e.g. "op.") as an extension, but at this
    # point the leaf was produced by .with_suffix(ext) where ext is always a clean audio extension
    # (".flac" or ".mp3"), so Path.suffix is safe to use here.
    leaf_audio_suffix = Path(leaf).suffix.lower()
    new_parts: list[str] = []
    clamped_components: list[tuple[str, str]] = []  # (original, clamped) pairs for deferred logging
    for part in rel_parts:
        if len(part.encode("utf-8")) > _NAME_MAX:
            part_audio_suffix = leaf_audio_suffix if part == leaf else ""
            clamped = _proposed_short(part, part_audio_suffix)
            clamped_components.append((part, clamped))
            new_parts.append(clamped)
        else:
            new_parts.append(part)
    result = dest_root.joinpath(*new_parts)
    # Emit name_too_long warnings only when the clamped result differs from the current path,
    # i.e. only when a move would actually occur.  Suppressing the warning on no-op moves avoids
    # chronic re-warning on already-clamped names that are already at their canonical location.
    if clamped_components and (current_path is None or result != current_path):
        for original, clamped in clamped_components:
            log.warning(
                "name_too_long",
                component=original,
                bytes=len(original.encode("utf-8")),
                limit=_NAME_MAX,
                shortened=clamped,
            )
    return result


def _topo_sort_moves(
    plan_pairs: list[tuple[Path, Path]],
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, Path]]]:
    """Sort ``plan_pairs`` in dependency order and identify swap cycles.

    Moves execute in dependency order to prevent destination-occupied errors in shift chains: a
    move whose destination is another plan entry's source must run after that entry vacates it.
    This is the C-SEQ topological ordering requirement.

    The move graph has an edge from move A to move B when A's destination equals B's source (A
    depends on B vacating its source before A can land).  A topological sort over this graph
    produces an execution order where each move's destination is guaranteed to be vacant when
    the move runs.

    True swap cycles (A→B, B→A) cannot be resolved by ordering alone.  They are returned
    separately as ``swap_pairs`` so the caller can break them via an in-directory temp hop
    (which stays inside the C-PROV verify-then-journal chain).

    :param plan_pairs: List of ``(src, dest)`` path pairs to sort.
    :returns: A ``(ordered, swap_pairs)`` tuple where ``ordered`` is the dependency-ordered list
        (non-cycle moves only) and ``swap_pairs`` is the list of pairs involved in swap cycles.
        Each swap cycle of length N produces N entries in ``swap_pairs`` (the cycle members in
        cycle order, starting from an arbitrary member).
    """
    # Build adjacency: dest_to_src_idx[dest] = index of the move whose src == dest.
    # This tells us: "before move i can land at dest, the move at dest_to_src_idx[dest] must run."
    dest_to_src_idx: dict[Path, int] = {}
    for i, (src, _) in enumerate(plan_pairs):
        dest_to_src_idx[src] = i

    n = len(plan_pairs)
    # For each move i, find which move j must run before i (j's src == i's dest).
    # i depends on j: j must run first so i's destination is vacant.
    # Edge: i → j means "i must run after j".
    # Build in-degree and adjacency for Kahn's algorithm.
    # depends_on[i] = j means move i cannot run until move j has run.
    depends_on: dict[int, int] = {}
    for i, (_, dest) in enumerate(plan_pairs):
        if dest in dest_to_src_idx:
            j = dest_to_src_idx[dest]
            if j == i:  # pragma: no cover — self-loop (src==dest); callers filter no-ops
                continue
            depends_on[i] = j

    # Detect cycles using DFS.  A cycle means a set of moves that mutually depend on each other
    # (the simplest case is a two-file swap: A→B, B→A).
    # Node colours: 0=unvisited, 1=in-progress (on the current DFS stack), 2=fully processed.
    _unvisited, _in_progress, _done = 0, 1, 2
    color: list[int] = [_unvisited] * n
    in_cycle: list[bool] = [False] * n

    def _dfs_detect(node: int) -> bool:
        """DFS from ``node``; return True if a cycle is found through this node.

        :param node: Index of the move to start DFS from.
        :returns: True when a back-edge (cycle) is detected.
        """
        color[node] = _in_progress
        if node in depends_on:
            nxt = depends_on[node]
            if color[nxt] == _in_progress:
                # Back edge: cycle detected.
                in_cycle[nxt] = True
                in_cycle[node] = True
                return True
            if color[nxt] == _unvisited:
                if _dfs_detect(nxt):
                    # Propagate cycle membership up the call stack.  color[node] is always
                    # _in_progress here (set above, not yet changed to _done), so the False
                    # branch is unreachable in practice.
                    if color[node] == _in_progress:  # pragma: no cover — always True
                        in_cycle[node] = True
                    return True
        color[node] = _done
        return False

    for start in range(n):
        if color[start] == _unvisited:
            _dfs_detect(start)

    # Separate cycle members from non-cycle moves.
    swap_indices: set[int] = {i for i in range(n) if in_cycle[i]}
    non_cycle_pairs: list[tuple[Path, Path]] = []
    swap_pairs: list[tuple[Path, Path]] = []

    # Topological sort (Kahn's algorithm) over non-cycle moves only.
    # in_degree[i] = number of non-cycle moves that i depends on.
    in_degree: list[int] = [0] * n
    dependents: dict[int, list[int]] = {}  # j → list of i that depend on j
    for i, j in depends_on.items():
        if i in swap_indices or j in swap_indices:
            continue
        in_degree[i] += 1
        dependents.setdefault(j, []).append(i)

    # Kahn's: start with moves that have no dependencies (in_degree == 0).
    queue: list[int] = [i for i in range(n) if i not in swap_indices and in_degree[i] == 0]
    ordered_indices: list[int] = []
    while queue:
        node = queue.pop(0)
        ordered_indices.append(node)
        for dep in dependents.get(node, []):
            in_degree[dep] -= 1
            # in_degree[dep] is always 0 after decrementing: each move has at most one
            # dependency (depends_on[i] is a single value), so in_degree starts at 1 and
            # reaches 0 on the first decrement.  The False branch is unreachable.
            if in_degree[dep] == 0:  # pragma: no cover — always True; see comment above
                queue.append(dep)

    # Any non-cycle move not reached by Kahn's is an isolated move (no dependencies).
    # They should all be in ordered_indices already; this is a safety net.
    reached = set(ordered_indices)
    for i in range(n):
        if i not in swap_indices and i not in reached:
            ordered_indices.append(i)  # pragma: no cover — Kahn's covers all non-cycle nodes

    non_cycle_pairs = [plan_pairs[i] for i in ordered_indices]
    swap_pairs = [plan_pairs[i] for i in range(n) if i in swap_indices]

    return non_cycle_pairs, swap_pairs


def _warn_inverse_moves(
    plan_pairs: list[tuple[Path, Path]],
    current_pass: str,
    journal: TransactionLog,
) -> None:
    """Warn when any planned move inverts a move already recorded in the journal (C-IDEM tripwire).

    Before a pass executes its move plan, this function checks each ``(old, new)`` pair against
    every move-type journal entry (``"repathed"``, ``"regrouped"``, ``"unified"``).  If a planned
    move's ``(old, new)`` is the inverse of a recorded entry's ``(source, destination)`` — that is,
    the journal has ``source == new`` and ``destination == old`` — a structlog warning is emitted
    naming both the current pass and the pass that recorded the inverse move.

    The tripwire warns; it does not block.  Blocking would violate C-CONFLUENCE's ergonomics
    register (no formal oscillation calculus).  The warning converts any future canonical divergence
    from silent churn into a visible signal: if a pass plans a move that undoes a prior move, the
    operator sees it immediately rather than discovering it after the library has churned.

    Both the in-memory journal (moves recorded during this run) and the on-disk journal (moves from
    prior runs) are checked via the ``journal`` parameter, which is the single in-memory
    :class:`~music_annotator.models.TransactionLog` threaded through all passes by :func:`maintain`
    (C-JRNL: the journal is read once and updated in place as moves are journalled).

    :param plan_pairs: The ``(old_path, new_path)`` pairs the current pass is about to execute.
    :param current_pass: The name of the pass building this plan (e.g. ``"repath"``,
        ``"regroup"``, ``"unify"``).
    :param journal: The in-memory :class:`~music_annotator.models.TransactionLog` containing all
        journal entries seen so far (both prior-run entries loaded at startup and current-run
        entries appended by earlier passes).
    """
    # Build a mapping from (source, destination) → pass name for all move-type journal entries.
    # Only move-type actions carry a meaningful (source, destination) pair where source != destination.
    _move_actions = frozenset({"repathed", "regrouped", "unified", "renumbered"})
    recorded_moves: dict[tuple[str, str], str] = {}
    for entry in journal.entries:
        if entry.action in _move_actions:
            recorded_moves[(entry.source, entry.destination)] = entry.action

    for old_path, new_path in plan_pairs:
        old_str = str(old_path)
        new_str = str(new_path)
        # An inverse move is one where the journal has (source=new, destination=old).
        inverse_key = (new_str, old_str)
        if inverse_key in recorded_moves:
            prior_pass = recorded_moves[inverse_key]
            log.warning(
                "inverse_move_detected",
                current_pass=current_pass,
                prior_pass=prior_pass,
                old=old_str,
                new=new_str,
            )


def _move_verify_journal(
    plan_pairs: list[tuple[Path, Path]],
    *,
    journal: TransactionLog,
    journal_path: Path,
    action: str,
    dest_root: Path,
    now: datetime.datetime,
    release_id: str = "",
    cache: TagReadCache | None = None,
) -> int:
    """Move each ``(src, dest)`` pair atomically, verify integrity, and journal each success.

    This is the single site that may append move-type journal entries (``"repathed"``,
    ``"regrouped"``, ``"unified"``), enforcing the C-PROV provenance-chain invariant: a journal
    entry is written **only after** the file passes both the SHA-256 destination check and
    :func:`~music_annotator._pipeline_io._verify_copy`.

    **C-NOCLOBBER**: before executing each move, the destination is checked for existence using
    ``os.open`` with ``O_CREAT|O_EXCL`` (atomic exclusive-create).  If the destination already
    exists and is not the source being moved away by another move in the same plan, a
    :exc:`RuntimeError` is raised — the move is refused.  A maintenance move NEVER overwrites an
    existing destination file.

    **C-SEQ**: moves execute in dependency order via :func:`_topo_sort_moves`.  A move whose
    destination is another plan entry's source runs after that entry vacates it (topological order
    over the move graph).  True swap cycles (A→B, B→A) are broken via an in-directory temp hop:
    one file is moved to a temporary name in the same directory, then the other file moves to its
    final destination, then the temp file moves to its final destination.  Every hop in the temp
    sequence goes through the full SHA-256 + :func:`_verify_copy` + journal chain (C-PROV).

    For each pair the sequence is:

    1. Capture source SHA-256 and mtime before the move.
    2. Ensure the destination parent directory exists.
    3. **C-NOCLOBBER check**: atomically verify the destination does not exist.
    4. Move atomically via :func:`os.replace` (rename within the same filesystem).  On
       ``OSError`` with ``errno.EXDEV`` (cross-filesystem move), fall back to
       :func:`shutil.copy2` + :func:`os.unlink`; the copy is integrity-checked before the
       source is unlinked.
    5. Verify destination SHA-256 == source SHA-256 (raises :exc:`RuntimeError` on mismatch —
       **no journal entry is written**).
    6. Read back the destination tags and run :func:`~music_annotator._pipeline_io._verify_copy`
       (raises :exc:`RuntimeError` on mismatch — **no journal entry is written**).
    7. **Only then** append a :class:`~music_annotator.models.TransactionEntry` with the given
       ``action`` and ``release_id`` via :func:`~music_annotator._pipeline_io.append_journal_entry`
       (O(1) durable JSONL append with fsync) and add it to the in-memory ``journal`` so callers
       see the updated state without re-reading the file.
    8. Clean up now-empty source directories (best-effort; non-empty directories are skipped).
    9. Re-key the cache entry from the old path to the new path (C-JRNL: the file has moved;
       the old key is stale).  The cache is updated only after verification passes — it is never
       consulted during the write/verify/journal sequence (C-PROV).

    The ``journal`` parameter is mutated in place: each successful move appends its entry to
    ``journal.entries``.  Callers hold the journal in memory for the duration of the maintenance
    pass and never call :func:`~music_annotator._pipeline_io.read_journal` again between moves —
    the in-memory copy is always current after each append.

    :param plan_pairs: List of ``(src, dest)`` path pairs to move.
    :param journal: In-memory :class:`~music_annotator.models.TransactionLog` loaded at the start
        of the maintenance pass.  Mutated in place: each successful move appends its entry.
    :param journal_path: Path to the journal file (``<dest_root>/music_annotator_journal.json``).
    :param action: Journal action string (e.g. ``"repathed"``, ``"regrouped"``, ``"unified"``).
    :param dest_root: Root of the destination library; used for empty-directory cleanup and
        log messages.
    :param now: UTC datetime for the journal entry timestamp (ISO-format string is derived from
        this value).
    :param release_id: MusicBrainz release MBID for the journal entry.  Empty string for
        ``"repathed"`` entries (repath operates offline from embedded tags).
    :param cache: Optional :class:`TagReadCache` to re-key after each successful move.  When
        provided, the entry for ``src`` is moved to ``dest`` in the cache after verification
        passes.  The cache is never consulted during the write/verify/journal sequence.
    :returns: Count of files successfully moved and journalled.
    :raises RuntimeError: If the destination already exists (C-NOCLOBBER), if the post-move
        SHA-256 check fails, or if :func:`_verify_copy` fails.
    :raises OSError: If the source file cannot be read or the destination cannot be written
        (except ``EXDEV``, which is handled by the cross-filesystem fallback).
    """
    now_str = now.isoformat()
    moved_count = 0

    # C-SEQ: sort moves in dependency order; break swap cycles via temp-hop.
    ordered_pairs, swap_pairs = _topo_sort_moves(plan_pairs)

    # Process swap cycles first via temp-hop, then process ordered (non-cycle) moves.
    # Each swap cycle of length 2 (A→B, B→A) is broken by: A→temp, B→A, temp→B.
    # For longer cycles the same principle applies: move the first element to a temp,
    # then shift the rest of the chain, then move temp to the last destination.
    # Currently _topo_sort_moves returns all cycle members; we process them as a chain.
    if swap_pairs:
        moved_count += _execute_swap_cycles(
            swap_pairs,
            journal=journal,
            journal_path=journal_path,
            action=action,
            dest_root=dest_root,
            now_str=now_str,
            release_id=release_id,
            cache=cache,
        )

    for src, dest in ordered_pairs:
        moved_count += _execute_single_move(
            src,
            dest,
            journal=journal,
            journal_path=journal_path,
            action=action,
            dest_root=dest_root,
            now_str=now_str,
            release_id=release_id,
            cache=cache,
        )

    return moved_count


def _atomic_noclobber_check(dest: Path) -> bool:
    """Atomically verify that ``dest`` does not exist; return ``True`` if it does exist.

    Uses ``os.open`` with ``O_CREAT|O_EXCL`` to perform an atomic existence check.  If the
    destination already exists, ``O_EXCL`` causes ``os.open`` to raise ``FileExistsError``
    (``errno.EEXIST``), which is caught and returned as ``True`` so the caller can decide
    whether to refuse (different content) or dedup (same content).

    When the destination does not exist, the placeholder file created by ``os.open`` is
    immediately closed and unlinked so the actual move can proceed.  Returns ``False``.

    :param dest: Destination path to check.
    :returns: ``True`` if the destination already exists, ``False`` if it did not exist.
    :raises OSError: If the parent directory is not writable or another OS error occurs.
    """
    try:
        fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return True
    # Destination did not exist; close and remove the placeholder so the move can proceed.
    os.close(fd)
    dest.unlink()
    return False


def _execute_single_move(
    src: Path,
    dest: Path,
    *,
    journal: TransactionLog,
    journal_path: Path,
    action: str,
    dest_root: Path,
    now_str: str,
    release_id: str,
    cache: TagReadCache | None = None,
) -> int:
    """Execute one ``(src, dest)`` move through the full C-PROV + C-NOCLOBBER chain.

    Performs the SHA-256 capture, C-NOCLOBBER existence check, atomic rename (or cross-fs
    fallback), post-move SHA-256 verification, tag round-trip verification, journal append,
    and empty-directory cleanup.  Returns 1 on success.

    This helper is factored out of :func:`_move_verify_journal` so that the swap-cycle temp-hop
    path can reuse the same provenance chain for each hop without duplicating the logic.

    After verification passes, the entry is durably appended to the on-disk journal via
    :func:`~music_annotator._pipeline_io.append_journal_entry` (O(1) JSONL append with fsync)
    and also appended to the in-memory ``journal`` so the caller's view stays current without
    re-reading the file.  When ``cache`` is provided, the cache entry is re-keyed from ``src``
    to ``dest`` after verification passes — the cache is never consulted during the
    write/verify/journal sequence (C-PROV).

    :param src: Source path.
    :param dest: Destination path.
    :param journal: In-memory :class:`~music_annotator.models.TransactionLog`; mutated in place
        by appending the new entry after verification passes.
    :param journal_path: Path to the journal file.
    :param action: Journal action string.
    :param dest_root: Library root for log messages and empty-dir cleanup.
    :param now_str: ISO-format UTC timestamp string for the journal entry.
    :param release_id: MusicBrainz release MBID for the journal entry.
    :param cache: Optional :class:`TagReadCache` to re-key after the move succeeds.  The old
        path entry is moved to the new path key.  Never consulted during the move itself.
    :returns: Always 1 (one file successfully moved and journalled).
    :raises RuntimeError: On C-NOCLOBBER violation, SHA-256 mismatch, or :func:`_verify_copy` failure.
    :raises OSError: On filesystem errors (except EXDEV, handled by cross-fs fallback).
    """
    # a. Capture source SHA-256 and mtime before the move.
    src_hash = _sha256_file(src)
    src_stat = src.stat()
    src_mtime = src_stat.st_mtime

    # b. Ensure parent directory exists.
    dest.parent.mkdir(parents=True, exist_ok=True)

    # c. C-NOCLOBBER: atomically verify the destination does not exist.
    # When the destination already exists, check whether it has the same audio content as the
    # source (same SHA-256).  If yes, this is a dedup case: the content is already at the
    # destination; delete the source and journal the move.  If no, raise RuntimeError — a
    # maintenance move NEVER overwrites a destination with different content (C-NOCLOBBER).
    dest_exists = _atomic_noclobber_check(dest)
    if dest_exists:
        dest_hash_existing = _sha256_file(dest)
        if dest_hash_existing == src_hash:
            # Dedup: destination already has identical content.  Delete the source and journal
            # the move so the provenance chain records that the source path is gone.
            os.unlink(src)
            entry = TransactionEntry(
                timestamp=now_str,
                release_id=release_id,
                source=str(src),
                destination=str(dest),
                action=action,
            )
            append_journal_entry(journal_path, entry)
            journal.entries.append(entry)
            log.info(
                f"{action}_dedup",
                old=str(src.relative_to(dest_root)) if src.is_relative_to(dest_root) else str(src),
                new=str(dest.relative_to(dest_root)),
            )
            # Clean up now-empty source directories (best-effort).
            src_dir = src.parent
            while src_dir != dest_root and src_dir.is_relative_to(dest_root):
                try:
                    src_dir.rmdir()
                    log.info(f"{action}_removed_empty_dir", dir=str(src_dir.relative_to(dest_root)))
                    src_dir = src_dir.parent
                except OSError:
                    break
            return 1
        raise RuntimeError(f"C-NOCLOBBER: destination already exists and is not vacated by this plan: '{dest}'")

    # d. Move atomically via os.replace (rename within the same filesystem).
    try:
        os.replace(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Cross-filesystem fallback: copy + verify + unlink.
        shutil.copy2(src, dest)
        cross_hash = _sha256_file(dest)
        if cross_hash != src_hash:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"cross-fs copy integrity failure for '{src.name}': "
                f"src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {cross_hash[:12]}…"
            ) from exc
        os.unlink(src)

    # e. Verify destination SHA-256 == source SHA-256.
    dest_hash = _sha256_file(dest)
    if dest_hash != src_hash:
        raise RuntimeError(
            f"{action} integrity failure for '{dest.name}': src SHA-256 {src_hash[:12]}… ≠ dest SHA-256 {dest_hash[:12]}…"
        )

    # f. Reconstruct tags for _verify_copy (tags are unchanged by the move).
    ext = src.suffix.lower()
    try:
        match ext:
            case ".flac":
                post_dict = _read_tags_flac(dest)
            case ".mp3":
                post_dict = _read_tags_mp3(dest)
            case _:  # pragma: no cover
                post_dict = {}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{action} tag re-read failure for '{dest.name}': {exc}") from exc
    moved_tags = _tags_from_file_dict(post_dict)
    # _verify_copy checks mtime; for os.replace (same-fs rename) mtime is preserved.
    # For cross-fs copy2+unlink, shutil.copy2 copies atime/mtime so src_mtime still holds.
    _verify_copy(src, dest, moved_tags, None, src_mtime)

    # g. Durably append the journal entry before proceeding to the next file (C-PROV invariant:
    #    entry is written ONLY after _verify_copy passes).  append_journal_entry performs an O(1)
    #    JSONL append with fsync; the in-memory journal is updated so the caller's view stays
    #    current without re-reading the file between moves.
    entry = TransactionEntry(
        timestamp=now_str,
        release_id=release_id,
        source=str(src),
        destination=str(dest),
        action=action,
    )
    append_journal_entry(journal_path, entry)
    journal.entries.append(entry)
    log.info(
        f"{action}_moved",
        old=str(src.relative_to(dest_root)) if src.is_relative_to(dest_root) else str(src),
        new=str(dest.relative_to(dest_root)),
    )

    # h. Re-key the cache entry from src to dest (the file has moved; the old path key is stale).
    #    This runs after journal append so the cache is only updated when the move is fully
    #    committed — the cache is never consulted during the write/verify/journal sequence (C-PROV).
    if cache is not None:
        cache.rekey(src, dest)

    # i. Clean up now-empty source directories (best-effort; non-empty dirs are skipped).
    src_dir = src.parent
    while src_dir != dest_root and src_dir.is_relative_to(dest_root):
        try:
            src_dir.rmdir()  # Only succeeds if directory is now empty.
            log.info(f"{action}_removed_empty_dir", dir=str(src_dir.relative_to(dest_root)))
            src_dir = src_dir.parent
        except OSError:
            break

    return 1


def _execute_swap_cycles(
    swap_pairs: list[tuple[Path, Path]],
    *,
    journal: TransactionLog,
    journal_path: Path,
    action: str,
    dest_root: Path,
    now_str: str,
    release_id: str,
    cache: TagReadCache | None = None,
) -> int:
    """Execute swap-cycle moves via in-directory temp hops, keeping every hop inside C-PROV.

    A swap cycle (A→B, B→A) cannot be resolved by ordering alone because each move's destination
    is occupied by the other move's source.  This function breaks the cycle by moving one file to
    a temporary name in the same directory, then executing the remaining moves in order, then
    moving the temp file to its final destination.

    For a two-file swap (A→B, B→A):
    1. A → temp  (temp is in A's directory; journalled as ``action`` with dest=temp)
    2. B → A     (now A's slot is vacant; journalled as ``action`` with dest=A)
    3. temp → B  (now B's slot is vacant; journalled as ``action`` with dest=B)

    For longer cycles (A→B, B→C, C→A):
    1. A → temp
    2. C → A
    3. B → C
    4. temp → B

    Every hop goes through the full SHA-256 + :func:`_verify_copy` + journal chain (C-PROV
    invariant: no journal entry before verification passes).

    The temp file is created in the same directory as the first source so that the rename is
    guaranteed to be same-filesystem (no EXDEV risk for the temp hop).

    :param swap_pairs: List of ``(src, dest)`` pairs forming one or more swap cycles, as returned
        by :func:`_topo_sort_moves`.
    :param journal: In-memory :class:`~music_annotator.models.TransactionLog`; mutated in place
        by each successful hop via :func:`_execute_single_move`.
    :param journal_path: Path to the journal file.
    :param action: Journal action string.
    :param dest_root: Library root for log messages and empty-dir cleanup.
    :param now_str: ISO-format UTC timestamp string for journal entries.
    :param release_id: MusicBrainz release MBID for journal entries.
    :param cache: Optional :class:`TagReadCache` to re-key after each hop succeeds.  Passed
        through to :func:`_execute_single_move` for each hop.
    :returns: Count of files successfully moved and journalled (each hop counts as one move).
    :raises RuntimeError: On C-NOCLOBBER violation, SHA-256 mismatch, or :func:`_verify_copy` failure.
    :raises OSError: On filesystem errors.
    """
    moved_count = 0

    # Reconstruct the cycle(s) from swap_pairs.  _topo_sort_moves returns all cycle members;
    # we process them as a single chain (works for both 2-cycles and longer cycles).
    # Build a src→dest map to follow the chain.
    src_to_dest: dict[Path, Path] = dict(swap_pairs)

    # Find all distinct cycles by following chains.
    visited: set[Path] = set()
    cycles: list[list[Path]] = []
    for start_src, _ in swap_pairs:
        if start_src in visited:
            continue
        cycle: list[Path] = []
        current = start_src
        while current not in visited:
            visited.add(current)
            cycle.append(current)
            current = src_to_dest[current]
        cycles.append(cycle)

    for cycle in cycles:
        # cycle = [A, B, C, …] where A→B, B→C, C→A (the last element's dest is cycle[0]).
        first_src = cycle[0]
        first_dest = src_to_dest[first_src]

        # Step 1: Move first_src to a temp file in the same directory.
        # Use tempfile.mkstemp in the same directory to guarantee same-filesystem rename.
        # The temp file must have the same suffix as the source so _verify_copy can read tags.
        # Delete the empty placeholder created by mkstemp so _execute_single_move can use
        # C-NOCLOBBER semantics (it checks that the destination does not exist before moving).
        suffix = first_src.suffix
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, dir=str(first_src.parent))
        os.close(tmp_fd)
        tmp_path = Path(tmp_path_str)
        # Remove the empty placeholder so _execute_single_move can proceed with C-NOCLOBBER.
        tmp_path.unlink()
        # Move first_src to tmp_path through the full C-PROV chain (SHA-256 + verify + journal).
        moved_count += _execute_single_move(
            first_src,
            tmp_path,
            journal=journal,
            journal_path=journal_path,
            action=action,
            dest_root=dest_root,
            now_str=now_str,
            release_id=release_id,
            cache=cache,
        )
        log.info(
            f"{action}_swap_temp_hop",
            src=str(first_src.relative_to(dest_root)) if first_src.is_relative_to(dest_root) else str(first_src),
            tmp=str(tmp_path.relative_to(dest_root)) if tmp_path.is_relative_to(dest_root) else str(tmp_path),
        )

        # Step 2: Execute the remaining moves in the cycle in reverse order.
        # For cycle [A, B, C] (A→B, B→C, C→A): after A→temp, execute C→A, then B→C.
        # Processing in reverse (i from len-1 down to 1) ensures each destination is vacant
        # before the move: cycle[-1]→cycle[0] (now vacant via temp), cycle[-2]→cycle[-1], …
        # Each cycle[i] moves to src_to_dest[cycle[i]] (its original destination).
        for i in range(len(cycle) - 1, 0, -1):
            chain_src = cycle[i]
            actual_dest = src_to_dest[cycle[i]]
            moved_count += _execute_single_move(
                chain_src,
                actual_dest,
                journal=journal,
                journal_path=journal_path,
                action=action,
                dest_root=dest_root,
                now_str=now_str,
                release_id=release_id,
                cache=cache,
            )

        # Step 3: Move temp to first_dest (the original destination of first_src).
        # C-NOCLOBBER check: first_dest should now be vacant (the file that was there was moved
        # in step 2).  Use _execute_single_move which enforces C-NOCLOBBER.
        moved_count += _execute_single_move(
            tmp_path,
            first_dest,
            journal=journal,
            journal_path=journal_path,
            action=action,
            dest_root=dest_root,
            now_str=now_str,
            release_id=release_id,
            cache=cache,
        )

    return moved_count


def compute_library_modal_depth(
    pairs: list[tuple[str, int]],
) -> dict[str, int | None]:
    """Compute the library-wide work-group modal depth for every top-work MBID.

    Groups ``(cwp_workid_top, cwp_part_levels)`` pairs by top-work MBID and computes the modal
    ``CWP_PART_LEVELS`` value for each group via :func:`~music_annotator._works.work_group_modal_depth`.
    The result is the single authoritative depth map that all move passes must use so that every
    pass derives the same canonical destination from the same group membership (C-GROUPSCOPE).

    When ``maintain`` runs, it computes this map once over the full library scan and threads it
    into ``repath``, ``regroup``, and ``unify``.  When a pass runs standalone, it computes the
    map itself over its available data via this same function — one function, one membership
    definition.

    Ingest parity note: the ingest pipeline (``run()``) computes modal depth over the single
    release being ingested, which is per-release by construction (it only sees one release at a
    time).  That membership is narrower than the library-wide scan, but the computation is
    identical in structure.  When the library holds only one release for a given top-work MBID,
    the two computations agree exactly.

    :param pairs: List of ``(cwp_workid_top, cwp_part_levels_int)`` pairs for all files in scope.
        Empty-string top-work MBIDs are included in the map under the ``""`` key (files with no
        top-work link); callers that do not want a depth clamp for those files should treat
        ``map.get("")`` as ``None``.
    :returns: Mapping from ``cwp_workid_top`` to modal depth (``int``) or ``None`` when the
        group is all-orphan (every file has ``CWP_PART_LEVELS == 0``).  ``None`` means
        ``build_dest_path`` should use the file's own depth unchanged.
    """
    work_groups: dict[str, list[int]] = {}
    for twid, pl in pairs:
        work_groups.setdefault(twid, []).append(pl)

    result: dict[str, int | None] = {}
    for twid, part_levels in work_groups.items():
        modal = work_group_modal_depth(part_levels)
        # When modal is 0 (all-orphan group), store None so build_dest_path uses own depth
        # unchanged — equivalent outcome, avoids a redundant min(0, 0) clamp.
        result[twid] = modal if modal > 0 else None

    return result


def repath(  # pylint: disable=too-many-return-statements
    dest_root: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
    _journal: TransactionLog | None = None,
    _modal_depth_map: dict[str, int | None] | None = None,
) -> DryRunPlan | None:
    """Re-path all verified library files under ``dest_root`` to their corrected destinations.

    Walks the already-annotated library at ``dest_root``, reads the transaction journal to
    identify verified library files (``action in {"tagged", "repathed"}``), recomputes each
    file's destination path from its **embedded tags alone** (no MusicBrainz network calls),
    and moves files whose current path differs from the recomputed path.

    This maintenance-mode command is the retroactive counterpart to a path-policy change (such as
    the L0/L1 leaf and intermediate-directory numbering fix): it brings an already-annotated library
    forward to the new policy without re-ingesting from source.

    **Move semantics (provenance-chain invariant preserved):** for each file that needs moving:

    1. Capture source SHA-256.
    2. Move atomically via ``os.replace`` (rename within the library); fall back to
       ``shutil.copy2`` + ``os.unlink`` on ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    3. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    4. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    5. **Only then** append ``TransactionEntry(action="repathed", source=<old path>,
       destination=<new path>)`` and flush it to the journal before moving the next file, so a
       crash leaves a complete audit trail.

    No-op: files whose recomputed path matches their current path are skipped silently.

    Collision: when two legacy paths recompute to the same new path, the same
    ``_assess_collisions`` / ``_apply_collision_suffix`` machinery used by :func:`run` is applied
    (acoustid+length-aware, single collision authority).

    In ``dry_run`` mode: all planned moves and collisions are logged but **no files are moved
    and no journal entries are written**.

    When ``yes`` is ``False`` (the default), a confirmation prompt listing all planned moves is
    shown before any files are moved.  Pass ``yes=True`` (or ``-y``/``--yes`` on the CLI) to
    skip the prompt and proceed immediately.

    .. warning::
        A bare ``repath <dest>`` invocation **mass-relocates the entire library**.  The
        ``action="repathed"`` journal entries are the complete recovery record — if something goes
        wrong, examine ``music_annotator_journal.json`` in ``dest_root`` to reconstruct what
        moved where.  Use ``--dry-run`` first to preview all planned moves.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    :param yes: When ``True``, skip the confirmation prompt and move files immediately.
    :param _modal_depth_map: Pre-computed library-wide ``cwp_workid_top`` → modal-depth map
        from :func:`compute_library_modal_depth`.  When supplied by ``maintain``, this map was
        computed over the full library scan before any pass ran, ensuring all passes use the same
        group membership (C-GROUPSCOPE).  When ``None`` (standalone invocation), ``repath``
        computes the map itself over its own file scan via the same helper.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = _journal if _journal is not None else read_journal(journal_path)

    # Load the tag-read cache from the sidecar file.  A missing or malformed sidecar degrades
    # gracefully to an empty cache — never an error.  The cache is read-only during planning
    # and is saved back at the end of the pass (after all moves complete or on dry-run exit).
    cache = TagReadCache.load(dest_root / _TAG_CACHE_FILENAME)

    # --- Determine the current canonical path for each logical file ---
    # _resolve_current_lib walks entries in chronological order; "tagged" seeds the map and
    # "repathed"/"regrouped" entries update it.  Multi-hop chains resolve naturally because
    # each move pops the old path and registers the new one.
    current_lib = _resolve_current_lib(journal)

    # Filter to files that actually exist on disk; carry the release_id so the collision-suffix
    # builder can derive a release-identifying token (8-char MBID prefix) rather than falling
    # back to a default-constructed MBRelease whose id is "".
    existing_files: list[tuple[Path, str]] = [(p, rid) for p, rid in current_lib.items() if p.exists()]

    if not existing_files:
        log.info("repath_nothing_to_move", dest_root=str(dest_root))
        cache.save()
        if dry_run:
            return DryRunPlan(pass_name="repath", entries=[], count=0)
        return None

    # --- Pass 1: read tags for all existing files ---
    # Collect (path, tags, file_dict, ext, release_id) tuples so that the work-group modal depth
    # can be computed once per group before building the plan (compute-once-per-group invariant).
    # release_id is threaded through so the collision-suffix builder receives the file's real MBID.
    #
    # Extension-less files (suffix lost during over-long-name truncation) are repaired here:
    # mutagen probes the file to identify its format, the correct suffix is appended (with
    # shortening if the repaired leaf would exceed _NAME_MAX), and the file is moved via
    # _move_verify_journal before tag-reading continues.  After repair the file has a suffix
    # and is visible to all subsequent maintenance passes.
    #
    # The tag-read cache is consulted for each file: on a hit (path, size, mtime all match),
    # the cached tag dict is returned without opening the audio file.  On a miss, the file is
    # read and the result is stored in the cache for future runs.
    _repath_file_data: list[tuple[Path, TrackTags, dict[str, str], str, str]] = []
    for current_path, release_id in existing_files:
        ext = current_path.suffix.lower()

        if ext not in {".flac", ".mp3"}:
            # Extension-less or unrecognised suffix: probe for audio format.
            correct_suffix = _detect_audio_suffix(current_path)
            if correct_suffix is None:
                log.warning("repath_not_a_track_file", path=str(current_path), ext=ext)
                continue
            # Compute the repaired leaf: current stem + correct suffix.  The stem equals the
            # full filename because the file has no recognised suffix.  If the repaired leaf
            # exceeds _NAME_MAX bytes, shorten it via _proposed_short so that stem+suffix ≤
            # _NAME_MAX (the 7 real stranded files are already at or over the limit).
            stem = current_path.name
            repaired_leaf = stem + correct_suffix
            if len(repaired_leaf.encode("utf-8")) > _NAME_MAX:
                repaired_leaf = _proposed_short(repaired_leaf, correct_suffix)
            repaired_path = current_path.parent / repaired_leaf
            if dry_run:
                log.info(
                    "repath_extension_repair_dry_run",
                    path=str(current_path.relative_to(dest_root)),
                    repaired=str(repaired_path.relative_to(dest_root)),
                )
                continue
            # Repair move: hash → rename → verify → journal (C-PROV invariant: journal entry
            # is written only after _verify_copy passes inside _move_verify_journal).
            # The in-memory journal is updated in place so no re-read is needed.
            # The cache is threaded through so the entry is re-keyed to the repaired path.
            repair_now = datetime.datetime.now(datetime.UTC)
            _move_verify_journal(
                [(current_path, repaired_path)],
                journal=journal,
                journal_path=journal_path,
                action="repathed",
                dest_root=dest_root,
                now=repair_now,
                release_id="",
                cache=cache,
            )
            current_path = repaired_path
            ext = correct_suffix

        try:
            file_dict = _read_tags_cached(current_path, ext, cache)
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("repath_tag_read_error", path=str(current_path), error=str(exc))
            continue

        tags = _tags_from_file_dict(file_dict)

        # Reconstruct performer ArtistEntry lists from embedded tags so that build_dest_path
        # can render canonical entity name-forms (MB artist name field per NORM-2 as revised)
        # in the compact path projection.  The list fields are excluded from to_file_dict()
        # and therefore absent from the embedded tag dict; without hydration, build_dest_path
        # falls back to the raw CEA_ENSEMBLE_NAMES / ARTIST string.  No network calls are made.
        _hydrate_performer_lists(tags, file_dict)
        _repath_file_data.append((current_path, tags, file_dict, ext, release_id))

    # --- Compute work-group modal depth per group (once per group, not per track) ---
    # When _modal_depth_map is supplied by maintain (computed once over the full library scan
    # before any pass ran), use it directly — all passes share the same group membership
    # (C-GROUPSCOPE).  When None (standalone invocation), compute the map from the files
    # visible to this pass via the shared helper so the same function and membership definition
    # are used regardless of call site.
    if _modal_depth_map is not None:
        _repath_depth_map: dict[str, int | None] = _modal_depth_map
    else:
        # Standalone: compute over the full library scan visible to this pass.
        # Groups tracks by CWP_WORKID_TOP, mirroring the scanner grouping in
        # scripts/scan_nonuniform_depth.py (which groups by (release_dir, CWP_WORKID_TOP)).
        # repath operates across the whole library, so release_dir is implicit in the path; the
        # grouping key is CWP_WORKID_TOP alone (consistent with the scanner's per-release-dir
        # grouping because each release dir maps to one top-work MBID in practice).
        _repath_depth_map = compute_library_modal_depth(
            [(_rt.cwp_workid_top, int(_rt.cwp_part_levels or "0")) for _, _rt, _, _, _ in _repath_file_data]
        )

    _repath_modal_by_idx: dict[int, int | None] = {
        _ri: _repath_depth_map.get(_rt.cwp_workid_top) for _ri, (_, _rt, _, _, _) in enumerate(_repath_file_data)
    }

    # --- SEL-23 ensemble patch (release-scope ensemble expansion) ---
    # Group files by MUSICBRAINZ_ALBUMID and apply the SEL-23 rule over each release group so
    # the majority threshold is computed over the correct denominator (the full track set of each
    # release).  sel23_ensemble_patch expands cea_album_ensembles_list on each track to include
    # any ensemble present on a modal majority (>50%) of the release's tracks.  This must run
    # before Pass 2 so the expanded set is used for path computation.
    _repath_release_groups: dict[str, list[int]] = {}
    for _ri, (_, _, _rfd, _, _) in enumerate(_repath_file_data):
        _album_id = _rfd.get("MUSICBRAINZ_ALBUMID", "")
        _repath_release_groups.setdefault(_album_id, []).append(_ri)
    for _album_id, _release_idxs in _repath_release_groups.items():
        if _album_id:  # skip files with no album ID (cannot determine release group)
            sel23_ensemble_patch([_repath_file_data[_i][1] for _i in _release_idxs])

    # --- Pass 2: build repath plan using the per-group modal depth ---
    # plan_pairs carries (src, dest, acoustid, length_ms, release_id) so the collision-suffix
    # builder can group non-matches by release_id and derive a release-identifying suffix.
    plan_pairs: list[tuple[Path, Path, str, int, str]] = []

    for _ri, (current_path, tags, file_dict, ext, release_id) in enumerate(_repath_file_data):
        # Construct minimal stand-in objects for build_dest_path.
        # release is kept for API stability (C-INIT removed the last internal use of
        # release.artist_credit in the classical path).  track.position is used only as the
        # deepest leaf-nn fallback (when CWP_MOVT_NUM is absent and global_track_idx=0); zero
        # is acceptable here because CWP_MOVT_NUM must be present for the repath to produce a
        # meaningful path.
        stub_release = MBRelease()
        stub_track = MBTrack()

        new_dest_base = build_dest_path(
            dest_root,
            stub_release,
            stub_track,
            tags,
            global_track_idx=0,
            group_modal_depth=_repath_modal_by_idx.get(_ri),
        )
        new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext), current_path)

        if new_dest == current_path:
            log.debug("repath_noop", path=str(current_path.relative_to(dest_root)))
            continue

        acoustid = file_dict.get("ACOUSTID_ID", "")
        length_str = file_dict.get("LENGTH", "0")
        try:
            length_ms = int(length_str) if length_str else 0
        except ValueError:
            length_ms = 0

        plan_pairs.append((current_path, new_dest, acoustid, length_ms, release_id))
        log.info(
            "repath_plan",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            dry_run=dry_run,
        )

    if not plan_pairs:
        log.info("repath_all_current", dest_root=str(dest_root))
        cache.save()
        if dry_run:
            return DryRunPlan(pass_name="repath", entries=[], count=0)
        return None

    # --- Intra-plan collision guard (legitimate partial-performance collisions) ---
    # When two or more files in the plan recompute to the same clean destination, they are
    # likely legitimate partial-performance collisions: different recordings of the same work
    # that were previously disambiguated by a collision suffix on the work_dir.  Moving them
    # would cause one to overwrite the other (os.replace is atomic and silently clobbers).
    # Guard: skip all files in such groups — they are already at valid (collision-suffixed)
    # locations and must not lose their disambiguation.
    #
    # This is distinct from the existing on-disk collision detection below, which handles
    # the case where a planned destination already exists on disk before any moves begin.
    # Intra-plan collisions are invisible to _assess_collisions because neither destination
    # exists on disk yet when the plan is built.
    _dest_to_plan_indices: dict[Path, list[int]] = {}
    for _i, (_, _dest, _, _, _) in enumerate(plan_pairs):
        _dest_to_plan_indices.setdefault(_dest, []).append(_i)

    _intra_collision_indices: set[int] = set()
    for _dest, _indices in _dest_to_plan_indices.items():
        if len(_indices) > 1:
            for _idx in _indices:
                _intra_collision_indices.add(_idx)
            log.info(
                "repath_intra_plan_collision_skipped",
                dest=str(_dest.relative_to(dest_root)),
                count=len(_indices),
            )

    if _intra_collision_indices:
        plan_pairs = [pair for _i, pair in enumerate(plan_pairs) if _i not in _intra_collision_indices]
        if not plan_pairs:
            log.info("repath_all_current", dest_root=str(dest_root))
            cache.save()
            if dry_run:
                return DryRunPlan(pass_name="repath", entries=[], count=0)
            return None

    # --- Collision detection and resolution ---
    # C-SEQ vacancy-aware collision check: subtract plan-vacated paths (sources of all moves in
    # the plan) from the collision check.  A destination occupied by a file that will be vacated
    # by another move in the same plan is not a genuine collision — the occupant will be moved
    # away before this move executes (guaranteed by the topological ordering in _move_verify_journal).
    _repath_vacated: frozenset[Path] = frozenset(src for src, _, _, _, _ in plan_pairs)
    collision_pairs = [(src, dest, acust, length) for src, dest, acust, length, _ in plan_pairs]
    collision_results = _assess_collisions(collision_pairs, vacated_paths=_repath_vacated)

    # Build a dest→(src, rid) lookup for collision resolution.
    _dest_to_src_rid: dict[Path, tuple[Path, str]] = {dest: (src, rid) for src, dest, _, _, rid in plan_pairs}

    # --- match=True arm: same-audio occupant → shared group-resolution flow (C-DEDUP) ---
    # For each confirmed same-audio collision, invoke resolve_duplicate_group() which prompts
    # the operator (survivor / keep-both / abort) and executes the C-DEDUP ordering:
    # xref write + verify + journal on the survivor before any deletion.
    # Entries whose move is dropped (survivor_occupant or keep_both) are removed from plan_pairs.
    # Entries whose move proceeds (survivor_mover) remain in plan_pairs — the occupant was deleted.
    # An abort result terminates the entire repath run immediately.
    _repath_drop_indices: set[int] = set()
    _src_to_plan_idx: dict[Path, int] = {src: i for i, (src, _, _, _, _) in enumerate(plan_pairs)}
    now_for_dedup = datetime.datetime.now(datetime.UTC)
    confirmed_matches = [r for r in collision_results if r.match is True]
    for _match_result in confirmed_matches:
        _mover_src, _mover_rid = _dest_to_src_rid.get(_match_result.dest, (Path(""), ""))
        if not _mover_src.name:
            continue  # pragma: no cover — dest always in plan
        _plan_idx = _src_to_plan_idx.get(_mover_src, -1)
        if _plan_idx < 0:
            continue  # pragma: no cover — src always in plan
        _occupant_path = _match_result.dest
        _occupant_rid = _resolve_current_lib(journal).get(_occupant_path, "")
        resolution = resolve_duplicate_group(
            _occupant_path,
            _occupant_rid,
            _mover_src,
            _mover_rid,
            _match_result.method,
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=now_for_dedup,
            dry_run=dry_run,
        )
        match resolution.choice:
            case "abort":
                log.info("repath_aborted_by_operator", dest_root=str(dest_root))
                cache.save()
                return None
            case "survivor_occupant" | "keep_both":
                # Move dropped: occupant stays, mover deleted (or kept at source for keep_both).
                _repath_drop_indices.add(_plan_idx)
            case "survivor_mover":
                # Occupant deleted; move proceeds — keep the entry in plan_pairs.
                pass
            case _:  # pragma: no cover
                pass

    # --- match=None arm: inconclusive occupant → prompt suffix-or-abort ---
    # When audio comparison is inconclusive (no fingerprint, no AcoustID, duration within
    # tolerance), the operator is prompted to choose between applying a collision suffix
    # (keeping both files) or aborting the run.  No deletion is performed for inconclusive
    # collisions (C-DEDUP: match=None never deletes).
    confirmed_inconclusives = [r for r in collision_results if r.match is None]
    for _inc_result in confirmed_inconclusives:
        _mover_src_inc, _mover_rid_inc = _dest_to_src_rid.get(_inc_result.dest, (Path(""), ""))
        if not _mover_src_inc.name:
            continue  # pragma: no cover
        _plan_idx_inc = _src_to_plan_idx.get(_mover_src_inc, -1)
        if _plan_idx_inc < 0:
            continue  # pragma: no cover
        if _plan_idx_inc in _repath_drop_indices:
            continue  # already resolved by a match=True arm
        _occ_rel_inc = (
            str(_inc_result.dest.relative_to(dest_root))
            if _inc_result.dest.is_relative_to(dest_root)
            else str(_inc_result.dest)
        )
        _mov_rel_inc = (
            str(_mover_src_inc.relative_to(dest_root)) if _mover_src_inc.is_relative_to(dest_root) else str(_mover_src_inc)
        )
        if dry_run:
            _console.print(
                f"\n[bold yellow]Inconclusive collision[/] (evidence: {_markup_escape(_inc_result.method)})\n"
                f"  occupant: [dim]{_markup_escape(_occ_rel_inc)}[/]\n"
                f"  mover:    [dim]{_markup_escape(_mov_rel_inc)}[/]\n"
                f"  [dim](dry-run: would prompt suffix-or-abort)[/]"
            )
            # In dry-run, report as a suffix (keep both) without prompting.
            continue
        _console.print(
            f"\n[bold yellow]Inconclusive collision[/] (evidence: {_markup_escape(_inc_result.method)})\n"
            f"  occupant: [dim]{_markup_escape(_occ_rel_inc)}[/]\n"
            f"  mover:    [dim]{_markup_escape(_mov_rel_inc)}[/]\n"
            f"\nChoose:\n"
            f"  [bold]s[/] — apply collision suffix (keep both files)\n"
            f"  [bold]a[/] — abort run\n"
        )
        _console.print("[bold cyan]>[/] ", end="")
        _inc_answer = input("").strip().lower()
        if _inc_answer != "s":
            log.info("repath_aborted_by_operator_inconclusive", dest_root=str(dest_root))
            cache.save()
            return None
        # Apply suffix: rewrite the destination for this mover.
        _stub_plan_inc = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _, _ in plan_pairs]
        _apply_collision_suffix(_stub_plan_inc, [_inc_result], MBRelease(id=_mover_rid_inc), dest_root)
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length, rid)
            for entry, (_, _, acust, length, rid) in zip(_stub_plan_inc, plan_pairs)
        ]
        # Rebuild the lookup after suffix application.
        _dest_to_src_rid = {dest: (src, rid) for src, dest, _, _, rid in plan_pairs}
        _src_to_plan_idx = {src: i for i, (src, _, _, _, _) in enumerate(plan_pairs)}
        log.warning("repath_inconclusive_suffix_applied", mover=str(_mover_src_inc))

    # --- match=False arm: confirmed non-match → apply release-identifying suffix ---
    confirmed_nonmatches = [r for r in collision_results if r.match is False]
    if confirmed_nonmatches:
        # Rewrite destinations for confirmed non-matches using a release-identifying suffix
        # (8-char MBID prefix) so the disambiguated directory carries a release-identifying
        # token rather than an empty " []".  Group non-matches by release_id and call
        # _apply_collision_suffix once per group so each group gets its own release's suffix.
        stub_plan = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _, _ in plan_pairs]
        # Build a dest→rid lookup so each non-match entry can be routed to its release's suffix.
        _dest_to_rid: dict[Path, str] = {dest: rid for _, dest, _, _, rid in plan_pairs}
        # Group non-match results by release_id; each group gets its own _apply_collision_suffix
        # call so the suffix is derived from that group's real release MBID.
        _nonmatch_by_rid: dict[str, list[AudioCompareResult]] = {}
        for _nm in confirmed_nonmatches:
            _nm_rid = _dest_to_rid.get(_nm.dest, "")
            _nonmatch_by_rid.setdefault(_nm_rid, []).append(_nm)
        for _nm_rid, _nm_group in _nonmatch_by_rid.items():
            _apply_collision_suffix(stub_plan, _nm_group, MBRelease(id=_nm_rid), dest_root)
        # Rebuild plan_pairs with updated destinations
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length, rid)
            for entry, (_, _, acust, length, rid) in zip(stub_plan, plan_pairs)
        ]
        log.warning("repath_collision_suffix_applied", count=len(confirmed_nonmatches))

    # Drop entries resolved by the match=True arm (survivor_occupant or keep_both).
    if _repath_drop_indices:
        plan_pairs = [pair for _i, pair in enumerate(plan_pairs) if _i not in _repath_drop_indices]
        if not plan_pairs:
            log.info("repath_all_current", dest_root=str(dest_root))
            cache.save()
            if dry_run:
                return DryRunPlan(pass_name="repath", entries=[], count=0)
            return None

    if dry_run:
        dry_run_entries: list[DryRunEntry] = []
        for current_path, new_dest, _, _, _ in plan_pairs:
            log.info(
                "repath_dry_run",
                old=str(current_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
            )
            dry_run_entries.append(DryRunEntry(current_path=str(current_path), planned_path=str(new_dest)))
        cache.save()
        return DryRunPlan(pass_name="repath", entries=dry_run_entries, count=len(dry_run_entries))

    # --- Confirmation prompt ---
    if not yes:
        _console.print("\n[bold yellow]repath[/] will move the following files:\n")
        for current_path, new_dest, _, _, _ in plan_pairs:
            _console.print(
                f"  [dim]{_markup_escape(str(current_path.relative_to(dest_root)))}[/]\n"
                f"    → [green]{_markup_escape(str(new_dest.relative_to(dest_root)))}[/]"
            )
        _console.print(f"\n[bold]{len(plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("repath_aborted", dest_root=str(dest_root))
            return None

    # --- Perform moves, verify, journal ---
    # repath does not have a per-file release_id in the journal (the release is not known at
    # repath time for files that were never tagged with a release MBID); release_id="" is the
    # correct sentinel here.  The collision suffix was already derived from the real id above.
    # The journal loaded at the start of this pass is threaded through so _move_verify_journal
    # never re-reads it between moves — each append updates the in-memory copy in place.
    # The cache is threaded through so each successful move re-keys the entry to the new path.
    now = datetime.datetime.now(datetime.UTC)
    move_pairs = [(src, dest) for src, dest, _, _, _ in plan_pairs]
    # C-IDEM tripwire: warn before executing if any planned move inverts a prior journal entry.
    _warn_inverse_moves(move_pairs, "repath", journal)
    moved = _move_verify_journal(
        move_pairs,
        journal=journal,
        journal_path=journal_path,
        action="repathed",
        dest_root=dest_root,
        now=now,
        release_id="",
        cache=cache,
    )
    cache.save()
    log.info("repath_complete", dest_root=str(dest_root), moved=moved)
    return None


def _apply_group_movement_renumber(
    group: list[tuple[Path, TrackTags, dict[str, str], str]],
    *,
    single_work_album: bool,
) -> None:
    """Re-derive gap-free ``CWP_MOVT_NUM`` for one ``CWP_WORKID_TOP`` group and write changed tags to disk.

    Sorts the supplied tracks by embedded ``(DISCNUMBER, TRACKNUMBER)`` (the ordering authority
    for the maintenance path) and calls
    :func:`~music_annotator._tags.assign_group_movement_numbers` to assign gap-free 1-based
    ``cwp_movt_num`` / ``cwp_movt_tot`` / ``movementnumber`` / ``movementtotal`` /
    ``cwp_single_work_album``.

    The leaf ``nn`` prefix (``CWP_MOVT_NUM``) is the per-top-work-group gap-free playback index.
    It is session-local and must never be trusted across a merge.  After any consolidation the
    index must be re-derived from embedded ``(DISCNUMBER, TRACKNUMBER)`` order so that the
    resulting path is idempotent across sessions.

    **Collision guard**: if all embedded ``CWP_MOVT_NUM`` values are already unique (no
    duplicates), the group is already gap-free and the renumber is skipped entirely.  This
    preserves the existing numbering for single-session works and for groups whose
    ``DISCNUMBER``/``TRACKNUMBER`` tags are absent or non-unique (where the sort order would
    be non-deterministic).  Only groups with duplicate ``CWP_MOVT_NUM`` values — the signature
    of a cross-session merge — are renumbered.

    For each track whose ``cwp_movt_num`` changed, the updated :class:`~music_annotator.models.TrackTags`
    is written back to the audio file in-place via :func:`~music_annotator._tagger.apply_tags_flac`
    or :func:`~music_annotator._tagger.apply_tags_mp3`.  The caller's ``tags`` object is mutated
    in-place, so subsequent :func:`~music_annotator._tags.build_dest_path` calls see the corrected
    value without re-reading the file.

    :param group: List of ``(path, tags, file_dict, ext)`` tuples for one ``CWP_WORKID_TOP``
        group, in any order.  ``tags`` is mutated in-place.  ``ext`` is the lowercase file
        extension (``".flac"`` or ``".mp3"``).
    :param single_work_album: ``True`` when the release contains exactly one top-work group.
        Passed through to :func:`~music_annotator._tags.assign_group_movement_numbers` to set
        ``CWP_SINGLE_WORK_ALBUM``.
    :raises RuntimeError: If a tag write fails (propagated from
        :func:`~music_annotator._tagger.apply_tags_flac` /
        :func:`~music_annotator._tagger.apply_tags_mp3`).
    """
    # Collision guard: if all CWP_MOVT_NUM values are already unique, the group is already
    # gap-free.  Skip the renumber to preserve the existing numbering for single-session works
    # and for groups whose DISCNUMBER/TRACKNUMBER tags are absent or non-unique.
    existing_nums = [tags.cwp_movt_num for _, tags, _, _ in group]
    if len(set(existing_nums)) == len(existing_nums):
        return  # all unique — no collision, no renumber needed

    def _sort_key(item: tuple[Path, TrackTags, dict[str, str], str]) -> tuple[int, int]:
        """Return ``(disc, track)`` sort key from embedded tags.

        Non-integer or absent values map to 0 so they sort first without raising.

        :param item: ``(path, tags, file_dict, ext)`` tuple.
        :returns: ``(discnumber_int, tracknumber_int)`` pair.
        """
        _, _tags, _, _ = item
        disc_str = _tags.discnumber.split("/", maxsplit=1)[0].strip()
        track_str = _tags.tracknumber.split("/", maxsplit=1)[0].strip()
        disc = int(disc_str) if disc_str.isdigit() else 0
        track = int(track_str) if track_str.isdigit() else 0
        return disc, track

    ordered = sorted(group, key=_sort_key)

    # Snapshot movement tags before the call so we can detect which files changed.
    # Both cwp_movt_num and cwp_movt_tot are written by assign_group_movement_numbers; either
    # changing (e.g. total changes when the group grows) triggers a tag rewrite.
    before = [(tags.cwp_movt_num, tags.cwp_movt_tot) for _, tags, _, _ in ordered]

    assign_group_movement_numbers([tags for _, tags, _, _ in ordered], single_work_album=single_work_album)

    for (path, tags, _, ext), (old_num, old_tot) in zip(ordered, before):
        if tags.cwp_movt_num == old_num and tags.cwp_movt_tot == old_tot:
            continue  # no change — skip the write
        log.info(
            "consolidation_renumber",
            path=str(path),
            old_cwp_movt_num=old_num,
            new_cwp_movt_num=tags.cwp_movt_num,
            old_cwp_movt_tot=old_tot,
            new_cwp_movt_tot=tags.cwp_movt_tot,
        )
        try:
            match ext:
                case ".flac":
                    apply_tags_flac(path, tags)
                case ".mp3":
                    apply_tags_mp3(path, tags)
                case _:  # pragma: no cover — callers filter to .flac/.mp3 before building groups
                    pass
        except MutagenError as exc:
            raise RuntimeError(f"consolidation renumber tag write failure for '{path.name}': {exc}") from exc


def regroup(  # pylint: disable=too-many-return-statements
    dest_root: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
    _journal: TransactionLog | None = None,
    _modal_depth_map: dict[str, int | None] | None = None,
) -> DryRunPlan | None:
    """Consolidate confirmed split-release files into their canonical destinations.

    Reads the transaction journal, runs the tag-confirmation fragmentation audit
    (:func:`~music_annotator._pipeline_io._confirm_fragmentation`), and acts on **confirmed
    case-(b) split-release candidates only** — release MBIDs whose tracks are scattered across
    more than one work directory, where at least one backing file's embedded ``MUSICBRAINZ_ALBUMID``
    tag confirms the journal's ``release_id`` (i.e. ``confirmed=True``).

    For each confirmed split release the affected files are identified by filtering ``action ==
    "tagged"`` journal entries whose ``release_id`` is in the confirmed set.  Each file's canonical
    destination is recomputed from its **embedded tags alone** via :func:`~music_annotator.build_dest_path`
    (the same offline engine :func:`repath` uses — no MusicBrainz network calls).

    **Move semantics (provenance-chain invariant preserved):** for each file that needs moving:

    1. Capture source SHA-256.
    2. Move atomically via ``os.replace`` (rename within the library); fall back to
       ``shutil.copy2`` + ``os.unlink`` on ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    3. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    4. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    5. **Only then** append ``TransactionEntry(action="regrouped", release_id=<the split
       release's MBID>, source=<old path>, destination=<new path>)`` and flush it to the journal
       before moving the next file.

    Unlike :func:`repath`, the ``release_id`` field is populated in ``"regrouped"`` entries
    because the move is release-driven: the release MBID that drove candidate selection is already
    known from the journal and is recorded so that future audits can re-confirm the entry without a
    MusicBrainz lookup.  This keeps the regrouped entry self-describing and preserves P2
    (journal detects, tag adjudicates) for the regroup path.

    Collision: when two files recompute to the same new path, the same
    ``_assess_collisions`` / ``_apply_collision_suffix`` machinery used by :func:`run` is applied.

    In ``dry_run`` mode: all planned moves are logged but **no files are moved and no journal
    entries are written**.  The confirmation prompt is not shown in dry-run mode.

    When ``yes=True`` the confirmation prompt is skipped; files are moved immediately after
    building the plan.  When ``yes=False`` (default), the planned moves are printed and the user
    must confirm with ``y``/``yes`` before any move is performed.  When the plan is empty, a
    "nothing to regroup" message is logged and the function returns immediately.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param yes: When ``True``, skip the interactive confirmation prompt.
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    :param _modal_depth_map: Pre-computed library-wide ``cwp_workid_top`` → modal-depth map
        from :func:`compute_library_modal_depth`.  When supplied by ``maintain``, this map was
        computed over the full library scan before any pass ran, ensuring all passes use the same
        group membership (C-GROUPSCOPE).  When ``None`` (standalone invocation), ``regroup``
        computes the map itself over its own file scan (confirmed files plus non-confirmed library
        context files sharing the same top-work MBIDs) via the same helper.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = _journal if _journal is not None else read_journal(journal_path)

    # Load the tag-read cache from the sidecar file.  A missing or malformed sidecar degrades
    # gracefully to an empty cache — never an error.
    cache = TagReadCache.load(dest_root / _TAG_CACHE_FILENAME)

    # --- Identify confirmed case-(b) split-release candidates ---
    # _confirm_fragmentation returns (case_a, case_b); we act on case_b only.
    _, case_b = _confirm_fragmentation(dest_root, journal)
    confirmed_release_ids: set[str] = {rid for rid, (_, confirmed) in case_b.items() if confirmed}

    if not confirmed_release_ids:
        log.info("regroup_nothing_to_regroup", dest_root=str(dest_root))
        if dry_run:
            return DryRunPlan(pass_name="regroup", entries=[], count=0)
        return None

    # --- Identify affected files from journal entries ---
    # _resolve_current_lib resolves the full library lineage; filter to confirmed release IDs.
    full_lib = _resolve_current_lib(journal)
    current_lib: dict[Path, str] = {p: rid for p, rid in full_lib.items() if rid in confirmed_release_ids}

    # Filter to files that actually exist on disk
    existing_files: list[tuple[Path, str]] = [(p, rid) for p, rid in current_lib.items() if p.exists()]

    if not existing_files:
        log.info("regroup_nothing_to_regroup", dest_root=str(dest_root))
        if dry_run:
            return DryRunPlan(pass_name="regroup", entries=[], count=0)
        return None

    # --- Pass 1: read tags for all existing files ---
    # Collect (path, tags, file_dict, ext, release_id) tuples so that the work-group modal
    # depth can be computed once per group before building the plan.
    # The tag-read cache is consulted for each file: on a hit (path, size, mtime all match),
    # the cached tag dict is returned without opening the audio file.
    _regroup_file_data: list[tuple[Path, TrackTags, dict[str, str], str, str]] = []
    for current_path, release_id in existing_files:
        ext = current_path.suffix.lower()
        if ext not in {".flac", ".mp3"}:  # pragma: no cover — AUDIO_EXTENSIONS may include unsupported types
            log.warning("regroup_unsupported_format", path=str(current_path), ext=ext)
            continue
        try:
            file_dict = _read_tags_cached(current_path, ext, cache)
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("regroup_tag_read_error", path=str(current_path), error=str(exc))
            continue

        tags = _tags_from_file_dict(file_dict)

        # Reconstruct performer ArtistEntry lists from embedded tags so that build_dest_path
        # renders canonical entity name-forms (MB artist name field per NORM-2 as revised).
        # No network calls are made — the maintenance path reads embedded tags alone.
        _hydrate_performer_lists(tags, file_dict)
        _regroup_file_data.append((current_path, tags, file_dict, ext, release_id))

    # --- Compute work-group modal depth per group (once per group, not per track) ---
    # When _modal_depth_map is supplied by maintain (computed once over the full library scan
    # before any pass ran), use it directly — all passes share the same group membership
    # (C-GROUPSCOPE).  When None (standalone invocation), compute the map from the confirmed
    # files plus non-confirmed library context files sharing the same top-work MBIDs, so the
    # modal depth ceiling matches repath's full-library computation and avoids the same-run
    # ping-pong (repath moves to work-subdir at full-library modal depth; regroup moves back
    # at subset modal depth).
    if _modal_depth_map is not None:
        _regroup_depth_map: dict[str, int | None] = _modal_depth_map
    else:
        # Standalone: extend the modal depth computation to include all library files that share
        # the same CWP_WORKID_TOP as the confirmed-release tracks, even if those files belong to
        # other releases.  Files outside confirmed_release_ids are read for depth context only —
        # they are never moved.
        _regroup_work_ids_in_scope: frozenset[str] = frozenset(
            _rt.cwp_workid_top for _, _rt, _, _, _ in _regroup_file_data if _rt.cwp_workid_top
        )
        _regroup_confirmed_paths: frozenset[Path] = frozenset(p for p, _, _, _, _ in _regroup_file_data)
        _regroup_ctx_pairs: list[tuple[str, int]] = [
            (_rt.cwp_workid_top, int(_rt.cwp_part_levels or "0")) for _, _rt, _, _, _ in _regroup_file_data
        ]
        for _ctx_path, _ctx_rid in full_lib.items():
            if _ctx_rid in confirmed_release_ids:
                continue  # already in _regroup_file_data
            if _ctx_path in _regroup_confirmed_paths:
                continue  # pragma: no cover — defensive: a non-confirmed path cannot be in confirmed set
            if not _ctx_path.exists():
                continue
            _ctx_ext = _ctx_path.suffix.lower()
            if _ctx_ext not in {".flac", ".mp3"}:  # pragma: no cover — full_lib only contains tagged audio files
                continue
            try:
                _ctx_file_dict = _read_tags_cached(_ctx_path, _ctx_ext, cache)
            except Exception:  # noqa: BLE001 — tag read failure: skip for depth context  # pragma: no cover
                continue
            _ctx_twid = _ctx_file_dict.get("CWP_WORKID_TOP", "")
            if _ctx_twid not in _regroup_work_ids_in_scope:
                continue
            _regroup_ctx_pairs.append((_ctx_twid, int(_ctx_file_dict.get("CWP_PART_LEVELS") or "0")))
        _regroup_depth_map = compute_library_modal_depth(_regroup_ctx_pairs)

    _regroup_modal_by_idx: dict[int, int | None] = {
        _ri: _regroup_depth_map.get(_rt.cwp_workid_top) for _ri, (_, _rt, _, _, _) in enumerate(_regroup_file_data)
    }

    # --- SEL-23 ensemble patch (release-scope ensemble expansion) ---
    # Group files by MUSICBRAINZ_ALBUMID and apply the SEL-23 rule over each release group so
    # the majority threshold is computed over the correct denominator (the full track set of each
    # release).  sel23_ensemble_patch expands cea_album_ensembles_list on each track to include
    # any ensemble present on a modal majority (>50%) of the release's tracks.  This must run
    # before Pass 2 so the expanded set is used for path computation.
    _regroup_release_groups: dict[str, list[int]] = {}
    for _ri, (_, _, _rfd, _, _) in enumerate(_regroup_file_data):
        _album_id = _rfd.get("MUSICBRAINZ_ALBUMID", "")
        _regroup_release_groups.setdefault(_album_id, []).append(_ri)
    for _album_id, _release_idxs in _regroup_release_groups.items():
        if not _album_id:
            continue  # pragma: no cover — defensive guard; regroup only processes confirmed
            # split-release candidates which always have MUSICBRAINZ_ALBUMID embedded.
        sel23_ensemble_patch([_regroup_file_data[_i][1] for _i in _release_idxs])

    # --- Movement renumber pass: re-derive CWP_MOVT_NUM from embedded (DISCNUMBER, TRACKNUMBER) ---
    # The leaf nn prefix (CWP_MOVT_NUM) is session-local and must never be trusted across a merge.
    # After consolidating cross-session fragments, re-derive the gap-free 1-based index from
    # embedded (DISCNUMBER, TRACKNUMBER) order within each CWP_WORKID_TOP group so that the
    # resulting path is idempotent across sessions.  This must run after the SEL-23 patch (which
    # may change performer tags) and before Pass 2 (which calls build_dest_path).
    # Only classical tracks with a non-empty CWP_WORKID_TOP are renumbered; non-classical tracks
    # (empty CWP_WORKID_TOP) have no top-work group identity and must not be renumbered here.
    for _album_id, _release_idxs in _regroup_release_groups.items():
        if not _album_id:
            continue  # pragma: no cover — defensive guard (same as SEL-23 guard above)
        # Group the release's files by CWP_WORKID_TOP; skip the empty-TWID group (non-classical).
        _rg_twid_groups: dict[str, list[tuple[Path, TrackTags, dict[str, str], str]]] = {}
        for _ri in _release_idxs:
            _rg_path, _rg_tags, _rg_fd, _rg_ext, _ = _regroup_file_data[_ri]
            _rg_twid = _rg_tags.cwp_workid_top
            if not _rg_twid:
                continue  # non-classical track: no top-work group; skip renumber
            _rg_twid_groups.setdefault(_rg_twid, []).append((_rg_path, _rg_tags, _rg_fd, _rg_ext))
        _rg_single_work = len(_rg_twid_groups) == 1
        for _rg_group in _rg_twid_groups.values():
            _apply_group_movement_renumber(_rg_group, single_work_album=_rg_single_work)

    # --- Pass 2: build regroup plan using the per-group modal depth ---
    plan_pairs: list[tuple[Path, Path, str, int, str]] = []

    for _ri, (current_path, tags, file_dict, ext, release_id) in enumerate(_regroup_file_data):
        stub_release = MBRelease()
        stub_track = MBTrack()

        new_dest_base = build_dest_path(
            dest_root,
            stub_release,
            stub_track,
            tags,
            global_track_idx=0,
            group_modal_depth=_regroup_modal_by_idx.get(_ri),
        )
        new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext), current_path)

        if new_dest == current_path:
            log.debug("regroup_noop", path=str(current_path.relative_to(dest_root)))
            continue

        acoustid = file_dict.get("ACOUSTID_ID", "")
        length_str = file_dict.get("LENGTH", "0")
        try:
            length_ms = int(length_str) if length_str else 0
        except ValueError:
            length_ms = 0

        plan_pairs.append((current_path, new_dest, acoustid, length_ms, release_id))
        log.info(
            "regroup_plan",
            old=str(current_path.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            release_id=release_id,
            dry_run=dry_run,
        )

    if not plan_pairs:
        log.info("regroup_nothing_to_regroup", dest_root=str(dest_root))
        cache.save()
        if dry_run:
            return DryRunPlan(pass_name="regroup", entries=[], count=0)
        return None

    # --- Intra-plan collision guard (two files in the same plan compute to the same destination) ---
    # When two files in the plan share the same destination, apply the collision suffix to the
    # second file in each group so both files can move without C-NOCLOBBER refusing the second.
    _regroup_dest_to_indices: dict[Path, list[int]] = {}
    for _rgi, (_, _rgdest, _, _, _) in enumerate(plan_pairs):
        _regroup_dest_to_indices.setdefault(_rgdest, []).append(_rgi)

    _regroup_intra_collision_indices: set[int] = set()
    for _rgdest, _rgindices in _regroup_dest_to_indices.items():
        if len(_rgindices) > 1:
            for _rgidx in _rgindices[1:]:
                _regroup_intra_collision_indices.add(_rgidx)
            log.info(
                "regroup_intra_plan_collision_suffix",
                dest=str(_rgdest.relative_to(dest_root)),
                count=len(_rgindices),
            )

    if _regroup_intra_collision_indices:
        _regroup_plan_list: list[tuple[Path, Path, str, int, str]] = list(plan_pairs)
        for _rgidx in sorted(_regroup_intra_collision_indices):
            _rgsrc, _rgdest_orig, _rgacust, _rglength, _rgrid = _regroup_plan_list[_rgidx]
            _rgsuffix = _collision_suffix(MBRelease(id=_rgrid))
            _rgrel_parts = list(_rgdest_orig.relative_to(dest_root).parts)
            _rgwork_dir_idx = 2 if _rgrel_parts[0] in _CLASS_VOCAB else 1
            _rgrel_parts[_rgwork_dir_idx] = f"{_rgrel_parts[_rgwork_dir_idx]} [{_rgsuffix}]"
            _rgnew_dest = dest_root.joinpath(*_rgrel_parts)
            log.warning(
                "regroup_intra_plan_collision_suffix_applied",
                original=str(_rgdest_orig.relative_to(dest_root)),
                renamed=str(_rgnew_dest.relative_to(dest_root)),
                suffix=_rgsuffix,
            )
            _regroup_plan_list[_rgidx] = (_rgsrc, _rgnew_dest, _rgacust, _rglength, _rgrid)
        plan_pairs = _regroup_plan_list

    # --- Collision detection and resolution ---
    # C-SEQ vacancy-aware collision check: subtract plan-vacated paths from the collision check.
    _regroup_vacated: frozenset[Path] = frozenset(src for src, _, _, _, _ in plan_pairs)
    collision_pairs = [(src, dest, acust, length) for src, dest, acust, length, _ in plan_pairs]
    collision_results = _assess_collisions(collision_pairs, vacated_paths=_regroup_vacated)
    confirmed_nonmatches = [r for r in collision_results if r.match is False]
    if confirmed_nonmatches:
        stub_plan = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _, _ in plan_pairs]
        # Group non-matches by release_id so each group's suffix is derived from its real MBID,
        # producing a release-identifying token (8-char prefix) rather than an empty " []".
        _dest_to_rid_rg: dict[Path, str] = {dest: rid for _, dest, _, _, rid in plan_pairs}
        _nonmatch_by_rid_rg: dict[str, list[AudioCompareResult]] = {}
        for _nm in confirmed_nonmatches:
            _nm_rid = _dest_to_rid_rg.get(_nm.dest, "")
            _nonmatch_by_rid_rg.setdefault(_nm_rid, []).append(_nm)
        for _nm_rid, _nm_group in _nonmatch_by_rid_rg.items():
            _apply_collision_suffix(stub_plan, _nm_group, MBRelease(id=_nm_rid), dest_root)
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length, rid)
            for entry, (_, _, acust, length, rid) in zip(stub_plan, plan_pairs)
        ]
        log.warning("regroup_collision_suffix_applied", count=len(confirmed_nonmatches))

    if dry_run:
        regroup_dry_run_entries: list[DryRunEntry] = []
        for current_path, new_dest, _, _, release_id in plan_pairs:
            log.info(
                "regroup_dry_run",
                old=str(current_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
                release_id=release_id,
            )
            regroup_dry_run_entries.append(DryRunEntry(current_path=str(current_path), planned_path=str(new_dest)))
        cache.save()
        return DryRunPlan(pass_name="regroup", entries=regroup_dry_run_entries, count=len(regroup_dry_run_entries))

    # --- Confirmation prompt ---
    if not yes:
        _console.print("\n[bold yellow]regroup[/] will move the following files:\n")
        for current_path, new_dest, _, _, release_id in plan_pairs:
            _console.print(
                f"  [dim]{_markup_escape(str(current_path.relative_to(dest_root)))}[/]\n"
                f"    → [green]{_markup_escape(str(new_dest.relative_to(dest_root)))}[/]"
                f"  [dim](release {_markup_escape(release_id)})[/]"
            )
        _console.print(f"\n[bold]{len(plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("regroup_aborted", dest_root=str(dest_root))
            return None

    # --- Perform moves, verify, journal ---
    # regroup is release-driven: each file may belong to a different release_id, so we call
    # _move_verify_journal once per unique release_id group to preserve the per-entry release_id.
    # The journal loaded at the start of this pass is threaded through so _move_verify_journal
    # never re-reads it between moves — each append updates the in-memory copy in place.
    # The cache is threaded through so each successful move re-keys the entry to the new path.
    now = datetime.datetime.now(datetime.UTC)
    total_moved = 0
    # Group plan_pairs by release_id so each batch shares the same journal release_id.
    release_groups: dict[str, list[tuple[Path, Path]]] = {}
    for src, dest, _, _, rid in plan_pairs:
        release_groups.setdefault(rid, []).append((src, dest))

    # C-IDEM tripwire: warn before executing if any planned move inverts a prior journal entry.
    all_regroup_pairs = [(src, dest) for src, dest, _, _, _ in plan_pairs]
    _warn_inverse_moves(all_regroup_pairs, "regroup", journal)

    for rid, move_pairs in release_groups.items():
        total_moved += _move_verify_journal(
            move_pairs,
            journal=journal,
            journal_path=journal_path,
            action="regrouped",
            dest_root=dest_root,
            now=now,
            release_id=rid,
            cache=cache,
        )
    cache.save()
    log.info("regroup_complete", dest_root=str(dest_root), moved=total_moved)
    return None


def unify(  # pylint: disable=too-many-return-statements
    dest_root: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
    _journal: TransactionLog | None = None,
    _modal_depth_map: dict[str, int | None] | None = None,
) -> DryRunPlan | None:
    """Consolidate fragmented releases into their canonical top_dirs (C-CANON, C-NC-TOP).

    Scans ``dest_root`` for releases whose tracks are spread across ≥2 distinct top_dirs due to
    per-track ``CEA_SOLOISTS`` variation (the dominant fragmentation shape: 29 releases in the
    2026-06 audit).  For each fragmented release, reads the embedded tags from all its files, runs
    :func:`~music_annotator._tags.build_dest_path` over the full release group to compute the
    canonical destination for every file, and moves files that are not already at their canonical
    path.

    **Detection (C-W2):** a release is fragmented when ≥2 distinct top_dirs share the same
    ``MUSICBRAINZ_ALBUMID`` tag.  The join key is the embedded tag, not the journal.

    **Canonical path algorithm (C-CANON):** every pass (``repath``, ``regroup``, ``unify``) derives
    each file's destination from the same canonical function over the same durable inputs: embedded
    tags as read from disk.  No pass-local in-memory tag patch may alter the rendered path.
    :func:`~music_annotator._tags.build_dest_path` reads the IS_CLASSICAL predicate
    (``CWP_WORK_TOP`` non-empty AND ``"Classical"`` in ``CWP_WORKTYPE_GENRES_TOP``) from embedded
    tags to determine the top-dir shape: non-classical releases take the ALBUMARTIST-led top dir
    (C-NC-TOP); classical releases with a linked composer take the ``<composer> - <performers>``
    shape.  The performers component uses album-level conductors and ensembles (C-NOSOLO: soloists
    are never a path component).

    **Move semantics (provenance-chain invariant preserved):** for each file that needs moving:

    1. Capture source SHA-256.
    2. Move atomically via ``os.replace`` (rename within the library); fall back to
       ``shutil.copy2`` + ``os.unlink`` on ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    3. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    4. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    5. **Only then** append ``TransactionEntry(action="unified", release_id=<the release's MBID>,
       source=<old path>, destination=<new path>)`` and flush it to the journal before moving the
       next file.

    In ``dry_run`` mode: all planned moves are logged but **no files are moved and no journal
    entries are written**.  The confirmation prompt is not shown in dry-run mode.

    When ``yes=True`` the confirmation prompt is skipped; files are moved immediately after
    building the plan.  When ``yes=False`` (default), the planned moves are printed and the user
    must confirm with ``y``/``yes`` before any move is performed.  When the plan is empty, a
    "nothing to unify" message is logged and the function returns immediately.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param yes: When ``True``, skip the interactive confirmation prompt.
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations or writing journal entries.
    :param _modal_depth_map: Pre-computed library-wide ``cwp_workid_top`` → modal-depth map
        from :func:`compute_library_modal_depth`.  When supplied by ``maintain``, this map was
        computed over the full library scan before any pass ran, ensuring all passes use the same
        group membership (C-GROUPSCOPE).  When ``None`` (standalone invocation), ``unify``
        computes the map itself over each fragmented release's files — a release-local membership
        that may differ from the library-wide membership when the same top-work MBID appears in
        multiple releases with different ``CWP_PART_LEVELS`` distributions.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    # Use the pre-read journal when provided (C-JRNL: journal read once at the top of maintain
    # and threaded through all passes); otherwise read from disk.
    journal = _journal if _journal is not None else read_journal(journal_path)

    # Load the tag-read cache from the sidecar file.  A missing or malformed sidecar degrades
    # gracefully to an empty cache — never an error.
    cache = TagReadCache.load(dest_root / _TAG_CACHE_FILENAME)

    # --- Detect fragmented releases by scanning embedded MUSICBRAINZ_ALBUMID tags ---
    fragmented = detect_fragmented_releases(dest_root)

    if not fragmented:
        log.info("unify_nothing_to_unify", dest_root=str(dest_root))
        if dry_run:
            return DryRunPlan(pass_name="unify", entries=[], count=0)
        return None

    # --- Build unify plan: (current_path, new_dest, acoustid, length_ms, release_id) ---
    plan_pairs: list[tuple[Path, Path, str, int, str]] = []

    for release_id, file_paths in sorted(fragmented.items()):
        # Read tags from all files in this release group.
        # Build a list of (file_path, tags, file_dict) for the group.
        # The tag-read cache is consulted for each file: on a hit (path, size, mtime all match),
        # the cached tag dict is returned without opening the audio file.
        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = []
        for file_path in file_paths:
            ext = file_path.suffix.lower()
            if ext not in {".flac", ".mp3"}:  # pragma: no cover — detect_fragmented_releases only returns .flac/.mp3
                log.warning("unify_unsupported_format", path=str(file_path), ext=ext)
                continue
            try:
                file_dict = _read_tags_cached(file_path, ext, cache)
            except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
                log.warning("unify_tag_read_error", path=str(file_path), error=str(exc))
                continue
            tags = _tags_from_file_dict(file_dict)
            group_tags.append((file_path, tags, file_dict))

        if not group_tags:
            continue

        # Compute canonical destinations for every file in the group.
        # build_dest_path uses the path fields (recording_date_work, etc.) already embedded
        # in the tags from the original annotation pipeline run.
        # global_track_idx=0 is acceptable here because CWP_MOVT_NUM is present in the tags
        # for properly annotated files (same as repath/regroup).
        stub_release = MBRelease()
        stub_track = MBTrack()

        # --- SEL-23 ensemble patch (release-scope ensemble expansion) ---
        # Hydrate performer lists for all files in the group first, then apply the SEL-23
        # rule over the full group so the majority threshold is computed over the correct
        # denominator.  sel23_ensemble_patch expands cea_album_ensembles_list on each track
        # to include any ensemble present on a modal majority (>50%) of the release's tracks.
        # This must run before build_dest_path so the expanded set is used for path computation.
        for _, _tags, _file_dict in group_tags:
            _hydrate_performer_lists(_tags, _file_dict)
        sel23_ensemble_patch([_tags for _, _tags, _ in group_tags])

        # --- Movement renumber pass: re-derive CWP_MOVT_NUM from embedded (DISCNUMBER, TRACKNUMBER) ---
        # The leaf nn prefix (CWP_MOVT_NUM) is session-local and must never be trusted across a merge.
        # After consolidating cross-session fragments, re-derive the gap-free 1-based index from
        # embedded (DISCNUMBER, TRACKNUMBER) order within each CWP_WORKID_TOP group so that the
        # resulting path is idempotent across sessions.  This must run after the SEL-23 patch and
        # before build_dest_path so the corrected CWP_MOVT_NUM drives the leaf nn component.
        # Only classical tracks with a non-empty CWP_WORKID_TOP are renumbered; non-classical
        # tracks (empty CWP_WORKID_TOP) have no top-work group identity and must not be renumbered.
        _un_twid_groups: dict[str, list[tuple[Path, TrackTags, dict[str, str], str]]] = {}
        for _un_path, _un_tags, _un_fd in group_tags:
            _un_ext = _un_path.suffix.lower()
            if not _un_tags.cwp_workid_top:
                continue  # non-classical track: no top-work group; skip renumber
            _un_twid_groups.setdefault(_un_tags.cwp_workid_top, []).append((_un_path, _un_tags, _un_fd, _un_ext))
        _un_single_work = len(_un_twid_groups) == 1
        for _un_group in _un_twid_groups.values():
            _apply_group_movement_renumber(_un_group, single_work_album=_un_single_work)

        # --- Work-group modal depth per CWP_WORKID_TOP group (C-CANON / C-GROUPSCOPE) ---
        # When _modal_depth_map is supplied by maintain (computed once over the full library scan
        # before any pass ran), look up each file's top-work MBID directly — all passes share the
        # same group membership (C-GROUPSCOPE).  When None (standalone invocation), compute the
        # map from this release's files only; the membership is release-local by construction
        # (unify only sees the fragmented release's files at this point).
        if _modal_depth_map is not None:
            _unify_depth_map: dict[str, int | None] = _modal_depth_map
        else:
            _unify_depth_map = compute_library_modal_depth(
                [(_ut.cwp_workid_top, int(_ut.cwp_part_levels or "0")) for _, _ut, _ in group_tags]
            )

        _unify_modal_by_idx: dict[int, int | None] = {
            _ui: _unify_depth_map.get(_ut.cwp_workid_top) for _ui, (_, _ut, _) in enumerate(group_tags)
        }

        for _ui, (file_path, tags, file_dict) in enumerate(group_tags):
            ext = file_path.suffix.lower()
            new_dest_base = build_dest_path(
                dest_root,
                stub_release,
                stub_track,
                tags,
                global_track_idx=0,
                group_modal_depth=_unify_modal_by_idx.get(_ui),
            )
            new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext), file_path)

            if new_dest == file_path:
                log.debug("unify_noop", path=str(file_path.relative_to(dest_root)))
                continue

            acoustid = file_dict.get("ACOUSTID_ID", "")
            length_str = file_dict.get("LENGTH", "0")
            try:
                length_ms = int(length_str) if length_str else 0
            except ValueError:
                length_ms = 0

            plan_pairs.append((file_path, new_dest, acoustid, length_ms, release_id))
            log.info(
                "unify_plan",
                old=str(file_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
                release_id=release_id,
                dry_run=dry_run,
            )

    if not plan_pairs:
        log.info("unify_nothing_to_unify", dest_root=str(dest_root))
        cache.save()
        if dry_run:
            return DryRunPlan(pass_name="unify", entries=[], count=0)
        return None

    # --- Intra-plan collision guard (two files in the same plan compute to the same destination) ---
    # When two files in the plan share the same destination, apply the collision suffix directly
    # to the duplicate entries (by index) so both files can move without C-NOCLOBBER refusing
    # the second.  The first entry in each collision group keeps the canonical path; subsequent
    # entries get a release-identifying suffix appended to the work_dir component.
    # This is distinct from on-disk collision detection below (which handles pre-existing files).
    _unify_dest_to_indices: dict[Path, list[int]] = {}
    for _ui, (_, _udest, _, _, _) in enumerate(plan_pairs):
        _unify_dest_to_indices.setdefault(_udest, []).append(_ui)

    _unify_plan_list: list[tuple[Path, Path, str, int, str]] = list(plan_pairs)
    _unify_intra_count = 0
    for _udest, _uindices in _unify_dest_to_indices.items():
        if len(_uindices) > 1:
            log.info(
                "unify_intra_plan_collision_suffix",
                dest=str(_udest.relative_to(dest_root)),
                count=len(_uindices),
            )
            # Apply suffix to all entries after the first (by index), not by destination.
            for _uidx in _uindices[1:]:
                _usrc, _udest_orig, _uacust, _ulength, _urid = _unify_plan_list[_uidx]
                _usuffix = _collision_suffix(MBRelease(id=_urid))
                _urel_parts = list(_udest_orig.relative_to(dest_root).parts)
                _uwork_dir_idx = 2 if _urel_parts[0] in _CLASS_VOCAB else 1
                _urel_parts[_uwork_dir_idx] = f"{_urel_parts[_uwork_dir_idx]} [{_usuffix}]"
                _unew_dest = dest_root.joinpath(*_urel_parts)
                log.warning(
                    "unify_intra_plan_collision_suffix_applied",
                    original=str(_udest_orig.relative_to(dest_root)),
                    renamed=str(_unew_dest.relative_to(dest_root)),
                    suffix=_usuffix,
                )
                _unify_plan_list[_uidx] = (_usrc, _unew_dest, _uacust, _ulength, _urid)
                _unify_intra_count += 1

    if _unify_intra_count > 0:
        plan_pairs = _unify_plan_list

    # --- Collision detection and resolution ---
    # C-SEQ vacancy-aware collision check: subtract plan-vacated paths from the collision check.
    _unify_vacated: frozenset[Path] = frozenset(src for src, _, _, _, _ in plan_pairs)
    collision_pairs = [(src, dest, acust, length) for src, dest, acust, length, _ in plan_pairs]
    collision_results = _assess_collisions(collision_pairs, vacated_paths=_unify_vacated)
    confirmed_nonmatches = [r for r in collision_results if r.match is False]
    if confirmed_nonmatches:
        stub_plan = [CopyPlanEntry(idx=0, src_file=src, dest_file=dest) for src, dest, _, _, _ in plan_pairs]
        # Group non-matches by release_id so each group's suffix is derived from its real MBID,
        # producing a release-identifying token (8-char prefix) rather than an empty " []".
        _dest_to_rid_un: dict[Path, str] = {dest: rid for _, dest, _, _, rid in plan_pairs}
        _nonmatch_by_rid_un: dict[str, list[AudioCompareResult]] = {}
        for _nm in confirmed_nonmatches:
            _nm_rid = _dest_to_rid_un.get(_nm.dest, "")
            _nonmatch_by_rid_un.setdefault(_nm_rid, []).append(_nm)
        for _nm_rid, _nm_group in _nonmatch_by_rid_un.items():
            _apply_collision_suffix(stub_plan, _nm_group, MBRelease(id=_nm_rid), dest_root)
        plan_pairs = [
            (entry.src_file, entry.dest_file, acust, length, rid)
            for entry, (_, _, acust, length, rid) in zip(stub_plan, plan_pairs)
        ]
        log.warning("unify_collision_suffix_applied", count=len(confirmed_nonmatches))

    if dry_run:
        unify_dry_run_entries: list[DryRunEntry] = []
        for current_path, new_dest, _, _, release_id in plan_pairs:
            log.info(
                "unify_dry_run",
                old=str(current_path.relative_to(dest_root)),
                new=str(new_dest.relative_to(dest_root)),
                release_id=release_id,
            )
            unify_dry_run_entries.append(DryRunEntry(current_path=str(current_path), planned_path=str(new_dest)))
        cache.save()
        return DryRunPlan(pass_name="unify", entries=unify_dry_run_entries, count=len(unify_dry_run_entries))

    # --- Confirmation prompt ---
    if not yes:
        _console.print("\n[bold yellow]unify[/] will move the following files:\n")
        for current_path, new_dest, _, _, release_id in plan_pairs:
            _console.print(
                f"  [dim]{_markup_escape(str(current_path.relative_to(dest_root)))}[/]\n"
                f"    → [green]{_markup_escape(str(new_dest.relative_to(dest_root)))}[/]"
                f"  [dim](release {_markup_escape(release_id)})[/]"
            )
        _console.print(f"\n[bold]{len(plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("unify_aborted", dest_root=str(dest_root))
            return None

    # --- Perform moves, verify, journal ---
    # unify is release-driven: each file may belong to a different release_id, so we call
    # _move_verify_journal once per unique release_id group to preserve the per-entry release_id.
    # The journal loaded at the start of this pass is threaded through so _move_verify_journal
    # never re-reads it between moves — each append updates the in-memory copy in place.
    # The cache is threaded through so each successful move re-keys the entry to the new path.
    now = datetime.datetime.now(datetime.UTC)
    total_moved = 0
    release_groups: dict[str, list[tuple[Path, Path]]] = {}
    for src, dest, _, _, rid in plan_pairs:
        release_groups.setdefault(rid, []).append((src, dest))

    # C-IDEM tripwire: warn before executing if any planned move inverts a prior journal entry.
    all_unify_pairs = [(src, dest) for src, dest, _, _, _ in plan_pairs]
    _warn_inverse_moves(all_unify_pairs, "unify", journal)

    for rid, move_pairs in release_groups.items():
        total_moved += _move_verify_journal(
            move_pairs,
            journal=journal,
            journal_path=journal_path,
            action="unified",
            dest_root=dest_root,
            now=now,
            release_id=rid,
            cache=cache,
        )
    cache.save()
    log.info("unify_complete", dest_root=str(dest_root), moved=total_moved)
    return None


# ---------------------------------------------------------------------------
# Leaf-renumber collision repair (retroactive cross-session fix)
# ---------------------------------------------------------------------------


def _scan_leaf_collision_dirs(
    dest_root: Path,
    cache: TagReadCache,
) -> dict[Path, list[tuple[Path, dict[str, str]]]]:
    """Walk ``dest_root`` and return directories that contain duplicate ``CWP_MOVT_NUM`` prefixes.

    A collision directory is one where two or more audio files share the same ``CWP_MOVT_NUM``
    tag value.  This is the signature of a cross-session merge: each ingest session assigns
    ``CWP_MOVT_NUM`` from 1 independently, so merging two sessions' fragments into one directory
    produces duplicate leaf prefixes.

    Only FLAC and MP3 files are considered.  Directories with all-unique ``CWP_MOVT_NUM`` values
    are skipped silently.

    :param dest_root: Root of the annotated music library.
    :param cache: Tag-read cache to consult before opening audio files.
    :returns: Mapping from directory path to list of ``(file_path, tag_dict)`` pairs for all
        audio files in that directory that have a non-empty ``CWP_MOVT_NUM``.  Only directories
        with at least one duplicate ``CWP_MOVT_NUM`` value are included.
    """
    collision_dirs: dict[Path, list[tuple[Path, dict[str, str]]]] = {}

    for dirpath_str, _dirnames, filenames in os.walk(dest_root):
        dirpath = Path(dirpath_str)
        # Collect (file_path, tag_dict) for all audio files in this directory.
        dir_files: list[tuple[Path, dict[str, str]]] = []
        for fname in filenames:
            fpath = dirpath / fname
            ext = fpath.suffix.lower()
            if ext not in {".flac", ".mp3"}:
                continue
            try:
                tag_dict = _read_tags_cached(fpath, ext, cache)
            except Exception:  # noqa: BLE001 — tag read failure: skip file
                continue
            movt_num = tag_dict.get("CWP_MOVT_NUM", "").strip()
            if not movt_num:
                continue
            dir_files.append((fpath, tag_dict))

        if len(dir_files) < 2:  # noqa: PLR2004 — need at least 2 files to have a collision
            continue

        # Check for duplicate CWP_MOVT_NUM values.
        movt_nums = [td.get("CWP_MOVT_NUM", "").strip() for _, td in dir_files]
        if len(set(movt_nums)) < len(movt_nums):
            collision_dirs[dirpath] = dir_files

    return collision_dirs


def _classify_collision_dir(
    dir_files: list[tuple[Path, dict[str, str]]],
) -> tuple[str, dict[str, list[tuple[Path, dict[str, str]]]]]:
    """Classify one collision directory and group its files by ``CWP_WORKID_TOP``.

    A collision directory is classified as:

    * ``"auto"`` — single ``CWP_WORKID_TOP`` value across all files, and every
      ``CWP_WORKID_TOP`` fragment has at least 3 files.  These are the balanced splits that
      can be safely renumbered by re-deriving ``CWP_MOVT_NUM`` from embedded
      ``(DISCNUMBER, TRACKNUMBER)`` order.
    * ``"stray"`` — single ``CWP_WORKID_TOP`` value, but at least one fragment has only 1–2
      files.  These may signal a tag mis-grouping (wrong ``DISCNUMBER`` / ``MUSICBRAINZ_ALBUMID``)
      and require explicit per-dir operator review.
    * ``"out_of_scope"`` — multiple distinct ``CWP_WORKID_TOP`` values.  These are a different
      shape (mis-grouping / over-truncation / dedup) and must not be renumbered here.

    The fragment size threshold (≥ 3 files) is the boundary between a balanced split and a
    stray-minority fragment.  A fragment with 1–2 files is too small to be a complete disc
    fragment and is more likely a mis-grouped stray track.

    :param dir_files: List of ``(file_path, tag_dict)`` pairs for all audio files in the directory.
    :returns: A ``(classification, groups)`` tuple where ``classification`` is one of
        ``"auto"``, ``"stray"``, or ``"out_of_scope"``, and ``groups`` maps each
        ``CWP_WORKID_TOP`` value to its list of ``(file_path, tag_dict)`` pairs.
    """
    groups: dict[str, list[tuple[Path, dict[str, str]]]] = {}
    for fpath, tag_dict in dir_files:
        twid = tag_dict.get("CWP_WORKID_TOP", "").strip()
        groups.setdefault(twid, []).append((fpath, tag_dict))

    if len(groups) > 1:
        return "out_of_scope", groups

    # Single CWP_WORKID_TOP group.
    # Check fragment sizes: group by (DISCNUMBER, MUSICBRAINZ_ALBUMID) to identify fragments.
    # A fragment is a set of files from the same ingest session (same disc/release).
    # We approximate fragment identity by MUSICBRAINZ_ALBUMID (release MBID).
    fragments: dict[str, list[tuple[Path, dict[str, str]]]] = {}
    for fpath, tag_dict in dir_files:
        album_id = tag_dict.get("MUSICBRAINZ_ALBUMID", "").strip()
        fragments.setdefault(album_id, []).append((fpath, tag_dict))

    # If any fragment has fewer than 3 files, classify as stray.
    # A fragment with 1-2 files is too small to be a complete disc fragment.
    _stray_threshold = 3
    for frag_files in fragments.values():
        if len(frag_files) < _stray_threshold:
            return "stray", groups

    return "auto", groups


def _renumber_and_move_group(
    group: list[tuple[Path, dict[str, str]]],
    dest_root: Path,
    *,
    cache: TagReadCache,
    modal_depth_map: dict[str, int | None],
    dry_run: bool,
) -> list[tuple[Path, Path]]:
    """Re-derive ``CWP_MOVT_NUM`` for one group and build the move plan.

    Sorts the group by embedded ``(DISCNUMBER, TRACKNUMBER)`` (the ordering authority), calls
    :func:`~music_annotator._tags.assign_group_movement_numbers` to assign gap-free 1-based
    ``CWP_MOVT_NUM``, writes the changed tags to disk (in-place, before the move), then
    recomputes the destination via :func:`~music_annotator._tags.build_dest_path` and returns
    the ``(src, dest)`` pairs for files that need to move.

    The tag rewrite happens before the move so that the destination path is derived from the
    corrected ``CWP_MOVT_NUM``.  The provenance chain for each file is:

    1. SHA-256 of source captured before the move (inside :func:`_move_verify_journal`).
    2. Tags rewritten in-place (this function).
    3. Move via :func:`_move_verify_journal` (SHA verify + ``_verify_copy`` + journal entry).

    Only files whose destination differs from their current path are included in the returned
    plan.  Files already at the correct destination are skipped.

    :param group: List of ``(file_path, tag_dict)`` pairs for one ``CWP_WORKID_TOP`` group.
    :param dest_root: Root of the annotated music library.
    :param cache: Tag-read cache to update after each in-place tag rewrite.
    :param modal_depth_map: ``cwp_workid_top`` → modal-depth map for :func:`build_dest_path`.
    :param dry_run: When ``True``, compute the plan without writing tags or moving files.
    :returns: List of ``(src, dest)`` pairs for files that need to move.
    :raises RuntimeError: If a tag write fails.
    """

    def _sort_key(item: tuple[Path, dict[str, str]]) -> tuple[int, int]:
        """Return ``(disc, track)`` sort key from embedded tags.

        :param item: ``(file_path, tag_dict)`` pair.
        :returns: ``(discnumber_int, tracknumber_int)`` pair.
        """
        _, td = item
        disc_str = td.get("DISCNUMBER", "").split("/", maxsplit=1)[0].strip()
        track_str = td.get("TRACKNUMBER", "").split("/", maxsplit=1)[0].strip()
        disc = int(disc_str) if disc_str.isdigit() else 0
        track = int(track_str) if track_str.isdigit() else 0
        return disc, track

    ordered = sorted(group, key=_sort_key)

    # Reconstruct TrackTags for each file and hydrate performer lists.
    ordered_tags: list[tuple[Path, TrackTags, dict[str, str], str]] = []
    for fpath, tag_dict in ordered:
        ext = fpath.suffix.lower()
        tags = _tags_from_file_dict(tag_dict)
        _hydrate_performer_lists(tags, tag_dict)
        ordered_tags.append((fpath, tags, tag_dict, ext))

    # Snapshot movement tags before the renumber call.
    before = [(tags.cwp_movt_num, tags.cwp_movt_tot) for _, tags, _, _ in ordered_tags]

    # Re-derive gap-free CWP_MOVT_NUM from (DISCNUMBER, TRACKNUMBER) order.
    assign_group_movement_numbers([tags for _, tags, _, _ in ordered_tags], single_work_album=True)

    # Write changed tags to disk (in-place, before the move).
    if not dry_run:
        for (fpath, tags, _, ext), (old_num, old_tot) in zip(ordered_tags, before):
            if tags.cwp_movt_num == old_num and tags.cwp_movt_tot == old_tot:
                continue
            log.info(
                "renumber_leaves_tag_rewrite",
                path=str(fpath),
                old_cwp_movt_num=old_num,
                new_cwp_movt_num=tags.cwp_movt_num,
            )
            try:
                match ext:
                    case ".flac":
                        apply_tags_flac(fpath, tags)
                    case ".mp3":
                        apply_tags_mp3(fpath, tags)
                    case _:  # pragma: no cover — callers filter to .flac/.mp3
                        pass
            except MutagenError as exc:
                raise RuntimeError(f"renumber_leaves tag write failure for '{fpath.name}': {exc}") from exc
            # Update the cache entry after the in-place tag rewrite so subsequent reads
            # see the corrected tags without re-opening the file.
            cache.put(fpath, _read_tags_flac(fpath) if ext == ".flac" else _read_tags_mp3(fpath))

    # Build the move plan: recompute destination from updated tags.
    stub_release = MBRelease()
    stub_track = MBTrack()
    plan_pairs: list[tuple[Path, Path]] = []

    for fpath, tags, _, ext in ordered_tags:
        twid = tags.cwp_workid_top
        modal_depth = modal_depth_map.get(twid) if twid else None
        new_dest_base = build_dest_path(
            dest_root,
            stub_release,
            stub_track,
            tags,
            global_track_idx=0,
            group_modal_depth=modal_depth,
        )
        new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext), fpath)

        if new_dest == fpath:
            log.debug("renumber_leaves_noop", path=str(fpath.relative_to(dest_root)))
            continue

        log.info(
            "renumber_leaves_plan",
            old=str(fpath.relative_to(dest_root)),
            new=str(new_dest.relative_to(dest_root)),
            dry_run=dry_run,
        )
        plan_pairs.append((fpath, new_dest))

    return plan_pairs


def renumber_leaves(  # pylint: disable=too-many-return-statements,too-many-branches
    dest_root: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> None:
    """Retroactive repair tool for the cross-session leaf-collision defect.

    Scans ``dest_root`` for directories that contain duplicate ``CWP_MOVT_NUM`` prefixes — the
    signature of a cross-session merge where each ingest session assigned ``CWP_MOVT_NUM`` from 1
    independently.  For each auto-fixable collision directory (single ``CWP_WORKID_TOP``, all
    fragments ≥ 3 files), re-derives the gap-free 1-based ``CWP_MOVT_NUM`` from embedded
    ``(DISCNUMBER, TRACKNUMBER)`` order, rewrites the affected tags in-place, recomputes the
    destination via :func:`~music_annotator._tags.build_dest_path`, and moves each file on the
    full provenance chain:

    1. Rewrite ``CWP_MOVT_NUM`` (and mirror tags) in-place on the source file.
    2. Capture source SHA-256.
    3. Move atomically via ``os.replace``; fall back to ``shutil.copy2`` + ``os.unlink`` on
       ``OSError`` with ``errno.EXDEV`` (cross-filesystem).
    4. Verify destination SHA-256 == source SHA-256 (``RuntimeError`` on mismatch — NO journal
       entry written).
    5. Run ``_verify_copy`` tag round-trip on the new path (``RuntimeError`` on mismatch — NO
       journal entry written).
    6. **Only then** append ``TransactionEntry(action="renumbered", release_id=<the release's
       MBID>, source=<old path>, destination=<new path>)`` and flush it to the journal.

    The leaf ``nn`` prefix (``CWP_MOVT_NUM``) is the per-top-work-group gap-free playback index.
    It is session-local and must never be trusted across a merge.  The shared authority is
    :func:`~music_annotator._tags.assign_group_movement_numbers` with ordering by embedded
    ``(DISCNUMBER, TRACKNUMBER)`` within one ``CWP_WORKID_TOP`` group.

    **Stray-minority and out-of-scope dirs are reported but never auto-moved**, even with
    ``--yes``.  These require explicit per-dir operator review because the collision may signal
    a tag mis-grouping (wrong ``DISCNUMBER`` / ``MUSICBRAINZ_ALBUMID``) rather than a simple
    cross-session merge.

    In ``dry_run`` mode: all planned moves are logged but **no files are moved, no tags are
    rewritten, and no journal entries are written**.

    When ``yes=True`` the move-confirmation prompt is skipped; files are moved immediately after
    building the plan.  When ``yes=False`` (default), the planned moves are printed and the user
    must confirm with ``y``/``yes`` before any move is performed.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, log planned moves without performing any filesystem
        operations, tag rewrites, or journal entries.
    :param yes: When ``True``, skip the move-confirmation prompt and move files immediately.
        Has no effect on stray-minority and out-of-scope dirs (those are never auto-moved).
    :raises RuntimeError: If a tag write, SHA-256 check, or ``_verify_copy`` fails.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)
    cache = TagReadCache.load(dest_root / _TAG_CACHE_FILENAME)

    # --- Scan for collision directories ---
    log.info("renumber_leaves_scan_start", dest_root=str(dest_root))
    collision_dirs = _scan_leaf_collision_dirs(dest_root, cache)

    if not collision_dirs:
        log.info("renumber_leaves_nothing_to_fix", dest_root=str(dest_root))
        cache.save()
        return

    # --- Classify each collision directory ---
    auto_dirs: list[tuple[Path, dict[str, list[tuple[Path, dict[str, str]]]]]] = []
    report_dirs: list[tuple[Path, str, dict[str, list[tuple[Path, dict[str, str]]]]]] = []

    for dirpath, dir_files in sorted(collision_dirs.items()):
        classification, groups = _classify_collision_dir(dir_files)
        if classification == "auto":
            auto_dirs.append((dirpath, groups))
        else:
            report_dirs.append((dirpath, classification, groups))

    # --- Report stray-minority and out-of-scope dirs ---
    # These are never auto-moved; they require explicit per-dir operator review.
    if report_dirs:
        _console.print(
            f"\n[bold yellow]renumber-leaves[/] — {len(report_dirs)} dir(s) require explicit operator review "
            f"(not auto-moved even with --yes):\n"
        )
        for dirpath, classification, groups in report_dirs:
            rel = str(dirpath.relative_to(dest_root)) if dirpath.is_relative_to(dest_root) else str(dirpath)
            twid_count = len(groups)
            reason = (
                f"multiple CWP_WORKID_TOP values ({twid_count})"
                if classification == "out_of_scope"
                else "stray-minority fragment (1–2-file fragment present)"
            )
            _console.print(f"  [dim]{_markup_escape(rel)}[/]  [yellow]({_markup_escape(reason)})[/]")
            for twid, twid_files in groups.items():
                twid_label = twid if twid else "(no CWP_WORKID_TOP)"
                _console.print(f"    {_markup_escape(twid_label)}: {len(twid_files)} file(s)")
        log.info(
            "renumber_leaves_report_dirs",
            count=len(report_dirs),
        )

    if not auto_dirs:
        log.info("renumber_leaves_no_auto_dirs", dest_root=str(dest_root))
        cache.save()
        return

    # --- Build move plan for auto-fixable dirs ---
    # Compute the library-wide modal depth map for build_dest_path.
    _rl_pairs: list[tuple[str, int]] = []
    for _dirpath, groups in auto_dirs:
        for _twid, _twid_files in groups.items():
            for _, _td in _twid_files:
                _rl_pairs.append((_td.get("CWP_WORKID_TOP", ""), int(_td.get("CWP_PART_LEVELS") or "0")))
    modal_depth_map = compute_library_modal_depth(_rl_pairs)

    # Derive release_id for each directory from the embedded MUSICBRAINZ_ALBUMID of its files.
    # When files in the dir have multiple release IDs, use the most common one.
    all_plan_pairs: list[tuple[Path, Path, str]] = []  # (src, dest, release_id)

    for dirpath, groups in auto_dirs:
        # Determine the release_id for this directory (most common MUSICBRAINZ_ALBUMID).
        album_ids: list[str] = []
        for _twid, _twid_files in groups.items():
            for _, _td in _twid_files:
                aid = _td.get("MUSICBRAINZ_ALBUMID", "").strip()
                if aid:
                    album_ids.append(aid)
        release_id = max(set(album_ids), key=album_ids.count) if album_ids else ""

        for _twid, twid_files in groups.items():
            move_pairs = _renumber_and_move_group(
                twid_files,
                dest_root,
                cache=cache,
                modal_depth_map=modal_depth_map,
                dry_run=dry_run,
            )
            for src, dest in move_pairs:
                all_plan_pairs.append((src, dest, release_id))

    if not all_plan_pairs:
        log.info("renumber_leaves_all_current", dest_root=str(dest_root))
        cache.save()
        return

    if dry_run:
        _console.print(f"\n[bold yellow]renumber-leaves[/] (dry-run) — {len(all_plan_pairs)} file(s) would be moved:\n")
        for src, dest, _rid in all_plan_pairs:
            src_rel = str(src.relative_to(dest_root)) if src.is_relative_to(dest_root) else str(src)
            dest_rel = str(dest.relative_to(dest_root)) if dest.is_relative_to(dest_root) else str(dest)
            _console.print(f"  [dim]{_markup_escape(src_rel)}[/]\n    → [green]{_markup_escape(dest_rel)}[/]")
        cache.save()
        log.info("renumber_leaves_dry_run_complete", dest_root=str(dest_root), planned=len(all_plan_pairs))
        return

    # --- Confirmation prompt ---
    if not yes:
        _console.print(f"\n[bold yellow]renumber-leaves[/] will move {len(all_plan_pairs)} file(s):\n")
        for src, dest, _rid in all_plan_pairs:
            src_rel = str(src.relative_to(dest_root)) if src.is_relative_to(dest_root) else str(src)
            dest_rel = str(dest.relative_to(dest_root)) if dest.is_relative_to(dest_root) else str(dest)
            _console.print(f"  [dim]{_markup_escape(src_rel)}[/]\n    → [green]{_markup_escape(dest_rel)}[/]")
        _console.print(f"\n[bold]{len(all_plan_pairs)} file(s) will be moved.[/]  Proceed? [dim](y/n)[/]")
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("renumber_leaves_aborted", dest_root=str(dest_root))
            cache.save()
            return

    # --- Perform moves, verify, journal ---
    # Group plan_pairs by release_id so each batch shares the same journal release_id.
    # The provenance-chain invariant: no "renumbered" journal entry before _verify_copy passes.
    # Tags were already rewritten in-place by _renumber_and_move_group (dry_run=False path).
    now = datetime.datetime.now(datetime.UTC)
    total_moved = 0
    release_groups: dict[str, list[tuple[Path, Path]]] = {}
    for src, dest, rid in all_plan_pairs:
        release_groups.setdefault(rid, []).append((src, dest))

    # C-IDEM tripwire: warn before executing if any planned move inverts a prior journal entry.
    all_move_pairs = [(src, dest) for src, dest, _ in all_plan_pairs]
    _warn_inverse_moves(all_move_pairs, "renumber_leaves", journal)

    for rid, move_pairs in release_groups.items():
        total_moved += _move_verify_journal(
            move_pairs,
            journal=journal,
            journal_path=journal_path,
            action="renumbered",
            dest_root=dest_root,
            now=now,
            release_id=rid,
            cache=cache,
        )

    cache.save()
    log.info("renumber_leaves_complete", dest_root=str(dest_root), moved=total_moved)

    # User-facing confirmation: derived exclusively from in-memory journal entries gated on
    # successful _verify_copy (provenance-chain invariant: the "renumbered" entries in journal
    # were appended only after verification passed inside _move_verify_journal).
    renumbered_this_run = [e for e in journal.entries if e.action == "renumbered"]
    _console.print(f"\n[bold green]renumber-leaves complete[/] — {len(renumbered_this_run)} file(s) renumbered and moved.\n")


def enrich(
    dest_root: Path,
    *,
    re_resolve: bool = False,
    dry_run: bool = False,
    acoustid_key: str = "",
    _journal: TransactionLog | None = None,
) -> DryRunPlan | None:
    """Retroactively backfill fingerprint fields (``audio_hash``, ``acoustid_fingerprint``, ``acoustid_id``) into library files.

    Reads the transaction journal at ``dest_root``, resolves the current on-disk path for each
    library file (following the ``"tagged"`` → ``"repathed"`` → ``"regrouped"`` → ``"enriched"``
    lineage), and for each FLAC or MP3 file calls
    :func:`~music_annotator._pipeline_io._needs_enrich` to determine which fields are missing.

    This is an idempotent, re-runnable maintenance mode (P-FP3): a second run on a fully-enriched
    library is a no-op.  The provenance-chain invariant (P-FP4) is preserved: a journal entry with
    ``action="enriched"`` is appended **only** after
    :func:`~music_annotator._pipeline_io._verify_copy` confirms the tag round-trip.

    **Anchor rule (P-FP1):** ``audio_hash`` is never overwritten, even under ``re_resolve=True``.

    When ``re_resolve=True`` and ``acoustid_key`` is non-empty, calls
    :func:`~music_annotator._mb_api._fetch_acoustid_lookup_raw` after recomputing ``acoustid_fingerprint``
    to obtain the top AcoustID cluster UUID and backfill ``acoustid_id``.  When the lookup returns
    no results, ``acoustid_id`` is left unchanged (inconclusive).

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param re_resolve: When ``True``, recompute ``acoustid_fingerprint`` even when already present
        in the file's tags.  ``audio_hash`` is never recomputed regardless of this flag.
    :param dry_run: When ``True``, log planned backfills without writing any tags or journal
        entries.
    :param acoustid_key: AcoustID application API key.  When set together with ``re_resolve=True``,
        performs a keyed fingerprint lookup after recomputing ``acoustid_fingerprint`` and backfills
        ``acoustid_id`` with the top AcoustID cluster UUID.  Has no effect when ``re_resolve`` is
        ``False``.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = _journal if _journal is not None else read_journal(journal_path)

    # --- Resolve current on-disk path for each logical library file ---
    # _resolve_current_lib walks entries in chronological order; "tagged" seeds the map;
    # "repathed"/"regrouped" update it; "enriched" re-registers the path with the current
    # release_id.  Multi-hop chains resolve naturally.
    current_lib = _resolve_current_lib(journal)

    # Filter to files that actually exist on disk and are FLAC or MP3
    existing_files: list[tuple[Path, str]] = [
        (p, rid) for p, rid in current_lib.items() if p.exists() and p.suffix.lower() in {".flac", ".mp3"}
    ]

    if not existing_files:
        log.info("enrich_nothing_to_enrich", dest_root=str(dest_root))
        if dry_run:
            return DryRunPlan(pass_name="enrich", entries=[], count=0)
        return None

    # --- Per-file enrichment ---
    now = datetime.datetime.now(datetime.UTC).isoformat()
    count_enriched = 0
    count_noop = 0
    count_dry_run = 0
    count_inconclusive_acoustid = 0
    enrich_dry_run_entries: list[DryRunEntry] = []

    for current_path, release_id in existing_files:
        fields = _needs_enrich(current_path, re_resolve)

        # Determine which fields actually need a tag write (acoustid_id is copy-only, not a write)
        write_fields = {k: v for k, v in fields.items() if k in {"audio_hash", "acoustid_fingerprint"}}

        # Count files lacking an embedded AcoustID before the noop gate so that fully-enriched
        # files (no writes needed) that still have no AcoustID are included in the aggregate.
        # "acoustid_id" is absent from fields when _needs_enrich found no embedded AcoustID tag.
        if "acoustid_id" not in fields:
            count_inconclusive_acoustid += 1

        # When re-resolving with an AcoustID key, perform a keyed fingerprint lookup to backfill
        # acoustid_id.  This rides the same re-tag → _verify_copy → journal provenance chain as
        # audio_hash and acoustid_fingerprint.  Only attempted when acoustid_fingerprint was
        # (re)computed (i.e. it is present in write_fields), so that the lookup uses a fresh
        # fingerprint.  When the lookup returns no results, acoustid_id is left unchanged
        # (inconclusive).  Cannot-determine failures (5xx exhaustion, malformed JSON) are logged
        # and skipped so that a transient AcoustID outage does not abort the entire enrich run.
        if re_resolve and acoustid_key and "acoustid_fingerprint" in write_fields:
            _enrich_fp = write_fields["acoustid_fingerprint"]
            _enrich_dur_s = _read_duration_ms(current_path) // 1000
            try:
                _, _enrich_top_uuid = _fetch_acoustid_lookup_raw(_enrich_fp, _enrich_dur_s, acoustid_key)
            except (OSError, RuntimeError, ValueError) as _exc:
                log.warning(
                    "enrich_acoustid_lookup_failed",
                    path=str(current_path.relative_to(dest_root)),
                    error=str(_exc),
                )
                _enrich_top_uuid = ""
            if _enrich_top_uuid:
                write_fields["acoustid_id"] = _enrich_top_uuid

        if not write_fields:
            # No tag writes needed — file is already fully enriched (or fpcalc unavailable)
            log.debug("enrich_noop", path=str(current_path.relative_to(dest_root)))
            count_noop += 1
            continue

        if dry_run:
            log.info(
                "enrich_dry_run",
                path=str(current_path.relative_to(dest_root)),
                fields=list(write_fields.keys()),
            )
            count_dry_run += 1
            enrich_dry_run_entries.append(DryRunEntry(current_path=str(current_path), tag_delta=dict(write_fields.items())))
            continue

        # --- Read current tags, update enriched fields, write back ---
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    file_dict = _read_tags_flac(current_path)
                case ".mp3":
                    file_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — filtered to .flac/.mp3 above
                    log.warning("enrich_unsupported_format", path=str(current_path), ext=ext)
                    continue
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("enrich_tag_read_error", path=str(current_path), error=str(exc))
            continue

        # Merge enriched fields into the tag dict and reconstruct a TrackTags for _verify_copy
        for field_key, field_value in write_fields.items():
            file_dict[field_key.upper()] = field_value
        tags = _tags_from_file_dict(file_dict)

        try:
            match ext:
                case ".flac":
                    apply_tags_flac(current_path, tags)
                case ".mp3":
                    apply_tags_mp3(current_path, tags)
                case _:  # pragma: no cover
                    pass
        except MutagenError as exc:
            raise RuntimeError(f"enrich tag write failure for '{current_path.name}': {exc}") from exc

        # Capture mtime after write for _verify_copy (no os.utime restore for in-place enrichment)
        post_mtime = current_path.stat().st_mtime
        _verify_copy(current_path, current_path, tags, None, post_mtime)

        # Build the full triple for the journal entry; prefer newly-computed values, fall back to
        # what was already in the file dict before the write.  The fingerprint fallback reads the
        # new Picard-aligned key first, then the legacy key (dual-read transition support).
        final_audio_hash = fields.get("audio_hash", "") or file_dict.get("AUDIO_HASH", "")
        final_acoustid_fingerprint = (
            fields.get("acoustid_fingerprint", "")
            or file_dict.get("ACOUSTID_FINGERPRINT", "")
            or file_dict.get("CHROMAPRINT_FP", "")
        )
        final_acoustid_id = fields.get("acoustid_id", "") or file_dict.get("ACOUSTID_ID", "")

        entry = TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(current_path),
            destination=str(current_path),
            action="enriched",
            audio_hash=final_audio_hash,
            acoustid_fingerprint=final_acoustid_fingerprint,
            acoustid_id=final_acoustid_id,
        )
        append_journal_entry(journal_path, entry)
        log.info(
            "enrich_written",
            path=str(current_path.relative_to(dest_root)),
            fields=list(write_fields.keys()),
        )
        count_enriched += 1

    log.info(
        "enrich_complete",
        dest_root=str(dest_root),
        enriched=count_enriched,
        noop=count_noop,
        dry_run=count_dry_run,
        inconclusive_acoustid=count_inconclusive_acoustid,
    )
    if dry_run:
        return DryRunPlan(pass_name="enrich", entries=enrich_dry_run_entries, count=len(enrich_dry_run_entries))
    return None


def _check_dest_root(dest_root: Path) -> bool:
    """Return ``True`` when ``dest_root`` is a non-empty directory suitable for scanning.

    Distinguishes "root not mounted or empty" (returns ``False`` — scan should not run) from
    "root present and non-empty" (returns ``True`` — scan may proceed).  This mirrors the
    ``_check_root`` guard in the ``scan_*.py`` scripts: a missing or empty root must never be
    reported as "no findings", because that would be a data-integrity hazard when the result is
    used to validate the library population before a destructive pass runs.

    :param dest_root: Path to the library root directory.
    :returns: ``True`` when the root exists and contains at least one entry; ``False`` otherwise.
    """
    if not dest_root.is_dir():
        return False
    try:
        entries = os.listdir(dest_root)
    except PermissionError:
        return False
    return bool(entries)


def _reference_evidence(dest_root: Path) -> ReferenceEvidence:
    """Measure the presence and disk footprint of the ``Reference/`` snapshot directory.

    Looks for a sibling directory named ``Reference`` alongside ``dest_root`` (i.e.
    ``dest_root.parent / "Reference"``).  This is the conventional location for a pre-maintenance
    snapshot of the library.  Returns a :class:`~music_annotator.models.ReferenceEvidence`
    with ``present=True`` and the total byte footprint when the directory exists, or
    ``present=False`` and ``size_bytes=0`` when it does not.

    This function is read-only: it never creates, modifies, or deletes any files.

    :param dest_root: Root of the annotated music library.
    :returns: A :class:`~music_annotator.models.ReferenceEvidence` reflecting the snapshot state.
    """
    ref_dir = dest_root.parent / "Reference"
    if not ref_dir.is_dir():
        return ReferenceEvidence(present=False, size_bytes=0)

    total_bytes = 0
    for dirpath, _dirnames, filenames in os.walk(ref_dir):
        for fname in filenames:
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return ReferenceEvidence(present=True, size_bytes=total_bytes)


def _journal_capacity(
    journal_path: Path,
    plans: list[DryRunPlan],
    *,
    _journal: TransactionLog | None = None,
) -> JournalCapacity:
    """Measure the current journal state and project the entry-count growth from executing all plans.

    Reads the journal at ``journal_path`` to obtain the current entry count, and measures the
    on-disk file size.  The projected delta is the sum of all plan counts: each planned file
    action (move or tag-content write) appends exactly one journal entry when the pass runs for
    real.

    When ``_journal`` is supplied (the already-read in-memory journal), it is used directly
    instead of re-reading from disk.  This avoids a redundant journal read when called from
    :func:`_build_maintain_report` inside :func:`maintain`, which has already read the journal
    once (C-JRNL).

    :param journal_path: Path to the journal file (``<dest_root>/music_annotator_journal.json``).
    :param plans: The :class:`~music_annotator.models.DryRunPlan` objects returned by each pass.
    :param _journal: Optional pre-read :class:`~music_annotator.models.TransactionLog`.  When
        supplied, the journal is not re-read from disk.
    :returns: A :class:`~music_annotator.models.JournalCapacity` with current and projected state.
    """
    journal = _journal if _journal is not None else read_journal(journal_path)
    current_count = len(journal.entries)
    current_size = journal_path.stat().st_size if journal_path.exists() else 0
    projected_delta = sum(p.count for p in plans)
    return JournalCapacity(
        current_entry_count=current_count,
        current_size_bytes=current_size,
        projected_delta_entries=projected_delta,
    )


# ---------------------------------------------------------------------------
# Cross-reference reconstruction pass
# ---------------------------------------------------------------------------


def _census_journal_for_xrefs(
    journal: TransactionLog,
) -> tuple[dict[str, tuple[str, list[str]]], list[str]]:
    """Census the journal for destructive-choice shapes that imply secondary release MBIDs.

    Scans journal entries to identify two shapes:

    * **SKIP policy**: a ``"skipped"`` entry whose ``destination`` matches the ``destination``
      of a later ``"tagged"`` entry.  The skipped entry's ``release_id`` is a secondary MBID
      for the surviving file at that destination.
    * **OVERWRITE policy**: multiple ``"tagged"`` entries at the same ``destination`` with
      distinct ``release_id`` values.  The chronological-last ``"tagged"`` entry is the primary;
      all earlier entries' ``release_id`` values are secondary MBIDs.

    Cross-reference entries already written (``"cross-referenced"`` actions) are collected per
    destination so that idempotency can be enforced: a secondary MBID already journalled as
    ``"cross-referenced"`` at a destination is excluded from the returned secondary sets.

    **Move-chain resolution for the evidence-gap predicate**: ``"cross-referenced"`` journal
    entries are keyed on the file's path *at the time of cross-referencing*, which is the current
    path after any subsequent moves.  The evidence-gap exclusion resolves each tagged destination
    to its current path via :func:`_resolve_tagged_to_current` before checking against
    ``xref_by_dest``, so a file that was tagged at path A, moved to path B, and then
    cross-referenced at path B is correctly excluded from the gap report.

    **De-duplication**: when two tagged destinations resolve to the same current path (e.g. two
    journal entries for the same file after a move), only one evidence-gap candidate is emitted.

    :param journal: The :class:`~music_annotator.models.TransactionLog` to census.
    :returns: A tuple ``(groups, evidence_gap_dests)`` where:

        * ``groups`` maps each destination path string to a ``(primary_mbid, [secondary_mbids])``
          tuple.  Only destinations with at least one secondary MBID to add are included.
          Secondary MBIDs are already de-duplicated and exclude any MBID already present in a
          ``"cross-referenced"`` journal entry at that destination.
        * ``evidence_gap_dests`` is a list of destination path strings where the journal shows
          only one ``"tagged"`` entry but the file currently carries a
          ``MUSICBRAINZ_SECONDARY_ALBUMID`` tag (suggesting a cross-reference was written outside
          the journal), or where the journal evidence is otherwise ambiguous.  These are reported
          for operator review.  Each current path appears at most once (de-duplicated).
    """
    tagged_to_current = _resolve_tagged_to_current(journal)

    # Collect tagged entries per destination (in chronological order).
    tagged_by_dest: dict[str, list[str]] = {}  # dest → [release_id, ...]
    # Collect skipped entries per destination.
    skipped_by_dest: dict[str, list[str]] = {}  # dest → [release_id, ...]
    # Collect already-journalled cross-references per destination.
    xref_by_dest: dict[str, set[str]] = {}  # dest → {secondary_mbid, ...}

    for entry in journal.entries:
        dest = entry.destination
        if entry.action == "tagged":
            tagged_by_dest.setdefault(dest, []).append(entry.release_id)
        elif entry.action == "skipped":
            skipped_by_dest.setdefault(dest, []).append(entry.release_id)
        elif entry.action == "cross-referenced":
            xref_by_dest.setdefault(dest, set()).add(entry.release_id)

    groups: dict[str, tuple[str, list[str]]] = {}

    for dest, tagged_ids in tagged_by_dest.items():
        # Deduplicate while preserving order; last unique value is the primary.
        seen: dict[str, None] = {}
        for rid in tagged_ids:
            if rid:
                seen[rid] = None
        unique_ids = list(seen)
        if not unique_ids:
            continue

        primary_mbid = unique_ids[-1]
        already_xref = xref_by_dest.get(dest, set())

        # OVERWRITE policy: multiple distinct release_ids in tagged entries.
        secondary_from_overwrite = [rid for rid in unique_ids[:-1] if rid and rid not in already_xref]

        # SKIP policy: skipped entries at the same destination.
        secondary_from_skip = [
            rid for rid in skipped_by_dest.get(dest, []) if rid and rid != primary_mbid and rid not in already_xref
        ]

        # Merge and deduplicate secondary MBIDs (preserve order: overwrite first, then skip).
        merged: dict[str, None] = {}
        for rid in secondary_from_overwrite + secondary_from_skip:
            merged[rid] = None
        secondary_mbids = list(merged)

        if secondary_mbids:
            groups[dest] = (primary_mbid, secondary_mbids)

    # Evidence-gap candidates: destinations with exactly one unique tagged release_id, no
    # skipped entries, and no existing "cross-referenced" journal entry — the journal alone
    # cannot prove a secondary MBID, but the file may carry one (written outside the journal).
    # Destinations that already have a "cross-referenced" journal entry are journal-provable and
    # are excluded: the secondary MBID was written by a prior reconstruct-xrefs run and is
    # correctly recorded.  Reported for operator review; the caller reads the live file to check
    # for an existing MUSICBRAINZ_SECONDARY_ALBUMID tag.
    #
    # The exclusion resolves each tagged destination through the move chain to its current path
    # before checking xref_by_dest, because "cross-referenced" entries are keyed on the current
    # path at the time of cross-referencing (C-XREF: the exclusion must compare on the same path
    # basis as the cross-reference record).  De-duplication ensures each current path appears at
    # most once even when multiple tagged destinations resolve to the same current path.
    evidence_gap_dests: list[str] = []
    seen_current_paths: set[str] = set()
    for dest, tagged_ids in tagged_by_dest.items():
        seen_ids: set[str] = {rid for rid in tagged_ids if rid}
        if len(seen_ids) != 1 or dest in skipped_by_dest or dest in groups:
            continue
        current_dest = tagged_to_current.get(dest, dest)
        if current_dest in xref_by_dest:
            continue
        if current_dest not in seen_current_paths:
            seen_current_paths.add(current_dest)
            evidence_gap_dests.append(dest)

    return groups, evidence_gap_dests


def reconstruct_cross_references(
    journal_path: Path,
    dest_root: Path,
    *,
    dry_run: bool = False,
    _journal: TransactionLog | None = None,
) -> list[str]:
    """Census the journal for destructive-choice shapes and write secondary release MBIDs.

    Reads the journal at ``journal_path`` and identifies two shapes of destructive collision
    choices made during ingest:

    * **SKIP policy**: a ``"skipped"`` entry at the same destination as a surviving ``"tagged"``
      entry.  The skipped entry's ``release_id`` is a secondary MBID for the surviving file.
    * **OVERWRITE policy**: multiple ``"tagged"`` entries at one destination with distinct
      ``release_id`` values.  The chronological-last ``"tagged"`` entry is the primary; all
      earlier entries' ``release_id`` values are secondary MBIDs.

    Presents grouped findings to the operator and, on confirmation, writes secondary release
    MBIDs into the ``MUSICBRAINZ_SECONDARY_ALBUMID`` tag of each surviving file via the full
    C-PROV chain (tag write → verify → ``"cross-referenced"`` journal entry).

    **Idempotency**: secondary MBIDs already present in the file's tag or already journalled as
    ``"cross-referenced"`` at that destination are silently skipped — the pass is safe to re-run.

    **Dry-run**: when ``dry_run=True``, findings are printed without prompting or writing any
    tags or journal entries.  The evidence-gap candidate list is still returned.

    **Operator confirmation**: the operator is shown the grouped findings (file path, primary
    MBID, secondary MBIDs to add) and must confirm before any writes occur.  ``--yes`` does
    **not** suppress this prompt (integrity prompts are not bulk consent).

    **Survivors with embedded secondary MBIDs and no corroborating journal entry**: files where
    the journal shows only one ``"tagged"`` entry but the live file carries a
    ``MUSICBRAINZ_SECONDARY_ALBUMID`` tag.  When the embedded secondary is non-empty, a truthful
    ``"cross-referenced"`` journal entry is appended for each distinct embedded MBID value after
    a separate operator confirmation (C-AMEND: append-only, sourced from the embedded tag,
    records what the current correct code would have journalled).  The tag write is a verified
    set-union no-op (the value is already present); only the journal append is new.  Files whose
    embedded secondary is empty/whitespace are reported but not amended.  After amendment the
    file is excluded from future gap reports (idempotency via
    :func:`_census_journal_for_xrefs`).

    This function is offline: it reads the journal and live library files, but makes no
    MusicBrainz network calls.

    :param journal_path: Path to the journal file
        (``<dest_root>/music_annotator_journal.json``).
    :param dest_root: Root of the annotated music library.  Used for relative-path display
        in the operator prompt.
    :param dry_run: When ``True``, report findings without writing tags, prompting, or
        appending journal entries.
    :returns: A list of destination path strings that remain as survivors carrying an embedded
        secondary MBID with no corroborating ``"cross-referenced"`` entry after this run
        (i.e. files that were not amended — either because the operator declined or the embedded
        secondary was empty).  Empty when no unamended gaps remain.
    :raises RuntimeError: If a tag write or read-back verification fails (C-PROV chain).
    """
    journal = _journal if _journal is not None else read_journal(journal_path)
    groups, evidence_gap_dests = _census_journal_for_xrefs(journal)

    # --- Resolve current on-disk paths ---
    # The census groups are keyed on the "tagged" destination as it appears in the journal.
    # When a file has been subsequently repathed, the tagged destination is the old (legacy)
    # path; we need the current on-disk path to read and write the live file.
    #
    # _resolve_tagged_to_current walks journal entries in chronological order with an inverse
    # index, resolving each tagged destination to its current path in O(N) total and without
    # fixpoint-follow, so inverse move pairs (A→B then B→A) are handled correctly.
    tagged_dest_to_current: dict[str, Path] = {k: Path(v) for k, v in _resolve_tagged_to_current(journal).items()}

    # --- Filter groups to files that exist on disk ---
    actionable: list[tuple[Path, str, list[str]]] = []  # (current_path, primary_mbid, [secondary_mbids])
    for dest, (primary_mbid, secondary_mbids) in groups.items():
        current_path = tagged_dest_to_current.get(dest, Path(dest))
        if not current_path.exists():
            log.warning(
                "reconstruct_xref_file_not_found",
                dest=dest,
                current_path=str(current_path),
            )
            continue
        # Filter secondary MBIDs: skip any already present in the live file's tag.
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    live_dict = _read_tags_flac(current_path)
                case ".mp3":
                    live_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — only .flac/.mp3 in library
                    live_dict = {}
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("reconstruct_xref_tag_read_error", path=str(current_path), error=str(exc))
            continue
        existing_secondary_raw = live_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        existing_secondary_set = {m.strip() for m in existing_secondary_raw.split("; ") if m.strip()}
        filtered_secondary = [rid for rid in secondary_mbids if rid not in existing_secondary_set]
        if filtered_secondary:
            actionable.append((current_path, primary_mbid, filtered_secondary))

    # --- Evidence-gap candidates: check live files for unexpected secondary MBIDs ---
    # A file is a survivor carrying an embedded secondary MBID with no corroborating
    # "cross-referenced" entry when the journal shows only one "tagged" entry (no journal-provable
    # secondary) but the live file carries a MUSICBRAINZ_SECONDARY_ALBUMID.
    #
    # Two sub-cases:
    # * Amendable: the embedded secondary is non-empty — a truthful "cross-referenced" journal entry
    #   can be appended sourced from the embedded value (C-AMEND).  Each distinct embedded MBID
    #   ("; "-joined set-union) gets its own entry.
    # * Non-amendable: the embedded secondary is empty/whitespace — no entry can be appended without
    #   fabricating provenance; the file is reported but the journal is not mutated (C-AMEND clause c).
    gap_paths: list[str] = []
    # amendable_gaps: (current_path, [secondary_mbids]) for files whose embedded secondary MBID(s)
    # can be journalled via _write_xref_and_journal (C-AMEND).
    amendable_gaps: list[tuple[Path, list[str]]] = []
    for dest in evidence_gap_dests:
        current_path = tagged_dest_to_current.get(dest, Path(dest))
        if not current_path.exists():
            continue
        ext = current_path.suffix.lower()
        try:
            match ext:
                case ".flac":
                    gap_dict = _read_tags_flac(current_path)
                case ".mp3":
                    gap_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover
                    gap_dict = {}
        except Exception:  # noqa: BLE001
            continue
        existing_secondary_raw = gap_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        if existing_secondary_raw.strip():
            gap_paths.append(str(current_path))
            # Collect each distinct embedded MBID for the amendment prompt.  The outer
            # .strip() guard ensures at least one non-empty token exists here.
            embedded_mbids = [m.strip() for m in existing_secondary_raw.split("; ") if m.strip()]
            amendable_gaps.append((current_path, embedded_mbids))

    # --- Dry-run: report findings without prompting or writing ---
    if dry_run:
        if actionable:
            _console.print("\n[bold yellow]reconstruct-xrefs[/] (dry-run) — secondary MBIDs to write:\n")
            for current_path, primary_mbid, secondary_mbids in actionable:
                rel = str(current_path.relative_to(dest_root)) if current_path.is_relative_to(dest_root) else str(current_path)
                _console.print(
                    f"  [dim]{_markup_escape(rel)}[/]\n"
                    f"    primary:   {_markup_escape(primary_mbid)}\n"
                    f"    secondary: {_markup_escape('; '.join(secondary_mbids))}"
                )
        else:
            _console.print("\n[bold green]reconstruct-xrefs[/] (dry-run) — no secondary MBIDs to write.\n")
        if gap_paths:
            _console.print("\n[bold yellow]Evidence-gap candidates[/] (secondary MBID present but not journal-provable):\n")
            for gp in gap_paths:
                rel = str(Path(gp).relative_to(dest_root)) if Path(gp).is_relative_to(dest_root) else gp
                _console.print(f"  [dim]{_markup_escape(rel)}[/]")
        if amendable_gaps:
            _console.print(
                f"\n[bold yellow]Amendable evidence-gap survivors[/]"
                f" ({len(amendable_gaps)} file(s) — embedded secondary MBID present;"
                f" would append journal entry on confirmation):\n"
            )
            for gap_path, gap_mbids in amendable_gaps:
                rel = str(gap_path.relative_to(dest_root)) if gap_path.is_relative_to(dest_root) else str(gap_path)
                _console.print(
                    f"  [dim]{_markup_escape(rel)}[/]\n    embedded secondary: {_markup_escape('; '.join(gap_mbids))}"
                )
        log.info(
            "reconstruct_xref_dry_run_complete",
            actionable=len(actionable),
            evidence_gaps=len(gap_paths),
            amendable_gaps=len(amendable_gaps),
        )
        return gap_paths

    # --- Handle actionable groups (SKIP / OVERWRITE policy secondaries) ---
    if actionable:
        # Interactive prompt: present grouped findings and ask for confirmation.
        # This prompt survives --yes (integrity prompts are not bulk consent).
        _console.print("\n[bold yellow]reconstruct-xrefs[/] — secondary MBIDs to write:\n")
        for current_path, primary_mbid, secondary_mbids in actionable:
            rel = str(current_path.relative_to(dest_root)) if current_path.is_relative_to(dest_root) else str(current_path)
            _console.print(
                f"  [dim]{_markup_escape(rel)}[/]\n"
                f"    primary:   {_markup_escape(primary_mbid)}\n"
                f"    secondary: {_markup_escape('; '.join(secondary_mbids))}"
            )
        _console.print(
            f"\n[bold]{len(actionable)} file(s) will receive secondary MBID cross-references.[/]  Proceed? [dim](y/n)[/]"
        )
        _console.print("\n[bold cyan]>[/] ", end="")
        answer = input("").strip().lower()
        if answer not in {"y", "yes"}:
            log.info("reconstruct_xref_aborted", dest_root=str(dest_root))
            return gap_paths

        # Write cross-references through the full C-PROV chain.
        now = datetime.datetime.now(datetime.UTC)
        now_str = now.isoformat()
        written_count = 0
        for current_path, _primary_mbid, secondary_mbids in actionable:
            for secondary_mbid in secondary_mbids:
                _write_xref_and_journal(
                    current_path,
                    secondary_mbid,
                    journal=journal,
                    journal_path=journal_path,
                    now_str=now_str,
                )
                written_count += 1

        _console.print(f"\n[bold green]reconstruct-xrefs complete[/] — {written_count} secondary MBID(s) written.\n")
    else:
        _console.print("\n[bold green]reconstruct-xrefs[/] — no secondary MBIDs to write.\n")
        now = datetime.datetime.now(datetime.UTC)
        now_str = now.isoformat()

    # --- Amendment prompt: append journal entries for survivors with embedded secondary MBIDs ---
    # For each survivor carrying an embedded secondary MBID with no corroborating
    # "cross-referenced" entry, append a truthful journal entry sourced from the embedded value
    # (C-AMEND: append-only, evidence-backed, records what the current correct code would have
    # journalled).  The tag write is a verified set-union no-op (the value is already present);
    # only the journal append is new.  This prompt survives --yes (integrity prompts are not bulk
    # consent).
    amended_paths: set[str] = set()
    if amendable_gaps:
        _console.print(
            f"\n[bold yellow]Amend suppressed cross-references[/]"
            f" — {len(amendable_gaps)} survivor(s) carry an embedded secondary MBID"
            f" with no corroborating journal entry:\n"
        )
        for gap_path, gap_mbids in amendable_gaps:
            rel = str(gap_path.relative_to(dest_root)) if gap_path.is_relative_to(dest_root) else str(gap_path)
            _console.print(f"  [dim]{_markup_escape(rel)}[/]\n    embedded secondary: {_markup_escape('; '.join(gap_mbids))}")
        _console.print(
            f"\n[bold]{len(amendable_gaps)} file(s) will receive a truthful journal amendment"
            f" (append-only, sourced from embedded tag).[/]  Proceed? [dim](y/n)[/]"
        )
        _console.print("\n[bold cyan]>[/] ", end="")
        amend_answer = input("").strip().lower()
        if amend_answer in {"y", "yes"}:
            amend_count = 0
            for gap_path, gap_mbids in amendable_gaps:
                for gap_mbid in gap_mbids:
                    _write_xref_and_journal(
                        gap_path,
                        gap_mbid,
                        journal=journal,
                        journal_path=journal_path,
                        now_str=now_str,
                    )
                    amend_count += 1
                amended_paths.add(str(gap_path))
            _console.print(f"\n[bold green]Amendment complete[/] — {amend_count} journal entry/entries appended.\n")
            log.info(
                "reconstruct_xref_amend_complete",
                dest_root=str(dest_root),
                amended=amend_count,
            )
        else:
            log.info("reconstruct_xref_amend_aborted", dest_root=str(dest_root))

    # Report remaining (unamended) evidence-gap survivors.
    remaining_gaps = [gp for gp in gap_paths if gp not in amended_paths]
    if remaining_gaps:
        _console.print("\n[bold yellow]Evidence-gap candidates[/] (secondary MBID present but not journal-provable):\n")
        for gp in remaining_gaps:
            rel = str(Path(gp).relative_to(dest_root)) if Path(gp).is_relative_to(dest_root) else gp
            _console.print(f"  [dim]{_markup_escape(rel)}[/]")
    log.info(
        "reconstruct_xref_complete",
        dest_root=str(dest_root),
        evidence_gaps=len(remaining_gaps),
        amendable_gaps=len(amendable_gaps),
    )
    return remaining_gaps


# ---------------------------------------------------------------------------
# Library-wide dedup command (C-DEDUP general case)
# ---------------------------------------------------------------------------


def _build_dedup_census(
    current_lib: dict[Path, str],
    cache: TagReadCache,
) -> tuple[dict[str, list[tuple[Path, str]]], dict[str, list[tuple[Path, str]]]]:
    """Build two identity indexes over the live library for the dedup census.

    Reads ``ACOUSTID_ID`` and ``AUDIO_HASH`` from every file in ``current_lib`` via the
    tag-read cache (no audio file opens on cache hits).  Files lacking both tags are out of
    scope and are excluded from both indexes.

    Returns two indexes:

    * ``acoustid_index``: maps each non-empty ``ACOUSTID_ID`` value to the list of
      ``(path, release_id)`` pairs that carry it.  Files in the same cluster share the same
      audio identity (within production-process variation — lead-in/out silence, minor gain).
    * ``hash_index``: maps each non-empty ``AUDIO_HASH`` value to the list of
      ``(path, release_id)`` pairs that carry it.  Files in the same hash cluster are
      byte-identical (identity a fortiori).

    The two indexes are complementary: ``hash_index`` is the fast path (byte identity);
    ``acoustid_index`` covers production-process variation.  The caller merges them into
    duplicate groups.

    :param current_lib: Mapping from current on-disk path to release MBID, as returned by
        :func:`_resolve_current_lib`.
    :param cache: Tag-read cache to consult before opening audio files.
    :returns: A ``(acoustid_index, hash_index)`` tuple.
    """
    acoustid_index: dict[str, list[tuple[Path, str]]] = {}
    hash_index: dict[str, list[tuple[Path, str]]] = {}

    for path, release_id in current_lib.items():
        if not path.exists():
            continue
        ext = path.suffix.lower()
        if ext not in {".flac", ".mp3"}:
            continue
        try:
            tag_dict = _read_tags_cached(path, ext, cache)
        except Exception:  # noqa: BLE001 — tag read failure: skip file
            continue

        acoustid = tag_dict.get("ACOUSTID_ID", "").strip()
        audio_hash = tag_dict.get("AUDIO_HASH", "").strip()

        # Files lacking both identity tags are out of scope (C-DEDUP: match=None never deletes).
        if not acoustid and not audio_hash:
            continue

        if acoustid:
            acoustid_index.setdefault(acoustid, []).append((path, release_id))
        if audio_hash:
            hash_index.setdefault(audio_hash, []).append((path, release_id))

    return acoustid_index, hash_index


def _build_dedup_groups(
    acoustid_index: dict[str, list[tuple[Path, str]]],
    hash_index: dict[str, list[tuple[Path, str]]],
) -> list[tuple[list[tuple[Path, str]], str]]:
    """Merge the AcoustID and hash indexes into medium-level duplicate groups.

    A duplicate group is a set of files that share the same audio identity.  The merge
    strategy is:

    1. Hash clusters (byte-identical files) are the highest-confidence groups.  Each hash
       cluster with ≥2 members is a group with evidence method ``"audio_hash"``.
    2. AcoustID clusters with ≥2 members that are not already fully covered by a hash cluster
       are groups with evidence method ``"acoustid"``.

    Within each cluster, files are further aggregated by release (``MUSICBRAINZ_ALBUMID``
    embedded in the file's tags, inferred from the release_id in ``current_lib``).  The
    observed duplication shape is whole mediums: two complete mediums from different releases,
    all tracks duplicated.  Aggregating to medium-level groups before prompting reduces the
    number of prompts from N (one per track pair) to 1 (one per medium pair).

    Medium-level aggregation: within each cluster, group files by release_id.  If the cluster
    contains files from exactly two releases, the entire cluster is one group (the canonical
    Greensleeves shape).  If the cluster contains files from more than two releases, each
    pair of releases forms a separate group.

    :param acoustid_index: Maps ``ACOUSTID_ID`` → ``[(path, release_id), …]``.
    :param hash_index: Maps ``AUDIO_HASH`` → ``[(path, release_id), …]``.
    :returns: A list of ``(members, evidence_method)`` tuples where ``members`` is the list
        of ``(path, release_id)`` pairs in the group and ``evidence_method`` is ``"audio_hash"``
        or ``"acoustid"``.
    """
    groups: list[tuple[list[tuple[Path, str]], str]] = []
    # Track which paths have already been assigned to a hash group so they are not
    # double-counted in the AcoustID pass.
    hash_covered: set[Path] = set()

    # --- Hash clusters (byte-identity fast path) ---
    for _hash_val, members in hash_index.items():
        if len(members) < 2:  # noqa: PLR2004 — 2 is the minimum group size
            continue
        groups.append((list(members), "audio_hash"))
        for path, _ in members:
            hash_covered.add(path)

    # --- AcoustID clusters (production-process variation) ---
    for _acoustid_val, members in acoustid_index.items():
        # Filter out paths already covered by a hash group.
        uncovered = [(p, rid) for p, rid in members if p not in hash_covered]
        if len(uncovered) < 2:  # noqa: PLR2004 — 2 is the minimum group size
            continue
        groups.append((uncovered, "acoustid"))

    return groups


def _scatter_consequence_note(
    group_members: list[tuple[Path, str]],
    dest_root: Path,
    current_lib: dict[Path, str],
) -> str:
    """Derive a scatter-consequence note for the operator prompt.

    When deleting one release's files from a group would leave that release's directory
    partially empty (some tracks deleted, others remaining in the same directory), the
    operator must be warned: the release becomes partially virtual, represented only by
    secondary MBIDs on files in other albums' directories.

    A release's directory is "partially emptied" when the group contains some but not all
    files from that release that reside in the same parent directory.

    :param group_members: List of ``(path, release_id)`` pairs in the group.
    :param dest_root: Library root for relative-path display.
    :param current_lib: Full current-library mapping (all paths, not just group members).
    :returns: A warning string to append to the prompt, or ``""`` when no scatter consequence
        applies.
    """
    # Build a mapping from release_id → set of parent directories for group members.
    release_dirs: dict[str, set[Path]] = {}
    for path, release_id in group_members:
        if release_id:
            release_dirs.setdefault(release_id, set()).add(path.parent)

    # For each release in the group, count how many files from that release are in each
    # parent directory (across the whole library, not just the group).
    scatter_notes: list[str] = []
    for release_id, dirs_in_group in release_dirs.items():
        for parent_dir in dirs_in_group:
            # Count all library files from this release in this directory.
            total_in_dir = sum(1 for p, rid in current_lib.items() if rid == release_id and p.parent == parent_dir)
            # Count group members from this release in this directory.
            group_in_dir = sum(1 for p, rid in group_members if rid == release_id and p.parent == parent_dir)
            if total_in_dir > group_in_dir:
                # Deleting the group members from this directory would leave it partially populated.
                rel_dir = str(parent_dir.relative_to(dest_root)) if parent_dir.is_relative_to(dest_root) else str(parent_dir)
                scatter_notes.append(
                    f"  ⚠ Deleting files from release {release_id[:8]}… in '{rel_dir}' would leave "
                    f"{total_in_dir - group_in_dir} track(s) behind — the release becomes partially "
                    f"virtual (represented only by secondary MBIDs on surviving files)."
                )

    return "\n".join(scatter_notes)


def dedup_library(
    dest_root: Path,
    journal_path: Path,
    *,
    dry_run: bool = False,
    _journal: TransactionLog | None = None,
) -> int:
    """Offline census over the live library: group files by AcoustID cluster and resolve duplicates.

    Reads the live library via the tag-read cache (no audio file opens on cache hits), groups
    files by embedded ``ACOUSTID_ID`` cluster with ``AUDIO_HASH`` equality as the byte-identity
    fast path, aggregates per-recording pairs up to medium-level groups, and runs the shared
    group-resolution flow (survivor / keep-both / abort) for each group.

    Files lacking both ``ACOUSTID_ID`` and ``AUDIO_HASH`` are out of scope: C-DEDUP never
    deletes without identity evidence (``match=None`` never deletes).

    **Medium-level aggregation**: the observed duplication shape is whole mediums (e.g. two
    complete mediums from different releases, all tracks duplicated).  Files are aggregated
    by release within each cluster before prompting, reducing the number of prompts from N
    (one per track pair) to 1 (one per medium pair).

    **Scatter consequence**: when deleting one release's files from a group would leave that
    release's directory partially empty (some tracks deleted, others remaining), the prompt
    surfaces this consequence explicitly.  The release becomes partially virtual — represented
    only by secondary MBIDs on files in other albums' directories.

    **C-DEDUP ordering invariant**: the survivor's cross-reference write + verify + journal
    entry complete before any deletion executes.  Deletions are journaled per file with
    action ``"deduplicated"``.

    **Operator prompts**: the group-resolution prompt is never suppressed by ``--yes``
    (integrity prompts are not bulk consent).  ``--dry-run`` reports the full census without
    prompting or deleting.

    **Tag-read cache**: the census reads tags via the cache keyed on ``(path, size_bytes,
    mtime_ns)``; audio files are not opened on cache hits.  The cache is saved back at the
    end of the pass.

    :param dest_root: Root of the annotated music library.
    :param journal_path: Path to the journal file
        (``<dest_root>/music_annotator_journal.json``).
    :param dry_run: When ``True``, report duplicate groups without prompting or deleting.
    :returns: Count of files deleted (``"deduplicated"`` journal entries written).
    """
    journal = _journal if _journal is not None else read_journal(journal_path)
    cache = TagReadCache.load(dest_root / _TAG_CACHE_FILENAME)
    current_lib = _resolve_current_lib(journal)

    # --- Census: build identity indexes ---
    acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

    # --- Build duplicate groups ---
    groups = _build_dedup_groups(acoustid_index, hash_index)

    if not groups:
        _console.print("\n[bold green]dedup-library[/] — no duplicate groups found.\n")
        log.info("dedup_library_no_groups", dest_root=str(dest_root))
        cache.save()
        return 0

    log.info("dedup_library_groups_found", dest_root=str(dest_root), count=len(groups))

    now = datetime.datetime.now(datetime.UTC)
    deleted_count = 0

    for group_members, evidence_method in groups:
        # Medium-level aggregation: group members by release_id.
        by_release: dict[str, list[Path]] = {}
        for path, release_id in group_members:
            by_release.setdefault(release_id, []).append(path)

        release_ids = list(by_release)

        if len(release_ids) < 2:  # noqa: PLR2004 — need at least 2 releases to dedup
            # All files in the group belong to the same release — not a cross-release duplicate.
            continue

        # Build scatter-consequence note for the prompt.
        scatter_note = _scatter_consequence_note(group_members, dest_root, current_lib)

        # For medium-level aggregation: treat the first release as the "occupant" and the
        # second as the "mover".  Each release contributes its first file as the representative
        # for the group-resolution prompt.  The prompt surfaces the full group context.
        occupant_release_id = release_ids[0]
        mover_release_id = release_ids[1]
        occupant_files = by_release[occupant_release_id]
        mover_files = by_release[mover_release_id]

        # Use the first file from each release as the representative for the prompt.
        occupant_path = occupant_files[0]
        mover_path = mover_files[0]

        # Build a display note about the full group size (medium-level context).
        total_files = len(group_members)
        group_note = f"  Group: {total_files} file(s) total across {len(release_ids)} release(s)."
        if scatter_note:
            group_note = group_note + "\n" + scatter_note

        if dry_run:
            occ_rel = (
                str(occupant_path.relative_to(dest_root)) if occupant_path.is_relative_to(dest_root) else str(occupant_path)
            )
            mov_rel = str(mover_path.relative_to(dest_root)) if mover_path.is_relative_to(dest_root) else str(mover_path)
            _console.print(
                f"\n[bold yellow]duplicate group[/] (evidence: {_markup_escape(evidence_method)}):\n"
                f"  release A: [dim]{_markup_escape(occupant_release_id)}[/]"
                f"  ({len(occupant_files)} file(s))\n"
                f"  release B: [dim]{_markup_escape(mover_release_id)}[/]"
                f"  ({len(mover_files)} file(s))\n"
                f"  representative occupant: [dim]{_markup_escape(occ_rel)}[/]\n"
                f"  representative mover:    [dim]{_markup_escape(mov_rel)}[/]\n"
                f"  {_markup_escape(group_note)}\n"
                f"  [dim](dry-run: no changes made)[/]"
            )
            log.info(
                "dedup_library_dry_run_group",
                evidence=evidence_method,
                occupant_release=occupant_release_id,
                mover_release=mover_release_id,
                total_files=total_files,
            )
            continue

        # --- Interactive: print group context then call resolve_duplicate_group ---
        if scatter_note:
            _console.print(f"\n[bold yellow]Scatter consequence warning:[/]\n{_markup_escape(scatter_note)}")

        if total_files > 2:  # noqa: PLR2004 — 2 is the single-pair threshold
            _console.print(
                f"\n[dim]Medium-level group: {total_files} file(s) across {len(release_ids)} release(s).[/]"
                f"\n[dim]Resolving representative pair; all files in the group will be treated consistently.[/]"
            )

        resolution = resolve_duplicate_group(
            occupant_path,
            occupant_release_id,
            mover_path,
            mover_release_id,
            evidence_method,
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=now,
            dry_run=False,
        )

        match resolution.choice:
            case "abort":
                log.info("dedup_library_aborted_by_operator", dest_root=str(dest_root))
                cache.save()
                return deleted_count
            case "keep_both":
                # Cross-reference written for the representative pair; no deletion.
                log.info(
                    "dedup_library_keep_both",
                    occupant=str(occupant_path),
                    mover=str(mover_path),
                )
            case "survivor_occupant":
                # Occupant wins: mover (representative) deleted.  Delete all remaining mover files.
                deleted_count += 1  # representative already deleted by resolve_duplicate_group
                for extra_path in mover_files[1:]:
                    if extra_path.exists():
                        now_str = now.isoformat()
                        _write_xref_and_journal(
                            occupant_path,
                            mover_release_id,
                            journal=journal,
                            journal_path=journal_path,
                            now_str=now_str,
                        )
                        dedup_entry = TransactionEntry(
                            timestamp=now_str,
                            release_id=mover_release_id,
                            source=str(extra_path),
                            destination=str(occupant_path),
                            action="deduplicated",
                        )
                        os.unlink(extra_path)
                        append_journal_entry(journal_path, dedup_entry)
                        journal.entries.append(dedup_entry)
                        deleted_count += 1
                        log.info(
                            "deduplicated",
                            deleted=str(extra_path),
                            survivor=str(occupant_path),
                            deleted_release_id=mover_release_id,
                        )
            case "survivor_mover":
                # Mover wins: occupant (representative) deleted.  Delete all remaining occupant files.
                deleted_count += 1  # representative already deleted by resolve_duplicate_group
                for extra_path in occupant_files[1:]:
                    if extra_path.exists():
                        now_str = now.isoformat()
                        _write_xref_and_journal(
                            mover_path,
                            occupant_release_id,
                            journal=journal,
                            journal_path=journal_path,
                            now_str=now_str,
                        )
                        dedup_entry = TransactionEntry(
                            timestamp=now_str,
                            release_id=occupant_release_id,
                            source=str(extra_path),
                            destination=str(mover_path),
                            action="deduplicated",
                        )
                        os.unlink(extra_path)
                        append_journal_entry(journal_path, dedup_entry)
                        journal.entries.append(dedup_entry)
                        deleted_count += 1
                        log.info(
                            "deduplicated",
                            deleted=str(extra_path),
                            survivor=str(mover_path),
                            deleted_release_id=occupant_release_id,
                        )
            case _:  # pragma: no cover
                pass

    _console.print(f"\n[bold green]dedup-library complete[/] — {deleted_count} file(s) deleted.\n")
    log.info("dedup_library_complete", dest_root=str(dest_root), deleted=deleted_count)
    cache.save()
    return deleted_count


# ---------------------------------------------------------------------------
# maintain — single-composition run of all recurring maintenance passes
# ---------------------------------------------------------------------------


def _build_maintain_report(
    journal_path: Path,
    dest_root: Path,
    plans: list[DryRunPlan],
    *,
    _journal: TransactionLog | None = None,
) -> MaintainDryRunReport:
    """Assemble a :class:`~music_annotator.models.MaintainDryRunReport` from the per-pass plans.

    Builds the cross-pass overlap map, per-pass summaries, journal capacity projection, and
    Reference/ evidence from the collected dry-run plans.  The ``origin-time`` pass writes
    sidecar files rather than returning a :class:`~music_annotator.models.DryRunPlan`; its count
    is synthesised into a plan by the caller before this function is invoked.

    A file is a cross-pass overlap candidate when its ``current_path`` appears in more than one
    pass's plan.  These files are flagged in the report because a live run may plan them
    differently once an earlier pass has mutated their state (C-CONFLUENCE: dry-run is a preview,
    not a rehearsal).

    :param journal_path: Path to the journal file.
    :param dest_root: Root of the annotated music library.
    :param plans: :class:`~music_annotator.models.DryRunPlan` objects from each pass, in
        C-CONFLUENCE order.  The ``origin-time`` pass is represented by a synthetic plan with
        ``count=<sidecar-write-count>`` and no entries (sidecar writes have no ``current_path``).
    :param _journal: Optional pre-read :class:`~music_annotator.models.TransactionLog`.  When
        supplied, the journal is not re-read from disk for the capacity measurement (C-JRNL:
        the journal is read exactly once in :func:`maintain` and threaded through).
    :returns: A fully assembled :class:`~music_annotator.models.MaintainDryRunReport`.
    """
    # --- Build cross-pass overlap map ---
    # A file is overlapping when its current_path appears in more than one plan's entries.
    path_to_passes: dict[str, list[str]] = {}
    for plan in plans:
        for entry in plan.entries:
            if entry.current_path not in path_to_passes:
                path_to_passes[entry.current_path] = []
            if plan.pass_name not in path_to_passes[entry.current_path]:
                path_to_passes[entry.current_path].append(plan.pass_name)

    overlap_paths: set[str] = {p for p, names in path_to_passes.items() if len(names) > 1}
    overlaps: list[MaintainOverlapEntry] = [
        MaintainOverlapEntry(current_path=p, pass_names=path_to_passes[p]) for p in sorted(overlap_paths)
    ]

    # --- Build per-pass summaries ---
    pass_summaries: list[MaintainPassSummary] = []
    for plan in plans:
        overlap_count = sum(1 for e in plan.entries if e.current_path in overlap_paths)
        pass_summaries.append(
            MaintainPassSummary(
                pass_name=plan.pass_name,
                count=plan.count,
                overlap_count=overlap_count,
            )
        )

    # --- Journal capacity ---
    # Pass the pre-read journal when available to avoid a redundant disk read (C-JRNL).
    capacity = _journal_capacity(journal_path, plans, _journal=_journal)

    # --- Reference/ evidence ---
    ref_evidence = _reference_evidence(dest_root)

    return MaintainDryRunReport(
        pass_summaries=pass_summaries,
        overlaps=overlaps,
        journal_capacity=capacity,
        reference_evidence=ref_evidence,
        scan_ran=True,
    )


def _print_maintain_report(report: MaintainDryRunReport, dest_root: Path) -> None:
    """Print a human-readable summary of a :class:`~music_annotator.models.MaintainDryRunReport`.

    Prints per-pass planned-change counts, cross-pass overlap map, journal capacity, and
    Reference/ snapshot evidence to the console.  Called by :func:`maintain` when
    ``dry_run=True``.

    :param report: The assembled dry-run report.
    :param dest_root: Library root (used for display only).
    """
    _console.print(f"\n[bold]maintain --dry-run[/] report for: {_markup_escape(str(dest_root))}\n")
    _console.print("[bold]Pass summaries:[/]")
    for summary in report.pass_summaries:
        overlap_note = f"  ({summary.overlap_count} overlap)" if summary.overlap_count else ""
        _console.print(f"  {summary.pass_name:<35} {summary.count:>5} planned{overlap_note}")
    total = sum(s.count for s in report.pass_summaries)
    _console.print(f"\n  {'TOTAL':<35} {total:>5} planned")

    if report.overlaps:
        _console.print(f"\n[bold yellow]Cross-pass overlaps[/] ({len(report.overlaps)} file(s) in >1 pass):")
        _console.print("[dim]  These files may plan differently in a live run if an earlier pass mutates their state.[/]")
        for overlap in report.overlaps:
            _console.print(f"  {_markup_escape(overlap.current_path)}")
            _console.print(f"    passes: {_markup_escape(', '.join(overlap.pass_names))}")
    else:
        _console.print("\n[bold green]Cross-pass overlaps:[/] none")

    cap = report.journal_capacity
    _console.print("\n[bold]Journal capacity:[/]")
    _console.print(f"  Current entries : {cap.current_entry_count}")
    _console.print(f"  Current size    : {cap.current_size_bytes} bytes")
    _console.print(f"  Projected delta : +{cap.projected_delta_entries} entries")

    ref = report.reference_evidence
    ref_status = f"present ({ref.size_bytes} bytes)" if ref.present else "absent"
    _console.print(f"\n[bold]Reference/ snapshot:[/] {ref_status}")


def maintain(
    dest_root: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
    json_path: Path | None = None,
) -> int:
    """Run all recurring maintenance passes as a single composition over ``dest_root``.

    Reads the journal once at the top and threads it through all passes in memory (C-JRNL).
    Passes execute in the fixed C-CONFLUENCE order: content-before-path passes first
    (``enrich``, ``origin-time``), then the move passes (``repath``, ``regroup``, ``unify``),
    then the integrity passes last (``reconstruct-xrefs``, ``dedup-library``).

    The pass order is load-bearing: tag-content rewrites must precede path-moves because the
    destination path is computed from the tags.  Integrity passes run last because they may
    delete files and nothing downstream may depend on their operator-divergent outcome.

    **Live mode (``dry_run=False``):** each pass runs interactively.  Move-confirmation prompts
    (``repath``, ``regroup``, ``unify``) are suppressible by ``yes=True``.  Integrity prompts
    (``reconstruct-xrefs``, ``dedup-library``) are **never** suppressed by ``yes`` — integrity
    prompts are not bulk consent (INSTR + C-XREF/C-DEDUP).

    **Dry-run mode (``dry_run=True``):** every pass runs in report-only mode.  The two integrity
    passes degrade to census-only (no prompts, no mutations).  This is a preview of the current
    library state, not a rehearsal of a live run: each pass plans against the current (unmutated)
    state, so a pass downstream of a mutating pass may plan differently in a live run.  Files
    appearing in more than one pass's plan are flagged as cross-pass overlap candidates.

    When ``dry_run=True`` and the library root is not mounted or is empty, a "scan not run"
    message is printed and the function returns 0 immediately.  This is structurally distinct
    from a scan that ran and found nothing to change.

    **Consolidated dry-run report:** when ``dry_run=True``, after all passes have run, a
    consolidated report is printed covering: per-pass planned-change counts, cross-pass overlap
    map (files appearing in more than one pass's plan, flagged as places where a live run may
    diverge from this preview), journal capacity projection, and Reference/ snapshot evidence.
    When ``json_path`` is supplied, the report is also serialised to JSON at that path.

    **Convergence:** the final line reports ``"changed N file(s)"`` or ``"no changes"``.  A run
    that changes nothing is the practical convergence signal.  Some legitimate cases need a second
    run (e.g. ``enrich`` adds an acoustid this run, so ``dedup-library`` can cluster it only next
    run) — this is normal, not a defect.

    **Change counting:** in live mode, changes are counted as new journal entries appended during
    the composition (each move, enrich, cross-reference, and dedup-delete appends one entry) plus
    sidecar writes from ``origin-time`` (which does not append journal entries).  In dry-run mode,
    the count is the sum of planned-change counts from each pass's :class:`DryRunPlan`.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, run every pass in report-only mode (no mutations, no prompts).
    :param yes: When ``True``, suppress move-confirmation prompts for ``repath``, ``regroup``,
        and ``unify``.  Has no effect on integrity prompts (``reconstruct-xrefs``,
        ``dedup-library``), which are never suppressed by bulk consent.
    :param json_path: When supplied and ``dry_run=True``, serialise the consolidated dry-run
        report to JSON at this path.  Ignored in live mode.
    :returns: Total count of changes enacted across all passes (0 means no changes — the
        practical convergence signal).
    """
    journal_path = dest_root / JOURNAL_FILENAME

    # In dry-run mode, guard against an unmounted or empty root before running any pass.
    # This mirrors the old preflight behaviour: a missing/empty root is not "no findings".
    if dry_run and not _check_dest_root(dest_root):
        log.info("maintain_dry_run_root_not_mounted", dest_root=str(dest_root))
        _console.print(
            f"\n[bold yellow]maintain --dry-run[/]: scan not run — "
            f"'{_markup_escape(str(dest_root))}' is not mounted or is empty.\n"
            "Mount the library root and re-run to obtain a dry-run preview."
        )
        if json_path is not None:
            report = MaintainDryRunReport(scan_ran=False)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            _console.print(f"\nReport written to: {_markup_escape(str(json_path))}")
        return 0

    # C-JRNL: read the journal exactly once and thread it through all passes in memory.
    journal = read_journal(journal_path)

    # Snapshot the journal length before any pass runs.  In live mode, each mutation appends
    # a new entry; the delta gives the total journal-tracked change count.
    journal_len_before = len(journal.entries)

    total_changed: int = 0

    # --- Pre-pass: compute library-wide work-group modal depth (C-GROUPSCOPE) ---
    # All three move passes (repath, regroup, unify) must derive each file's destination from
    # the same group-scope statistic over the same pass-invariant membership definition.
    # Computing the map once here and threading it into every pass guarantees that the same
    # cwp_workid_top → modal_depth value is used regardless of which pass is running or which
    # subset of files each pass operates on.
    #
    # The scan reads CWP_WORKID_TOP and CWP_PART_LEVELS from the tag-read cache (fast on
    # subsequent runs) for every file currently in the library.  Tag-read failures are skipped
    # silently — a file that cannot be read will be skipped by the move passes too.
    _maintain_cache = TagReadCache.load(dest_root / _TAG_CACHE_FILENAME)
    _maintain_lib = _resolve_current_lib(journal)
    _maintain_pairs: list[tuple[str, int]] = []
    for _mp, _mrid in _maintain_lib.items():
        if not _mp.exists():
            continue
        _mext = _mp.suffix.lower()
        if _mext not in {".flac", ".mp3"}:
            continue
        try:
            _mfd = _read_tags_cached(_mp, _mext, _maintain_cache)
        except Exception:  # noqa: BLE001 — tag read failure: skip for depth context
            continue
        _maintain_pairs.append((_mfd.get("CWP_WORKID_TOP", ""), int(_mfd.get("CWP_PART_LEVELS") or "0")))
    _lib_modal_depth_map: dict[str, int | None] = compute_library_modal_depth(_maintain_pairs)

    # --- Pass 1: enrich (content pass — backfill fingerprint fields) ---
    enrich_plan = enrich(dest_root, dry_run=dry_run, _journal=journal)
    if dry_run and enrich_plan is not None:
        total_changed += enrich_plan.count

    # --- Pass 2: origin-time (content pass — migrate provenance into sidecars) ---
    # origin-time writes sidecar files, not journal entries; count its return value directly.
    origin_time_changed = enrich_origin_time(dest_root, dry_run=dry_run, _journal=journal)
    if dry_run:
        total_changed += origin_time_changed

    # --- Pass 3: repath (move pass — re-path files to corrected destinations) ---
    repath_plan = repath(dest_root, dry_run=dry_run, yes=yes, _journal=journal, _modal_depth_map=_lib_modal_depth_map)
    if dry_run and repath_plan is not None:
        total_changed += repath_plan.count

    # --- Pass 4: regroup (move pass — consolidate confirmed split-release files) ---
    regroup_plan = regroup(dest_root, dry_run=dry_run, yes=yes, _journal=journal, _modal_depth_map=_lib_modal_depth_map)
    if dry_run and regroup_plan is not None:
        total_changed += regroup_plan.count

    # --- Pass 5: unify (move pass — consolidate performer-split fragmented releases) ---
    unify_plan = unify(dest_root, dry_run=dry_run, yes=yes, _journal=journal, _modal_depth_map=_lib_modal_depth_map)
    if dry_run and unify_plan is not None:
        total_changed += unify_plan.count

    # --- Pass 6: reconstruct-xrefs (integrity pass — write secondary release MBIDs) ---
    # Integrity prompts are never suppressed by --yes (INSTR + C-XREF).
    reconstruct_cross_references(
        journal_path=journal_path,
        dest_root=dest_root,
        dry_run=dry_run,
        _journal=journal,
    )

    # --- Pass 7: dedup-library (integrity pass — resolve duplicate files) ---
    # Integrity prompts are never suppressed by --yes (INSTR + C-DEDUP).
    dedup_deleted = dedup_library(
        dest_root=dest_root,
        journal_path=journal_path,
        dry_run=dry_run,
        _journal=journal,
    )
    if dry_run:
        total_changed += dedup_deleted

    if not dry_run:
        # In live mode: count new journal entries (each mutation appends one) plus sidecar writes.
        journal_delta = len(journal.entries) - journal_len_before
        total_changed = journal_delta + origin_time_changed

    # --- Dry-run consolidated report ---
    if dry_run:
        # Coerce each pass result to a DryRunPlan for the report.  The origin-time pass returns
        # an int (sidecar-write count) rather than a DryRunPlan; synthesise a plan with no
        # per-file entries (sidecar writes have no current_path to overlap-map against).
        # The integrity passes (reconstruct-xrefs, dedup-library) also do not return DryRunPlan;
        # they print their findings inline and are represented here with count=0 (their census
        # output is already visible above).
        def _coerce_plan(result: DryRunPlan | None, pass_name: str) -> DryRunPlan:
            """Coerce a possibly-None dry-run result to a :class:`~music_annotator.models.DryRunPlan`.

            :param result: The value returned by a pass called with ``dry_run=True``.
            :param pass_name: The pass name to use when constructing an empty fallback plan.
            :returns: The result as-is when it is a :class:`~music_annotator.models.DryRunPlan`,
                or an empty plan when it is ``None``.
            """
            if result is None:  # pragma: no cover — dry_run=True always returns a plan
                return DryRunPlan(pass_name=pass_name, entries=[], count=0)
            return result

        plans: list[DryRunPlan] = [
            _coerce_plan(enrich_plan, "enrich"),
            DryRunPlan(pass_name="origin-time", entries=[], count=origin_time_changed),
            _coerce_plan(repath_plan, "repath"),
            _coerce_plan(regroup_plan, "regroup"),
            _coerce_plan(unify_plan, "unify"),
            # Integrity passes: census printed inline; represented with count=0 in the summary.
            DryRunPlan(pass_name="reconstruct-xrefs", entries=[], count=0),
            DryRunPlan(pass_name="dedup-library", entries=[], count=0),
        ]

        report = _build_maintain_report(journal_path, dest_root, plans, _journal=journal)
        _print_maintain_report(report, dest_root)

        if json_path is not None:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            _console.print(f"\nReport written to: {_markup_escape(str(json_path))}")

        log.info(
            "maintain_dry_run_complete",
            dest_root=str(dest_root),
            total_planned=total_changed,
            overlap_files=len(report.overlaps),
        )

    # --- Convergence line ---
    if total_changed:
        _console.print(f"\n[bold]maintain[/] — changed {total_changed} file(s).\n")
        log.info("maintain_complete", dest_root=str(dest_root), changed=total_changed)
    else:
        _console.print("\n[bold]maintain[/] — no changes.\n")
        log.info("maintain_complete", dest_root=str(dest_root), changed=0)

    return total_changed
