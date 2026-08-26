"""Library maintenance operations for music-annotator.

Provides the six maintenance-mode commands that operate on an already-annotated library
without making MusicBrainz network calls (except :func:`repatch_acoustid_tags` when an
AcoustID API key is supplied):

* :func:`repath`                  — re-path all verified library files to their corrected destinations.
* :func:`regroup`                 — consolidate confirmed split-release files into their canonical destinations.
* :func:`unify`                   — consolidate performer-split and composer-split fragmented releases.
* :func:`enrich`                  — retroactively backfill fingerprint fields into library files.
* :func:`repatch_catalogue_colon` — rewrite ``CWP_PART_*`` / ``CWP_GROUPHEADING`` tags corrupted by the
  pre-fix bare-``":"`` split (catalogue-colon labels such as Hoboken ``"Hob. III:31"``), re-deriving each
  label offline from the embedded ``CWP_WORK`` pair per the shipped ``": "`` rule (NORM-9 / STYLEGUIDE 4.x).
* :func:`repatch_acoustid_tags`   — migrate the legacy ``CHROMAPRINT_FP`` fingerprint key to the
  Picard-aligned ``ACOUSTID_FINGERPRINT`` key, and (when an AcoustID API key is supplied) re-source
  ``ACOUSTID_ID`` from the fingerprint ``/v2/lookup`` endpoint.

Also provides the shared primitives consumed by all commands:

* :func:`_move_verify_journal`    — the single journal-append site for move-type entries (C-PROV).
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

Private helpers used exclusively by :func:`unify`:

* :func:`_is_composer_split_release`
* :func:`_canonical_composer_component`
* :func:`_unify_classical_composer_groups`
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

from music_annotator._artists import last_name
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
    _read_acoustid_fingerprint_tag,
    _read_duration_ms,
    _read_tags_flac,
    _read_tags_mp3,
    _sha256_file,
    _verify_copy,
    append_journal_entry,
    read_journal,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3, write_secondary_albumid_flac, write_secondary_albumid_mp3
from music_annotator._tags import _CLASS_VOCAB, _NAME_MAX, _proposed_short, build_dest_path, sel23_ensemble_patch
from music_annotator._works import (
    _Rederivation,
    is_catalogue_colon_corrupt,
    rederive_part_label,
    work_group_modal_depth,
)
from music_annotator.models import (
    JSON,
    ArtistEntry,
    CopyPlanEntry,
    DryRunEntry,
    DryRunPlan,
    JournalCapacity,
    MBRelease,
    MBTrack,
    PreflightOverlapEntry,
    PreflightPassSummary,
    PreflightReport,
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
        elif entry.action in {"repathed", "regrouped", "unified"}:
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


def resolve_duplicate_group(
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

    # Interactive prompt — survives --yes (integrity prompts are not bulk consent).
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
    _console.print("[bold cyan]>[/] ", end="")
    answer = input("").strip().lower()

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
        case _:
            # Any other input (including "a") aborts the run.
            log.info("duplicate_group_aborted", occupant=str(occupant_path), mover=str(mover_path))
            return DuplicateResolution(
                choice="abort",
                survivor_path=occupant_path,
                deleted_path=None,
                deleted_release_id="",
                secondary_mbid="",
                proceed_with_move=False,
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


def _clamp_maint_dest(dest_root: Path, dest: Path) -> Path:
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
    component.  The function is idempotent: components already within the limit pass through
    unchanged.

    :param dest_root: Library root.  Used only to compute the relative parts of ``dest``.
    :param dest: Full absolute destination path including the audio extension (i.e. the result of
        ``build_dest_path(...).with_suffix(ext)``).
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
    for part in rel_parts:
        if len(part.encode("utf-8")) > _NAME_MAX:
            part_audio_suffix = leaf_audio_suffix if part == leaf else ""
            clamped = _proposed_short(part, part_audio_suffix)
            log.warning(
                "name_too_long",
                component=part,
                bytes=len(part.encode("utf-8")),
                limit=_NAME_MAX,
                shortened=clamped,
            )
            new_parts.append(clamped)
        else:
            new_parts.append(part)
    return dest_root.joinpath(*new_parts)


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


def repath(dest_root: Path, *, dry_run: bool = False, yes: bool = False) -> DryRunPlan | None:  # pylint: disable=too-many-return-statements
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
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

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
    # Groups tracks by CWP_WORKID_TOP, mirroring the scanner grouping in
    # scripts/scan_nonuniform_depth.py (which groups by (release_dir, CWP_WORKID_TOP)).
    # repath operates across the whole library, so release_dir is implicit in the path; the
    # grouping key is CWP_WORKID_TOP alone (consistent with the scanner's per-release-dir
    # grouping because each release dir maps to one top-work MBID in practice).
    _repath_work_groups: dict[str, list[int]] = {}
    for _ri, (_rp, _rt, _rfd, _re, _rrid) in enumerate(_repath_file_data):
        _twid = _rt.cwp_workid_top
        if _twid not in _repath_work_groups:
            _repath_work_groups[_twid] = []
        _repath_work_groups[_twid].append(_ri)

    _repath_modal_by_idx: dict[int, int | None] = {}
    for _twid, _group_idxs in _repath_work_groups.items():
        _part_levels = [int(_repath_file_data[_i][1].cwp_part_levels or "0") for _i in _group_idxs]
        _modal = work_group_modal_depth(_part_levels)
        # When modal is 0 (all-orphan group), pass None so build_dest_path uses own depth
        # unchanged — equivalent outcome, avoids a redundant min(0, 0) clamp.
        _modal_or_none: int | None = _modal if _modal > 0 else None
        for _i in _group_idxs:
            _repath_modal_by_idx[_i] = _modal_or_none

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
        new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext))

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


def regroup(dest_root: Path, *, yes: bool = False, dry_run: bool = False) -> DryRunPlan | None:  # pylint: disable=too-many-return-statements
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
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

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
    # Groups tracks by CWP_WORKID_TOP, mirroring the scanner grouping in
    # scripts/scan_nonuniform_depth.py (which groups by (release_dir, CWP_WORKID_TOP)).
    _regroup_work_groups: dict[str, list[int]] = {}
    for _ri, (_rp, _rt, _rfd, _re, _rrid) in enumerate(_regroup_file_data):
        _twid = _rt.cwp_workid_top
        if _twid not in _regroup_work_groups:
            _regroup_work_groups[_twid] = []
        _regroup_work_groups[_twid].append(_ri)

    _regroup_modal_by_idx: dict[int, int | None] = {}
    for _twid, _group_idxs in _regroup_work_groups.items():
        _part_levels = [int(_regroup_file_data[_i][1].cwp_part_levels or "0") for _i in _group_idxs]
        _modal = work_group_modal_depth(_part_levels)
        # When modal is 0 (all-orphan group), pass None so build_dest_path uses own depth
        # unchanged — equivalent outcome, avoids a redundant min(0, 0) clamp.
        _modal_or_none: int | None = _modal if _modal > 0 else None
        for _i in _group_idxs:
            _regroup_modal_by_idx[_i] = _modal_or_none

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
        new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext))

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


def _is_composer_split_release(group_tags: list[tuple[Path, TrackTags, dict[str, str]]]) -> bool:
    """Return ``True`` when a release group is a multi-composer compilation (non-classical).

    A release is a multi-composer compilation when ``CEA_COMPOSER_LASTNAMES`` is non-empty and
    takes ≥2 distinct values across the tracks of the group, AND the release is confirmed
    non-classical.

    The non-classical scope gate: the release is non-classical when **any** track in the group
    satisfies either of:

    * ``cwp_work_top`` is empty (no MB work link → non-classical), or
    * ``cwp_worktype_genres_top`` does not contain ``"Classical"``.

    A classical release with varying ``CEA_COMPOSER_LASTNAMES`` (e.g. a multi-composer anthology)
    routes through the existing cross-medium composer-pass unification (W2c), not this rule.

    :param group_tags: List of ``(file_path, tags, file_dict)`` triples for all files in the
        release group, as built by :func:`unify`.
    :returns: ``True`` when the group is a non-classical multi-composer compilation.
    """
    # Collect distinct non-empty CEA_COMPOSER_LASTNAMES values
    composer_values: set[str] = set()
    for _, tags, _ in group_tags:
        if tags.cea_composer_lastnames:
            composer_values.add(tags.cea_composer_lastnames)

    if len(composer_values) < 2:  # noqa: PLR2004 — 2 is the multi-composer threshold (C-W2)
        return False

    # Scope gate: apply only to non-classical releases.
    # A release is non-classical when any track lacks a CWP_WORK_TOP (no MB work link) OR
    # its CWP_WORKTYPE_GENRES_TOP does not contain "Classical".
    for _, tags, _ in group_tags:
        if not tags.cwp_work_top:
            return True
        if "Classical" not in tags.cwp_worktype_genres_top:
            return True

    return False


def _canonical_composer_component(group_tags: list[tuple[Path, TrackTags, dict[str, str]]]) -> str:
    """Derive the canonical composer path component for a multi-composer compilation.

    Reads ``ALBUMARTISTSORT`` from the first track in the group (it is uniform across a release)
    and applies :func:`~music_annotator._artists.last_name` to produce the sort-name last-name
    form.

    Fallback: if ``ALBUMARTISTSORT`` is empty or ``"Various Artists"``, returns ``"Various"``
    (the CE convention for multi-artist compilations with no single canonical identity).

    :param group_tags: List of ``(file_path, tags, file_dict)`` triples for all files in the
        release group.  Must be non-empty.
    :returns: The canonical composer component string (e.g. ``"Goodman, Benny"`` or
        ``"Various"``).
    """
    # Read ALBUMARTISTSORT from the first track (uniform across a release)
    _, first_tags, _ = group_tags[0]
    album_artist_sort = first_tags.albumartistsort.strip()

    if not album_artist_sort or album_artist_sort == "Various Artists":
        return "Various"

    return last_name(album_artist_sort)


def _unify_classical_composer_groups(group_tags: list[tuple[Path, TrackTags, dict[str, str]]]) -> None:
    """Propagate the fullest author chain within each top-work group for classical releases.

    Implements the W2c arranger/finisher retroactive fix for already-annotated libraries.  When a
    classical release has movements where an arranger or finisher was credited as ``"composer"``
    with the ``"additional"`` attribute on only some movements, those movements may have a different
    ``CEA_COMPOSER_LASTNAMES`` embedded in their tags than the movements with a plain primary-composer
    relation (the Mozart K.626 Süßmayr shape).

    Because ``cwp_composers_is_fallback`` is never written to audio files (it is an in-memory
    pipeline flag only), the retroactive pass cannot distinguish primary from fallback credits
    directly.  Instead, it uses the **fullest author chain** within each top-work group: the
    non-empty ``cea_composer_lastnames`` value with the most composers (most ``"; "``-separated
    entries) across all movements of the same ``cwp_workid_top`` is taken as the canonical
    composer chain, and all movements that differ are patched to match.  This is the upward
    unification direction per SEL-8 / REND-27: primary + completer propagates to every movement,
    including those that only credited the primary.  Ties (equal composer count) are broken by
    first-appearance order (stable).

    This mirrors the cross-medium composer pass in :func:`run` (which propagates the fullest
    author chain to all movements in the group), but operates on already-embedded tags rather
    than in-memory :class:`~music_annotator.models.TrackTags` objects built during annotation.

    Mutates ``group_tags`` in-place (patches both ``tags.cea_composer_lastnames`` and
    ``tags.cwp_composer_lastnames`` on affected entries, since :func:`~music_annotator._tags.build_dest_path`
    prefers ``CWP_COMPOSER_LASTNAMES`` over ``CEA_COMPOSER_LASTNAMES`` when both are present).
    Only called for classical releases (scope gate is in :func:`unify`).

    :param group_tags: List of ``(file_path, tags, file_dict)`` triples for all files in the
        release group, as built by :func:`unify`.
    """
    # Group tracks by top-work MBID (cwp_workid_top, falling back to musicbrainz_workid).
    # Tracks without any work ID are grouped under "" and skipped (no work context to unify).
    work_groups: dict[str, list[int]] = {}
    for i, (_, tags, _) in enumerate(group_tags):
        work_id = tags.cwp_workid_top or tags.musicbrainz_workid
        work_groups.setdefault(work_id, []).append(i)

    for work_id, idxs in work_groups.items():
        if not work_id:
            continue  # no work context — skip

        # Collect distinct non-empty cea_composer_lastnames values in this work group.
        composer_values: set[str] = set()
        for i in idxs:
            _, tags, _ = group_tags[i]
            val = tags.cea_composer_lastnames
            if val:
                composer_values.add(val)

        if len(composer_values) < 2:  # noqa: PLR2004 — 2 is the multi-value threshold
            continue  # all movements agree — nothing to unify

        # Fullest author chain: the non-empty cea_composer_lastnames value with the most
        # composers (most "; "-separated entries).  Ties are broken by first-appearance order
        # (stable: we iterate group_tags in file-path order and track the first occurrence).
        canonical = ""
        canonical_count = 0
        for i in idxs:
            _, tags, _ = group_tags[i]
            val = tags.cea_composer_lastnames
            if not val:
                continue
            count = val.count(";") + 1
            if count > canonical_count:
                canonical = val
                canonical_count = count

        log.info(
            "unify_classical_composer_group",
            work_id=work_id,
            canonical=canonical,
            distinct_values=sorted(composer_values),
        )

        for i in idxs:
            _, tags, _ = group_tags[i]
            # Patch both CEA_COMPOSER_LASTNAMES and CWP_COMPOSER_LASTNAMES so that
            # build_dest_path (which prefers CWP_COMPOSER_LASTNAMES) produces the
            # canonical path for all movements.
            if tags.cea_composer_lastnames != canonical:
                tags.cea_composer_lastnames = canonical
            if tags.cwp_composer_lastnames != canonical:
                tags.cwp_composer_lastnames = canonical


def unify(dest_root: Path, *, yes: bool = False, dry_run: bool = False) -> DryRunPlan | None:  # pylint: disable=too-many-return-statements
    """Consolidate performer-split and composer-split fragmented releases into their canonical top_dirs.

    Scans ``dest_root`` for releases whose tracks are spread across ≥2 distinct top_dirs due to
    per-track ``CEA_SOLOISTS`` variation (the dominant fragmentation shape: 29 releases in the 2026-06 audit)
    or per-track ``CEA_COMPOSER_LASTNAMES`` variation on non-classical compilations (the Benny
    Goodman shape).  For each fragmented release, reads the embedded tags from all its files, runs
    :func:`~music_annotator._tags.build_dest_path` over the full release group to compute the
    canonical destination for every file, and moves files that are not already at their canonical
    path.

    **Detection (C-W2):** a release is fragmented when ≥2 distinct top_dirs share the same
    ``MUSICBRAINZ_ALBUMID`` tag.  The join key is the embedded tag, not the journal.

    **Canonical path algorithm (C-W2):** :func:`~music_annotator._tags.build_dest_path` already
    computes the correct unified path when given all tracks of the release as a group, because the
    cross-medium composer pass and ``recording_date_work`` pass run over the full release group.
    The performers component uses album-level conductors and ensembles (C-NOSOLO: soloists are
    never a path component).

    **Composer-split pre-processing (W2b):** when ``CEA_COMPOSER_LASTNAMES`` varies across tracks
    of a non-classical release, :func:`_is_composer_split_release` detects the shape and
    :func:`_canonical_composer_component` derives the canonical composer component from
    ``ALBUMARTISTSORT`` (falling back to ``"Various"``).  Every track's ``cea_composer_lastnames``
    is patched to this canonical value before :func:`~music_annotator._tags.build_dest_path` is
    called.  ``build_dest_path`` itself is unchanged.

    **Classical arranger/finisher unification (W2c):** for classical releases where
    ``CEA_COMPOSER_LASTNAMES`` varies across movements of the same top work (the Mozart K.626
    Süßmayr shape — an arranger/finisher credited as ``"composer"`` with the ``"additional"``
    attribute on only some movements), :func:`_unify_classical_composer_groups` propagates the
    plurality composer value within each ``CWP_WORKID_TOP`` group.  This is the retroactive
    counterpart to the cross-medium composer pass in :func:`run`: files annotated before that pass
    was implemented may have an arranger/finisher's name embedded as the composer for some
    movements.  The W2c pass runs only when W2b does not apply (i.e. the release is classical or
    has uniform ``CEA_COMPOSER_LASTNAMES``).

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
    """
    journal_path = dest_root / JOURNAL_FILENAME
    # Read the journal once at the start of the pass.  The in-memory copy is threaded through
    # to _move_verify_journal so no re-read occurs between moves.
    journal = read_journal(journal_path)

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

        # --- Composer-split pre-processing (W2b) ---
        # When CEA_COMPOSER_LASTNAMES varies across tracks of a non-classical release, patch
        # every track's tags with the canonical composer component derived from ALBUMARTISTSORT
        # before calling build_dest_path.  This ensures all tracks land in the same top_dir.
        # build_dest_path itself is unchanged — the normalisation is pre-processing only.
        if _is_composer_split_release(group_tags):
            canonical_composer = _canonical_composer_component(group_tags)
            log.info(
                "unify_composer_split_detected",
                release_id=release_id,
                canonical_composer=canonical_composer,
            )
            for _, tags, _ in group_tags:
                tags.cea_composer_lastnames = canonical_composer

        # --- Classical arranger/finisher unification (W2c) ---
        # For classical releases where CEA_COMPOSER_LASTNAMES varies across movements of the
        # same top work, propagate the plurality composer value within each work group.  This
        # is the retroactive counterpart to the cross-medium composer pass in run(): files
        # annotated before that pass was implemented may have an arranger/finisher's name
        # embedded as the composer for some movements (the Mozart K.626 Süßmayr shape).
        # _is_composer_split_release() returns False for classical releases, so this pass
        # runs independently of W2b and handles the classical case only.
        # The scope gate (classical release) is enforced inside _unify_classical_composer_groups
        # by checking that at least one track has cwp_work_top set and cwp_worktype_genres_top
        # contains "Classical".  We pre-check here to avoid the function call overhead when
        # the release is clearly non-classical (already handled by W2b above).
        else:
            _unify_classical_composer_groups(group_tags)

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

        for file_path, tags, file_dict in group_tags:
            ext = file_path.suffix.lower()
            new_dest_base = build_dest_path(dest_root, stub_release, stub_track, tags, global_track_idx=0)
            new_dest = _clamp_maint_dest(dest_root, new_dest_base.with_suffix(ext))

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


def enrich(dest_root: Path, *, re_resolve: bool = False, dry_run: bool = False, acoustid_key: str = "") -> DryRunPlan | None:
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
    journal = read_journal(journal_path)

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

        # Count inconclusive acoustid for files that need enrichment but have no acoustid tag
        if "acoustid_id" not in fields:
            count_inconclusive_acoustid += 1

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


def repatch_catalogue_colon(dest_root: Path, *, dry_run: bool = False) -> DryRunPlan | None:
    """Rewrite ``CWP_PART_*`` / ``CWP_GROUPHEADING`` tags corrupted by the pre-fix bare-``":"`` split.

    Scans each FLAC and MP3 file in the annotated library (resolved via the journal lineage) for
    ``CWP_PART_{i}`` values that were produced by the retired bare-``":"`` fallback in
    ``strip_common_prefix`` — a colon inside a catalogue number (e.g. Hoboken ``"Hob. III:31"``)
    caused the old split to truncate the label to a bare fragment (``"31"``).  The forward fix
    (NORM-9 / STYLEGUIDE 4.x) keys on ``": "`` (colon-followed-by-space) so new ingests are
    correct; this pass re-derives the corrected label offline from the ``CWP_WORK_{i}`` /
    ``CWP_WORK_{i+1}`` pair already embedded in the file — no MusicBrainz network call is needed.

    For each file the pass:

    1. Iterates levels ``i = 1, 2, 3, …`` (stopping when ``CWP_WORK_{i}`` is absent), applying
       :func:`~music_annotator._works.is_catalogue_colon_corrupt` to each ``CWP_PART_{i}``.
    2. For each corrupt level, re-derives the corrected label via
       :func:`~music_annotator._works.rederive_part_label`.  When
       :data:`~music_annotator._works.CANNOT_RECOMPUTE` is returned (the ``CWP_WORK_{i}`` title
       is absent), the level is left untouched.
    3. When any level was corrected, rebuilds ``CWP_GROUPHEADING`` from the corrected part labels
       using the ``build_cwp_tags`` grammar: ``" :: ".join([work_top, *inter_parts, bottom_part])``
       (NORM-9 / STYLEGUIDE 4.x).
    4. Writes the corrected tags via :func:`~music_annotator._tagger.apply_tags_flac` /
       :func:`~music_annotator._tagger.apply_tags_mp3`, then confirms the round-trip via
       :func:`~music_annotator._pipeline_io._verify_copy`, and **only then** appends a journal
       entry with ``action="repatched"`` (the confirmation-provenance invariant: a journal entry
       is never written before verification succeeds).

    The pass is idempotent: a second run on a library where all labels are already correct is a
    no-op (no writes, no journal entries).  When ``dry_run=True``, planned repatches are logged
    but no tags are written and no journal entries are appended.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, log planned repatches without writing any tags or journal
        entries.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = read_journal(journal_path)

    # Resolve current on-disk path for each logical library file.
    # _resolve_current_lib walks entries in chronological order; "tagged" seeds the map;
    # "repathed"/"regrouped" update it; "enriched"/"repatched" re-register the path in-place.
    current_lib = _resolve_current_lib(journal)

    # Filter to files that actually exist on disk and are FLAC or MP3
    existing_files: list[tuple[Path, str]] = [
        (p, rid) for p, rid in current_lib.items() if p.exists() and p.suffix.lower() in {".flac", ".mp3"}
    ]

    if not existing_files:
        log.info("repatch_catalogue_colon_nothing_to_repatch", dest_root=str(dest_root))
        if dry_run:
            return DryRunPlan(pass_name="repatch_catalogue_colon", entries=[], count=0)
        return None

    now = datetime.datetime.now(datetime.UTC).isoformat()
    count_repatched = 0
    count_noop = 0
    count_dry_run = 0
    cat_colon_dry_run_entries: list[DryRunEntry] = []

    for current_path, release_id in existing_files:
        ext = current_path.suffix.lower()

        # Read current tags from the file
        try:
            match ext:
                case ".flac":
                    file_dict = _read_tags_flac(current_path)
                case ".mp3":
                    file_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — filtered to .flac/.mp3 above
                    log.warning("repatch_catalogue_colon_unsupported_format", path=str(current_path), ext=ext)
                    continue
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("repatch_catalogue_colon_tag_read_error", path=str(current_path), error=str(exc))
            continue

        # Scan levels i = 1, 2, 3, … for corrupt CWP_PART_{i} labels.
        # Level 0 is the leaf movement title; the catalogue-colon bug affects intermediate levels
        # where the work title embeds a catalogue colon (e.g. Hoboken "Hob. III:31").
        # The loop terminates when rederive_part_label returns CANNOT_RECOMPUTE (CWP_WORK_{i}
        # absent), which signals no more levels exist in the hierarchy.
        corrected_parts: dict[int, str] = {}
        i = 1
        while True:
            child_title = file_dict.get(f"CWP_WORK_{i}", "")
            parent_title = file_dict.get(f"CWP_WORK_{i + 1}", "")
            stored_label = file_dict.get(f"CWP_PART_{i}", "")

            recomputed = rederive_part_label(child_title, parent_title)
            match recomputed:
                case _Rederivation.CANNOT_RECOMPUTE:
                    # CWP_WORK_{i} is absent — no more levels in the hierarchy; stop scanning.
                    break
                case str() as label:
                    # Recomputation succeeded; check whether the stored label carries the
                    # catalogue-colon corruption signature.  A correct label recomputes to
                    # itself and is_catalogue_colon_corrupt returns False — no-op.
                    if is_catalogue_colon_corrupt(stored_label, child_title, parent_title):
                        corrected_parts[i] = label
                case _:  # pragma: no cover — rederive_part_label returns str | _Rederivation only
                    pass
            i += 1

        if not corrected_parts:
            # No corrupt levels found — file is already correct (idempotency: no-op).
            log.debug("repatch_catalogue_colon_noop", path=str(current_path.relative_to(dest_root)))
            count_noop += 1
            continue

        # Rebuild CWP_GROUPHEADING from the corrected part labels using the build_cwp_tags grammar:
        #   " :: ".join([work_top, *intermediate_parts_top_down, bottom_part])
        # where intermediate parts run from level n_levels-2 down to level 1, and bottom_part is
        # CWP_PART_0.  This replicates _tags.py:561-570 exactly — no second assembler is minted.
        work_top = file_dict.get("CWP_WORK_TOP", "")
        # CWP_PART_LEVELS = n_levels - 1 (the number of part-label levels, excluding the root).
        # The total level count is part_levels + 1; the grammar loop runs from level part_levels-1
        # down to level 1 (intermediate levels), then appends level 0 (leaf).
        part_levels = int(file_dict.get("CWP_PART_LEVELS", "0") or "0")
        n_levels = part_levels + 1

        gh_parts: list[str] = [work_top] if work_top else []
        for j in range(n_levels - 2, 0, -1):
            inter_part = corrected_parts.get(j, file_dict.get(f"CWP_PART_{j}", file_dict.get(f"CWP_WORK_{j}", "")))
            if inter_part:
                gh_parts.append(inter_part)
        bottom_part = file_dict.get("CWP_PART_0", "")
        if bottom_part:
            gh_parts.append(bottom_part)
        new_groupheading = " :: ".join(gh_parts)

        if dry_run:
            log.info(
                "repatch_catalogue_colon_dry_run",
                path=str(current_path.relative_to(dest_root)),
                levels=list(corrected_parts.keys()),
                new_groupheading=new_groupheading,
            )
            count_dry_run += 1
            tag_delta: dict[str, str] = {f"CWP_PART_{i}": label for i, label in corrected_parts.items()}
            if new_groupheading:
                tag_delta["CWP_GROUPHEADING"] = new_groupheading
            cat_colon_dry_run_entries.append(DryRunEntry(current_path=str(current_path), tag_delta=tag_delta))
            continue

        # Apply corrected labels and rebuilt groupheading to the tag dict, then write back.
        for level_idx, corrected_label in corrected_parts.items():
            file_dict[f"CWP_PART_{level_idx}"] = corrected_label
        if new_groupheading:
            file_dict["CWP_GROUPHEADING"] = new_groupheading

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
            raise RuntimeError(f"repatch_catalogue_colon tag write failure for '{current_path.name}': {exc}") from exc

        # Confirm the tag round-trip before journalling (confirmation-provenance invariant).
        # The journal entry is appended only after _verify_copy confirms the write succeeded.
        post_mtime = current_path.stat().st_mtime
        _verify_copy(current_path, current_path, tags, None, post_mtime)

        entry = TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(current_path),
            destination=str(current_path),
            action="repatched",
        )
        append_journal_entry(journal_path, entry)
        log.info(
            "repatch_catalogue_colon_written",
            path=str(current_path.relative_to(dest_root)),
            levels=list(corrected_parts.keys()),
        )
        count_repatched += 1

    log.info(
        "repatch_catalogue_colon_complete",
        dest_root=str(dest_root),
        repatched=count_repatched,
        noop=count_noop,
        dry_run=count_dry_run,
    )
    if dry_run:
        return DryRunPlan(
            pass_name="repatch_catalogue_colon",
            entries=cat_colon_dry_run_entries,
            count=len(cat_colon_dry_run_entries),
        )
    return None


