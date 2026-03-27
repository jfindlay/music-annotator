"""Integration tests for music_annotator.run() and discover().

These tests exercise the full pipeline end-to-end against a fabricated release.  All MusicBrainz API calls are replaced by
pytest-mock stubs and all file I/O is handled by pyfakefs so no real network or disk access occurs.  Internal helpers such as
apply_tags_flac and _verify_copy are not patched, so the real mutagen write-and-read-back path executes.
"""

# pylint: disable=duplicate-code  # test helper factories intentionally mirror test_pipeline.py scaffolding

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import JOURNAL_FILENAME, CollisionPolicy
from music_annotator._discover import DiscoverUI
from music_annotator.models import CoverArt, CoverImage, MBMedium, MBRecording, MBRelease, MBReleaseCandidate, MBWork

# ---------------------------------------------------------------------------
# Minimal audio file byte sequences
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

# Minimal valid MP3: ID3v2.3 header (10 bytes, size 0) + one MPEG frame header + padding.
_ID3_HEADER = b"ID3\x03\x00\x00" + b"\x00\x00\x00\x00"
_MINIMAL_MP3 = _ID3_HEADER + b"\xff\xfb\x90\x00" + b"\x00" * 413


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

    def test_mismatch_raises_runtime_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Track-count mismatch raises RuntimeError with a descriptive message.

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

        with pytest.raises(RuntimeError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src_dir,
                dest_root=dest_root,
                user_agent="Test/1.0",
                dry_run=True,
                fetch_rels=False,
            )


# ---------------------------------------------------------------------------
# Smoke test 4: full non-dry-run — real FLAC tagging + journal written
# ---------------------------------------------------------------------------


