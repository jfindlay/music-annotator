"""Filesystem I/O helpers for the music-annotator pipeline.

Provides functions for finding source audio files, writing the transaction journal, computing SHA-256
checksums, reading back tags for verification, and verifying copy integrity after tagging.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import structlog
from mutagen.flac import FLAC
from mutagen.id3 import ID3

from music_annotator._tagger import _MP3_STD_KEYS, _MP3_TXXX_MAP
from music_annotator.models import JSON, CoverArt, CoverImage, TrackTags, TransactionEntry, TransactionLog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Supported audio file extensions.
AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".mp3", ".ogg", ".m4a", ".aac", ".wav"})

#: Filename of the JSON transaction journal written inside the destination root.
JOURNAL_FILENAME: str = "music_annotator_journal.json"


def find_source_files(src_dir: Path) -> list[Path]:
    """Return a sorted list of audio files in ``src_dir``.

    Only the immediate children of ``src_dir`` are checked (not recursive).  Files are included when their lowercased
    suffix appears in :data:`AUDIO_EXTENSIONS`.

    :param src_dir: Directory to scan.
    :returns: A list of :class:`~pathlib.Path` objects for all matching files, sorted by filename.
    :raises OSError: If ``src_dir`` does not exist or is not readable.
    """
    return sorted(
        (p for p in src_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: p.name,
    )


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


def _check_collisions(dest_files: list[Path]) -> list[Path]:
    """Return the subset of ``dest_files`` that already exist on disk.

    Used by :func:`run` to identify which planned output files would be overwritten before copying
    begins, so the user can be warned and asked whether to proceed.

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

    # 2. Cover art — build expected (pic_type, data) pairs from all CoverArt fields then compare to file.
    if cover and cover.available:
        expected_pics: list[tuple[int, bytes]] = []
        _cov_groups: list[tuple[int, list[CoverImage]]] = [
            (3, cover.front),
            (4, cover.back),
            (5, cover.booklet),
            (6, cover.medium),
        ]
        for pic_type, images in _cov_groups:
            for img in images:
                expected_pics.append((pic_type, img.data))

        match ext:
            case ".flac":
                actual_pics = [(p.type, p.data) for p in FLAC(str(dest_file)).pictures]
            case ".mp3":
                actual_pics = [
                    (f.type, f.data)
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
