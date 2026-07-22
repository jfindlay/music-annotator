"""High-level read-only audit operations for the music-annotator library.

Provides functions for auditing the transaction journal, diffing the on-disk journal against a
freshly-rebuilt in-memory cache, and detecting fragmented releases.  All functions in this module
are read-only: they do not move files or write any journal entries.

The three identity-integrity passes (journal scan, tag adjudication, audio anchor confirmation)
are implemented as private helpers and called by :func:`audit`.  The tier-enumeration pass
(:func:`_audit_tier_pass`) reads ``annotation_tier`` from each work directory's provenance sidecar
and counts per-tier, provisional, and spot-check populations.  The fragmentation-detection
helpers (:func:`_journal_fragmentation_groups`, :func:`_confirm_fragmentation`) are also private
and called by :func:`audit` and :func:`detect_fragmented_releases`.
"""
# pylint: disable=duplicate-code  # journal-entry iteration and library-walk patterns are
# structurally identical to _pipeline_io.py; the duplication is inherent to the module split.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from music_annotator._pipeline_io import (
    _REBUILD_AUDIO_EXTENSIONS,
    JOURNAL_FILENAME,
    PROVENANCE_FILENAME,
    _audio_hash,
    _find_freedb_sidecar,
    _read_acoustid_tag,
    _read_albumid_tag,
    _read_audio_hash_tag,
    _read_provenance_sidecar,
    read_journal,
    rebuild_journal,
)
from music_annotator._tags import _work_dir_component, _work_top_dir
from music_annotator.models import AccurateRipSummary, AnnotationTier, TransactionEntry, TransactionLog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Fields on :class:`~music_annotator.models.TransactionEntry` that ``diff_journal`` compares
#: field-by-field between the on-disk journal and the in-memory rebuild.  ``source`` is excluded
#: because rebuild sets ``source == destination`` (offline from the original rip path) and the
#: journal's ``source`` is the original rip path — a provenance difference, not an authority leak.
#: ``timestamp`` is excluded because mtime drifts on filesystem operations and is not an authority
#: field.  ``action`` is excluded because rebuild always emits ``"tagged"`` or ``"sidecar"``
#: regardless of the original action.
_DIFF_FIELDS: tuple[str, ...] = ("release_id", "audio_hash", "chromaprint_fp", "acoustid_id", "origin_time")

#: Keys used in the :func:`_make_audit_counts` counter dict, one per finding category.
_AUDIT_COUNT_KEYS: tuple[str, ...] = (
    "total",
    "needs_enrich",
    "acoustid_missing",
    "acoustid_journal_mismatch",
    "audio_hash_tag_mismatch",
    "audio_drift",
    "audio_stable",
    "file_missing",
    # Tier-enumeration pass (Pass 4) — counts are per destination file; a work_dir's tier
    # applies to all its tracks (sidecar-per-work-dir vs entry-per-file aggregation).
    "tier_full",
    "tier_search",
    "tier_partial",
    "tier_alt",
    "tier_source_only",
    "provisional_total",
    "needs_spot_check",
)


@dataclass
class JournalDiffResult:
    """Result of :func:`diff_journal`: three buckets of destination paths.

    Produced by :func:`diff_journal` and consumed by the ``audit --diff`` CLI mode.

    :ivar matches: Destination paths present in both the on-disk journal and the in-memory rebuild
        with all :data:`_DIFF_FIELDS` matching.
    :ivar stale: Destination paths present in the on-disk journal but absent from the in-memory
        rebuild.  Expected after a ``repath`` or ``regroup`` operation moves files to new paths;
        the journal retains the old path until the next ``rebuild --write`` run.
    :ivar leaked: Destination paths present in both the on-disk journal and the in-memory rebuild
        but with at least one :data:`_DIFF_FIELDS` value differing.  Not expected in a healthy
        library; indicates a real authority leak (a field value in the journal that cannot be
        reproduced from the tracks and sidecars alone).
    """

    matches: list[str]
    stale: list[str]
    leaked: list[tuple[str, dict[str, tuple[str, str]]]]
    """Each entry is ``(destination, {field: (journal_value, rebuild_value)})``."""


