"""Filesystem I/O helpers for the music-annotator pipeline.

Provides functions for finding source audio files, writing the transaction journal, computing SHA-256
checksums, reading back tags for verification, verifying copy integrity after tagging, assessing
audio-content similarity for collision resolution, and performing the pre-flight duration check
against MB track lengths.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml
from mutagen import File as MutagenFile  # type: ignore[attr-defined]
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from music_annotator._tagger import _MP3_STD_KEYS, _MP3_TXXX_MAP
from music_annotator._tags import _CLASS_VOCAB, _work_top_dir
from music_annotator.models import (
    JSON,
    AccurateRipResult,
    AccurateRipSummary,
    AccurateRipTrack,
    AccurateRipTrackResult,
    AnnotationTier,
    CoverArt,
    MBTrack,
    PictureEntry,
    ProvenanceSidecar,
    TrackTags,
    TransactionEntry,
    TransactionLog,
    annotation_tier_rank,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Supported audio file extensions.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3", ".ogg", ".m4a", ".aac", ".wav"})

#: Filename of the JSON transaction journal written inside the destination root.
#: The file is stored in append-only JSON Lines (JSONL) format: one JSON object per line.
#: A legacy JSON-array journal at this path is automatically migrated on first read.
JOURNAL_FILENAME: str = "music_annotator_journal.json"

#: Suffix appended to the original JSON-array journal file when it is migrated to JSONL format.
#: The backup is created once and never deleted by the tool.
JOURNAL_BACKUP_SUFFIX: str = ".array-backup"

#: CD table-of-contents audio file written by some rippers alongside the real tracks.  It has a
#: ``.flac`` extension and would otherwise be picked up as a source track.
_DISC_TOC_FILENAME: str = "00 - disc TOC.flac"

#: FreeDB disc-info YAML file written alongside ripped tracks by some rippers.
_DISC_INFO_FILENAME: str = "00 - disc info.yaml"

#: Set of filenames that must never be treated as source audio tracks.
_EXCLUDED_FILENAMES: frozenset[str] = frozenset({_DISC_TOC_FILENAME, _DISC_INFO_FILENAME})

#: Duration tolerance (in milliseconds) used by compare_audio_collision when falling back to
#: duration comparison.  Two tracks whose durations differ by more than this value are reported
#: as a definite non-match.
_DURATION_TOLERANCE_MS: int = 2000

#: Whether a one-time warning about the missing ``fpcalc`` binary has already been emitted this
#: process lifetime.  Wrapped in a list to avoid a ``global`` statement; only element 0 is used.
#: Written by :func:`compare_audio_collision`; not thread-safe (acceptable for a single-threaded
#: CLI tool).
_FPCALC_WARNED: list[bool] = [False]

#: Minimum Hamming-distance similarity score for two Chromaprint fingerprints to be considered a
#: match.  AcoustID's own clustering heuristics treat fingerprints from the same recording as
#: having >90% bit similarity, so 0.90 is the appropriate threshold for fuzzy comparison.
#: Similarity is computed as ``1 - (hamming_distance / total_bits)`` over the decoded 32-bit
#: integer arrays.
_CHROMAPRINT_SIMILARITY_THRESHOLD: float = 0.90


@dataclass
class AudioCompareResult:
    """Result of comparing a planned source file against an already-existing destination file.

    Produced by :func:`compare_audio_collision` and aggregated by :func:`_assess_collisions`.
    Consumed by :func:`~music_annotator._pipeline.run` to decide whether a collision should
    trigger a user prompt (identical / inconclusive audio) or an automatic path-disambiguation
    suffix (definitively different audio).

    The ``method`` field is drawn from :data:`_IDENTITY_METHODS` and carries identity-rung results
    (``"audio_hash"``, ``"acoustid"``, ``"chromaprint"``, ``"isrc"``) as well as collision-resolution
    results (``"sha256"``, ``"duration"``, ``"unknown"``).
    """

    src: Path
    dest: Path
    match: bool | None
    """``True`` = same audio content; ``False`` = different audio content; ``None`` = inconclusive."""
    method: str
    """Comparison method used: one of the strings in :data:`_IDENTITY_METHODS`."""
    detail: str
    """One-line human-readable summary for display in the collision prompt."""


#: All valid ``method`` values for :class:`AudioCompareResult`, covering both collision-resolution
#: rungs (``"sha256"``, ``"duration"``, ``"unknown"``), archival-identity rungs
#: (``"acoustid"``, ``"chromaprint"``, ``"audio_hash"``, ``"isrc"``), and the medium-sequence
#: corroboration rung (``"sequence"``).
_IDENTITY_METHODS: frozenset[str] = frozenset(
    {"sha256", "acoustid", "chromaprint", "duration", "unknown", "isrc", "audio_hash", "sequence"}
)


def _audio_hash(path: Path) -> str:
    """Return an algorithm-tagged decoded-audio hash for ``path``, or ``""`` for unsupported formats.

    The hash is tagging-invariant: it reflects only the decoded audio content, not the container
    metadata.  For FLAC files the encoder's native STREAMINFO MD5 is used; for MP3 files the
    SHA-256 of the raw audio-frame bytes (excluding ID3 tags) is computed.

    The returned string has the format ``"<algo>:<hexdigest>"``, e.g.
    ``"flac-md5:00000000000000000000000000000000"`` or
    ``"mp3-stream-sha256:abcdef…"``.

    A FLAC whose STREAMINFO MD5 is all-zero (e.g. a minimal test file with no audio samples)
    yields ``"flac-md5:00000000000000000000000000000000"`` — stored as-is, not special-cased.

    :param path: Path to the audio file to hash.
    :returns: An algorithm-tagged hash string, or ``""`` for unsupported file extensions or on
        any read error.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                md5_int = FLAC(str(path)).info.md5_signature
                return f"flac-md5:{md5_int:032x}"
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                audio_start = id3.size
                raw = path.read_bytes()
                audio_bytes = raw[audio_start:]
                # Strip trailing ID3v1 tag (exactly 128 bytes starting with b"TAG")
                if len(audio_bytes) >= 128 and audio_bytes[-128:-125] == b"TAG":  # noqa: PLR2004
                    audio_bytes = audio_bytes[:-128]
                digest = hashlib.sha256(audio_bytes).hexdigest()
                return f"mp3-stream-sha256:{digest}"
            case _:
                return ""
    except Exception:  # noqa: BLE001 — best-effort; any failure means no hash
        return ""


def _read_acoustid_tag(path: Path) -> str:
    """Read the ``ACOUSTID_ID`` tag from a FLAC or MP3 file and return it, or ``""`` on failure.

    For FLAC files the Vorbis Comment key ``"acoustid_id"`` is looked up (case-insensitive).
    For MP3 files the TXXX frame with description ``"Acoustid Id"`` is looked up.

    :param path: Path to the audio file to inspect.
    :returns: The AcoustID UUID string, or ``""`` if absent or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = FLAC(str(path))
                values = audio.get("acoustid_id") or audio.get("ACOUSTID_ID") or []
                return values[0] if values else ""
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
                    if frame.desc == "Acoustid Id" and frame.text:
                        return str(frame.text[0])
                return ""
            case _:
                return ""
    except Exception:  # noqa: BLE001 — best-effort tag read; any failure means no AcoustID
        return ""


def _read_audio_hash_tag(path: Path) -> str:
    """Read the ``audio_hash`` tag from a FLAC or MP3 file, returning ``""`` on any failure.

    For FLAC files the Vorbis Comment key ``"audio_hash"`` is looked up (case-insensitive).
    For MP3 files the TXXX frame with description ``"Audio Hash"`` is looked up (from
    :data:`~music_annotator._tagger._MP3_TXXX_MAP`: ``"AUDIO_HASH": "Audio Hash"``).

    :param path: Path to the audio file to inspect.
    :returns: The algorithm-tagged audio hash string (e.g. ``"flac-md5:…"``), or ``""`` if absent
        or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = FLAC(str(path))
                values = audio.get("audio_hash") or audio.get("AUDIO_HASH") or []
                return values[0] if values else ""
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
                    if frame.desc == "Audio Hash" and frame.text:
                        return str(frame.text[0])
                return ""
            case _:
                return ""
    except Exception:  # noqa: BLE001 — best-effort tag read; any failure means no audio hash
        return ""


def _read_acoustid_fingerprint_tag(path: Path) -> str:
    """Read the AcoustID fingerprint tag from a FLAC or MP3 file, returning ``""`` on any failure.

    Implements dual-read: the new Picard-aligned key is tried first, then the legacy key, so a
    mixed library (files not yet migrated to the new key) reads correctly throughout the transition.

    For FLAC files the Vorbis Comment keys ``"acoustid_fingerprint"`` / ``"ACOUSTID_FINGERPRINT"``
    are tried first (the Picard-aligned key written by the current forward path), then the legacy
    ``"chromaprint_fp"`` / ``"CHROMAPRINT_FP"``.

    For MP3 files the TXXX frame with description ``"Acoustid Fingerprint"`` is tried first (from
    :data:`~music_annotator._tagger._MP3_TXXX_MAP`: ``"ACOUSTID_FINGERPRINT": "Acoustid Fingerprint"``),
    then the legacy ``"Chromaprint Fingerprint"``.

    :param path: Path to the audio file to inspect.
    :returns: The Chromaprint fingerprint string, or ``""`` if absent or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = FLAC(str(path))
                values = (
                    audio.get("acoustid_fingerprint")
                    or audio.get("ACOUSTID_FINGERPRINT")
                    or audio.get("chromaprint_fp")
                    or audio.get("CHROMAPRINT_FP")
                    or []
                )
                return values[0] if values else ""
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
                    if frame.desc == "Acoustid Fingerprint" and frame.text:
                        return str(frame.text[0])
                for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
                    if frame.desc == "Chromaprint Fingerprint" and frame.text:
                        return str(frame.text[0])
                return ""
            case _:
                return ""
    except Exception:  # noqa: BLE001 — best-effort tag read; any failure means no fingerprint
        return ""


def _needs_enrich(path: Path, re_resolve: bool) -> dict[str, str]:
    """Determine which fingerprint fields need to be written to ``path``.

    Reads the current on-disk tag values and computes which of the three archival-identity fields
    (``audio_hash``, ``acoustid_fingerprint``, ``acoustid_id``) require a write.  Returns a mapping
    of field name → new value for every field that should be updated.

    **Idempotency contract:** a second call on the same file (after the first call's writes have
    been applied) returns an empty dict — no field is written twice unless ``re_resolve=True``
    explicitly requests a re-derivation of ``acoustid_fingerprint``.

    **Anchor rule (P-FP1):** ``audio_hash`` is the tagging-invariant anchor.  Once written it is
    NEVER overwritten, even under ``re_resolve=True``.  This preserves the ability to detect
    bit-for-bit audio identity across re-tags and format conversions.

    Per-field logic:

    * ``"audio_hash"``: if the tag is empty, compute :func:`_audio_hash` and include the result
      when non-empty.  If the tag is already present, skip unconditionally (anchor rule).
    * ``"acoustid_fingerprint"``: if the tag is empty (dual-read: new key first, legacy second),
      compute :func:`_run_fpcalc` and include when non-empty.  If the tag is present and
      ``re_resolve=True``, recompute and include (overwrite).  If the tag is present and
      ``re_resolve=False``, skip.
    * ``"acoustid_id"``: if the tag is present, copy the tag value into the result dict (so the
      journal entry carries the current AcoustID).  If the tag is absent, skip (no network call
      in F4 — logged once as inconclusive).

    :param path: Path to the FLAC or MP3 file to inspect.
    :param re_resolve: When ``True``, recompute ``acoustid_fingerprint`` even when already present.
    :returns: A ``{field_name: new_value}`` dict of fields that need writing, or ``{}`` when the
        file is already fully enriched.
    """
    result: dict[str, str] = {}

    # --- audio_hash: anchor — never overwrite ---
    existing_hash = _read_audio_hash_tag(path)
    if not existing_hash:
        computed_hash = _audio_hash(path)
        if computed_hash:
            result["audio_hash"] = computed_hash

    # --- acoustid_fingerprint: compute when absent; re-compute when re_resolve=True ---
    # Dual-read: new Picard-aligned key first, legacy key second (transition support).
    existing_fp = _read_acoustid_fingerprint_tag(path)
    if not existing_fp:
        computed_fp = _run_fpcalc(path)
        if computed_fp:
            result["acoustid_fingerprint"] = computed_fp
    elif re_resolve:
        computed_fp = _run_fpcalc(path)
        if computed_fp:
            result["acoustid_fingerprint"] = computed_fp

    # --- acoustid_id: copy tag→result when present; skip when absent ---
    existing_acoustid = _read_acoustid_tag(path)
    if existing_acoustid:
        result["acoustid_id"] = existing_acoustid
    else:
        log.info("enrich_acoustid_inconclusive", path=str(path))

    return result


def _read_isrc_tag(path: Path) -> str:
    """Read the ISRC tag from a FLAC or MP3 file and return it, or ``""`` on failure.

    For FLAC files the Vorbis Comment key ``"isrc"`` is looked up (case-insensitive).
    For MP3 files the standard ``TSRC`` frame is read.

    This is a best-effort read: any exception (corrupt file, unsupported format, missing tag) returns
    ``""`` rather than propagating.  The caller is responsible for treating an empty return as
    "no ISRC available" rather than as an error.

    :param path: Path to the audio file to inspect.
    :returns: The ISRC string, or ``""`` if absent or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = FLAC(str(path))
                values = audio.get("isrc") or audio.get("ISRC") or []
                return values[0] if values else ""
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                frame = id3.get("TSRC")  # type: ignore[no-untyped-call]
                if frame and frame.text:
                    return str(frame.text[0])
                return ""
            case _:
                return ""
    except Exception:  # noqa: BLE001 — best-effort tag read; any failure means no ISRC
        return ""


