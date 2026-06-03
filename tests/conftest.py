"""Shared test helpers and constants for the music-annotator test suite.

Plain functions (not pytest fixtures) so they can be imported explicitly in each test module.
Hoisted here to eliminate duplicate definitions across test_annotator.py, test_pipeline.py,
test_main.py, and the new test_pipeline_maint.py / test_audit.py modules.
"""

from __future__ import annotations

from music_annotator.models import (
    JSON,
    MBArtistCredit,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
)

# ---------------------------------------------------------------------------
# Typed factory helpers
# ---------------------------------------------------------------------------


def _w(d: dict[str, JSON]) -> MBWork:
    """Validate a raw work dict into an MBWork model.

    :param d: Raw dict matching the musicbrainzngs work response shape.
    :returns: An :class:`~music_annotator.models.MBWork` instance.
    """
    return MBWork.model_validate(d)


def _rec(d: dict[str, JSON]) -> MBRecording:
    """Validate a raw recording dict into an MBRecording model.

    :param d: Raw dict matching the musicbrainzngs recording response shape.
    :returns: An :class:`~music_annotator.models.MBRecording` instance.
    """
    return MBRecording.model_validate(d)


def _rel(d: dict[str, JSON]) -> MBRelease:
    """Validate a raw release dict into an MBRelease model.

    :param d: Raw dict matching the musicbrainzngs release response shape.
    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(d)


def _trk(d: dict[str, JSON]) -> MBTrack:
    """Validate a raw track dict into an MBTrack model.

    :param d: Raw dict matching the musicbrainzngs track response shape.
    :returns: An :class:`~music_annotator.models.MBTrack` instance.
    """
    return MBTrack.model_validate(d)


def _ac(items: list[JSON]) -> list[MBArtistCredit | str]:
    """Validate a raw artist-credit list into typed items.

    :param items: Raw artist-credit list from musicbrainzngs response.
    :returns: A list of :class:`~music_annotator.models.MBArtistCredit` or ``str`` items.
    """
    result: list[MBArtistCredit | str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            result.append(MBArtistCredit.model_validate(item))
    return result


# ---------------------------------------------------------------------------
# Minimal valid FLAC bytes (magic + STREAMINFO block, last-metadata bit set)
# ---------------------------------------------------------------------------

# Valid minimal FLAC: magic + STREAMINFO block (last-metadata, 44100 Hz, 2 ch, 16-bit, 0 samples)
_MINIMAL_FLAC = (
    b"fLaC"
    b"\x80\x00\x00\x22"  # block header: last=1, type=0, length=34
    b"\x10\x00\x10\x00"  # min_blocksize=4096, max_blocksize=4096
    b"\x00\x00\x00"  # min_framesize=0
    b"\x00\x00\x00"  # max_framesize=0
    b"\x0a\xc4\x42\xf0\x00\x00\x00\x00"  # 44100 Hz, 2ch, 16-bit, 0 samples
    b"\x00" * 16  # MD5
)

# ---------------------------------------------------------------------------
# Minimal valid MP3: ID3v2.3 header + one null frame
# ---------------------------------------------------------------------------

_ID3_HEADER = b"ID3\x03\x00\x00" + b"\x00\x00\x00\x00"  # 10-byte header, size 0
_MINIMAL_MP3 = _ID3_HEADER + b"\xff\xfb\x90\x00" + b"\x00" * 413  # one MP3 frame