def _make_audit_counts() -> dict[str, int]:
    """Return a zeroed counter dict for all audit passes.

    Keys correspond to :data:`_AUDIT_COUNT_KEYS`:

    * ``total`` — unique destination paths from eligible journal entries.
    * ``needs_enrich`` — files with an empty ``audio_hash`` in the journal entry.
    * ``acoustid_missing`` — files with an empty ``acoustid_id`` in the journal entry.
    * ``acoustid_journal_mismatch`` — journal ``acoustid_id`` differs from the tag.
    * ``audio_hash_tag_mismatch`` — journal ``audio_hash`` differs from the tag.
    * ``audio_drift`` — recomputed ``audio_hash`` differs from the stored tag.
    * ``audio_stable`` — recomputed ``audio_hash`` matches the stored tag.
    * ``file_missing`` — destination file no longer exists on disk.
    * ``tier_full`` — destination files whose work_dir sidecar carries ``full-mb-verified``.
    * ``tier_search`` — destination files whose work_dir sidecar carries ``mb-search-resolved``.
    * ``tier_partial`` — destination files whose work_dir sidecar carries ``mb-partial``.
    * ``tier_alt`` — destination files whose work_dir sidecar carries ``alternate-source``.
    * ``tier_source_only`` — destination files whose work_dir sidecar carries ``source-tags-only``.
    * ``provisional_total`` — destination files below ``full-mb-verified`` (all non-full tiers).
    * ``needs_spot_check`` — destination files whose work_dir sidecar has ``needs_spot_check=True``.

    :returns: A ``dict[str, int]`` with all keys initialised to ``0``.
    """
    return dict.fromkeys(_AUDIT_COUNT_KEYS, 0)