class TestRunFullNonDryRunFlac:
    """End-to-end non-dry-run smoke test: FLAC files are copied, tagged, and journalled.

    Unlike the unit tests in test_pipeline.py this class does NOT patch apply_tags_flac
    or _verify_copy, so the real mutagen write path executes against minimal FLAC bytes
    and the post-copy verification round-trip is exercised.
    """

    def test_flac_files_tagged_and_journalled(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Two FLAC tracks are copied to dest, tagged with TITLE/ALBUMARTIST, and a journal is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/respighi")
        dest_root = Path("/dest")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        release = _make_release()
        _patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # Two FLAC files should be present somewhere under dest_root
        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 2

        # Each file should carry at minimum a TITLE and ALBUMARTIST Vorbis comment
        for flac_path in flac_files:
            tags = FLAC(str(flac_path))
            assert tags.get("TITLE"), f"TITLE missing in {flac_path.name}"
            assert tags.get("ALBUMARTIST"), f"ALBUMARTIST missing in {flac_path.name}"

        # Journal file should exist and record two "tagged" actions
        journal_path = dest_root / JOURNAL_FILENAME
        assert journal_path.exists()
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 2
        assert all(e["action"] == "tagged" for e in entries)


# ---------------------------------------------------------------------------
# Smoke test 5: full non-dry-run — MP3 source files are copied and ID3-tagged
# ---------------------------------------------------------------------------


class TestRunFullNonDryRunMp3:
    """End-to-end non-dry-run smoke test using MP3 source files.

    Exercises the apply_tags_mp3 code path, which writes ID3v2 frames, and
    confirms that the correct ID3 tags are present in the copied file.
    """

    def test_mp3_files_tagged_and_journalled(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Two MP3 tracks are copied to dest, ID3-tagged, and a journal is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/respighi_mp3")
        dest_root = Path("/dest_mp3")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.mp3"), contents=_MINIMAL_MP3)
        fs.create_file(str(src_dir / "02 - track2.mp3"), contents=_MINIMAL_MP3)

        release = _make_release()
        _patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        mp3_files = sorted(dest_root.rglob("*.mp3"))
        assert len(mp3_files) == 2

        # Each file should carry at minimum a TIT2 (title) and TPE2 (album artist) ID3 frame
        for mp3_path in mp3_files:
            id3 = ID3(str(mp3_path))  # type: ignore[no-untyped-call]
            assert "TIT2" in id3, f"TIT2 missing in {mp3_path.name}"
            assert "TPE2" in id3, f"TPE2 missing in {mp3_path.name}"

        journal_path = dest_root / JOURNAL_FILENAME
        assert journal_path.exists()
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 2
        assert all(e["action"] == "tagged" for e in entries)


# ---------------------------------------------------------------------------
# Smoke test 6: work hierarchy — two-level parent produces cwp_work_0/1 tags
# ---------------------------------------------------------------------------


def _make_recording_with_movement(rec_id: str, track_title: str) -> MBRecording:
    """Return a recording whose performance relation points to a movement-level work.

    The work itself has a ``"parts"`` backward relation to a parent symphony work,
    so build_work_hierarchy will return a two-element list and the pipeline will
    write both ``cwp_work_0`` and ``cwp_work_1`` extra tags.

    :param rec_id: Recording MBID.
    :param track_title: Recording title.
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
                    "artist": {"id": "k1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                    "attribute-list": [],
                }
            ],
            "work-relation-list": [{"type": "performance", "work": {"id": "w-movement", "title": "I. Allegro con brio"}}],
        }
    )


def _make_movement_work() -> MBWork:
    """Return a movement-level work with a backward ``"parts"`` relation to a parent symphony.

    This causes build_work_hierarchy to fetch the parent (stubbed as ``_make_parent_work``)
    and return a two-level hierarchy.

    :returns: An :class:`~music_annotator.models.MBWork` instance.
    """
    return MBWork.model_validate(
        {
            "id": "w-movement",
            "title": "I. Allegro con brio",
            "type": "",
            "artist-relation-list": [],
            "work-relation-list": [
                {
                    "type": "parts",
                    "direction": "backward",
                    "work": {"id": "w-symphony", "title": "Symphony No. 5 in C minor, Op. 67"},
                }
            ],
            "tag-list": [],
            "attribute-list": [],
        }
    )


def _make_parent_work() -> MBWork:
    """Return the top-level symphony work (parent of the movement).

    :returns: An :class:`~music_annotator.models.MBWork` instance.
    """
    return MBWork.model_validate(
        {
            "id": "w-symphony",
            "title": "Symphony No. 5 in C minor, Op. 67",
            "type": "Symphony",
            "artist-relation-list": [
                {
                    "type": "composer",
                    "artist": {"id": "beet1", "name": "Ludwig van Beethoven", "sort-name": "Beethoven, Ludwig van"},
                }
            ],
            "work-relation-list": [],
            "tag-list": [{"name": "classical"}],
            "attribute-list": [{"type": "key", "value": "C minor"}],
        }
    )


class TestRunWorkHierarchy:
    """End-to-end smoke test for a two-level work hierarchy (movement → symphony).

    Exercises the full build_work_hierarchy + build_track_tags pipeline path and
    confirms that both cwp_work_0 (the movement) and cwp_work_1 (the symphony) are
    written into the output FLAC file as extra Vorbis comment tags.
    """

    def test_two_level_hierarchy_writes_cwp_work_tags(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """cwp_work_0 and cwp_work_1 are written when the work has a parent.

        A recording linked to a movement work that itself is ``"parts"`` of a symphony causes
        ``build_work_hierarchy`` to return a two-element list.  The pipeline then writes
        ``cwp_work_0`` (movement title) and ``cwp_work_1`` (symphony title) into the FLAC tags.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/beethoven")
        dest_root = Path("/dest_beethoven")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - allegro.flac"), contents=_MINIMAL_FLAC)

        # Single-track release for Beethoven 5 I. Allegro
        release = MBRelease.model_validate(
            {
                "id": "rel-beethoven",
                "title": "Beethoven: Symphony No. 5",
                "date": "1963",
                "status": "Official",
                "barcode": "",
                "artist-credit": [
                    {
                        "name": "Karajan",
                        "artist": {"id": "k1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                    }
                ],
                "release-group": {"id": "rg-b5", "primary-type": "Album", "first-release-date": "1963"},
                "label-info-list": [],
                "text-representation": {"script": "Latn", "language": "deu"},
                "medium-list": [
                    {
                        "position": 1,
                        "format": "CD",
                        "track-list": [
                            {
                                "id": "trk-b1",
                                "position": 1,
                                "recording": {
                                    "id": "rec-b1",
                                    "title": "I. Allegro con brio",
                                    "artist-credit": [],
                                },
                            }
                        ],
                    }
                ],
            }
        )

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            return_value=_make_recording_with_movement("rec-b1", "I. Allegro con brio"),
        )
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        # _get_bottom_work calls fetch_work_detail via _mb_api's binding (the embedded work is a stub)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_movement_work())
        # build_work_hierarchy calls fetch_work_detail via _works's binding to fetch the parent
        mocker.patch("music_annotator._works.fetch_work_detail", return_value=_make_parent_work())

        music_annotator.run(
            release_id="rel-beethoven",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files = list(dest_root.rglob("*.flac"))
        assert len(flac_files) == 1

        tags = FLAC(str(flac_files[0]))
        # cwp_work_0 holds the movement title, cwp_work_1 holds the parent symphony title
        assert tags.get("cwp_work_0"), "cwp_work_0 (movement title) not written"
        assert tags.get("cwp_work_1"), "cwp_work_1 (symphony title) not written"
        assert "Allegro" in tags["cwp_work_0"][0]
        assert "Symphony" in tags["cwp_work_1"][0]


# ---------------------------------------------------------------------------
# Smoke test 7: fetch_rels=False non-dry-run — fast-path copy with minimal tags
# ---------------------------------------------------------------------------


class TestRunFastPath:
    """End-to-end smoke test for fetch_rels=False non-dry-run mode.

    In this mode the pipeline skips all per-recording API calls and builds tags
    directly from the release model.  This is the fast path used when the caller
    knows it does not need full Classical Extras metadata.
    """

    def test_fast_path_copies_and_tags_without_recording_calls(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Files are copied and minimally tagged without any fetch_recording_detail calls.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/fast")
        dest_root = Path("/dest_fast")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release())
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        spy = mocker.patch("music_annotator._pipeline.fetch_recording_detail")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        # No per-recording calls should have been made
        spy.assert_not_called()

        # Both files should still be present, tagged, and journalled
        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 2

        for flac_path in flac_files:
            tags = FLAC(str(flac_path))
            assert tags.get("ALBUM"), f"ALBUM missing in {flac_path.name}"

        journal_path = dest_root / JOURNAL_FILENAME
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Smoke test 8: discover() end-to-end with a stub DiscoverUI
# ---------------------------------------------------------------------------


class TestDiscoverEndToEnd:
    """End-to-end smoke tests for discover() using a stub DiscoverUI.

    These tests wire the full discover → run pipeline with all network calls
    mocked, demonstrating the complete "search → select → copy → (optionally delete)"
    workflow as a user would experience it.
    """

    def _make_candidate(self) -> MBReleaseCandidate:
        """Return a single release candidate for the Respighi release.

        :returns: An :class:`~music_annotator.models.MBReleaseCandidate` instance.
        """
        return MBReleaseCandidate(release_id="rel-1", score=95, title="Respighi: Fontane di Roma", artist="Karajan")

    def test_discover_selects_candidate_and_calls_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """discover() finds a candidate, the UI selects it, and run() is invoked for the directory.

        The source directory is left intact because confirm_delete returns False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/Fontane di Roma")
        dest_root = Path("/dest_discover")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[self._make_candidate()])
        _patch_mb(mocker, _make_release())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        class _AutoSelectUI:
            """Stub DiscoverUI: always picks the first candidate and never deletes."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return the first candidate's release_id unconditionally."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Decline deletion."""
                return False

        stub: DiscoverUI = _AutoSelectUI()
        music_annotator.discover(
            src_dirs=[src_dir],
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=stub,
        )

        # Source directory must still exist (confirm_delete returned False)
        assert src_dir.exists()
        # Two tagged FLAC files should be in the destination
        assert len(list(dest_root.rglob("*.flac"))) == 2

    def test_discover_deletes_source_after_successful_copy(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """discover() removes the source directory when confirm_delete returns True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/Fontane di Roma 2")
        dest_root = Path("/dest_discover2")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[self._make_candidate()])
        _patch_mb(mocker, _make_release())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        class _DeleteUI:
            """Stub DiscoverUI: always picks the first candidate and always confirms deletion."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return the first candidate's release_id unconditionally."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Confirm deletion."""
                return True

        stub: DiscoverUI = _DeleteUI()
        music_annotator.discover(
            src_dirs=[src_dir],
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            delete=True,
            ui=stub,
        )

        # Source directory must have been removed
        assert not src_dir.exists()
        # Destination files should exist
        assert len(list(dest_root.rglob("*.flac"))) == 2

    def test_discover_skip_leaves_source_untouched(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """discover() leaves the source directory intact when the UI skips the directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/Fontane di Roma 3")
        dest_root = Path("/dest_discover3")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[self._make_candidate()])
        mock_run = mocker.patch("music_annotator._discover.run")

        class _SkipUI:
            """Stub DiscoverUI: always skips."""

            def choose_release(self, _src_dir: object, _candidates: list[MBReleaseCandidate]) -> str | None:
                """Return None to skip."""
                return None

            def confirm_disc(
                self, _m: object, proposed: MBMedium, _d: object, _u: object
            ) -> MBMedium | None:  # pragma: no cover
                """Should never be called when skipping."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Should never be called when skipping."""
                return False  # pragma: no cover

        stub: DiscoverUI = _SkipUI()
        music_annotator.discover(
            src_dirs=[src_dir],
            dest_root=dest_root,
            user_agent="Test/1.0",
            ui=stub,
        )

        mock_run.assert_not_called()
        assert src_dir.exists()

    def test_discover_collision_policy_forwarded_to_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """discover() forwards the collision_policy argument to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/Fontane di Roma 4")
        dest_root = Path("/dest_discover4")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[self._make_candidate()])
        mock_run = mocker.patch("music_annotator._discover.run")

        class _AutoSelectUI:
            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return the first candidate's release_id."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Decline deletion."""
                return False

        stub: DiscoverUI = _AutoSelectUI()
        music_annotator.discover(
            src_dirs=[src_dir],
            dest_root=dest_root,
            user_agent="Test/1.0",
            collision_policy=CollisionPolicy.SKIP,
            ui=stub,
        )

        _, kwargs = mock_run.call_args
        assert kwargs.get("collision_policy") == CollisionPolicy.SKIP


# ---------------------------------------------------------------------------
# Smoke test 9: collision policy SKIP — second run journals "skipped" entries
# ---------------------------------------------------------------------------


class TestRunCollisionPolicySkip:
    """End-to-end smoke test for CollisionPolicy.SKIP.

    The first run copies two FLAC files.  The second run targets the same release with the same
    source files so all destination paths already exist.  With ``collision_policy=SKIP`` the
    pipeline must leave the existing files untouched and record ``"skipped"`` journal entries.
    """

    def test_skip_policy_journals_skipped_entries(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Second run with SKIP policy writes 'skipped' journal entries for colliding destinations.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/collision_skip")
        dest_root = Path("/dest_collision_skip")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        release = _make_release()
        _patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        # First run — copies both files and creates the journal.
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files_first = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files_first) == 2
        first_mtime = flac_files_first[0].stat().st_mtime

        # Second run — destinations exist; SKIP policy must leave them untouched.
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            collision_policy=CollisionPolicy.SKIP,
        )

        # Files must be unchanged (mtime preserved by SKIP).
        assert flac_files_first[0].stat().st_mtime == first_mtime

        # Journal must now contain four entries: 2 "tagged" + 2 "skipped".
        journal_path = dest_root / JOURNAL_FILENAME
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 4
        actions = [e["action"] for e in entries]
        assert actions.count("tagged") == 2
        assert actions.count("skipped") == 2


# ---------------------------------------------------------------------------
# Smoke test 10: collision policy OVERWRITE — second run replaces existing files
# ---------------------------------------------------------------------------


class TestRunCollisionPolicyOverwrite:
    """End-to-end smoke test for CollisionPolicy.OVERWRITE.

    The first run copies two FLAC files.  The second run uses OVERWRITE, which must replace the
    existing files (producing updated mtimes) and journal both as ``"tagged"``.
    """

    def test_overwrite_policy_replaces_existing_files(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Second run with OVERWRITE policy replaces existing destination files.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/collision_overwrite")
        dest_root = Path("/dest_collision_overwrite")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        release = _make_release()
        _patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        # First run.
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 2

        # Second run with OVERWRITE — must not raise even though files already exist.
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            collision_policy=CollisionPolicy.OVERWRITE,
        )

        # Journal: 4 entries total, all "tagged".
        journal_path = dest_root / JOURNAL_FILENAME
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 4
        assert all(e["action"] == "tagged" for e in entries)


# ---------------------------------------------------------------------------
# Smoke test 11: collision policy ABORT — second run raises SystemExit
# ---------------------------------------------------------------------------


class TestRunCollisionPolicyAbort:
    """End-to-end smoke test for CollisionPolicy.ABORT.

    The first run copies two FLAC files.  The second run uses ABORT, which must raise
    ``SystemExit(1)`` without touching any destination files.
    """

    def test_abort_policy_raises_system_exit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Second run with ABORT policy raises SystemExit when destinations already exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/collision_abort")
        dest_root = Path("/dest_collision_abort")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        release = _make_release()
        _patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        # First run — establishes the destination files.
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # Second run with ABORT — must raise SystemExit.
        with pytest.raises(SystemExit) as exc_info:
            music_annotator.run(
                release_id="rel-1",
                src_dir=src_dir,
                dest_root=dest_root,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=True,
                collision_policy=CollisionPolicy.ABORT,
            )
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Smoke test 12: multi-disc release — _select_medium picks the right disc
# ---------------------------------------------------------------------------


def _make_two_disc_release() -> MBRelease:
    """Return a two-disc release: disc 1 has 2 tracks, disc 2 has 3 tracks.

    Used to test that :func:`_select_medium` (called inside :func:`run`) correctly
    selects the medium whose track count matches the number of source files in the
    directory.

    :returns: An :class:`~music_annotator.models.MBRelease` instance with two mediums.
    """
    disc1_tracks = [
        {"id": "t1-1", "position": 1, "recording": {"id": "r1-1", "title": "Disc 1 Track 1", "artist-credit": []}},
        {"id": "t1-2", "position": 2, "recording": {"id": "r1-2", "title": "Disc 1 Track 2", "artist-credit": []}},
    ]
    disc2_tracks = [
        {"id": "t2-1", "position": 1, "recording": {"id": "r2-1", "title": "Disc 2 Track 1", "artist-credit": []}},
        {"id": "t2-2", "position": 2, "recording": {"id": "r2-2", "title": "Disc 2 Track 2", "artist-credit": []}},
        {"id": "t2-3", "position": 3, "recording": {"id": "r2-3", "title": "Disc 2 Track 3", "artist-credit": []}},
    ]
    return MBRelease.model_validate(
        {
            "id": "rel-2disc",
            "title": "Two-Disc Album",
            "date": "2000",
            "status": "Official",
            "barcode": "",
            "artist-credit": [
                {
                    "name": "Artist",
                    "artist": {"id": "a1", "name": "Test Artist", "sort-name": "Artist, Test", "type": "Person"},
                }
            ],
            "release-group": {"id": "rg-2d", "primary-type": "Album", "first-release-date": "2000"},
            "label-info-list": [],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": [
                {"position": 1, "format": "CD", "track-list": disc1_tracks},
                {"position": 2, "format": "CD", "track-list": disc2_tracks},
            ],
        }
    )


class TestRunMultiDisc:
    """End-to-end smoke test for multi-disc release selection.

    When a release has two mediums with different track counts, :func:`run` calls
    :func:`_select_medium` to identify the matching medium.  This test verifies
    that the correct disc is selected based on the number of source files.
    """

    def test_selects_disc_by_track_count(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() selects the medium whose track count matches the source directory file count.

        The source directory contains 3 FLAC files, matching disc 2 of the two-disc release.
        The pipeline must copy and tag exactly those 3 tracks.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/disc2")
        dest_root = Path("/dest_multidisc")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        for i in range(1, 4):
            fs.create_file(str(src_dir / f"0{i} - track{i}.flac"), contents=_MINIMAL_FLAC)

        release = _make_two_disc_release()
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        music_annotator.run(
            release_id="rel-2disc",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        # Exactly 3 FLAC files should be in the destination (disc 2 tracks).
        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 3

        # Journal should record 3 "tagged" entries.
        journal_path = dest_root / JOURNAL_FILENAME
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 3
        assert all(e["action"] == "tagged" for e in entries)


# ---------------------------------------------------------------------------
# Smoke test 13: cover art embedded end-to-end in FLAC and MP3
# ---------------------------------------------------------------------------

# Minimal 1×1 JPEG bytes (a valid but tiny JFIF image).
_TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\x1e"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    b"\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04"
    b"\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa"
    b'\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br'
    b"\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJ"
    b"STUVWXYZ\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xf5\x0a\xff\xd9"
)


class TestRunCoverArtFlac:
    """End-to-end smoke test: cover art is embedded into a FLAC file.

    When ``fetch_cover_art`` returns a :class:`CoverArt` with a non-empty ``front`` image,
    :func:`apply_tags_flac` must embed it as a FLAC PICTURE block, and :func:`_verify_copy`
    must confirm the round-trip succeeds.
    """

    def test_cover_art_embedded_in_flac(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Front cover art bytes are embedded and round-tripped correctly in a FLAC file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/coverflac")
        dest_root = Path("/dest_coverflac")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        cover = CoverArt(front=[CoverImage(data=_TINY_JPEG, mime="image/jpeg")])
        release = _make_release()
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=cover)
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            side_effect=lambda rec_id: _make_recording_detail(
                rec_id,
                {
                    "rec1": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
                    "rec2": "Fontane di Roma: II. La fontana del Tritone",
                }.get(rec_id, "Unknown"),
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 2

        for flac_path in flac_files:
            pics = FLAC(str(flac_path)).pictures
            assert len(pics) == 1, f"expected 1 picture in {flac_path.name}, got {len(pics)}"
            assert pics[0].data == _TINY_JPEG
            assert pics[0].type == 3  # COVER_FRONT


class TestRunCoverArtMp3:
    """End-to-end smoke test: cover art is embedded into an MP3 file.

    When ``fetch_cover_art`` returns a :class:`CoverArt` with a non-empty ``front`` image,
    :func:`apply_tags_mp3` must embed it as an APIC frame, and :func:`_verify_copy`
    must confirm the round-trip succeeds.
    """

    def test_cover_art_embedded_in_mp3(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Front cover art bytes are embedded and round-tripped correctly in an MP3 file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/covermp3")
        dest_root = Path("/dest_covermp3")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.mp3"), contents=_MINIMAL_MP3)
        fs.create_file(str(src_dir / "02 - track2.mp3"), contents=_MINIMAL_MP3)

        cover = CoverArt(front=[CoverImage(data=_TINY_JPEG, mime="image/jpeg")])
        release = _make_release()
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=cover)
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            side_effect=lambda rec_id: _make_recording_detail(
                rec_id,
                {
                    "rec1": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
                    "rec2": "Fontane di Roma: II. La fontana del Tritone",
                }.get(rec_id, "Unknown"),
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        mp3_files = sorted(dest_root.rglob("*.mp3"))
        assert len(mp3_files) == 2

        for mp3_path in mp3_files:
            apic_frames = ID3(str(mp3_path)).getall("APIC")  # type: ignore[no-untyped-call]
            assert len(apic_frames) == 1, f"expected 1 APIC frame in {mp3_path.name}, got {len(apic_frames)}"
            assert apic_frames[0].data == _TINY_JPEG
            assert apic_frames[0].type == 3  # COVER_FRONT


# ---------------------------------------------------------------------------
# Smoke test 14: write_transaction_log merges entries into an existing journal
# ---------------------------------------------------------------------------


class TestJournalMerge:
    """End-to-end smoke test: a second run appends to an existing journal file.

    The first run writes 2 ``"tagged"`` entries.  The second run (with OVERWRITE so it can
    proceed) also writes 2 ``"tagged"`` entries.  The resulting journal must contain all 4.
    """

    def test_second_run_appends_to_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Journal entries from a second run are merged with those from the first run.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/journal_merge")
        dest_root = Path("/dest_journal_merge")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        release = _make_release()
        _patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        music_annotator.run(
            release_id="rel-1",
            src_dir=src_dir,
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            collision_policy=CollisionPolicy.OVERWRITE,
        )

        journal_path = dest_root / JOURNAL_FILENAME
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(entries) == 4
        assert all(e["action"] == "tagged" for e in entries)


# ---------------------------------------------------------------------------
# Smoke test 15: discover() dry_run=True — delete prompt is suppressed
# ---------------------------------------------------------------------------


class TestDiscoverDryRun:
    """Smoke test: discover() in dry_run mode never calls confirm_delete.

    Even when a release is successfully selected and run() completes (in dry-run mode, so no
    files are actually copied), discover() must not call ui.confirm_delete because ``dry_run``
    suppresses the delete prompt entirely.
    """

    def test_dry_run_does_not_call_confirm_delete(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """confirm_delete is never called when discover() runs in dry_run=True mode.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/discover_dry")
        dest_root = Path("/dest_discover_dry")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        candidate = MBReleaseCandidate(release_id="rel-1", score=90, title="Respighi: Fontane di Roma", artist="Karajan")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[candidate])
        _patch_mb(mocker, _make_release())

        class _SelectUI:
            """Stub DiscoverUI: selects first candidate; confirm_delete must never be called."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return first candidate's release_id."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Must not be reached in dry-run mode."""
                raise AssertionError("confirm_delete called during dry_run")  # pragma: no cover

        stub: DiscoverUI = _SelectUI()
        # Should complete without calling confirm_delete (no AssertionError raised).
        music_annotator.discover(
            src_dirs=[src_dir],
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=True,
            ui=stub,
        )


# ---------------------------------------------------------------------------
# Smoke test 16: discover() — search_releases_by_dir raises ValueError → logged, continues
# ---------------------------------------------------------------------------


class TestDiscoverSearchError:
    """Smoke test: discover() skips a directory when search_releases_by_dir raises ValueError.

    The remaining directories (if any) should still be processed; the error is only logged.
    """

    def test_search_error_is_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """discover() continues to the next directory when search raises ValueError.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir_bad = Path("/src/empty_dir")
        src_dir_good = Path("/src/good_dir")
        dest_root = Path("/dest_search_err")
        fs.create_dir(str(src_dir_bad))
        # src_dir_bad has no audio files — search_releases_by_dir would raise ValueError.
        fs.create_dir(str(src_dir_good))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir_good / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir_good / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        candidate = MBReleaseCandidate(release_id="rel-1", score=90, title="Respighi: Fontane di Roma", artist="Karajan")

        # Bad dir raises; good dir returns a candidate.
        def _search_side_effect(src_dir: Path, limit: int = 10) -> list[MBReleaseCandidate]:  # pylint: disable=unused-argument
            if src_dir == src_dir_bad:
                raise ValueError("no audio files found")
            return [candidate]

        mocker.patch("music_annotator._discover.search_releases_by_dir", side_effect=_search_side_effect)
        _patch_mb(mocker, _make_release())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        class _AutoSelectUI:
            """Stub DiscoverUI: always picks first candidate."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return first candidate's release_id."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Decline deletion."""
                return False

        stub: DiscoverUI = _AutoSelectUI()
        # Must not raise — the bad dir is skipped, the good dir is processed.
        music_annotator.discover(
            src_dirs=[src_dir_bad, src_dir_good],
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=stub,
        )

        # Good dir should have produced two tagged FLAC files in the destination.
        assert len(list(dest_root.rglob("*.flac"))) == 2


# ---------------------------------------------------------------------------
# Smoke test 17: discover() — run() raises RuntimeError → logged, continues
# ---------------------------------------------------------------------------


class TestDiscoverRunError:
    """Smoke test: discover() continues to the next directory when run() raises RuntimeError.

    This exercises the ``except (ValueError, WebServiceError, RuntimeError, OSError)`` handler
    in :func:`discover` so that a single failed copy does not abort the whole batch.
    """

    def test_run_error_is_logged_and_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """discover() logs a RuntimeError from run() and processes the next directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir_bad = Path("/src/bad_run")
        src_dir_good = Path("/src/good_run")
        dest_root = Path("/dest_run_err")
        fs.create_dir(str(src_dir_bad))
        fs.create_dir(str(src_dir_good))
        fs.create_dir(str(dest_root))
        fs.create_file(str(src_dir_bad / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir_good / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir_good / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        candidate = MBReleaseCandidate(release_id="rel-1", score=90, title="Respighi: Fontane di Roma", artist="Karajan")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[candidate])

        # run() raises for the bad dir and succeeds for the good dir.
        call_count = 0

        def _run_side_effect(**kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            if kwargs.get("src_dir") == src_dir_bad:
                raise RuntimeError("simulated copy failure")
            # Delegate real run() for the good dir — need full patches active.

        mocker.patch("music_annotator._discover.run", side_effect=_run_side_effect)

        class _AutoSelectUI:
            """Stub DiscoverUI: always picks first candidate."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return first candidate's release_id."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(
                self, _m: object, proposed: MBMedium, _d: object, _u: object
            ) -> MBMedium | None:  # pragma: no cover
                """Should not be reached because run is patched."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:  # pragma: no cover
                """Should not be reached because run is patched to raise."""
                return False

        stub: DiscoverUI = _AutoSelectUI()
        # Must not raise — the bad dir is skipped after RuntimeError.
        music_annotator.discover(
            src_dirs=[src_dir_bad, src_dir_good],
            dest_root=dest_root,
            user_agent="Test/1.0",
            dry_run=False,
            ui=stub,
        )

        # run() must have been called twice (once per directory).
        assert call_count == 2
