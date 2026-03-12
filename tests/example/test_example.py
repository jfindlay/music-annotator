"""Example / integration smoke tests for music_annotator.run().

These tests exercise the full pipeline end-to-end against a fabricated two-track release.  All MusicBrainz API calls are
replaced by pytest-mock stubs and all file I/O is handled by pyfakefs so no real network or disk access occurs.
"""

from __future__ import annotations

import struct
from pathlib import Path

from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator.models import CoverArt, MBRecording, MBRelease, MBWork

# ---------------------------------------------------------------------------
# Minimal FLAC file factory
# ---------------------------------------------------------------------------

_FLAC_MAGIC = b"fLaC"
_STREAMINFO_BLOCK = (
    # block type 0 (STREAMINFO) | last-metadata-block bit | length = 34
    struct.pack(">I", (1 << 31) | (0 << 24) | 34) + bytes(34)  # 34 zero bytes is enough for mutagen to accept the block
)
# A minimal valid FLAC is just the magic + a STREAMINFO block.
_MINIMAL_FLAC = _FLAC_MAGIC + _STREAMINFO_BLOCK


def _make_release(release_id: str = "rel-1") -> MBRelease:
    """Return a minimal but structurally valid MB release model.

    :param release_id: The MBID to use for this release.
    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(
        {
            "id": release_id,
            "title": "Respighi: Fontane di Roma",
            "date": "1995",
            "status": "Official",
            "barcode": "028944972429",
            "artist-credit": [
                {
                    "name": "Karajan",
                    "artist": {
                        "id": "k1",
                        "name": "Herbert von Karajan",
                        "sort-name": "Karajan, Herbert von",
                        "type": "Person",
                    },
                }
            ],
            "release-group": {
                "id": "rg-1",
                "primary-type": "Album",
                "first-release-date": "1995",
            },
            "label-info-list": [
                {
                    "label": {"id": "lab1", "name": "Deutsche Grammophon"},
                    "catalog-number": "449 724-2",
                }
            ],
            "text-representation": {"script": "Latn", "language": "ita"},
            "medium-list": [
                {
                    "position": 1,
                    "format": "CD",
                    "track-list": [
                        {
                            "id": "trk1",
                            "position": 1,
                            "recording": {
                                "id": "rec1",
                                "title": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
                                "artist-credit": [
                                    {
                                        "name": "Karajan",
                                        "artist": {
                                            "id": "k1",
                                            "name": "Herbert von Karajan",
                                            "sort-name": "Karajan, Herbert von",
                                        },
                                    }
                                ],
                            },
                        },
                        {
                            "id": "trk2",
                            "position": 2,
                            "recording": {
                                "id": "rec2",
                                "title": "Fontane di Roma: II. La fontana del Tritone",
                                "artist-credit": [
                                    {
                                        "name": "Karajan",
                                        "artist": {
                                            "id": "k1",
                                            "name": "Herbert von Karajan",
                                            "sort-name": "Karajan, Herbert von",
                                        },
                                    }
                                ],
                            },
                        },
                    ],
                }
            ],
        }
    )


def _make_recording_detail(rec_id: str, track_title: str) -> MBRecording:
    """Return a minimal recording detail model with a single composer relation.

    :param rec_id: The recording MBID.
    :param track_title: The recording title.
    :returns: An :class:`~music_annotator.models.MBRecording` instance.
    """
    return MBRecording.model_validate(
        {
            "id": rec_id,
            "title": track_title,
            "artist-credit": [],
            "artist-relation-list": [
                {
                    "type": "conductor",
                    "artist": {
                        "id": "k1",
                        "name": "Herbert von Karajan",
                        "sort-name": "Karajan, Herbert von",
                    },
                    "attribute-list": [],
                },
                {
                    "type": "performing orchestra",
                    "artist": {
                        "id": "bp1",
                        "name": "Berliner Philharmoniker",
                        "sort-name": "Berliner Philharmoniker",
                    },
                    "attribute-list": [],
                },
            ],
            "work-relation-list": [
                {
                    "type": "performance",
                    "work": {"id": "w1", "title": "Fontane di Roma, P 106"},
                }
            ],
        }
    )


def _make_work_detail() -> MBWork:
    """Return a minimal work model for Fontane di Roma.

    :returns: An :class:`~music_annotator.models.MBWork` instance.
    """
    return MBWork.model_validate(
        {
            "id": "w1",
            "title": "Fontane di Roma, P 106",
            "type": "Symphonic poem",
            "artist-relation-list": [
                {
                    "type": "composer",
                    "artist": {
                        "id": "r1",
                        "name": "Ottorino Respighi",
                        "sort-name": "Respighi, Ottorino",
                    },
                }
            ],
            "work-relation-list": [],
            "tag-list": [{"name": "impressionism"}],
            "attribute-list": [{"type": "composed date", "value": "1916"}],
            "life-span": {"begin": "", "end": "", "ended": False},
        }
    )


# ---------------------------------------------------------------------------
# Helper: wire up all MB stubs
# ---------------------------------------------------------------------------


def _patch_mb(mocker: MockerFixture, release: MBRelease) -> None:
    """Patch all musicbrainzngs calls used by run().

    :param mocker: pytest-mock fixture.
    :param release: The MBRelease model to return from fetch_release.
    """
    mocker.patch("music_annotator._mb_api.mb.set_useragent")
    mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
    mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

    def _rec_detail(rec_id: str) -> MBRecording:
        titles = {
            "rec1": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
            "rec2": "Fontane di Roma: II. La fontana del Tritone",
        }
        return _make_recording_detail(rec_id, titles.get(rec_id, "Unknown"))

    mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_rec_detail)
    mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())


# ---------------------------------------------------------------------------
# Smoke test 1: dry-run mode (no file I/O)
# ---------------------------------------------------------------------------


class TestRunDryRun:
    """Smoke tests for run() in --dry-run mode."""

    def test_dry_run_completes_without_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() in dry-run mode logs planned ops without raising.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/album")
        fs.create_dir(str(src_dir))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)
        dest_root = Path("/dest")
        fs.create_dir(str(dest_root))

        release = _make_release()
        _patch_mb(mocker, release)

        # Should not raise
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=True,
        )

    def test_dry_run_no_files_written(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() in dry-run mode writes no files to dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/album")
        fs.create_dir(str(src_dir))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)
        dest_root = Path("/dest")
        fs.create_dir(str(dest_root))

        release = _make_release()
        _patch_mb(mocker, release)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=True,
        )

        # dest_root should contain no FLAC files
        dest_files = list(dest_root.rglob("*.flac"))
        assert not dest_files


# ---------------------------------------------------------------------------
# Smoke test 2: no-fetch-rels mode (minimal tags, no per-recording calls)
# ---------------------------------------------------------------------------


class TestRunNoFetchRels:
    """Smoke tests for run() with fetch_rels=False."""

    def test_no_fetch_rels_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() with fetch_rels=False + dry_run=True completes without error.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/album2")
        fs.create_dir(str(src_dir))
        fs.create_file(str(src_dir / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02.flac"), contents=_MINIMAL_FLAC)
        dest_root = Path("/dest2")
        fs.create_dir(str(dest_root))

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release())
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        # fetch_recording_detail should NOT be called
        spy = mocker.patch("music_annotator._pipeline.fetch_recording_detail")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=False,
        )

        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Smoke test 3: track-count mismatch just warns, does not raise
# ---------------------------------------------------------------------------


class TestRunTrackCountMismatch:
    """Smoke tests for run() when source file count != release track count."""

    def test_mismatch_does_not_raise(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Track-count mismatch is logged as a warning, not an exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/mismatch")
        fs.create_dir(str(src_dir))
        # Only one source file but release has two tracks
        fs.create_file(str(src_dir / "01.flac"), contents=_MINIMAL_FLAC)
        dest_root = Path("/dest3")
        fs.create_dir(str(dest_root))

        release = _make_release()
        _patch_mb(mocker, release)

        # Should not raise despite mismatch
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=False,
        )