def _audit_journal_scan(
    entries: list[TransactionEntry],
    counts: dict[str, int],
) -> None:
    """Pass 1 — journal scan: flag entries with empty ``audio_hash`` or ``acoustid_id`` fields.

    Iterates eligible journal entries (``action`` in ``{"tagged", "enriched"}``).  For each entry
    whose destination path is unique (first occurrence wins), logs one event per finding:

    * ``audit_needs_enrich`` — ``audio_hash`` is empty in the journal entry.
    * ``audit_acoustid_missing`` — ``acoustid_id`` is empty in the journal entry.

    Increments ``counts["total"]`` for each unique destination processed and the corresponding
    per-finding counters.  This pass performs no file I/O.

    :param entries: All :class:`~music_annotator.models.TransactionEntry` objects from the journal.
    :param counts: Mutable counter dict from :func:`_make_audit_counts`, updated in place.
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.action not in {"tagged", "enriched"}:
            continue
        dest = entry.destination
        if dest in seen:
            continue
        seen.add(dest)
        counts["total"] += 1

        if not entry.audio_hash:
            counts["needs_enrich"] += 1
            log.info(
                "audit_needs_enrich",
                path=dest,
                message="audio_hash empty in journal — run 'audit --enrich' to backfill",
            )

        if not entry.acoustid_id:
            counts["acoustid_missing"] += 1
            log.info("audit_acoustid_missing", path=dest, message="acoustid_id empty in journal entry")


def _audit_tag_adjudication(
    entries: list[TransactionEntry],
    counts: dict[str, int],
) -> None:
    """Pass 2 — tag adjudication: compare journal identity fields against on-disk tags.

    For each eligible journal entry (``action`` in ``{"tagged", "enriched"}``), reads the
    ``ACOUSTID_ID`` and ``audio_hash`` tags from the destination file and compares them to the
    journal's stored values.  Logs one event per mismatch:

    * ``audit_file_missing`` — destination file does not exist on disk (skipped gracefully).
    * ``audit_acoustid_journal_mismatch`` — journal ``acoustid_id`` differs from the tag value
      (and neither is empty).
    * ``audit_audio_hash_tag_mismatch`` — journal ``audio_hash`` differs from the tag value
      (and neither is empty).

    Only the most-recent journal entry per destination path is adjudicated (first occurrence in
    reverse-chronological order, i.e. the last entry in the list).

    :param entries: All :class:`~music_annotator.models.TransactionEntry` objects from the journal.
    :param counts: Mutable counter dict from :func:`_make_audit_counts`, updated in place.
    """
    # Build a mapping from destination → most-recent eligible entry (last write wins).
    latest: dict[str, TransactionEntry] = {}
    for entry in entries:
        if entry.action in {"tagged", "enriched"}:
            latest[entry.destination] = entry

    for dest, entry in latest.items():
        path = Path(dest)
        if not path.exists():
            counts["file_missing"] += 1
            log.warning("audit_file_missing", path=dest, message="destination file no longer exists on disk")
            continue

        tag_acoustid = _read_acoustid_tag(path)
        if entry.acoustid_id and tag_acoustid and entry.acoustid_id != tag_acoustid:
            counts["acoustid_journal_mismatch"] += 1
            log.warning(
                "audit_acoustid_journal_mismatch",
                path=dest,
                journal_acoustid=entry.acoustid_id,
                tag_acoustid=tag_acoustid,
                message="journal acoustid_id differs from embedded tag",
            )

        tag_audio_hash = _read_audio_hash_tag(path)
        if entry.audio_hash and tag_audio_hash and entry.audio_hash != tag_audio_hash:
            counts["audio_hash_tag_mismatch"] += 1
            log.warning(
                "audit_audio_hash_tag_mismatch",
                path=dest,
                journal_hash=entry.audio_hash,
                tag_hash=tag_audio_hash,
                message="journal audio_hash differs from embedded tag — tag was changed after journal was written",
            )


def _audit_audio_anchor(
    entries: list[TransactionEntry],
    counts: dict[str, int],
) -> None:
    """Pass 3 — audio anchor confirmation: recompute ``audio_hash`` and compare to the stored tag.

    For each eligible journal entry (``action`` in ``{"tagged", "enriched"}``), reads the
    ``audio_hash`` tag from the destination file, recomputes the hash via :func:`_audio_hash`,
    and compares the two values.  Logs one event per finding:

    * ``audit_file_missing`` — destination file does not exist (already counted in pass 2; skipped
      here to avoid double-counting).
    * ``audit_needs_enrich`` — stored tag is empty (no anchor yet; already counted in pass 1 if
      the journal was also empty; counted here only when the tag is empty but the journal is not).
    * ``audit_audio_drift`` — recomputed hash differs from the stored tag (audio content changed).
    * ``audit_audio_stable`` — recomputed hash matches the stored tag (anchor confirmed; logged at
      DEBUG level).

    Only the most-recent journal entry per destination path is processed.

    :param entries: All :class:`~music_annotator.models.TransactionEntry` objects from the journal.
    :param counts: Mutable counter dict from :func:`_make_audit_counts`, updated in place.
    """
    latest: dict[str, TransactionEntry] = {}
    for entry in entries:
        if entry.action in {"tagged", "enriched"}:
            latest[entry.destination] = entry

    for dest, entry in latest.items():
        path = Path(dest)
        if not path.exists():
            # Already counted and logged in pass 2; skip silently here.
            continue

        stored_hash = _read_audio_hash_tag(path)
        if not stored_hash:
            # Only flag as needs_enrich here when the journal also lacks the hash (pass 1 already
            # flagged the journal-empty case); if the journal has a hash but the tag is empty, that
            # is an audio_hash_tag_mismatch (pass 2) — not a separate needs_enrich event.
            if not entry.audio_hash:
                # Already counted in pass 1; log at debug level only to avoid duplicate warnings.
                log.debug("audit_needs_enrich_tag_empty", path=dest, message="audio_hash tag empty — anchor not yet written")
            continue

        recomputed = _audio_hash(path)
        if not recomputed:
            # Unsupported format or read error — cannot confirm anchor; skip silently.
            continue

        if recomputed != stored_hash:
            counts["audio_drift"] += 1
            log.warning(
                "audit_audio_drift",
                path=dest,
                stored_hash=stored_hash,
                recomputed_hash=recomputed,
                message="recomputed audio_hash differs from stored tag — audio content has changed (re-rip or replacement)",
            )
        else:
            counts["audio_stable"] += 1
            log.debug("audit_audio_stable", path=dest, audio_hash=stored_hash, message="audio anchor confirmed")


def _audit_tier_pass(
    dest_root: Path,
    entries: list[TransactionEntry],
    counts: dict[str, int],
) -> None:
    """Pass 4 — tier enumeration: count per-tier, provisional, and spot-check populations.

    Reads ``annotation_tier`` and ``needs_spot_check`` from each eligible work directory's
    provenance sidecar and aggregates counts per destination file.  A work directory's tier
    applies to all its tracks (sidecar-per-work-dir vs entry-per-file aggregation).

    The eligible set is the same as passes 1–3: ``action in {"tagged", "enriched"}``, deduplicated
    by destination path (first occurrence wins).  This keeps the tier denominator consistent with
    ``counts["total"]`` so per-tier counts reconcile against the same base.

    For each eligible destination, the work_top_dir is derived via
    :func:`~music_annotator._tags._work_top_dir`, which handles both legacy two-level and
    class-prefixed three-level paths (C-CLASS).  The sidecar is resolved by
    :func:`~music_annotator._pipeline_io._find_freedb_sidecar` (``freedb_disc_*.yaml``) with
    fallback to :data:`~music_annotator._pipeline_io.PROVENANCE_FILENAME`.  Destinations whose
    path cannot be made relative to ``dest_root`` or whose relative path has fewer than two parts
    are silently skipped (same guard as the fragmentation passes).

    Logs one event per finding:

    * ``audit_tier_unset`` — sidecar exists but ``annotation_tier`` is empty (defect state per
      the lossless principle; S3 write path must always set it).
    * ``audit_tier_full`` — ``full-mb-verified`` (logged at DEBUG; expected clean state).
      Includes ``origin_source`` from the sidecar so an operator can see the identity basis:
      ``"download"`` for ISRC-promoted entries, ``"whipper"`` for TOC-promoted entries, ``""``
      for embedded-MBID entries.
    * ``audit_tier_provisional`` — any below-``full-mb-verified`` tier (logged at INFO).
    * ``audit_tier_needs_spot_check`` — ``needs_spot_check=True`` (logged at INFO), with
      AccurateRip status attached: ``ar_verified`` (``True`` when ``log_sha256`` is non-empty),
      ``accurately_ripped``, and ``in_ar_database`` counts from the sidecar's
      ``accuraterip_summary``.  This allows a rip that is AccurateRip-verified but only
      search-resolved to be visibly distinguished from one with no AR data.

    :param dest_root: Root of the annotated music library.
    :param entries: All :class:`~music_annotator.models.TransactionEntry` objects from the journal.
    :param counts: Mutable counter dict from :func:`_make_audit_counts`, updated in place.
    """
    # Build a deduplicated set of (destination, work_top_dir) pairs from eligible entries.
    # First occurrence per destination wins (same dedup rule as pass 1).
    seen: set[str] = set()
    dest_to_work_top: dict[str, Path] = {}
    for entry in entries:
        if entry.action not in {"tagged", "enriched"}:
            continue
        dest = entry.destination
        if dest in seen:
            continue
        seen.add(dest)
        try:
            rel = Path(dest).relative_to(dest_root)
        except ValueError:
            continue
        if len(rel.parts) < 2:  # noqa: PLR2004 — structural constant (min 2 parts for work_top_dir)
            continue
        dest_to_work_top[dest] = _work_top_dir(Path(dest), dest_root)

    # Cache sidecar reads per work_top_dir to avoid re-reading for multi-track work dirs.
    # Tuple: (annotation_tier, needs_spot_check, accuraterip_summary, origin_source) — the AR
    # summary is attached to the spot-check log event so AR-verified search-resolved entries are
    # visibly distinguished from those with no AR data (J1 spot-check gate, S5).  origin_source
    # is included in the audit_tier_full event so an operator can see the identity basis (e.g.
    # "download" for ISRC-promoted entries, "whipper" for TOC-promoted entries).
    sidecar_cache: dict[Path, tuple[AnnotationTier | str, bool, AccurateRipSummary, str]] = {}

    for dest, work_top_dir in dest_to_work_top.items():
        if work_top_dir not in sidecar_cache:
            sidecar_path = _find_freedb_sidecar(work_top_dir)
            if sidecar_path is None:
                sidecar_path = work_top_dir / PROVENANCE_FILENAME
            sidecar = _read_provenance_sidecar(sidecar_path)
            sidecar_cache[work_top_dir] = (
                sidecar.annotation_tier,
                sidecar.needs_spot_check,
                sidecar.accuraterip_summary,
                sidecar.origin_source,
            )

        tier_raw, spot_check, ar_summary, origin_source = sidecar_cache[work_top_dir]

        if not tier_raw:
            log.warning(
                "audit_tier_unset",
                path=dest,
                message="annotation_tier is unset in sidecar — S3 write path must always set it",
            )
            continue

        try:
            tier = AnnotationTier(str(tier_raw))
        except ValueError:
            log.warning(
                "audit_tier_unset",
                path=dest,
                tier_raw=str(tier_raw),
                message="annotation_tier value in sidecar is not a recognised AnnotationTier",
            )
            continue

        match tier:
            case AnnotationTier.FULL_MB_VERIFIED:
                counts["tier_full"] += 1
                log.debug("audit_tier_full", path=dest, tier=str(tier), origin_source=origin_source)
            case AnnotationTier.MB_SEARCH_RESOLVED:
                counts["tier_search"] += 1
                counts["provisional_total"] += 1
                log.info(
                    "audit_tier_provisional",
                    path=dest,
                    tier=str(tier),
                    message="below full-mb-verified — upgrade candidate",
                )
            case AnnotationTier.MB_PARTIAL:
                counts["tier_partial"] += 1
                counts["provisional_total"] += 1
                log.info(
                    "audit_tier_provisional",
                    path=dest,
                    tier=str(tier),
                    message="below full-mb-verified — upgrade candidate",
                )
            case AnnotationTier.ALTERNATE_SOURCE:
                counts["tier_alt"] += 1
                counts["provisional_total"] += 1
                log.info(
                    "audit_tier_provisional",
                    path=dest,
                    tier=str(tier),
                    message="below full-mb-verified — upgrade candidate",
                )
            case AnnotationTier.SOURCE_TAGS_ONLY:
                counts["tier_source_only"] += 1
                counts["provisional_total"] += 1
                log.info(
                    "audit_tier_provisional",
                    path=dest,
                    tier=str(tier),
                    message="below full-mb-verified — upgrade candidate",
                )
            case _:  # pragma: no cover
                pass

        if spot_check:
            counts["needs_spot_check"] += 1
            log.info(
                "audit_tier_needs_spot_check",
                path=dest,
                tier=str(tier),
                ar_verified=bool(ar_summary.log_sha256),
                accurately_ripped=ar_summary.accurately_ripped,
                in_ar_database=ar_summary.in_ar_database,
                message="mb-search-resolved entry awaiting human spot-check",
            )


def _journal_fragmentation_groups(
    dest_root: Path,
    journal: TransactionLog,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Derive work-dir → release-id and release-id → work-dir groupings from ``action == "tagged"`` journal entries.

    Iterates only ``action == "tagged"`` entries.  For each entry, the ``work_dir`` component is extracted via
    :func:`~music_annotator._tags._work_dir_component`, which handles both legacy two-level paths
    (``parts[1]`` in ``<top_dir>/<work_dir>/…``) and class-prefixed three-level paths
    (``parts[2]`` in ``<class>/<top_dir>/<work_dir>/…`` introduced by C-CLASS).

    Entries whose ``destination`` is not under ``dest_root`` or whose relative path has fewer than two parts are
    silently skipped: they represent malformed or foreign journal entries that cannot be safely attributed to a
    work directory.

    Groupings are returned sorted for deterministic output.

    :param dest_root: Root of the annotated music library.
    :param journal: :class:`~music_annotator.models.TransactionLog` to analyse.
    :returns: A pair ``(work_dir_to_release_ids, release_id_to_work_dirs)`` where each value is a sorted
        list of unique identifiers.
    """
    work_dir_to_release_ids: dict[str, set[str]] = {}
    release_id_to_work_dirs: dict[str, set[str]] = {}

    for entry in journal.entries:
        if entry.action != "tagged":
            continue
        try:
            rel = Path(entry.destination).relative_to(dest_root)
        except ValueError:
            continue
        if len(rel.parts) < 2:  # noqa: PLR2004 — min 2 parts required for work_dir extraction
            continue
        work_dir = _work_dir_component(rel.parts)
        release_id = entry.release_id
        work_dir_to_release_ids.setdefault(work_dir, set()).add(release_id)
        release_id_to_work_dirs.setdefault(release_id, set()).add(work_dir)

    return (
        {k: sorted(v) for k, v in sorted(work_dir_to_release_ids.items())},
        {k: sorted(v) for k, v in sorted(release_id_to_work_dirs.items())},
    )