def _isrc_matches(src: Path, isrc_list: list[str]) -> AudioCompareResult:
    """Apply the ISRC identity rung: check whether the ISRC embedded in ``src`` appears in ``isrc_list``.

    This is rung 1 of the archival identity ladder.  When a source file carries an ISRC tag that
    matches any entry in the candidate recording's ``isrc_list``, that is a definitive offline
    identity signal — no network call required.

    A non-empty ``isrc_list`` with no match returns ``match=False``; an empty ``isrc_list`` or an
    unreadable source ISRC tag returns ``match=None`` (inconclusive).

    :param src: Path to the source audio file whose embedded ISRC tag is read.
    :param isrc_list: List of ISRC strings from the candidate :class:`~music_annotator.models.MBRecording`.
    :returns: An :class:`AudioCompareResult` with ``method="isrc"``.
    """
    src_isrc = _read_isrc_tag(src)
    if not src_isrc or not isrc_list:
        return AudioCompareResult(
            src=src,
            dest=src,
            match=None,
            method="isrc",
            detail="no ISRC available for comparison",
        )
    if src_isrc in isrc_list:
        return AudioCompareResult(
            src=src,
            dest=src,
            match=True,
            method="isrc",
            detail=f"ISRC match ({src_isrc})",
        )
    return AudioCompareResult(
        src=src,
        dest=src,
        match=False,
        method="isrc",
        detail=f"ISRC mismatch: source {src_isrc!r} not in candidate list",
    )


def _corroborate_medium_sequence(
    track_results: list[AudioCompareResult],
    candidate_track_ids: list[str],
    source_track_ids: list[str],
) -> AudioCompareResult:
    """Apply medium-sequence corroboration: assess a joint identity verdict for an ordered track sequence.

    A single weak or short-duration track fingerprint is unreliable in isolation, but when the
    *ordered sequence* of per-track resolutions matches the ordered sequence of tracks on a candidate
    medium, the joint hypothesis is much stronger.  This rescues weak fingerprints (e.g. a 25-second
    chant verse that would never be identified alone).

    The function is a pure computation — it performs no I/O.  The ``src`` and ``dest`` fields of the
    returned :class:`AudioCompareResult` are set to ``Path(".")`` as a sentinel indicating that the
    result represents the whole medium rather than a single file pair.

    .. note::
        Cross-medium-span generalisation (where a single recording spans multiple media) is a natural
        extension of this logic but requires the multi-medium substrate.  Implement medium-scoped only
        for now; the cross-medium case can be added once that substrate lands.

    Classification per position ``i``:

    * **Confirmed**: ``track_results[i].match is True`` AND
      ``source_track_ids[i] == candidate_track_ids[i]``.
    * **Inconclusive**: ``track_results[i].match is None``.
    * **Contradicted**: ``track_results[i].match is False`` OR
      (``track_results[i].match is True`` AND ``source_track_ids[i] != candidate_track_ids[i]``).

    Decision rules (evaluated in order):

    1. Empty inputs → ``match=None, detail="empty sequence"``.
    2. Length mismatch → ``match=None, detail="length mismatch"``.
    3. Any contradicted position → ``match=False``.
    4. ``confirmed / total >= 0.5`` (majority confirmed, none contradicted) → ``match=True``.
    5. Otherwise (all inconclusive or confirmed < 50%) → ``match=None``.

    :param track_results: Per-track :class:`AudioCompareResult` objects from the identity ladder,
        one per source track in order.
    :param candidate_track_ids: Ordered list of recording MBIDs on the candidate medium (from
        ``MBMedium.track_list → MBTrack.recording.id``).
    :param source_track_ids: Ordered list of recording MBIDs claimed by the source files' embedded
        tags (rung 0 — what the files say they are), in the same order as ``track_results``.
    :returns: An :class:`AudioCompareResult` with ``method="sequence"`` summarising the joint verdict.
    """
    _sentinel = Path(".")

    if not track_results or not candidate_track_ids:
        return AudioCompareResult(src=_sentinel, dest=_sentinel, match=None, method="sequence", detail="empty sequence")

    if len(track_results) != len(candidate_track_ids):
        return AudioCompareResult(src=_sentinel, dest=_sentinel, match=None, method="sequence", detail="length mismatch")

    total = len(track_results)
    confirmed = 0
    inconclusive = 0
    contradicted = 0

    for i, result in enumerate(track_results):
        src_id = source_track_ids[i] if i < len(source_track_ids) else ""
        cand_id = candidate_track_ids[i]
        if result.match is None:
            inconclusive += 1
        elif result.match is True and src_id == cand_id:
            confirmed += 1
        else:
            # match is False, OR match is True but IDs disagree
            contradicted += 1

    if contradicted > 0:
        return AudioCompareResult(
            src=_sentinel,
            dest=_sentinel,
            match=False,
            method="sequence",
            detail=f"contradicted={contradicted}/{total}",
        )

    confirmed_fraction = confirmed / total
    if confirmed_fraction >= 0.5:  # noqa: PLR2004 — 0.5 is the majority threshold, not a magic number
        return AudioCompareResult(
            src=_sentinel,
            dest=_sentinel,
            match=True,
            method="sequence",
            detail=f"confirmed={confirmed}/{total}",
        )

    return AudioCompareResult(
        src=_sentinel,
        dest=_sentinel,
        match=None,
        method="sequence",
        detail=f"confirmed={confirmed}/{total},inconclusive={inconclusive}/{total}",
    )