def _has_legacy_acoustid_key(path: Path) -> bool:
    """Return ``True`` when ``path`` carries the legacy ``CHROMAPRINT_FP`` fingerprint key on disk.

    Reads the file directly via mutagen to detect the legacy key without relying on the
    ``_read_tags_flac`` / ``_read_tags_mp3`` helpers (which only return keys they know about).

    For FLAC files the Vorbis Comment key ``"chromaprint_fp"`` (case-insensitive) is checked.
    For MP3 files the TXXX frame with description ``"Chromaprint Fingerprint"`` is checked.

    :param path: Path to the FLAC or MP3 file to inspect.
    :returns: ``True`` when the legacy key is present, ``False`` otherwise (including on read error).
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = MutagenFLAC(str(path))
                # Vorbis Comment keys are case-insensitive; mutagen stores them lowercase.
                return bool(audio.get("chromaprint_fp") or audio.get("CHROMAPRINT_FP"))
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
                    if frame.desc == "Chromaprint Fingerprint":
                        return True
                return False
            case _:
                return False
    except Exception:  # noqa: BLE001 — best-effort; any failure means cannot determine
        return False


def repatch_acoustid_tags(
    journal: Path,
    dest_root: Path,
    acoustid_key: str = "",
    dry_run: bool = False,
) -> DryRunPlan | list[TransactionEntry]:
    """Migrate the legacy ``CHROMAPRINT_FP`` fingerprint key to the Picard-aligned ``ACOUSTID_FINGERPRINT`` key.

    Scans each FLAC and MP3 file in the annotated library (resolved via the journal lineage) for
    the legacy ``CHROMAPRINT_FP`` Vorbis Comment key (FLAC) or TXXX ``"Chromaprint Fingerprint"``
    frame (MP3).  For each file carrying the legacy key, migrates the fingerprint value to the
    Picard-aligned ``ACOUSTID_FINGERPRINT`` key and removes the legacy key.  When an AcoustID API
    key is supplied and a fingerprint is present, re-sources ``ACOUSTID_ID`` from the fingerprint
    ``/v2/lookup`` endpoint (the same path :func:`enrich` uses under ``re_resolve=True``).

    **Provenance-chain invariant (C-PROV):** a journal entry with ``action="acoustid-repatched"``
    is appended **only** after :func:`~music_annotator._pipeline_io._verify_copy` confirms the
    tag round-trip.  The entry is never written before verification succeeds.

    **Idempotency:** a file already carrying ``ACOUSTID_FINGERPRINT`` and no legacy
    ``CHROMAPRINT_FP`` key is a no-op.  A second run on a fully-migrated library appends no
    journal entries.

    **Key migration mechanics:** :func:`~music_annotator._tagger.apply_tags_flac` calls
    ``audio.clear()`` before writing, so the legacy ``CHROMAPRINT_FP`` Vorbis Comment is
    automatically removed when the new ``ACOUSTID_FINGERPRINT`` is written.  Similarly,
    :func:`~music_annotator._tagger.apply_tags_mp3` deletes all existing ID3 tags before writing,
    so the legacy TXXX ``"Chromaprint Fingerprint"`` frame is removed automatically.

    :param journal: Path to the journal file (``<dest_root>/music_annotator_journal.json``).
    :param dest_root: Root of the annotated music library; used for log messages.
    :param acoustid_key: AcoustID application API key.  When non-empty and a fingerprint is
        present, performs a keyed ``/v2/lookup`` after migrating the key and backfills
        ``ACOUSTID_ID`` with the top AcoustID cluster UUID.  When empty, ``ACOUSTID_ID`` is
        left unchanged.
    :param dry_run: When ``True``, log planned migrations without writing any tags or journal
        entries.  Returns a :class:`~music_annotator.models.DryRunPlan` capturing the change-set
        the pass would enact (empty plan when no files need migration).
    :returns: A :class:`~music_annotator.models.DryRunPlan` when ``dry_run=True`` (the structured
        change-set the pass would enact); otherwise the list of
        :class:`~music_annotator.models.TransactionEntry` objects appended to the journal during
        this run (empty list when no files needed migration).
    """
    tx_log = read_journal(journal)

    # Resolve current on-disk path for each logical library file.
    # _resolve_current_lib walks entries in chronological order; "tagged" seeds the map;
    # "repathed"/"regrouped" update it; "enriched"/"repatched"/"acoustid-repatched" re-register
    # the path in-place.  Multi-hop chains resolve naturally.
    current_lib = _resolve_current_lib(tx_log)

    # Filter to files that actually exist on disk and are FLAC or MP3
    existing_files: list[tuple[Path, str]] = [
        (p, rid) for p, rid in current_lib.items() if p.exists() and p.suffix.lower() in {".flac", ".mp3"}
    ]

    if not existing_files:
        log.info("repatch_acoustid_tags_nothing_to_repatch", dest_root=str(dest_root))
        if dry_run:
            return DryRunPlan(pass_name="repatch_acoustid_tags", entries=[], count=0)
        return []

    now = datetime.datetime.now(datetime.UTC).isoformat()
    count_migrated = 0
    count_noop = 0
    count_dry_run = 0
    appended: list[TransactionEntry] = []
    acoustid_dry_run_entries: list[DryRunEntry] = []

    for current_path, release_id in existing_files:
        ext = current_path.suffix.lower()

        # Idempotency check: skip files that do not carry the legacy key.
        # A file already migrated to ACOUSTID_FINGERPRINT (and no CHROMAPRINT_FP) is a no-op.
        if not _has_legacy_acoustid_key(current_path):
            log.debug("repatch_acoustid_tags_noop", path=str(current_path.relative_to(dest_root)))
            count_noop += 1
            continue

        # Read the fingerprint value via the dual-read helper (new key first, legacy second).
        # Since _has_legacy_acoustid_key returned True, the legacy key is present; the dual-read
        # helper will find it even if the new key is absent.
        fingerprint = _read_acoustid_fingerprint_tag(current_path)

        if dry_run:
            log.info(
                "repatch_acoustid_tags_dry_run",
                path=str(current_path.relative_to(dest_root)),
                has_fingerprint=bool(fingerprint),
                will_re_resolve=bool(acoustid_key and fingerprint),
            )
            count_dry_run += 1
            acoustid_tag_delta: dict[str, str] = {}
            if fingerprint:
                acoustid_tag_delta["ACOUSTID_FINGERPRINT"] = fingerprint
            if acoustid_key and fingerprint:
                acoustid_tag_delta["ACOUSTID_ID"] = "(re-resolved)"
            acoustid_dry_run_entries.append(DryRunEntry(current_path=str(current_path), tag_delta=acoustid_tag_delta))
            continue

        # Read current tags, update the fingerprint field, and write back.
        try:
            match ext:
                case ".flac":
                    file_dict = _read_tags_flac(current_path)
                case ".mp3":
                    file_dict = _read_tags_mp3(current_path)
                case _:  # pragma: no cover — filtered to .flac/.mp3 above
                    log.warning("repatch_acoustid_tags_unsupported_format", path=str(current_path), ext=ext)
                    continue
        except Exception as exc:  # noqa: BLE001 — tag read failure: log and skip
            log.warning("repatch_acoustid_tags_tag_read_error", path=str(current_path), error=str(exc))
            continue

        # Migrate the fingerprint to the new key in the tag dict.
        # The legacy CHROMAPRINT_FP key is removed automatically by apply_tags_flac/apply_tags_mp3
        # (both clear all existing tags before writing), so we only need to set the new key.
        file_dict["ACOUSTID_FINGERPRINT"] = fingerprint
        # Remove the legacy key from the dict so _tags_from_file_dict does not carry it forward
        # as an extra field (which would cause _verify_copy to fail on round-trip).
        file_dict.pop("CHROMAPRINT_FP", None)

        # When an AcoustID key is available and a fingerprint is present, re-source ACOUSTID_ID
        # from the fingerprint /v2/lookup endpoint — the same path enrich(re_resolve) uses.
        # On lookup failure, leave ACOUSTID_ID unchanged (inconclusive; not a hard error).
        if acoustid_key and fingerprint:
            _dur_s = _read_duration_ms(current_path) // 1000
            try:
                _, _top_uuid = _fetch_acoustid_lookup_raw(fingerprint, _dur_s, acoustid_key)
            except (OSError, RuntimeError, ValueError) as _exc:
                log.warning(
                    "repatch_acoustid_tags_lookup_failed",
                    path=str(current_path.relative_to(dest_root)),
                    error=str(_exc),
                )
                _top_uuid = ""
            if _top_uuid:
                file_dict["ACOUSTID_ID"] = _top_uuid

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
            raise RuntimeError(f"repatch_acoustid_tags write failure for '{current_path.name}': {exc}") from exc

        # Confirm the tag round-trip before journalling (provenance-chain invariant C-PROV):
        # the journal entry is appended only after _verify_copy confirms the write succeeded.
        post_mtime = current_path.stat().st_mtime
        _verify_copy(current_path, current_path, tags, None, post_mtime)

        final_fingerprint = tags.acoustid_fingerprint
        final_acoustid_id = file_dict.get("ACOUSTID_ID", "")

        entry = TransactionEntry(
            timestamp=now,
            release_id=release_id,
            source=str(current_path),
            destination=str(current_path),
            action="acoustid-repatched",
            acoustid_fingerprint=final_fingerprint,
            acoustid_id=final_acoustid_id,
        )
        append_journal_entry(journal, entry)
        appended.append(entry)
        log.info(
            "repatch_acoustid_tags_written",
            path=str(current_path.relative_to(dest_root)),
            re_resolved=bool(acoustid_key and fingerprint and final_acoustid_id),
        )
        count_migrated += 1

    log.info(
        "repatch_acoustid_tags_complete",
        dest_root=str(dest_root),
        migrated=count_migrated,
        noop=count_noop,
        dry_run=count_dry_run,
    )
    if dry_run:
        return DryRunPlan(
            pass_name="repatch_acoustid_tags",
            entries=acoustid_dry_run_entries,
            count=len(acoustid_dry_run_entries),
        )
    return appended


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
    ``dest_root.parent / "Reference"``).  This is the conventional location for a pre-repatch
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


def _journal_capacity(journal_path: Path, plans: list[DryRunPlan]) -> JournalCapacity:
    """Measure the current journal state and project the entry-count growth from executing all plans.

    Reads the journal at ``journal_path`` to obtain the current entry count, and measures the
    on-disk file size.  The projected delta is the sum of all plan counts: each planned file
    action (move or tag-content write) appends exactly one journal entry when the pass runs for
    real.

    :param journal_path: Path to the journal file (``<dest_root>/music_annotator_journal.json``).
    :param plans: The :class:`~music_annotator.models.DryRunPlan` objects returned by each pass.
    :returns: A :class:`~music_annotator.models.JournalCapacity` with current and projected state.
    """
    journal = read_journal(journal_path)
    current_count = len(journal.entries)
    current_size = journal_path.stat().st_size if journal_path.exists() else 0
    projected_delta = sum(p.count for p in plans)
    return JournalCapacity(
        current_entry_count=current_count,
        current_size_bytes=current_size,
        projected_delta_entries=projected_delta,
    )


def compose_preflight_report(dest_root: Path, journal_path: Path) -> PreflightReport:
    """Run all maintenance passes with ``dry_run=True`` and assemble a consolidated preflight report.

    Calls each of the six maintenance passes (:func:`repath`, :func:`regroup`, :func:`unify`,
    :func:`enrich`, :func:`repatch_catalogue_colon`, :func:`repatch_acoustid_tags`) with
    ``dry_run=True`` over ``dest_root``, collects the returned :class:`~music_annotator.models.DryRunPlan`
    objects, and assembles a :class:`~music_annotator.models.PreflightReport` containing:

    - **Per-pass summaries**: the count of planned changes per pass and the cross-pass overlap
      count for each pass.
    - **Cross-pass overlap map**: files appearing in more than one pass's plan, keyed on
      ``current_path``.  A file in multiple plans means the ordering of those passes is
      load-bearing (tag-content rewrites must precede path rewrites so the corrected tags drive
      the new destination path).
    - **Journal capacity**: the current journal entry count, on-disk file size, and the projected
      entry-count delta if all planned passes were executed.
    - **Reference/ evidence**: read-only presence check and disk footprint of the
      ``Reference/`` snapshot directory alongside ``dest_root``.

    When ``dest_root`` is not mounted or is empty, returns a :class:`~music_annotator.models.PreflightReport`
    with ``scan_ran=False`` and all other fields at their defaults.  This is structurally distinct
    from a report with ``scan_ran=True`` and all counts zero (which means the scan ran and found
    nothing to change).

    This function is non-mutating: it calls every pass with ``dry_run=True`` and never reaches
    any mutating branch.  No files are moved, no tags are written, and no journal entries are
    appended.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param journal_path: Path to the journal file (typically
        ``dest_root / JOURNAL_FILENAME``).
    :returns: A :class:`~music_annotator.models.PreflightReport` with the consolidated dry-run
        evidence, or a not-run report when the root is absent or empty.
    """
    if not _check_dest_root(dest_root):
        log.info("compose_preflight_report_root_not_mounted", dest_root=str(dest_root))
        return PreflightReport(scan_ran=False)

    # Run all six passes with dry_run=True.  The asymmetric repatch_acoustid_tags signature
    # (journal: Path as first positional arg) is handled explicitly; all other passes take
    # dest_root as their first positional arg.
    repath_plan = repath(dest_root, dry_run=True)
    regroup_plan = regroup(dest_root, dry_run=True)
    unify_plan = unify(dest_root, dry_run=True)
    enrich_plan = enrich(dest_root, dry_run=True)
    cat_colon_plan = repatch_catalogue_colon(dest_root, dry_run=True)
    acoustid_result = repatch_acoustid_tags(journal_path, dest_root, dry_run=True)

    # All six passes return DryRunPlan when dry_run=True.  The type checker knows
    # repatch_acoustid_tags returns DryRunPlan | list[TransactionEntry]; assert the dry-run arm.
    assert isinstance(acoustid_result, DryRunPlan), "repatch_acoustid_tags(dry_run=True) must return DryRunPlan"

    # Collect plans; each pass returns DryRunPlan | None on the dry-run arm (except
    # repatch_acoustid_tags which always returns DryRunPlan on dry_run=True).  The move passes
    # return None only on the non-dry-run arm, so under dry_run=True they always return a plan.
    # Guard defensively: treat None as an empty plan so the report is always complete.
    def _as_plan(result: DryRunPlan | None, pass_name: str) -> DryRunPlan:
        """Coerce a possibly-None dry-run result to a DryRunPlan.

        :param result: The value returned by a pass called with ``dry_run=True``.
        :param pass_name: The pass name to use when constructing an empty fallback plan.
        :returns: The result as-is when it is a :class:`~music_annotator.models.DryRunPlan`,
            or an empty plan when it is ``None``.
        """
        if result is None:  # pragma: no cover — dry_run=True always returns a plan
            return DryRunPlan(pass_name=pass_name, entries=[], count=0)
        return result

    plans: list[DryRunPlan] = [
        _as_plan(repath_plan, "repath"),
        _as_plan(regroup_plan, "regroup"),
        _as_plan(unify_plan, "unify"),
        _as_plan(enrich_plan, "enrich"),
        _as_plan(cat_colon_plan, "repatch_catalogue_colon"),
        acoustid_result,
    ]

    # --- Build cross-pass overlap map ---
    # A file is overlapping when its current_path appears in more than one plan's entries.
    # Keyed on current_path; value is the list of pass names that include the file.
    path_to_passes: dict[str, list[str]] = {}
    for plan in plans:
        for entry in plan.entries:
            if entry.current_path not in path_to_passes:
                path_to_passes[entry.current_path] = []
            if plan.pass_name not in path_to_passes[entry.current_path]:
                path_to_passes[entry.current_path].append(plan.pass_name)

    overlap_paths: set[str] = {p for p, names in path_to_passes.items() if len(names) > 1}
    overlaps: list[PreflightOverlapEntry] = [
        PreflightOverlapEntry(current_path=p, pass_names=path_to_passes[p]) for p in sorted(overlap_paths)
    ]

    # --- Build per-pass summaries ---
    pass_summaries: list[PreflightPassSummary] = []
    for plan in plans:
        overlap_count = sum(1 for e in plan.entries if e.current_path in overlap_paths)
        pass_summaries.append(
            PreflightPassSummary(
                pass_name=plan.pass_name,
                count=plan.count,
                overlap_count=overlap_count,
            )
        )

    # --- Journal capacity ---
    capacity = _journal_capacity(journal_path, plans)

    # --- Reference/ evidence ---
    ref_evidence = _reference_evidence(dest_root)

    log.info(
        "compose_preflight_report_complete",
        dest_root=str(dest_root),
        total_planned=sum(p.count for p in plans),
        overlap_files=len(overlaps),
    )
    return PreflightReport(
        pass_summaries=pass_summaries,
        overlaps=overlaps,
        journal_capacity=capacity,
        reference_evidence=ref_evidence,
        scan_ran=True,
    )