def _confirm_fragmentation(
    dest_root: Path,
    journal: TransactionLog,
) -> tuple[dict[str, tuple[list[str], bool]], dict[str, tuple[list[str], bool]]]:
    """Adjudicate each fragmentation candidate by reading ``MUSICBRAINZ_ALBUMID`` from destination files.

    Extends the groupings from :func:`_journal_fragmentation_groups` with present-state tag evidence:
    for every journal entry backing a candidate, :func:`_read_albumid_tag` is called on
    ``entry.destination`` and the result is compared to ``entry.release_id``.  A candidate is
    **CONFIRMED** (real present-state fragmentation) when at least one backing entry's embedded tag
    matches the journal's ``release_id``.  A candidate is **STALE** when every backing entry's tag is
    absent, differs, or the file is missing/unreadable — meaning the present state no longer backs
    the journal's claim.

    Only candidates that exhibit fragmentation (case-a: more than one release_id for a work_dir;
    case-b: more than one work_dir for a release_id) are returned.  Clean work_dirs and release_ids
    are omitted.

    :param dest_root: Root of the annotated music library.
    :param journal: :class:`~music_annotator.models.TransactionLog` to analyse.
    :returns: A pair ``(case_a, case_b)`` where:

        * ``case_a`` maps ``work_dir → (release_ids, confirmed)`` for work_dirs with >1 release_id.
        * ``case_b`` maps ``release_id → (work_dirs, confirmed)`` for release_ids with >1 work_dir.

        The ``confirmed`` bool is ``True`` when at least one backing entry's embedded
        ``MUSICBRAINZ_ALBUMID`` tag matches the journal's ``release_id``.
    """
    work_dir_to_ids, release_id_to_dirs = _journal_fragmentation_groups(dest_root, journal)

    # Build a per-(work_dir, release_id) and per-(release_id, work_dir) lookup of entries so we
    # can retrieve the destination files backing each candidate without a second full scan.
    wd_rid_to_dests: dict[tuple[str, str], list[str]] = {}
    rid_wd_to_dests: dict[tuple[str, str], list[str]] = {}
    for entry in journal.entries:
        if entry.action != "tagged":
            continue
        try:
            rel = Path(entry.destination).relative_to(dest_root)
        except ValueError:
            continue
        if len(rel.parts) < 2:  # noqa: PLR2004 — min 2 parts required for work_dir extraction
            continue
        work_dir = _work_dir_component(rel.parts)
        release_id = entry.release_id
        wd_rid_to_dests.setdefault((work_dir, release_id), []).append(entry.destination)
        rid_wd_to_dests.setdefault((release_id, work_dir), []).append(entry.destination)

    def _is_confirmed(dests: list[str], expected_release_id: str) -> bool:
        """Return True if any destination file's MUSICBRAINZ_ALBUMID matches expected_release_id."""
        for dest in dests:
            if _read_albumid_tag(Path(dest)) == expected_release_id:
                return True
        return False

    case_a: dict[str, tuple[list[str], bool]] = {}
    for work_dir, release_ids in work_dir_to_ids.items():
        if len(release_ids) <= 1:
            continue
        confirmed = any(_is_confirmed(wd_rid_to_dests.get((work_dir, rid), []), rid) for rid in release_ids)
        case_a[work_dir] = (release_ids, confirmed)

    case_b: dict[str, tuple[list[str], bool]] = {}
    for release_id, work_dirs in release_id_to_dirs.items():
        if len(work_dirs) <= 1:
            continue
        confirmed = any(_is_confirmed(rid_wd_to_dests.get((release_id, wd), []), release_id) for wd in work_dirs)
        case_b[release_id] = (work_dirs, confirmed)

    return case_a, case_b


