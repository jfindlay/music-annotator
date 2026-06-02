"""Filesystem I/O helpers for the music-annotator pipeline.

Provides functions for finding source audio files, writing the transaction journal, computing SHA-256
checksums, reading back tags for verification, verifying copy integrity after tagging, assessing
audio-content similarity for collision resolution, and performing the pre-flight duration check
against MB track lengths.
"""

from __future__ import annotations

import base64
import hashlib
import json
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
from music_annotator.models import JSON, CoverArt, MBTrack, PictureEntry, TrackTags, TransactionEntry, TransactionLog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Supported audio file extensions.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3", ".ogg", ".m4a", ".aac", ".wav"})

#: Filename of the JSON transaction journal written inside the destination root.
JOURNAL_FILENAME: str = "music_annotator_journal.json"

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
#: rungs (``"sha256"``, ``"duration"``, ``"unknown"``) and archival-identity rungs
#: (``"acoustid"``, ``"chromaprint"``, ``"audio_hash"``, ``"isrc"``).
_IDENTITY_METHODS: frozenset[str] = frozenset(
    {"sha256", "acoustid", "chromaprint", "duration", "unknown", "isrc", "audio_hash"}
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


def _read_chromaprint_fp_tag(path: Path) -> str:
    """Read the ``chromaprint_fp`` tag from a FLAC or MP3 file, returning ``""`` on any failure.

    For FLAC files the Vorbis Comment key ``"chromaprint_fp"`` is looked up (case-insensitive).
    For MP3 files the TXXX frame with description ``"Chromaprint Fingerprint"`` is looked up (from
    :data:`~music_annotator._tagger._MP3_TXXX_MAP`: ``"CHROMAPRINT_FP": "Chromaprint Fingerprint"``).

    :param path: Path to the audio file to inspect.
    :returns: The Chromaprint fingerprint string, or ``""`` if absent or unreadable.
    """
    try:
        match path.suffix.lower():
            case ".flac":
                audio = FLAC(str(path))
                values = audio.get("chromaprint_fp") or audio.get("CHROMAPRINT_FP") or []
                return values[0] if values else ""
            case ".mp3":
                id3 = ID3(str(path))  # type: ignore[no-untyped-call]
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
    (``audio_hash``, ``chromaprint_fp``, ``acoustid_id``) require a write.  Returns a mapping of
    field name → new value for every field that should be updated.

    **Idempotency contract:** a second call on the same file (after the first call's writes have
    been applied) returns an empty dict — no field is written twice unless ``re_resolve=True``
    explicitly requests a re-derivation of ``chromaprint_fp``.

    **Anchor rule (P-FP1):** ``audio_hash`` is the tagging-invariant anchor.  Once written it is
    NEVER overwritten, even under ``re_resolve=True``.  This preserves the ability to detect
    bit-for-bit audio identity across re-tags and format conversions.

    Per-field logic:

    * ``"audio_hash"``: if the tag is empty, compute :func:`_audio_hash` and include the result
      when non-empty.  If the tag is already present, skip unconditionally (anchor rule).
    * ``"chromaprint_fp"``: if the tag is empty, compute :func:`_run_fpcalc` and include when
      non-empty.  If the tag is present and ``re_resolve=True``, recompute and include (overwrite).
      If the tag is present and ``re_resolve=False``, skip.
    * ``"acoustid_id"``: if the tag is present, copy the tag value into the result dict (so the
      journal entry carries the current AcoustID).  If the tag is absent, skip (no network call
      in F4 — logged once as inconclusive).

    :param path: Path to the FLAC or MP3 file to inspect.
    :param re_resolve: When ``True``, recompute ``chromaprint_fp`` even when already present.
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

    # --- chromaprint_fp: compute when absent; re-compute when re_resolve=True ---
    existing_fp = _read_chromaprint_fp_tag(path)
    if not existing_fp:
        computed_fp = _run_fpcalc(path)
        if computed_fp:
            result["chromaprint_fp"] = computed_fp
    elif re_resolve:
        computed_fp = _run_fpcalc(path)
        if computed_fp:
            result["chromaprint_fp"] = computed_fp

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
) -> list[AudioCompareResult]:
    """Assess each planned-destination collision against its source for audio content similarity.

    Filters ``plan_pairs`` to entries whose destination already exists on disk, then calls
    :func:`compare_audio_collision` for each one.  Plan entries with no pre-existing destination
    are omitted from the result.

    :param plan_pairs: A list of ``(src_file, dest_file, acoustid, length_ms)`` tuples, one per
        planned copy operation.  ``acoustid`` is the incoming track's AcoustID UUID (may be ``""``);
        ``length_ms`` is its duration in milliseconds (may be ``0``).
    :returns: A (possibly empty) list of :class:`AudioCompareResult` objects, one per collision.
    """
    results: list[AudioCompareResult] = []
    for src, dest, acoustid, length_ms in plan_pairs:
        if dest.exists():
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