def _read_recording_id_tag(path: Path) -> str:
    """Read the ``MUSICBRAINZ_TRACKID`` tag from a FLAC or MP3 file, returning ``""`` on any failure.

    ``MUSICBRAINZ_TRACKID`` stores the MusicBrainz recording MBID embedded by the pipeline (or by
    other taggers such as MusicBrainz Picard).  It is used by :func:`_corroborate_candidate_medium`
    as rung-0 identity evidence: a file that already carries a recording MBID was previously
    identified, and that MBID can be compared against a candidate medium's track list.

    For FLAC files the Vorbis Comment key ``"musicbrainz_trackid"`` is looked up (case-insensitive).
    For MP3 files the TXXX frame with description ``"MusicBrainz Track Id"`` is looked up (the
    standard Picard/MusicBrainz convention).

    :param path: Path to the audio file to inspect.
    :returns: The recording MBID string, or ``""`` if absent or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = FLAC(str(path))
                values = audio.get("musicbrainz_trackid") or audio.get("MUSICBRAINZ_TRACKID") or []
                return values[0] if values else ""
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
                for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
                    if frame.desc == "MusicBrainz Track Id" and frame.text:
                        return str(frame.text[0])
                return ""
            case _:
                return ""
    except Exception:  # noqa: BLE001 — best-effort tag read; any failure means no recording ID
        return ""


def _corroborate_candidate_medium(
    source_paths: list[Path],
    candidate_medium_track_ids: list[str],
) -> AudioCompareResult:
    """Corroborate a candidate medium against source files using embedded recording-ID tags.

    Reads the ``MUSICBRAINZ_TRACKID`` tag from each source file to obtain the recording MBIDs
    claimed by the files' embedded metadata (rung 0 — what the files say they are).  Files that
    carry a recording MBID are treated as having a confirmed per-track identity result
    (``match=True``); files without a tag are inconclusive (``match=None``).

    These synthetic per-track results are then passed to :func:`_corroborate_medium_sequence`
    together with the candidate medium's ordered recording IDs.  The joint verdict indicates
    whether the source files' embedded identity is consistent with the candidate medium.

    This is the primary hook for wiring sequence corroboration into the discovery flow.  It is
    intentionally lightweight — no network calls, no audio decoding — so it can be called for
    every candidate without significant overhead.

    :param source_paths: Ordered list of source audio file paths, one per track.
    :param candidate_medium_track_ids: Ordered list of recording MBIDs on the candidate medium
        (from ``MBMedium.track_list → MBTrack.recording.id``).
    :returns: An :class:`AudioCompareResult` with ``method="sequence"`` summarising the joint verdict.
    """
    _sentinel = Path(".")
    source_track_ids: list[str] = [_read_recording_id_tag(p) for p in source_paths]
    track_results: list[AudioCompareResult] = (
        [
            AudioCompareResult(
                src=p,
                dest=p,
                match=True if src_id else None,
                method="isrc",
                detail="recording-id tag present" if src_id else "no recording-id tag",
            )
            for p, src_id in zip(source_paths, source_track_ids)
        ]
        if source_paths
        else []
    )
    return _corroborate_medium_sequence(track_results, candidate_medium_track_ids, source_track_ids)


def _read_duration_ms(path: Path) -> int:
    """Read the audio duration of ``path`` in milliseconds via mutagen, or ``0`` on failure.

    Uses ``mutagen.File`` to handle any mutagen-supported format.

    :param path: Path to the audio file.
    :returns: Duration in milliseconds, or ``0`` if the file cannot be read or has no duration.
    """
    try:
        audio = MutagenFile(str(path))
        if audio is not None and hasattr(audio, "info") and hasattr(audio.info, "length"):
            return int(audio.info.length * 1000)
        return 0
    except Exception:  # noqa: BLE001 — best-effort read
        return 0


def check_duration_preflight(
    src_files: list[Path],
    track_pairs: list[tuple[MBTrack, int]],
    tolerance_ms: int = 10_000,
) -> list[str]:
    """Compare each source file's audio duration against the corresponding MB track length.

    Reads each source file's duration via mutagen and compares it against ``MBTrack.length`` (in
    milliseconds).  Tracks for which MB has no duration data (``length == 0``) are skipped silently
    — this is common for classical recordings where MB coverage is incomplete.

    This is a pre-flight check intended to catch flagrantly wrong MBID assignments (e.g. a
    30-minute symphony matched against a 3-minute pop single) before any files are copied.  It is
    not a substitute for full AcoustID fingerprint verification: MB duration data is crowd-sourced
    and may legitimately differ by several seconds from a specific pressing, so false positives are
    possible even with a generous tolerance.

    :param src_files: Ordered list of source audio file paths, already matched to ``track_pairs``
        by position.
    :param track_pairs: Ordered list of ``(MBTrack, medium_position)`` tuples corresponding to
        each source file.
    :param tolerance_ms: Maximum acceptable absolute difference between source duration and MB
        track length, in milliseconds.  Defaults to 10 000 ms (10 s).
    :returns: A list of human-readable warning strings, one per track whose duration deviates
        beyond ``tolerance_ms``.  An empty list means all checked tracks are within tolerance.
    """
    warnings: list[str] = []
    for src_file, (track, _medium_pos) in zip(src_files, track_pairs):
        mb_ms = track.length
        if mb_ms == 0:
            continue
        src_ms = _read_duration_ms(src_file)
        if src_ms == 0:
            continue
        delta_ms = abs(src_ms - mb_ms)
        if delta_ms > tolerance_ms:
            warnings.append(
                f"  track {track.position} '{track.recording.title}': "
                f"source {src_ms / 1000:.1f}s, MB {mb_ms / 1000:.1f}s "
                f"(delta {delta_ms / 1000:.1f}s)"
            )
    return warnings


def _run_fpcalc(path: Path) -> str:
    """Run ``fpcalc -json`` on ``path`` and return the fingerprint string, or ``""`` on failure.

    :param path: Path to the audio file to fingerprint.
    :returns: The Chromaprint fingerprint string from ``fpcalc``'s JSON output, or ``""`` if
        ``fpcalc`` is not available or the invocation fails.
    """
    result = subprocess.run(  # noqa: S603 — path validated by caller via shutil.which
        ["fpcalc", "-json", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    try:
        data: object = json.loads(result.stdout)
        if isinstance(data, dict):
            fp: object = data.get("fingerprint")
            return str(fp) if fp else ""
        return ""
    except json.JSONDecodeError:
        return ""


def _chromaprint_similarity(fp_a: str, fp_b: str) -> float | None:
    """Compute the Hamming-distance similarity between two Chromaprint fingerprint strings.

    Decodes both fingerprints from base64url (no-padding) format into sequences of 32-bit
    unsigned integers, then computes the Hamming distance by XOR-ing each pair of integers and
    counting the set bits.  Returns a similarity score in ``[0.0, 1.0]`` as
    ``1 - (hamming_distance / total_bits)``.

    Returns ``None`` when either fingerprint is empty, cannot be decoded, or the decoded arrays
    have different lengths (indicating incompatible fingerprint versions).

    :param fp_a: First Chromaprint fingerprint string (base64url-encoded, no padding).
    :param fp_b: Second Chromaprint fingerprint string (base64url-encoded, no padding).
    :returns: Similarity score in ``[0.0, 1.0]``, or ``None`` when comparison is not possible.
    """
    if not fp_a or not fp_b:
        return None
    try:
        # Add the correct amount of base64 padding: 0, 1, or 2 '=' characters so that the total
        # length is a multiple of 4.  Adding a fixed "==" is wrong when len(fp) % 4 == 0.
        pad_a = "=" * (-len(fp_a) % 4)
        pad_b = "=" * (-len(fp_b) % 4)
        data_a = base64.b64decode(fp_a + pad_a, altchars=b"-_", validate=True)
        data_b = base64.b64decode(fp_b + pad_b, altchars=b"-_", validate=True)
    except Exception:  # noqa: BLE001 — any decode failure means fingerprint is unusable
        return None
    n_a = len(data_a) // 4
    n_b = len(data_b) // 4
    if n_a == 0 or n_b == 0 or n_a != n_b:
        return None
    ints_a: tuple[int, ...] = struct.unpack(f"<{n_a}I", data_a[: n_a * 4])
    ints_b: tuple[int, ...] = struct.unpack(f"<{n_b}I", data_b[: n_b * 4])
    hamming_distance = sum((a ^ b).bit_count() for a, b in zip(ints_a, ints_b))
    total_bits = n_a * 32
    return 1.0 - (hamming_distance / total_bits)


def _compare_chromaprint_and_duration(
    src: Path,
    dest: Path,
    incoming_length_ms: int,
) -> AudioCompareResult | None:
    """Apply Chromaprint (layer 3) and duration (layer 4) comparison, returning a result or ``None``.

    Called by :func:`compare_audio_collision` after SHA-256 and AcoustID checks have not produced
    a definitive result.  Returns ``None`` when neither layer yields a usable result so the caller
    can fall through to the final ``"unknown"`` outcome.

    Emits a one-time ``structlog`` warning when ``fpcalc`` is absent and duration comparison
    flagged a candidate match, so the operator knows a more reliable check was unavailable.

    :param src: Source file path (for Chromaprint).
    :param dest: Destination file path (for Chromaprint and duration).
    :param incoming_length_ms: Incoming track duration in ms; ``0`` skips duration comparison.
    :returns: A definitive or inconclusive :class:`AudioCompareResult`, or ``None`` when no layer
        produced usable data.
    """
    fpcalc_path = shutil.which("fpcalc")

    if fpcalc_path:
        src_fp = _run_fpcalc(src)
        dest_fp = _run_fpcalc(dest)
        similarity = _chromaprint_similarity(src_fp, dest_fp)
        if similarity is not None:
            if similarity >= _CHROMAPRINT_SIMILARITY_THRESHOLD:
                return AudioCompareResult(
                    src=src,
                    dest=dest,
                    match=True,
                    method="chromaprint",
                    detail=f"similarity={similarity:.3f}",
                )
            return AudioCompareResult(
                src=src,
                dest=dest,
                match=False,
                method="chromaprint",
                detail=f"similarity={similarity:.3f}",
            )

    if incoming_length_ms > 0:
        dest_length_ms = _read_duration_ms(dest)
        if dest_length_ms > 0:
            delta = abs(dest_length_ms - incoming_length_ms)
            if delta > _DURATION_TOLERANCE_MS:
                return AudioCompareResult(
                    src=src,
                    dest=dest,
                    match=False,
                    method="duration",
                    detail=(f"duration mismatch: incoming {incoming_length_ms} ms vs dest {dest_length_ms} ms (Δ{delta} ms)"),
                )
            if fpcalc_path is None and not _FPCALC_WARNED[0]:
                _FPCALC_WARNED[0] = True
                log.warning(
                    "fpcalc_not_found",
                    msg=(
                        "fpcalc not found; Chromaprint comparison skipped"
                        " — install chromaprint for more reliable audio matching"
                    ),
                )
            if fpcalc_path is None:
                return AudioCompareResult(
                    src=src,
                    dest=dest,
                    match=None,
                    method="duration",
                    detail=f"duration within ±{_DURATION_TOLERANCE_MS} ms but not confirmed (fpcalc unavailable)",
                )

    return None


def compare_audio_collision(
    src: Path,
    dest: Path,
    incoming_acoustid: str,
    incoming_length_ms: int,
) -> AudioCompareResult:
    """Compare a planned source file against an existing destination file for audio content similarity.

    Applies four comparison layers in order, short-circuiting on the first definitive result:

    1. **SHA-256** — byte-for-byte identity.  A match means the source was already copied; a
       mismatch here does *not* rule out audio similarity (tagging will have changed the bytes).
    2. **AcoustID UUID** — reads the ``ACOUSTID_ID`` tag from ``dest`` and compares it to
       ``incoming_acoustid``.  Requires that ``dest`` was tagged by this pipeline.  A non-empty
       match on both sides is definitive in either direction.
    3. **Chromaprint** — invokes ``fpcalc -json`` on both files if the binary is on ``$PATH``.
       Fingerprint string equality is a strong (though not fuzzy) similarity signal; inequality
       after an earlier duration-candidate result resolves the inconclusive case.  A one-time
       ``structlog`` warning is emitted when ``fpcalc`` is absent and duration comparison has
       already flagged a possible match (so the user knows a better comparison was available).
    4. **Duration** — compares ``dest``'s mutagen-read duration against ``incoming_length_ms``
       within a ±:data:`_DURATION_TOLERANCE_MS` window.  Within tolerance →
       ``match=None`` (inconclusive); outside → ``match=False`` (definitive non-match).

    When none of the above produce a usable result (empty AcoustID, no fpcalc, no duration data),
    ``match=None, method="unknown"`` is returned.  Layers 3 and 4 are implemented in
    :func:`_compare_chromaprint_and_duration`.

    :param src: Planned source file path (used for SHA-256 and Chromaprint comparison).
    :param dest: Already-existing destination file path.
    :param incoming_acoustid: AcoustID UUID for the incoming track (from
        :attr:`~music_annotator.models.TrackTags.acoustid_id`); may be ``""``.
    :param incoming_length_ms: Duration of the incoming track in milliseconds (from
        :attr:`~music_annotator.models.TrackTags.length` parsed as int); may be ``0``.
    :returns: An :class:`AudioCompareResult` describing the comparison outcome.
    """
    # --- Layer 1: SHA-256 ---
    if _sha256_file(src) == _sha256_file(dest):
        return AudioCompareResult(src=src, dest=dest, match=True, method="sha256", detail="byte-identical files (same SHA-256)")

    # --- Layer 2: AcoustID UUID ---
    dest_acoustid = _read_acoustid_tag(dest)
    if incoming_acoustid and dest_acoustid:
        if incoming_acoustid == dest_acoustid:
            return AudioCompareResult(
                src=src, dest=dest, match=True, method="acoustid", detail=f"same AcoustID cluster ({incoming_acoustid[:8]}…)"
            )
        return AudioCompareResult(
            src=src,
            dest=dest,
            match=False,
            method="acoustid",
            detail=f"different AcoustID clusters ({incoming_acoustid[:8]}… vs {dest_acoustid[:8]}…)",
        )

    # --- Layers 3 + 4: Chromaprint and duration ---
    if (result := _compare_chromaprint_and_duration(src, dest, incoming_length_ms)) is not None:
        return result

    return AudioCompareResult(
        src=src,
        dest=dest,
        match=None,
        method="unknown",
        detail="insufficient data for audio comparison (no AcoustID tags, no fpcalc, no duration)",
    )


def _assess_collisions(
    plan_pairs: list[tuple[Path, Path, str, int]],
    vacated_paths: frozenset[Path] | None = None,
) -> list[AudioCompareResult]:
    """Assess each planned-destination collision against its source for audio content similarity.

    Filters ``plan_pairs`` to entries whose destination already exists on disk **and** is not
    vacated by the same plan, then calls :func:`compare_audio_collision` for each one.  Plan
    entries with no pre-existing destination, or whose destination is in ``vacated_paths`` (i.e.
    it will be vacated by another move in the same plan before this move executes), are omitted
    from the result.

    The ``vacated_paths`` parameter implements the C-SEQ vacancy-aware collision rule: the suffix
    fires only when the occupant is NOT vacated by the same plan AND audio differs
    (acoustid/length).  Callers must pass the set of source paths from the same plan so that
    destinations that are also sources (shift chains, swaps) are not falsely flagged as collisions.

    :param plan_pairs: A list of ``(src_file, dest_file, acoustid, length_ms)`` tuples, one per
        planned copy operation.  ``acoustid`` is the incoming track's AcoustID UUID (may be ``""``);
        ``length_ms`` is its duration in milliseconds (may be ``0``).
    :param vacated_paths: Optional set of paths that will be vacated by the same plan (i.e. the
        source paths of all moves in the plan).  When a destination is in this set, it is not
        treated as a collision — the occupant will be moved away before this move executes.
        Defaults to ``None`` (no vacancy subtraction, equivalent to an empty set).
    :returns: A (possibly empty) list of :class:`AudioCompareResult` objects, one per collision.
    """
    effective_vacated: frozenset[Path] = vacated_paths if vacated_paths is not None else frozenset()
    results: list[AudioCompareResult] = []
    for src, dest, acoustid, length_ms in plan_pairs:
        if dest.exists() and dest not in effective_vacated:
            results.append(compare_audio_collision(src, dest, acoustid, length_ms))
    return results


def find_source_files(src_dir: Path) -> list[Path]:
    """Return a sorted list of audio files in ``src_dir``, excluding ripper metadata files.

    Only the immediate children of ``src_dir`` are checked (not recursive).  A file is included
    when its lowercased suffix appears in :data:`AUDIO_EXTENSIONS` **and** its name is not in
    :data:`_EXCLUDED_FILENAMES`.  This prevents CD table-of-contents FLAC files (e.g.
    ``00 - disc TOC.flac``) from being counted as source tracks and causing a track-count mismatch
    against the MusicBrainz release.

    :param src_dir: Directory to scan.
    :returns: A list of :class:`~pathlib.Path` objects for all matching files, sorted by filename.
    :raises OSError: If ``src_dir`` does not exist or is not readable.
    """
    return sorted(
        (p for p in src_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS and p.name not in _EXCLUDED_FILENAMES),
        key=lambda p: p.name,
    )


def _load_disc_info_yaml(src_dir: Path) -> dict[str, object] | None:
    """Load and parse the ``00 - disc info.yaml`` file from ``src_dir``, returning its top-level dict.

    Shared by :func:`parse_disc_toc` and :func:`~music_annotator._discover.parse_disc_info_yaml` to avoid
    duplicating the file-open and YAML-parse boilerplate.

    :param src_dir: Directory that may contain a ``00 - disc info.yaml`` file.
    :returns: The parsed top-level mapping, or ``None`` if the file is absent or its content is not a dict.
    :raises yaml.YAMLError: Propagated if the file exists but cannot be parsed.
    """
    yaml_path = src_dir / _DISC_INFO_FILENAME
    if not yaml_path.is_file():
        return None
    with yaml_path.open(encoding="utf-8") as fh:
        data: object = yaml.full_load(fh)
    if not isinstance(data, dict):
        return None
    return data


def _preferred_disc_record(records: list[object]) -> dict[str, object] | None:
    """Return the preferred FreeDB record dict from ``records``, or ``None``.

    Shared by :func:`parse_disc_title` and :func:`~music_annotator._discover.parse_disc_info_yaml`
    to avoid duplicating the preferred-record selection loop.

    :param records: The ``record`` list from a ``00 - disc info.yaml`` document.
    :returns: The preferred record dict, or the first dict-typed record, or ``None``.
    """
    for rec in records:
        if isinstance(rec, dict) and rec.get("preferred"):
            return rec
    first = records[0] if records else None
    return first if isinstance(first, dict) else None


def parse_disc_title(src_dir: Path) -> str:
    """Extract the FreeDB disc title string from ``00 - disc info.yaml``.

    The ``DTITLE`` field in the preferred FreeDB record uses the format ``"artist / title"``.  This
    function returns only the **title portion** (everything after the first `` / ``), or the whole
    string when no `` / `` separator is present.  An empty string is returned when the file is absent,
    unreadable, or has no usable ``DTITLE``.

    Used by :func:`~music_annotator._pipeline.run` to supply a FreeDB title hint to
    :func:`~music_annotator._pipeline._select_medium_with_reason` for title-based medium selection when
    MusicBrainz has no disc IDs registered for the release.

    :param src_dir: Directory that may contain a ``00 - disc info.yaml`` file.
    :returns: The FreeDB disc title string, or ``""`` if unavailable.
    """
    data = _load_disc_info_yaml(src_dir)
    if data is None:
        return ""
    records: object = data.get("record")
    if not isinstance(records, list) or not records:
        return ""
    preferred = _preferred_disc_record(records)
    if preferred is None:
        return ""
    track_info: object = preferred.get("track_info")
    if not isinstance(track_info, dict):
        return ""
    dtitle = str(track_info.get("DTITLE", "")).strip()
    if not dtitle:
        return ""
    _, _, suffix = dtitle.partition(" / ")
    return suffix.strip() if suffix else dtitle


def _parse_disc_id_list(disc_id: list[object]) -> tuple[int, int, list[int]] | None:
    """Validate and decode a FreeDB ``disc_id`` list into ``(num_tracks, leadout_frame, track_frames)``.

    The expected structure is ``[freedb_crc, num_tracks, offset_1, …, offset_N, total_seconds]`` where ``total_seconds * 75``
    gives the MusicBrainz lead-out frame address.

    :param disc_id: The raw ``disc_id`` list from the YAML document.
    :returns: A ``(num_tracks, leadout_frame, track_frames)`` triple, or ``None`` when the list is malformed (too short, wrong
        types, or mismatched offset count).
    """
    # Minimum viable list: [crc, num_tracks, offset_1, total_seconds] → length 4
    if len(disc_id) < 4:  # noqa: PLR2004
        return None
    num_tracks_raw = disc_id[1]
    total_seconds_raw = disc_id[-1]
    if not isinstance(num_tracks_raw, int) or num_tracks_raw < 1:
        return None
    if not isinstance(total_seconds_raw, int) or total_seconds_raw < 1:
        return None
    offsets_raw: list[object] = disc_id[2:-1]
    if len(offsets_raw) != num_tracks_raw:
        return None
    track_frames: list[int] = []
    for offset in offsets_raw:
        if not isinstance(offset, int):
            return None
        track_frames.append(offset)
    return num_tracks_raw, total_seconds_raw * 75, track_frames


def parse_disc_toc(src_dir: Path) -> tuple[int, int, list[int]] | None:
    """Extract the CD table-of-contents from a FreeDB ``00 - disc info.yaml`` file.

    The ``disc_id`` field in the YAML is a list with the structure::

        [freedb_crc, num_tracks, offset_1, offset_2, …, offset_N, total_seconds]

    where:

    * ``freedb_crc`` — the FreeDB CRC checksum (element 0, ignored here).
    * ``num_tracks`` — number of audio tracks (element 1).
    * ``offset_1 … offset_N`` — per-track frame offsets in CD frames (elements 2 through N+1).
    * ``total_seconds`` — the disc's total playing time in seconds; multiplying by 75 gives the lead-out frame address as
      expected by the MusicBrainz disc-ID and TOC lookup APIs.

    :param src_dir: Directory that may contain a ``00 - disc info.yaml`` file.
    :returns: A ``(num_tracks, leadout_frame, track_frames)`` triple when a valid TOC is found, or ``None`` if the file is
        absent, the ``disc_id`` key is missing, or the list is too short to contain at least one track offset.
    :raises yaml.YAMLError: Propagated if the file exists but cannot be parsed.
    """
    data = _load_disc_info_yaml(src_dir)
    if data is None:
        return None
    disc_id: object = data.get("disc_id")
    if not isinstance(disc_id, list):
        return None
    return _parse_disc_id_list(disc_id)


def _find_whipper_log(src_dir: Path) -> Path | None:
    """Return the path of the whipper native log in ``src_dir``, or ``None``.

    Scans ``src_dir`` for ``*.log`` files whose last non-empty line matches the whipper
    self-attesting SHA-256 signature (``SHA-256 hash: <UPPERHEX>``).  This is the C-WHIP strong
    signature (1): the trailing SHA-256 line distinguishes a whipper native log from any other
    ``.log`` file in the directory.

    :param src_dir: Directory to scan.
    :returns: The path of the first matching log file (sorted for determinism), or ``None``.
    """
    _sha256_line_re = re.compile(r"^SHA-256 hash: [0-9A-F]{64}$")
    for log_path in sorted(src_dir.glob("*.log")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Find the last non-empty line
        lines = text.splitlines()
        last_nonempty = next((ln for ln in reversed(lines) if ln.strip()), "")
        if _sha256_line_re.match(last_nonempty):
            return log_path
    return None


def _parse_ar_track_result(version: str, block: object) -> AccurateRipTrackResult:
    """Parse one AccurateRip version block (v1 or v2) from a whipper log track dict.

    Handles the case where the block is absent (``None``) or is a dict with a ``Result`` key.
    A missing or non-dict block yields a default ``NOT_PRESENT`` result.

    :param version: DB generation identifier: ``"v1"`` or ``"v2"``.
    :param block: The raw YAML value for the ``AccurateRip v1`` / ``AccurateRip v2`` key.
    :returns: A typed :class:`~music_annotator.models.AccurateRipTrackResult`.
    """
    if not isinstance(block, dict):
        return AccurateRipTrackResult(version=version)

    raw_result: object = block.get("Result", "")
    try:
        result = AccurateRipResult(str(raw_result))
    except ValueError:
        result = AccurateRipResult.NOT_PRESENT

    raw_confidence: object = block.get("Confidence", 0)
    confidence = int(raw_confidence) if isinstance(raw_confidence, int) else 0

    local_crc_raw: object = block.get("Local CRC")
    local_crc = str(local_crc_raw) if local_crc_raw is not None else ""
    remote_crc_raw: object = block.get("Remote CRC")
    remote_crc = str(remote_crc_raw) if remote_crc_raw is not None else ""

    return AccurateRipTrackResult(
        version=version,
        result=result,
        confidence=confidence,
        local_crc=local_crc,
        remote_crc=remote_crc,
    )


def _parse_ar_track(track_dict: object) -> AccurateRipTrack:
    """Parse a single track entry from the whipper log ``Tracks`` block.

    Extracts ``AccurateRip v1``, ``AccurateRip v2``, ``Test CRC``, ``Copy CRC``, and ``Status``
    from the raw YAML dict.  Missing keys yield empty/default values per the C-AR contract.

    :param track_dict: The raw YAML value for one track number key in the ``Tracks`` block.
    :returns: A typed :class:`~music_annotator.models.AccurateRipTrack`.
    """
    if not isinstance(track_dict, dict):
        return AccurateRipTrack()

    v1 = _parse_ar_track_result("v1", track_dict.get("AccurateRip v1"))
    v2 = _parse_ar_track_result("v2", track_dict.get("AccurateRip v2"))

    test_crc_raw: object = track_dict.get("Test CRC")
    copy_crc_raw: object = track_dict.get("Copy CRC")
    status_raw: object = track_dict.get("Status")

    return AccurateRipTrack(
        v1=v1,
        v2=v2,
        test_crc=str(test_crc_raw) if test_crc_raw is not None else "",
        copy_crc=str(copy_crc_raw) if copy_crc_raw is not None else "",
        status=str(status_raw) if status_raw is not None else "",
    )


def parse_whipper_log(src_dir: Path) -> tuple[AccurateRipSummary, dict[int, AccurateRipTrack]]:
    """Parse the whipper native-logger YAML log in ``src_dir`` into C-AR models.

    Locates the whipper log via the C-WHIP strong signature (1): a ``*.log`` file whose last
    non-empty line is ``SHA-256 hash: <UPPERHEX>``.  Parses the ``CD metadata`` block (MB disc-ID,
    CDDB disc-ID), the per-track ``Tracks`` ``AccurateRip v1``/``v2`` blocks, the ``Conclusive
    status report`` summary, and the trailing ``SHA-256 hash`` line.

    The log's self-attesting SHA-256 is verified against the recomputed hash of the log body
    (everything before the ``SHA-256 hash:`` line, including the trailing newline).  A mismatch
    is logged as a WARNING — the dir is still recognised as whipper and the parse continues.

    HTOA (track 0 / hidden track one audio): whipper may emit a track keyed ``0`` in the
    ``Tracks`` block.  It is mapped to key ``0`` in the returned dict (not skipped), so callers
    can decide whether to use it.

    :param src_dir: Directory containing the whipper rip (must pass C-WHIP strong signature 1).
    :returns: A ``(AccurateRipSummary, dict[int, AccurateRipTrack])`` tuple where the dict key
        is the track number (``0`` for HTOA, ``1``–``N`` for regular tracks).
    :raises FileNotFoundError: If no whipper native log is found in ``src_dir``.
    """
    log_path = _find_whipper_log(src_dir)
    if log_path is None:
        raise FileNotFoundError(f"No whipper native log found in {src_dir}")

    raw_text = log_path.read_text(encoding="utf-8", errors="replace")

    # Split body (everything before the SHA-256 hash line) from the trailing hash line.
    # The hash is computed over the body including its trailing newline.
    sha256_marker = "SHA-256 hash: "
    body_text: str
    log_sha256: str
    if sha256_marker in raw_text:
        split_idx = raw_text.rfind(sha256_marker)
        # Body is everything up to and including the newline before the SHA-256 line.
        # Find the start of the SHA-256 line (the newline before it is part of the body).
        body_text = raw_text[:split_idx]
        hash_line = raw_text[split_idx:].splitlines()[0]
        log_sha256 = hash_line[len(sha256_marker) :].strip()
    else:  # pragma: no cover — _find_whipper_log guarantees the marker is present
        body_text = raw_text
        log_sha256 = ""

    # Verify the self-attesting SHA-256 (C-WHIP: mismatch → WARNING, not hard failure).
    # log_sha256 is always non-empty here: _find_whipper_log's regex requires 64 uppercase hex chars.
    computed = hashlib.sha256(body_text.encode("utf-8")).hexdigest().upper()
    if computed != log_sha256:
        log.warning(
            "whipper_log_sha256_mismatch",
            log_path=str(log_path),
            expected=log_sha256,
            computed=computed,
        )

    # Parse the YAML body.  yaml.safe_load returns object; narrow to dict for field access.
    yaml_data: object = yaml.safe_load(body_text)
    doc: dict[str, object] = dict(yaml_data) if isinstance(yaml_data, dict) else {}

    # --- CD metadata block ---
    cd_meta: object = doc.get("CD metadata")
    mb_disc_id = ""
    cddb_disc_id = ""
    if isinstance(cd_meta, dict):
        mb_raw: object = cd_meta.get("MusicBrainz Disc ID")
        mb_disc_id = str(mb_raw) if mb_raw is not None else ""
        cddb_raw: object = cd_meta.get("CDDB Disc ID")
        cddb_disc_id = str(cddb_raw) if cddb_raw is not None else ""

    # --- Conclusive status report block ---
    status_report: object = doc.get("Conclusive status report")
    accurately_ripped = 0
    in_ar_database = 0
    summary_text = ""
    if isinstance(status_report, dict):
        ar_summary_raw: object = status_report.get("AccurateRip summary")
        summary_text = str(ar_summary_raw) if ar_summary_raw is not None else ""
        ar_ripped_raw: object = status_report.get("Accurately ripped", 0)
        accurately_ripped = int(ar_ripped_raw) if isinstance(ar_ripped_raw, int) else 0
        ar_db_raw: object = status_report.get("Tracks in AR database", 0)
        in_ar_database = int(ar_db_raw) if isinstance(ar_db_raw, int) else 0

    summary = AccurateRipSummary(
        mb_disc_id=mb_disc_id,
        cddb_disc_id=cddb_disc_id,
        log_sha256=log_sha256,
        accurately_ripped=accurately_ripped,
        in_ar_database=in_ar_database,
        summary_text=summary_text,
    )

    # --- Tracks block ---
    tracks_raw: object = doc.get("Tracks")
    tracks: dict[int, AccurateRipTrack] = {}
    if isinstance(tracks_raw, dict):
        for track_key, track_val in tracks_raw.items():
            try:
                track_num = int(track_key)
            except (ValueError, TypeError):
                continue
            tracks[track_num] = _parse_ar_track(track_val)

    return summary, tracks


def append_journal_entry(journal_path: Path, entry: TransactionEntry) -> None:
    """Append a single entry to the JSONL transaction journal at ``journal_path``, durably.

    Opens the file in append mode, writes one JSON object followed by a newline, then flushes
    the Python buffer and calls ``os.fsync`` on the file descriptor to guarantee the write is
    durable before returning.  This is an O(1) operation regardless of journal size.

    The journal file must already be in JSONL format (or absent — the file is created on first
    call).  Callers must not mix this function with :func:`write_transaction_log` on the same
    journal path without first migrating the file to JSONL via :func:`read_journal`.

    :param journal_path: Absolute path of the journal file (typically
        ``<dest_root>/music_annotator_journal.json``).
    :param entry: The :class:`~music_annotator.models.TransactionEntry` to append.
    """
    line = json.dumps(entry.model_dump(), ensure_ascii=False) + "\n"
    with journal_path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    log.debug("journal_entry_appended", path=str(journal_path), action=entry.action)


def _migrate_array_journal(journal_path: Path) -> None:
    """Migrate a legacy JSON-array journal at ``journal_path`` to JSONL format in place.

    Reads the existing JSON array, writes a backup of the original file (with
    :data:`JOURNAL_BACKUP_SUFFIX` appended), then atomically replaces the journal with a JSONL
    file containing one JSON object per line.  The backup is never deleted by the tool.

    :param journal_path: Absolute path of the journal file containing a JSON array.
    :raises RuntimeError: If the backup file already exists (migration was already attempted but
        the journal was not replaced — indicates a partial failure on a previous run).
    :raises json.JSONDecodeError: If the file cannot be parsed as JSON (should not happen since
        the caller already parsed it, but guards against TOCTOU races).
    :raises OSError: If the backup or replacement write fails.
    """
    raw = journal_path.read_text(encoding="utf-8")
    parsed: object = json.loads(raw)
    # Caller guarantees this is a list, but guard defensively.
    if not isinstance(parsed, list):  # pragma: no cover
        raise RuntimeError(f"journal_migrate: expected list, got {type(parsed).__name__}")

    backup_path = journal_path.with_suffix(journal_path.suffix + JOURNAL_BACKUP_SUFFIX)
    if not backup_path.exists():
        # Write the backup before touching the live file.
        backup_path.write_bytes(journal_path.read_bytes())
        # Make the backup read-only so it is clearly an archive.
        backup_path.chmod(0o444)

    # Build the JSONL content and write atomically via a temp file + os.replace.
    lines = [json.dumps(e, ensure_ascii=False) + "\n" for e in parsed]
    tmp_path = journal_path.with_suffix(journal_path.suffix + ".tmp")
    tmp_path.write_text("".join(lines), encoding="utf-8")
    os.replace(str(tmp_path), str(journal_path))

    log.info(
        "journal_migrated_to_jsonl",
        path=str(journal_path),
        backup=str(backup_path),
        entries=len(parsed),
    )


def _parse_jsonl_journal(journal_path: Path, raw: str) -> TransactionLog:
    """Parse a JSONL journal string into a :class:`~music_annotator.models.TransactionLog`.

    Reads the content line by line.  A final line that cannot be parsed as JSON is treated as a
    torn write (the process was interrupted mid-line) and is silently ignored after emitting a
    WARNING.  Any malformed line that is not the final line is a hard error — it indicates
    corruption, not a torn tail, and raises :class:`RuntimeError`.

    :param journal_path: Path used only for log messages and error context.
    :param raw: The full text content of the JSONL file.
    :returns: A :class:`~music_annotator.models.TransactionLog` with all valid entries.
    :raises RuntimeError: If a non-final line cannot be parsed as JSON (corruption).
    """
    lines = raw.splitlines()
    # Strip trailing empty lines so a file ending with "\n" does not produce a spurious torn-tail
    # warning.  A genuinely torn tail is a non-empty line that fails JSON parsing.
    while lines and not lines[-1].strip():
        lines.pop()

    entries: list[TransactionEntry] = []
    for i, line in enumerate(lines):
        is_last = i == len(lines) - 1
        try:
            obj: object = json.loads(line)
            entries.append(TransactionEntry.model_validate(obj))
        except (json.JSONDecodeError, ValueError) as exc:
            if is_last:
                log.warning(
                    "journal_torn_tail_ignored",
                    path=str(journal_path),
                    line_number=i + 1,
                    error=str(exc),
                )
            else:
                raise RuntimeError(f"journal corruption at line {i + 1} of '{journal_path}': {exc}") from exc

    return TransactionLog(entries=entries)


def write_transaction_log(journal_path: Path, new_entries: list[TransactionEntry]) -> None:
    """Append ``new_entries`` to the JSONL transaction journal at ``journal_path``.

    If the journal file already exists and contains a valid JSONL journal, the new entries are
    appended.  If the file contains a legacy JSON array, it is migrated to JSONL first.  If the
    file is absent it is created.

    Each entry is written as a single JSON line.  After all entries are written the file is
    flushed and ``os.fsync`` is called for durability.

    :param journal_path: Absolute path of the journal file (typically
        ``<dest_root>/music_annotator_journal.json``).
    :param new_entries: The :class:`~music_annotator.models.TransactionEntry` objects to append.
    """
    # Ensure the journal is in JSONL format before appending.
    if journal_path.exists():
        raw = journal_path.read_text(encoding="utf-8")
        if raw.lstrip().startswith("["):
            # Legacy JSON-array format — migrate first.  A corrupt array (invalid JSON) is reset
            # to an empty file so the new entries are not lost.
            try:
                _migrate_array_journal(journal_path)
            except json.JSONDecodeError:
                log.warning("journal_corrupt_reset", path=str(journal_path))
                journal_path.write_text("", encoding="utf-8")

    for entry in new_entries:
        append_journal_entry(journal_path, entry)

    log.info("journal_written", path=str(journal_path), appended=len(new_entries))


def read_journal(journal_path: Path) -> TransactionLog:
    """Read and parse the JSONL transaction journal at ``journal_path``.

    Handles two on-disk formats:

    * **JSONL** (primary): one JSON object per line.  A single torn final line (the result of a
      process being interrupted mid-write) is tolerated: it is logged at WARNING level and
      ignored.  Any other malformed line is a hard error (:class:`RuntimeError`), never a silent
      reset.
    * **Legacy JSON array** (migration path): if the file begins with ``[``, it is parsed as a
      JSON array, migrated to JSONL in place (with the original preserved as a read-only backup),
      and the entries are returned.

    Returns an empty :class:`~music_annotator.models.TransactionLog` only when the file is absent
    (logged at INFO level).

    :param journal_path: Absolute path of the journal file (typically
        ``<dest_root>/music_annotator_journal.json``).
    :returns: A :class:`~music_annotator.models.TransactionLog` with all persisted entries, or an
        empty one if the file is absent.
    :raises RuntimeError: If a non-final line cannot be parsed (JSONL corruption).
    :raises json.JSONDecodeError: If the file begins with ``[`` but is not valid JSON (legacy
        array corruption).
    :raises OSError: If the file cannot be read.
    """
    if not journal_path.exists():
        log.info("journal_not_found", path=str(journal_path))
        return TransactionLog()

    raw = journal_path.read_text(encoding="utf-8")
    stripped = raw.lstrip()

    if stripped.startswith("["):
        # Legacy JSON-array format: parse, migrate, return entries.
        # json.loads raises json.JSONDecodeError on corrupt input; a valid JSON "[…]" always
        # produces a list, so no isinstance guard is needed here.
        parsed_array: list[JSON] = json.loads(raw)  # raises json.JSONDecodeError on corrupt input
        _migrate_array_journal(journal_path)
        return TransactionLog(entries=[TransactionEntry.model_validate(e) for e in parsed_array])

    # JSONL format.
    return _parse_jsonl_journal(journal_path, raw)


def _resolve_tagged_to_current(journal: TransactionLog) -> dict[str, str]:
    """Map each tagged-destination path string to its current path string.

    Walks ``journal.entries`` in chronological order (list order is chronological), maintaining
    an inverse index so each move entry updates affected tagged-destination pointers in O(1)
    amortized; O(N) total over all entries.  Cycle-proof because no fixpoint-follow occurs — an
    inverse move (A→B then B→A) simply moves the pointer back to A; no loop ever happens.

    Two dicts are maintained:

    * ``tagged_to_current`` — tagged-dest → current path (the output).
    * ``current_to_tagged`` — current path → list of tagged-dests currently there (inverse index).

    On a ``"tagged"`` entry the destination is registered as its own current path.  On a
    ``"repathed"``, ``"regrouped"``, or ``"unified"`` entry all tagged-dests currently at
    ``entry.source`` are forwarded to ``entry.destination``.  On a ``"deduplicated"`` entry the
    source (the deleted copy) is a terminal: all tagged-dests currently at ``entry.source`` are
    removed from both maps — the chain ends here and those tagged-dests resolve to nothing.
    All other actions are ignored.

    :param journal: The :class:`~music_annotator.models.TransactionLog` to walk.
    :returns: Mapping from each tagged-destination path string to its current path string.
        Tagged destinations that were never subsequently moved map to themselves.
        Tagged destinations whose chain terminates at a deduplicated-deleted file are absent
        from the returned mapping (they resolve to nothing).
    """
    tagged_to_current: dict[str, str] = {}
    current_to_tagged: dict[str, list[str]] = {}

    for entry in journal.entries:
        dest = entry.destination
        if entry.action == "tagged":
            tagged_to_current[dest] = dest
            current_to_tagged.setdefault(dest, []).append(dest)
        elif entry.action in {"repathed", "regrouped", "unified"}:
            affected = current_to_tagged.pop(entry.source, [])
            for tagged_dest in affected:
                tagged_to_current[tagged_dest] = dest
            if affected:
                current_to_tagged.setdefault(dest, []).extend(affected)
        elif entry.action == "deduplicated":
            # The source was deleted by dedup; its chain is terminal.  Remove all tagged-dests
            # that currently point to the deleted source so they resolve to nothing — they are
            # expected history, not present-state evidence.
            affected = current_to_tagged.pop(entry.source, [])
            for tagged_dest in affected:
                tagged_to_current.pop(tagged_dest, None)

    return tagged_to_current


def _is_enoent(exc: BaseException) -> bool:
    """Return True when ``exc`` is or wraps an ENOENT (errno 2) OS error.

    Mutagen wraps ``FileNotFoundError`` in ``mutagen.MutagenError`` (a plain ``Exception``
    subclass, not ``OSError``), so a direct ``isinstance(exc, OSError)`` check misses the common
    case.  This helper unwraps one level of chaining (``exc.args[0]``) to catch both the direct
    and wrapped forms.

    :param exc: The exception to inspect.
    :returns: ``True`` when the exception or its first wrapped argument is an ``OSError`` with
        ``errno == 2`` (ENOENT).
    """
    if isinstance(exc, OSError) and exc.errno == 2:  # noqa: PLR2004 — ENOENT is errno 2
        return True
    inner = exc.args[0] if exc.args else None
    return isinstance(inner, OSError) and inner.errno == 2  # noqa: PLR2004 — ENOENT is errno 2


def _read_albumid_tag(path: Path) -> str:
    """Read the ``MUSICBRAINZ_ALBUMID`` tag from a FLAC or MP3 file, returning ``""`` on any failure.

    Uses suffix-dispatch to invoke :func:`_read_tags_flac` or :func:`_read_tags_mp3`, then
    extracts the ``MUSICBRAINZ_ALBUMID`` key (uppercased by both readers).  On a missing file
    (ENOENT — either a direct ``FileNotFoundError`` or a ``mutagen.MutagenError`` wrapping one)
    the function returns ``""`` silently — the caller is responsible for deciding whether a missing
    file is expected history or a real error.  On any other read error a warning is logged so
    unexpected failures are visible.

    Factored into a dedicated helper so the regroup-move path can reuse it and the suffix-dispatch is
    testable in isolation.

    :param path: Path to the audio file to inspect.
    :returns: The MusicBrainz release MBID string from the embedded tag, or ``""`` if absent,
        unreadable, or for an unsupported file format.
    """
    try:
        ext = path.suffix.lower()
        match ext:
            case ".flac":
                file_dict = _read_tags_flac(path)
            case ".mp3":
                file_dict = _read_tags_mp3(path)
            case _:  # pragma: no cover — only FLAC/MP3 reach this helper via journal entries
                log.warning("albumid_tag_unsupported_format", path=str(path), suffix=ext)
                return ""
        return file_dict.get("MUSICBRAINZ_ALBUMID", "")
    except Exception as exc:  # noqa: BLE001 — best-effort read; any failure means unconfirmed/stale
        if _is_enoent(exc):
            # File absent: expected for historical journal paths; adjudicate stale silently.
            return ""
        log.warning("albumid_tag_read_error", path=str(path), exc_type=type(exc).__name__, exc_msg=str(exc))
        return ""


def _check_collisions(dest_files: list[Path]) -> list[Path]:
    """Return the subset of ``dest_files`` that already exist on disk.

    This is the low-level existence check used internally by :func:`_assess_collisions`.
    Callers that need audio-content comparison should use :func:`_assess_collisions` instead.

    :param dest_files: Ordered list of absolute destination paths to check.
    :returns: A (possibly empty) list of paths from ``dest_files`` that already exist.
    """
    return [p for p in dest_files if p.exists()]


def _sha256_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of the file at ``path``.

    Reads the file in 64 KiB chunks to avoid loading large audio files into memory at once.

    :param path: Path to the file to hash.
    :returns: A lowercase hexadecimal SHA-256 digest string.
    :raises OSError: If the file cannot be opened or read.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_tags_flac(path: Path) -> dict[str, str]:
    """Read Vorbis Comment tags from a FLAC file and return them in the same format as :meth:`~TrackTags.to_file_dict`.

    Only the first value of each multi-valued comment is returned.  Keys are uppercased to match :meth:`~TrackTags.to_file_dict`
    output.  The PICTURE block is excluded (cover art is verified separately in :func:`_verify_copy`).

    :param path: Path to the FLAC file to read.
    :returns: A ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    :raises mutagen.MutagenError: If the file cannot be read.
    """
    audio = FLAC(str(path))
    return {k.upper(): v[0] for k, v in audio.items() if v and v[0]}


def _read_tags_mp3(path: Path) -> dict[str, str]:
    """Read ID3v2.4 tags from an MP3 file and return them in the same format as :meth:`~TrackTags.to_file_dict`.

    Reconstructs the standard text-frame fields (``TITLE``, ``ARTIST``, etc.) and all ``TXXX`` frames listed in
    :data:`_MP3_TXXX_MAP` from the inverse mapping.  The ``APIC`` cover-art frame is excluded (cover art is verified
    separately in :func:`_verify_copy`).  The ``TRACKNUMBER`` field is normalised to strip the ``/total`` suffix written
    by :func:`apply_tags_mp3` so that it can be compared directly to :meth:`~TrackTags.to_file_dict` output.

    :param path: Path to the MP3 file to read.
    :returns: A ``{UPPERCASE_KEY: value}`` dict of non-empty tag values.
    :raises mutagen.MutagenError: If the file cannot be read.
    """
    id3 = ID3(str(path))  # type: ignore[no-untyped-call]
    result: dict[str, str] = {}

    # Standard text frames
    _std_frames: dict[str, str] = {
        "TIT2": "TITLE",
        "TPE1": "ARTIST",
        "TPE2": "ALBUMARTIST",
        "TALB": "ALBUM",
        "TPOS": "DISCNUMBER",
        "TDRC": "DATE",
        "TDOR": "ORIGINALDATE",
        "TCOM": "COMPOSER",
        "TPE3": "CONDUCTOR",
        "TPUB": "ORGANIZATION",
    }
    for frame_id, tag_key in _std_frames.items():
        frame = id3.get(frame_id)  # type: ignore[no-untyped-call]
        if frame and str(frame):
            result[tag_key] = str(frame)

    # TRCK may be "N/total" — keep only the track number
    trck = id3.get("TRCK")  # type: ignore[no-untyped-call]
    if trck and str(trck):
        result["TRACKNUMBER"] = str(trck).split("/", maxsplit=1)[0]

    # TXXX frames — invert _MP3_TXXX_MAP (desc → tag_key)
    _txxx_inv = {desc: key for key, desc in _MP3_TXXX_MAP.items()}
    for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
        maybe_key: str | None = _txxx_inv.get(frame.desc)
        if maybe_key and frame.text and frame.text[0]:
            result[maybe_key] = frame.text[0]

    return {k: v for k, v in result.items() if v}


def _verify_copy(
    src_file: Path,
    dest_file: Path,
    tags: TrackTags,
    cover: CoverArt | None,
    src_mtime: float,
) -> None:
    """Verify that ``dest_file`` has been correctly tagged and has the expected modification time.

    Called by :func:`run` after :func:`apply_tags_flac` / :func:`apply_tags_mp3` and :func:`os.utime` have been applied.
    Raw copy integrity (SHA-256 equality before tagging) is verified inline in :func:`run` immediately after
    :func:`shutil.copy2`; this function handles the post-tagging checks:

    1. **Tag round-trip** — tags read back from ``dest_file`` match the expected :meth:`~TrackTags.to_file_dict` output.
    2. **Cover art** — if ``cover`` is provided and available, the embedded image bytes match ``cover.data``.
    3. **mtime** — ``dest_file`` modification time matches ``src_mtime`` (restored by :func:`os.utime`).

    :param src_file: Original source audio file (used for format detection and human-readable error messages).
    :param dest_file: Destination file to inspect.
    :param tags: The :class:`~music_annotator.models.TrackTags` instance that was written to ``dest_file``.
    :param cover: Optional :class:`~music_annotator.models.CoverArt`; cover bytes are verified when ``cover.available``.
    :param src_mtime: Expected ``st_mtime`` value as restored by :func:`os.utime`.
    :raises RuntimeError: If any verification check fails.
    """
    # 1. Tag round-trip
    ext = src_file.suffix.lower()
    match ext:
        case ".flac":
            actual_tags = _read_tags_flac(dest_file)
            expected_tags = tags.to_file_dict()
        case ".mp3":
            actual_tags = _read_tags_mp3(dest_file)
            # apply_tags_mp3 only writes standard frames + _MP3_TXXX_MAP keys; filter to that writable set.
            writable = _MP3_STD_KEYS | frozenset(_MP3_TXXX_MAP)
            expected_tags = {k: v for k, v in tags.to_file_dict().items() if k in writable}
        case _:  # pragma: no cover
            actual_tags = {}
            expected_tags = {}
    if actual_tags != expected_tags:
        missing = {k: expected_tags[k] for k in expected_tags if k not in actual_tags}
        extra = {k: actual_tags[k] for k in actual_tags if k not in expected_tags}
        wrong = {
            k: (expected_tags[k], actual_tags[k])
            for k in expected_tags
            if k in actual_tags and actual_tags[k] != expected_tags[k]
        }
        raise RuntimeError(
            f"tag verification failure for '{dest_file.name}': "
            f"missing={list(missing)}, extra={list(extra)}, wrong={list(wrong)}"
        )

    # 2. Cover art — verify only the 500 px front images that were actually embedded.
    # Back, booklet, medium, and original-resolution front images are written as sidecar files
    # and are not embedded in the audio file; they are not checked here.
    if cover and cover.front:
        expected_pics: list[PictureEntry] = [PictureEntry(pic_type=3, data=img.data) for img in cover.front]

        match ext:
            case ".flac":
                actual_pics = [PictureEntry(pic_type=p.type, data=p.data) for p in FLAC(str(dest_file)).pictures]
            case ".mp3":
                actual_pics = [
                    PictureEntry(pic_type=f.type, data=f.data)
                    for f in ID3(str(dest_file)).getall("APIC")  # type: ignore[no-untyped-call]
                ]
            case _:  # pragma: no cover
                actual_pics = list(expected_pics)

        if actual_pics != expected_pics:
            raise RuntimeError(
                f"cover art verification failure for '{dest_file.name}': "
                f"expected {len(expected_pics)} picture(s), got {len(actual_pics)}"
            )

    # 3. mtime
    dest_mtime = dest_file.stat().st_mtime
    if dest_mtime != src_mtime:
        raise RuntimeError(f"mtime verification failure for '{dest_file.name}': expected {src_mtime}, got {dest_mtime}")


#: Filename of the fallback provenance sidecar written when no ``freedb_disc_N.yaml`` exists.
PROVENANCE_FILENAME: str = "music_annotator_provenance.yaml"


def _collect_work_dir_provenance(
    dest_root: Path,
    journal: TransactionLog,
) -> dict[Path, ProvenanceSidecar]:
    """Derive per-work-dir provenance from ``action == "tagged"`` journal entries.

    Groups entries by work_top_dir (via :func:`~music_annotator._tags._work_top_dir`, which handles
    both legacy two-level and class-prefixed three-level paths introduced by C-CLASS).  For each group, takes
    the entry with the lexicographically earliest ``timestamp`` as the canonical annotation time
    (ISO-8601 strings sort correctly without parsing), and takes the parent of that entry's
    ``source`` path as the origin provenance label.

    Entries whose ``destination`` is not under ``dest_root`` or whose relative path has fewer than
    two parts are silently skipped.

    :param dest_root: Root of the annotated music library.
    :param journal: :class:`~music_annotator.models.TransactionLog` to analyse.
    :returns: A mapping from absolute work_top_dir :class:`~pathlib.Path` to a
        :class:`~music_annotator.models.ProvenanceSidecar` holding the earliest timestamp and
        corresponding source parent.
    """
    # work_top_dir -> list of (timestamp, source) from "tagged" entries
    groups: dict[Path, list[tuple[str, str]]] = {}

    for entry in journal.entries:
        if entry.action != "tagged":
            continue
        try:
            rel = Path(entry.destination).relative_to(dest_root)
        except ValueError:
            continue
        if len(rel.parts) < 2:  # noqa: PLR2004 — min 2 parts required for work_top_dir
            continue
        work_top_dir = _work_top_dir(Path(entry.destination), dest_root)
        groups.setdefault(work_top_dir, []).append((entry.timestamp, entry.source))

    result: dict[Path, ProvenanceSidecar] = {}
    for work_top_dir, ts_src_pairs in groups.items():
        earliest_ts, earliest_src = min(ts_src_pairs, key=lambda x: x[0])
        result[work_top_dir] = ProvenanceSidecar(
            origin_time=earliest_ts,
            origin_source=str(Path(earliest_src).parent),
        )
    return result


def _read_provenance_sidecar(sidecar_path: Path) -> ProvenanceSidecar:
    """Read and parse a provenance sidecar YAML file, returning a :class:`~music_annotator.models.ProvenanceSidecar`.

    Returns an empty :class:`~music_annotator.models.ProvenanceSidecar` when the file is absent,
    unreadable, or does not contain a mapping.

    :param sidecar_path: Absolute path to the YAML sidecar file.
    :returns: A :class:`~music_annotator.models.ProvenanceSidecar` with the fields read from the
        file, or an empty one if the file is absent or unreadable.
    """
    if not sidecar_path.is_file():
        return ProvenanceSidecar()
    try:
        with sidecar_path.open(encoding="utf-8") as fh:
            data: object = yaml.full_load(fh)
        if not isinstance(data, dict):
            return ProvenanceSidecar()
        return ProvenanceSidecar.model_validate(data)
    except Exception:  # noqa: BLE001 — best-effort read; any failure means empty provenance
        return ProvenanceSidecar()


def _write_provenance_fields(sidecar_path: Path, provenance: ProvenanceSidecar) -> None:
    """Merge provenance fields into the YAML sidecar at ``sidecar_path``.

    Reads the existing YAML content (if any), merges the provenance fields in, and writes the
    result back.  Existing keys other than the provenance fields are preserved.

    **Idempotency rules:**

    - ``origin_time`` and ``origin_source`` are written once and never overwritten.  If both are
      already present with the correct values, the file is not modified.
    - ``annotation_tier`` follows the **monotonic-upgrade rule**: it is written when unset (``""``)
      or when the incoming tier ranks strictly higher than the current one.  A re-resolve may raise
      the tier but never lower it.  An empty incoming tier is not written.
    - ``needs_spot_check`` is written whenever ``annotation_tier`` is written.
    - ``accuraterip_summary`` follows the **monotonic-upgrade rule** (C-AR): an incoming populated
      summary (``log_sha256`` non-empty) is written; an incoming empty summary never overwrites a
      populated one.
    - ``applied_case_ids`` follows the **set-union append-only rule**: the incoming set is unioned
      with the recorded set and the sorted union is written when it differs from the existing set.
      An incoming empty list never shrinks or erases the recorded set.

    :param sidecar_path: Absolute path to the YAML sidecar file to update or create.
    :param provenance: The :class:`~music_annotator.models.ProvenanceSidecar` whose fields are
        written.
    :raises yaml.YAMLError: If the existing file cannot be parsed.
    :raises OSError: If the file cannot be written.
    """
    existing: dict[str, object] = {}
    if sidecar_path.is_file():
        with sidecar_path.open(encoding="utf-8") as fh:
            raw: object = yaml.full_load(fh)
        if isinstance(raw, dict):
            existing = dict(raw)

    existing["origin_time"] = provenance.origin_time
    existing["origin_source"] = provenance.origin_source

    # annotation_tier: monotonic-upgrade rule — write only when incoming tier ranks higher.
    incoming_tier_raw = provenance.annotation_tier
    if incoming_tier_raw:
        try:
            incoming_tier = AnnotationTier(str(incoming_tier_raw))
        except ValueError:
            incoming_tier = None  # unrecognised incoming value — skip write
        if incoming_tier is not None:
            current_tier_raw = existing.get("annotation_tier", "")
            should_write = False
            if not current_tier_raw:
                should_write = True
            else:
                try:
                    current_tier = AnnotationTier(str(current_tier_raw))
                    should_write = annotation_tier_rank(incoming_tier) > annotation_tier_rank(current_tier)
                except ValueError:
                    # Current value is not a valid tier — treat as unset, allow write.
                    should_write = True
            if should_write:
                existing["annotation_tier"] = str(incoming_tier)
                existing["needs_spot_check"] = provenance.needs_spot_check

    # accuraterip_summary: monotonic-upgrade rule (C-AR) — write only when incoming is populated
    # (log_sha256 non-empty) and the existing summary is absent or empty.
    incoming_ar = provenance.accuraterip_summary
    if incoming_ar.log_sha256:
        existing_ar_raw = existing.get("accuraterip_summary")
        existing_ar_sha256 = ""
        if isinstance(existing_ar_raw, dict):
            existing_ar_sha256 = str(existing_ar_raw.get("log_sha256", ""))
        if not existing_ar_sha256:
            existing["accuraterip_summary"] = incoming_ar.model_dump()

    # applied_case_ids: set-union append-only rule — union incoming with recorded set and write the
    # sorted union when it differs; an incoming empty list never shrinks or erases the recorded set.
    incoming_ids = provenance.applied_case_ids
    if incoming_ids:
        existing_ids_raw = existing.get("applied_case_ids", [])
        existing_ids: list[str] = [str(v) for v in existing_ids_raw] if isinstance(existing_ids_raw, list) else []
        union_ids = sorted(set(existing_ids) | set(incoming_ids))
        if union_ids != existing_ids:
            existing["applied_case_ids"] = union_ids

    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with sidecar_path.open("w", encoding="utf-8") as fh:
        yaml.dump(existing, fh, allow_unicode=True, default_flow_style=False)


def _find_freedb_sidecar(work_top_dir: Path) -> Path | None:
    """Return the path of the first ``freedb_disc_N.yaml`` file in ``work_top_dir``, or ``None``.

    Scans ``work_top_dir`` for files matching the ``freedb_disc_*.yaml`` pattern and returns the
    first match (sorted for determinism).  Returns ``None`` when no such file exists.

    :param work_top_dir: The work top directory to scan.
    :returns: The path of the first matching sidecar file, or ``None`` if none is found.
    """
    candidates = sorted(work_top_dir.glob("freedb_disc_*.yaml"))
    return candidates[0] if candidates else None


def enrich_origin_time(
    dest_root: Path,
    *,
    dry_run: bool = False,
    _journal: TransactionLog | None = None,
) -> int:
    """Migrate rip/download origin-time from the journal into authoritative sidecar YAML files.

    Reads the transaction journal at ``dest_root``, groups ``action == "tagged"`` entries by
    work_top_dir, and for each work_top_dir writes ``origin_time`` (earliest journal timestamp)
    and ``origin_source`` (parent of the earliest entry's ``source`` rip-path) into the sidecar:

    * If a ``freedb_disc_N.yaml`` file exists in the work_top_dir, the fields are merged into it.
    * Otherwise a ``music_annotator_provenance.yaml`` sibling sidecar is created (or updated).

    This is an idempotent, re-runnable maintenance mode: a second run on a library where all
    sidecars already carry the provenance fields is a no-op.

    The sidecar field convention established here is consumed by W1b's ``rebuild`` subcommand to
    populate ``origin_time`` on reconstructed :class:`~music_annotator.models.TransactionEntry`
    objects.

    When called from the ``maintain`` orchestrator, ``_journal`` carries the pre-read
    :class:`~music_annotator.models.TransactionLog` so the journal is not re-read from disk
    (C-JRNL: journal read once at the top of ``maintain`` and threaded through all passes).

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True``, log planned writes without modifying any files.
    :param _journal: Pre-read journal to use instead of reading from disk.  When ``None``
        (the default), the journal is read from ``dest_root / JOURNAL_FILENAME``.
    :returns: Count of sidecar files written (0 when dry-run or nothing to do).
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal = _journal if _journal is not None else read_journal(journal_path)

    provenance_map = _collect_work_dir_provenance(dest_root, journal)

    if not provenance_map:
        log.info("enrich_origin_time_nothing_to_do", dest_root=str(dest_root))
        return 0

    count_written = 0
    count_noop = 0
    count_dry_run = 0

    for work_top_dir, provenance in sorted(provenance_map.items()):
        sidecar_path = _find_freedb_sidecar(work_top_dir)
        if sidecar_path is None:
            sidecar_path = work_top_dir / PROVENANCE_FILENAME

        existing = _read_provenance_sidecar(sidecar_path)
        if existing.origin_time == provenance.origin_time and existing.origin_source == provenance.origin_source:
            log.debug(
                "enrich_origin_time_noop",
                sidecar=str(sidecar_path.relative_to(dest_root)),
            )
            count_noop += 1
            continue

        if dry_run:
            log.info(
                "enrich_origin_time_dry_run",
                sidecar=str(sidecar_path.relative_to(dest_root)),
                origin_time=provenance.origin_time,
                origin_source=provenance.origin_source,
            )
            count_dry_run += 1
            continue

        _write_provenance_fields(sidecar_path, provenance)
        log.info(
            "enrich_origin_time_written",
            sidecar=str(sidecar_path.relative_to(dest_root)),
            origin_time=provenance.origin_time,
            origin_source=provenance.origin_source,
        )
        count_written += 1

    log.info(
        "enrich_origin_time_complete",
        dest_root=str(dest_root),
        written=count_written,
        noop=count_noop,
        dry_run=count_dry_run,
    )
    return count_written


#: Audio file extensions that ``rebuild_journal`` reconstructs as ``action="tagged"`` entries.
_REBUILD_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3"})

#: Sidecar file extensions that ``rebuild_journal`` reconstructs as ``action="sidecar"`` entries.
_REBUILD_SIDECAR_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml", ".jpg", ".jpeg", ".png", ".pdf", ".json"})

#: Filenames that ``rebuild_journal`` always skips (the journal itself and disc-info sidecars that
#: are not provenance sidecars).
_REBUILD_SKIP_FILENAMES: frozenset[str] = frozenset({JOURNAL_FILENAME})


def _read_albumid_from_tags(path: Path) -> str:
    """Read ``MUSICBRAINZ_ALBUMID`` from a FLAC or MP3 file's tags, returning ``""`` on failure.

    Delegates to :func:`_read_tags_flac` or :func:`_read_tags_mp3` and extracts the
    ``MUSICBRAINZ_ALBUMID`` key.  Returns ``""`` for unsupported extensions or on any read error.

    :param path: Path to the audio file.
    :returns: The release MBID string, or ``""`` if absent or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                tags = _read_tags_flac(path)
            case ".mp3":
                tags = _read_tags_mp3(path)
            case _:  # pragma: no cover — only called for .flac/.mp3 by rebuild_journal
                return ""  # pragma: no cover
        return tags.get("MUSICBRAINZ_ALBUMID", "")
    except Exception:  # noqa: BLE001 — best-effort read; any failure means no release ID
        return ""


def _mtime_iso(path: Path) -> str:
    """Return the modification time of ``path`` as an ISO-8601 UTC string.

    Uses ``datetime.datetime.fromtimestamp`` with ``datetime.UTC`` to convert the POSIX mtime to
    a timezone-aware UTC datetime, then formats it with ``isoformat()``.

    :param path: Path to the file whose mtime is read.
    :returns: ISO-8601 UTC timestamp string (e.g. ``"2024-06-01T08:00:00+00:00"``).
    :raises OSError: If the file cannot be stat'd.
    """
    mtime = path.stat().st_mtime
    return datetime.datetime.fromtimestamp(mtime, tz=datetime.UTC).isoformat()


def _read_origin_time_for_dir(work_top_dir: Path) -> str:
    """Read the ``origin_time`` field from the provenance sidecar in ``work_top_dir``.

    Looks first for a ``freedb_disc_N.yaml`` sidecar, then falls back to
    ``music_annotator_provenance.yaml``.  Returns ``""`` when neither file exists or neither
    carries an ``origin_time`` field.

    :param work_top_dir: The work top directory (two levels below ``dest_root``).
    :returns: The ``origin_time`` ISO-8601 string, or ``""`` if unavailable.
    """
    sidecar = _find_freedb_sidecar(work_top_dir)
    if sidecar is None:
        sidecar = work_top_dir / PROVENANCE_FILENAME
    return _read_provenance_sidecar(sidecar).origin_time


def _rebuild_audio_entry(path: Path, dest_root: Path, origin_time: str) -> TransactionEntry:
    """Reconstruct a ``TransactionEntry`` for a single audio file during ``rebuild_journal``.

    Reads the file's embedded tags to obtain ``release_id``, ``acoustid_fingerprint``, and
    ``acoustid_id``; recomputes ``audio_hash`` from the decoded audio content; and derives
    ``timestamp`` from the file's mtime.  The fingerprint is read via dual-read (new Picard-aligned
    key first, legacy key second) so files not yet migrated to the new key are handled correctly.

    :param path: Absolute path to the FLAC or MP3 file.
    :param dest_root: Root of the annotated music library (used only for log messages).
    :param origin_time: ISO-8601 origin time from the work directory's provenance sidecar, or
        ``""`` when no sidecar is present.
    :returns: A :class:`~music_annotator.models.TransactionEntry` with ``action="tagged"``.
    """
    release_id = _read_albumid_from_tags(path)
    acoustid_fingerprint = _read_acoustid_fingerprint_tag(path)
    acoustid_id = _read_acoustid_tag(path)
    audio_hash = _audio_hash(path)
    try:
        timestamp = _mtime_iso(path)
    except OSError:  # pragma: no cover — pyfakefs always provides mtime; defensive guard only
        log.warning("rebuild_mtime_error", path=str(path.relative_to(dest_root)))  # pragma: no cover
        timestamp = ""  # pragma: no cover

    return TransactionEntry(
        timestamp=timestamp,
        release_id=release_id,
        source=str(path),
        destination=str(path),
        action="tagged",
        audio_hash=audio_hash,
        acoustid_fingerprint=acoustid_fingerprint,
        acoustid_id=acoustid_id,
        origin_time=origin_time,
    )


def _rebuild_sidecar_entry(path: Path, dest_root: Path, origin_time: str) -> TransactionEntry:
    """Reconstruct a ``TransactionEntry`` for a single sidecar file during ``rebuild_journal``.

    Derives ``timestamp`` from the file's mtime.  ``release_id`` and identity fields are empty
    for sidecar entries (sidecars are not audio files and carry no MB identity tags).

    :param path: Absolute path to the sidecar file.
    :param dest_root: Root of the annotated music library (used only for log messages).
    :param origin_time: ISO-8601 origin time from the work directory's provenance sidecar, or
        ``""`` when no sidecar is present.
    :returns: A :class:`~music_annotator.models.TransactionEntry` with ``action="sidecar"``.
    """
    try:
        timestamp = _mtime_iso(path)
    except OSError:  # pragma: no cover — pyfakefs always provides mtime; defensive guard only
        log.warning("rebuild_mtime_error", path=str(path.relative_to(dest_root)))  # pragma: no cover
        timestamp = ""  # pragma: no cover

    return TransactionEntry(
        timestamp=timestamp,
        release_id="",
        source=str(path),
        destination=str(path),
        action="sidecar",
        origin_time=origin_time,
    )


def rebuild_journal(dest_root: Path, *, dry_run: bool = True) -> TransactionLog:
    """Walk ``dest_root``, read tags and sidecars per file, and emit a new :class:`TransactionLog`.

    Reconstructs the transaction journal from the library on disk, proving the
    database-as-infrastructure claim: the journal is regenerable from the tracks and sidecars alone.

    Each reconstructed :class:`~music_annotator.models.TransactionEntry` carries:

    * ``destination`` — the file's current absolute path.
    * ``release_id`` — from the ``MUSICBRAINZ_ALBUMID`` tag (audio files only).
    * ``audio_hash`` — recomputed from decoded audio content (audio files only).
    * ``acoustid_fingerprint`` — read from the ``ACOUSTID_FINGERPRINT`` tag (new key) or the legacy
      ``CHROMAPRINT_FP`` tag (dual-read; audio files only).
    * ``acoustid_id`` — read from the ``ACOUSTID_ID`` tag (audio files only).
    * ``timestamp`` — annotation time from the file's mtime, ISO-8601 UTC.
    * ``origin_time`` — from the ``freedb_disc_N.yaml`` or ``music_annotator_provenance.yaml``
      sidecar in the file's work top directory (populated by ``enrich --origin-time``).
    * ``action`` — ``"tagged"`` for audio files, ``"sidecar"`` for sidecar files.

    The ``source`` field is set equal to ``destination`` (the file's current path) because
    ``rebuild`` operates offline from the original rip source.

    **Dry-run default**: when ``dry_run=True`` (the default), the rebuilt log is returned but the
    journal file on disk is **not** modified.  Pass ``dry_run=False`` to replace
    ``music_annotator_journal.json`` with the rebuilt log.

    The walk covers the two-level ``<top_dir>/<work_dir>/`` structure.  Files directly under
    ``dest_root`` (e.g. the journal itself) are skipped.  The journal filename is always excluded.

    :param dest_root: Root of the annotated music library (contains
        ``music_annotator_journal.json``).
    :param dry_run: When ``True`` (default), return the rebuilt log without writing it to disk.
        When ``False``, replace ``music_annotator_journal.json`` with the rebuilt log.
    :returns: A :class:`~music_annotator.models.TransactionLog` with all reconstructed entries,
        sorted by destination path for deterministic output.
    """
    entries: list[TransactionEntry] = []

    # Walk the library structure, handling both legacy two-level paths
    # (dest_root/<top_dir>/<work_dir>/…) and class-prefixed three-level paths
    # (dest_root/<class>/<top_dir>/<work_dir>/…) introduced by C-CLASS.
    # Files directly under dest_root are skipped (they are not library files).
    if not dest_root.is_dir():
        log.warning("rebuild_dest_root_missing", dest_root=str(dest_root))
        return TransactionLog()

    for top_dir in sorted(dest_root.iterdir()):
        if not top_dir.is_dir():
            continue
        if top_dir.name in _CLASS_VOCAB:
            # Class-prefixed three-level path: top_dir is the class; iterate one level deeper
            # to find the actual <top_dir>/<work_dir> pairs.
            for artist_dir in sorted(top_dir.iterdir()):
                if not artist_dir.is_dir():
                    continue
                for work_dir in sorted(artist_dir.iterdir()):
                    if not work_dir.is_dir():
                        continue
                    origin_time = _read_origin_time_for_dir(work_dir)
                    for file_path in sorted(work_dir.rglob("*")):
                        if not file_path.is_file():
                            continue
                        if file_path.name in _REBUILD_SKIP_FILENAMES:
                            continue
                        ext = file_path.suffix.lower()
                        if ext in _REBUILD_AUDIO_EXTENSIONS:
                            entries.append(_rebuild_audio_entry(file_path, dest_root, origin_time))
                        elif ext in _REBUILD_SIDECAR_EXTENSIONS:
                            entries.append(_rebuild_sidecar_entry(file_path, dest_root, origin_time))
        else:
            # Legacy two-level path: top_dir is the <composer> - <performers> dir.
            for work_dir in sorted(top_dir.iterdir()):
                if not work_dir.is_dir():
                    continue
                # Read origin_time once per work_dir (shared by all files in the directory tree)
                origin_time = _read_origin_time_for_dir(work_dir)
                # Walk the work_dir tree (may have sub-directories for intermediate divisions)
                for file_path in sorted(work_dir.rglob("*")):
                    if not file_path.is_file():
                        continue
                    if file_path.name in _REBUILD_SKIP_FILENAMES:
                        continue
                    ext = file_path.suffix.lower()
                    if ext in _REBUILD_AUDIO_EXTENSIONS:
                        entries.append(_rebuild_audio_entry(file_path, dest_root, origin_time))
                    elif ext in _REBUILD_SIDECAR_EXTENSIONS:
                        entries.append(_rebuild_sidecar_entry(file_path, dest_root, origin_time))
                    # Files with other extensions (e.g. .cue, .log) are silently skipped.

    log.info(
        "rebuild_journal_complete",
        dest_root=str(dest_root),
        total=len(entries),
        dry_run=dry_run,
    )

    rebuilt = TransactionLog(entries=entries)

    if not dry_run:
        journal_path = dest_root / JOURNAL_FILENAME
        # Write atomically: build JSONL content in a temp file, then os.replace to avoid a
        # truncate-then-write window that would destroy the journal on crash or full disk.
        tmp_path = journal_path.with_suffix(journal_path.suffix + ".tmp")
        lines = [json.dumps(e.model_dump(), ensure_ascii=False) + "\n" for e in rebuilt.entries]
        tmp_path.write_text("".join(lines), encoding="utf-8")
        os.replace(str(tmp_path), str(journal_path))
        log.info("rebuild_journal_written", path=str(journal_path), total=len(entries))

    return rebuilt