def audit(dest_root: Path) -> None:
    """Read the journal at ``dest_root`` and report release-fragmentation anomalies and identity integrity findings.

    Reads :data:`~music_annotator._pipeline_io.JOURNAL_FILENAME` from ``dest_root`` and analyses
    ``action == "tagged"`` entries to surface two fragmentation shapes:

    * **Case (a) — regrouping candidate:** one ``work_dir`` (the second path component under ``dest_root``)
      is populated from more than one MusicBrainz release MBID.  This indicates that the same work
      directory was tagged from multiple distinct releases and may need regrouping.
    * **Case (b) — split release:** one release MBID has tracks landing in more than one ``work_dir``.
      This indicates that a single release's tracks are spread across multiple work directories.

    Each candidate is further adjudicated by reading ``MUSICBRAINZ_ALBUMID`` from the destination
    files (via :func:`_confirm_fragmentation`): a candidate is **confirmed** when the embedded tag
    on at least one backing file agrees with the journal's ``release_id``, indicating real
    present-state fragmentation.  A candidate is **stale** when every backing file's tag is absent,
    differs, or the file cannot be read — indicating the journal no longer reflects present state.

    When neither shape is detected a clean "no fragmentation detected" message is logged.

    In addition, four passes are run over all ``action == "tagged"`` and ``action == "enriched"``
    journal entries:

    * **Pass 1 — journal scan:** flags entries with empty ``audio_hash`` or ``acoustid_id`` fields.
    * **Pass 2 — tag adjudication:** reads on-disk tags and compares them to the journal's stored
      identity fields, flagging mismatches.
    * **Pass 3 — audio anchor confirmation:** recomputes ``audio_hash`` from the file's decoded
      audio content and compares it to the stored tag, flagging drift (audio content changed).
    * **Pass 4 — tier enumeration:** reads ``annotation_tier`` from each work directory's
      provenance sidecar and counts per-tier, provisional, and spot-check populations.

    A summary of finding counts is logged at the end.

    This function is **read-only**: it does not move files or write any journal entries.

    :param dest_root: Root of the annotated music library (contains ``music_annotator_journal.json``).
    """
    journal = read_journal(dest_root / JOURNAL_FILENAME)
    case_a, case_b = _confirm_fragmentation(dest_root, journal)

    if not case_a and not case_b:
        log.info("audit_clean", dest_root=str(dest_root), message="no fragmentation detected")

    for work_dir, (release_ids, confirmed) in sorted(case_a.items()):
        log.warning(
            "audit_multiple_release_ids",
            work_dir=work_dir,
            release_ids=release_ids,
            confirmed=confirmed,
            message=(
                "one work_dir has multiple release_ids (regrouping candidate — tag-confirmed)"
                if confirmed
                else "one work_dir has multiple release_ids (regrouping candidate — journal stale)"
            ),
        )

    for release_id, (work_dirs, confirmed) in sorted(case_b.items()):
        log.warning(
            "audit_split_release",
            release_id=release_id,
            work_dirs=work_dirs,
            confirmed=confirmed,
            message=(
                "one release_id maps to multiple work_dirs (split release — tag-confirmed)"
                if confirmed
                else "one release_id maps to multiple work_dirs (split release — journal stale)"
            ),
        )

    counts = _make_audit_counts()
    _audit_journal_scan(journal.entries, counts)
    _audit_tag_adjudication(journal.entries, counts)
    _audit_audio_anchor(journal.entries, counts)
    _audit_tier_pass(dest_root, journal.entries, counts)

    log.info(
        "audit_summary",
        dest_root=str(dest_root),
        total_scanned=counts["total"],
        needs_enrich=counts["needs_enrich"],
        acoustid_missing=counts["acoustid_missing"],
        acoustid_journal_mismatch=counts["acoustid_journal_mismatch"],
        audio_hash_tag_mismatch=counts["audio_hash_tag_mismatch"],
        audio_drift=counts["audio_drift"],
        audio_stable=counts["audio_stable"],
        file_missing=counts["file_missing"],
        tier_full=counts["tier_full"],
        tier_search=counts["tier_search"],
        tier_partial=counts["tier_partial"],
        tier_alt=counts["tier_alt"],
        tier_source_only=counts["tier_source_only"],
        provisional_total=counts["provisional_total"],
        needs_spot_check=counts["needs_spot_check"],
    )