def write_transaction_log(journal_path: Path, new_entries: list[TransactionEntry]) -> None:
    """Append ``new_entries`` to the JSON transaction journal at ``journal_path``.

    If the journal file already exists and contains a valid JSON array of objects, the new entries are
    merged into the existing list before writing.  If the file is absent, corrupt, or empty it is
    (re-)created with only ``new_entries``.

    The journal is written atomically: entries are serialised to a temporary in-memory string first
    and the file is only opened for writing once the serialisation succeeds.

    :param journal_path: Absolute path of the journal file (typically
        ``<dest_root>/music_annotator_journal.json``).
    :param new_entries: The :class:`~music_annotator.models.TransactionEntry` objects to append.
    """
    existing: list[JSON] = []
    if journal_path.exists():
        try:
            raw = journal_path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                existing = parsed
        except (OSError, json.JSONDecodeError):
            log.warning("journal_corrupt_reset", path=str(journal_path))

    combined = TransactionLog(entries=[TransactionEntry.model_validate(e) for e in existing] + new_entries)
    journal_path.write_text(
        json.dumps([e.model_dump() for e in combined.entries], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("journal_written", path=str(journal_path), total=len(combined.entries))


def read_journal(journal_path: Path) -> TransactionLog:
    """Read and parse the JSON transaction journal at ``journal_path``.

    Returns an empty :class:`~music_annotator.models.TransactionLog` when the file is absent (logged
    at INFO level, as the source directory may simply have been pruned already) or when the file
    cannot be parsed (logged at WARNING level, as this indicates unexpected corruption).

    :param journal_path: Absolute path of the journal file (typically
        ``<dest_root>/music_annotator_journal.json``).
    :returns: A :class:`~music_annotator.models.TransactionLog` with all persisted entries, or an
        empty one if the file is absent or unreadable.
    """
    if not journal_path.exists():
        log.info("journal_not_found", path=str(journal_path))
        return TransactionLog()
    try:
        raw = journal_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return TransactionLog(entries=[TransactionEntry.model_validate(e) for e in parsed])
        log.warning("journal_invalid_format", path=str(journal_path))
        return TransactionLog()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("journal_read_error", path=str(journal_path), error=str(exc))
        return TransactionLog()


def _read_albumid_tag(path: Path) -> str:
    """Read the ``MUSICBRAINZ_ALBUMID`` tag from a FLAC or MP3 file, returning ``""`` on any failure.

    Uses suffix-dispatch to invoke :func:`_read_tags_flac` or :func:`_read_tags_mp3`, then
    extracts the ``MUSICBRAINZ_ALBUMID`` key (uppercased by both readers).  On a missing file, an
    unsupported extension, or any read error the function returns ``""`` and logs a warning so the
    caller can treat the entry as unconfirmed/stale without crashing.

    Factored into a dedicated helper so S8 (regroup move) can reuse it and the suffix-dispatch is
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
    except Exception:  # noqa: BLE001 — best-effort read; any failure means unconfirmed/stale
        log.warning("albumid_tag_read_error", path=str(path))
        return ""


def _journal_fragmentation_groups(
    dest_root: Path,
    journal: TransactionLog,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Derive work-dir → release-id and release-id → work-dir groupings from ``action == "tagged"`` journal entries.

    Iterates only ``action == "tagged"`` entries.  For each entry, the ``work_dir`` component is extracted as
    ``Path(e.destination).relative_to(dest_root).parts[1]`` (parts[0] is the top-level performers/composer directory;
    parts[1] is the work directory in the ``<Composer> - <Performers>/<Work [YYYY]>/…`` layout).

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
        if len(rel.parts) < 2:  # noqa: PLR2004 — 2 is a structural constant (parts[0], parts[1])
            continue
        work_dir = rel.parts[1]
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
        if len(rel.parts) < 2:  # noqa: PLR2004 — structural constant (parts[0], parts[1])
            continue
        work_dir = rel.parts[1]
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
    """Read the journal at ``dest_root`` and report release-fragmentation anomalies.

    Reads :data:`JOURNAL_FILENAME` from ``dest_root`` and analyses ``action == "tagged"`` entries to
    surface two fragmentation shapes:

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

    This function is **read-only**: it does not move files or write any journal entries.

    :param dest_root: Root of the annotated music library (contains ``music_annotator_journal.json``).
    """
    journal = read_journal(dest_root / JOURNAL_FILENAME)
    case_a, case_b = _confirm_fragmentation(dest_root, journal)

    if not case_a and not case_b:
        log.info("audit_clean", dest_root=str(dest_root), message="no fragmentation detected")
        return

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