def detect_fragmented_releases(dest_root: Path) -> dict[str, list[Path]]:
    """Scan ``dest_root`` for releases fragmented across ≥2 distinct top_dirs.

    Walks the two-level ``<top_dir>/<work_dir>/`` library structure under ``dest_root``, reads the
    ``MUSICBRAINZ_ALBUMID`` tag from every FLAC and MP3 file, and groups the files by release MBID.
    A release is **fragmented** when its files are spread across ≥2 distinct top_dirs (the first
    path component under ``dest_root``).

    The join key is the embedded tag, not the journal (C-W2 contract).  Files whose tag cannot be
    read or is empty are silently skipped.

    :param dest_root: Root of the annotated music library.
    :returns: A mapping from release MBID to a sorted list of all audio file paths belonging to
        that release, for every release with ≥2 distinct top_dirs.  Releases whose files all share
        the same top_dir are omitted.
    """
    # release_id -> list of (top_dir_name, file_path)
    release_files: dict[str, list[tuple[str, Path]]] = {}

    if not dest_root.is_dir():
        return {}

    for top_dir in sorted(dest_root.iterdir()):
        if not top_dir.is_dir():
            continue
        for work_dir in sorted(top_dir.iterdir()):
            if not work_dir.is_dir():
                continue
            for file_path in sorted(work_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() not in _REBUILD_AUDIO_EXTENSIONS:
                    continue
                release_id = _read_albumid_tag(file_path)
                if not release_id:
                    continue
                release_files.setdefault(release_id, []).append((top_dir.name, file_path))

    result: dict[str, list[Path]] = {}
    for release_id, entries in release_files.items():
        top_dirs = {td for td, _ in entries}
        if len(top_dirs) >= 2:  # noqa: PLR2004 — 2 is the fragmentation threshold (C-W2)
            result[release_id] = sorted(fp for _, fp in entries)

    return result


def diff_journal(dest_root: Path) -> JournalDiffResult:
    """Diff the on-disk journal against a freshly-rebuilt in-memory cache, field by field per destination path.

    Calls :func:`~music_annotator._pipeline_io.rebuild_journal` with ``dry_run=True`` to produce an
    in-memory rebuild, then reads the on-disk journal via
    :func:`~music_annotator._pipeline_io.read_journal`.  For each ``action == "tagged"`` entry in the
    on-disk journal, the destination path is looked up in the rebuild and the :data:`_DIFF_FIELDS`
    are compared.

    Three buckets are returned:

    * **matches** — destination path present in both, all :data:`_DIFF_FIELDS` match.
    * **stale** — destination path present in the on-disk journal but absent from the rebuild.
      Expected after a ``repath``/``regroup`` operation; the journal retains the old path until
      the next ``rebuild --write`` run.
    * **leaked** — destination path present in both but at least one :data:`_DIFF_FIELDS` value
      differs.  Not expected in a healthy library; surfaces a real authority leak.

    A summary line is logged at INFO level: ``"N matches, N stale (expected after repath/regroup), N leaked"``.
    Any ``leaked`` entries are also logged individually at WARNING level.

    This function is **read-only**: it does not move files or write any journal entries.

    :param dest_root: Root of the annotated music library (contains ``music_annotator_journal.json``).
    :returns: A :class:`JournalDiffResult` with the three buckets populated.
    """
    journal = read_journal(dest_root / JOURNAL_FILENAME)
    rebuilt = rebuild_journal(dest_root, dry_run=True)

    # Build a lookup: destination path → most-recent "tagged" entry from the rebuild.
    # rebuild_journal always emits action="tagged" for audio files; use the last entry per dest.
    rebuild_map: dict[str, TransactionEntry] = {}
    for entry in rebuilt.entries:
        if entry.action == "tagged":
            rebuild_map[entry.destination] = entry

    # Iterate the on-disk journal: latest "tagged" entry per destination path wins.
    journal_latest: dict[str, TransactionEntry] = {}
    for entry in journal.entries:
        if entry.action == "tagged":
            journal_latest[entry.destination] = entry

    matches: list[str] = []
    stale: list[str] = []
    leaked: list[tuple[str, dict[str, tuple[str, str]]]] = []

    for dest, j_entry in sorted(journal_latest.items()):
        r_entry = rebuild_map.get(dest)
        if r_entry is None:
            stale.append(dest)
            continue

        diffs: dict[str, tuple[str, str]] = {}
        for field in _DIFF_FIELDS:
            j_val: str = getattr(j_entry, field)
            r_val: str = getattr(r_entry, field)
            # Only flag as leaked when the journal carries a non-empty value that rebuild cannot
            # reproduce.  An empty journal field means the entry predates enrichment — not a leak.
            if j_val and j_val != r_val:
                diffs[field] = (j_val, r_val)

        if diffs:
            leaked.append((dest, diffs))
            log.warning(
                "audit_diff_leaked",
                destination=dest,
                diffs={f: {"journal": jv, "rebuild": rv} for f, (jv, rv) in diffs.items()},
                message="journal has field value not reproducible by rebuild — authority leak",
            )
        else:
            matches.append(dest)

    log.info(
        "audit_diff_summary",
        dest_root=str(dest_root),
        matches=len(matches),
        stale=len(stale),
        leaked=len(leaked),
        message=f"{len(matches)} matches, {len(stale)} stale (expected after repath/regroup), {len(leaked)} leaked",
    )

    return JournalDiffResult(matches=matches, stale=stale, leaked=leaked)
