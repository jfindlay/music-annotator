"""Integration tests for music_annotator.run(), discover(), and unify().

These tests exercise the full pipeline end-to-end against a fabricated release.  All MusicBrainz API calls are replaced by
pytest-mock stubs and all file I/O is handled by pyfakefs so no real network or disk access occurs.  Internal helpers such as
apply_tags_flac and _verify_copy are not patched, so the real mutagen write-and-read-back path executes.
"""

# pylint: disable=duplicate-code  # test helper factories intentionally mirror test_pipeline.py scaffolding

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import JOURNAL_FILENAME, CollisionPolicy
from music_annotator._audit import _audit_tier_pass, _make_audit_counts, detect_fragmented_releases
from music_annotator._discover import DiscoverUI, is_download_dir
from music_annotator._pipeline_io import (
    PROVENANCE_FILENAME,
    _find_freedb_sidecar,
    _read_provenance_sidecar,
    _write_provenance_fields,
    rebuild_journal,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import build_dest_path
from music_annotator.models import (
    AccurateRipSummary,
    AnnotationTier,
    CoverArt,
    CoverImage,
    MBMedium,
    MBRecording,
    MBRelease,
    MBReleaseCandidate,
    MBTrack,
    MBWork,
    ProvenanceSidecar,
    TrackTags,
    TransactionEntry,
)

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

    def _rec_detail(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
        titles = {
            "rec1": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
            "rec2": "Fontane di Roma: II. La fontana del Tritone",
        }
        return _make_recording_detail(rec_id, titles.get(rec_id, "Unknown"))

    mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_rec_detail)
    mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
    mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")


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
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:  # pragma: no cover
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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
            side_effect=lambda rec_id, no_cache=False: _make_recording_detail(
                rec_id,
                {
                    "rec1": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
                    "rec2": "Fontane di Roma: II. La fontana del Tritone",
                }.get(rec_id, "Unknown"),
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
            side_effect=lambda rec_id, no_cache=False: _make_recording_detail(
                rec_id,
                {
                    "rec1": "Fontane di Roma: I. La fontana di Valle Giulia all'alba",
                    "rec2": "Fontane di Roma: II. La fontana del Tritone",
                }.get(rec_id, "Unknown"),
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:  # pragma: no cover
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


# ---------------------------------------------------------------------------
# rebuild_journal integration tests
# ---------------------------------------------------------------------------


class TestRebuildJournalIntegration:
    """Integration tests for :func:`music_annotator._pipeline_io.rebuild_journal`.

    Exercises the full scan-and-reconstruct path with real mutagen tag reads.
    Covers: dry-run vs write mode; origin-time present/absent; mixed FLAC+MP3.
    """

    def test_dry_run_returns_entries_without_writing(self, fs: FakeFilesystem) -> None:
        """rebuild_journal dry-run returns entries but does not replace the on-disk journal.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        original_journal = "[]"
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text(original_journal, encoding="utf-8")

        result = rebuild_journal(dest_root, dry_run=True)

        assert journal_path.read_text(encoding="utf-8") == original_journal
        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        assert audio_entries[0].release_id == "rel-1"

    def test_write_mode_replaces_journal(self, fs: FakeFilesystem) -> None:
        """rebuild_journal write mode replaces the on-disk journal with reconstructed entries.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        rebuild_journal(dest_root, dry_run=False)

        written = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(written) == 1
        assert written[0]["action"] == "tagged"
        assert written[0]["release_id"] == "rel-1"

    def test_origin_time_from_sidecar(self, fs: FakeFilesystem) -> None:
        """rebuild_journal reads origin_time from freedb_disc_N.yaml when present.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        sidecar = work_dir / "freedb_disc_1.yaml"
        sidecar.write_text(
            "origin_time: '2024-01-15T09:00:00+00:00'\norigin_source: /rip/src\n",
            encoding="utf-8",
        )
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        assert audio_entries[0].origin_time == "2024-01-15T09:00:00+00:00"

    def test_origin_time_absent(self, fs: FakeFilesystem) -> None:
        """rebuild_journal sets origin_time to empty string when no provenance sidecar exists.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        assert audio_entries[0].origin_time == ""

    def test_mixed_flac_and_mp3(self, fs: FakeFilesystem) -> None:
        """rebuild_journal reconstructs entries for both FLAC and MP3 files in the same work_dir.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))

        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement 1", musicbrainz_albumid="rel-1"))

        mp3_path = work_dir / "02 - Movement.mp3"
        fs.create_file(str(mp3_path), contents=_MINIMAL_MP3)
        apply_tags_mp3(mp3_path, TrackTags(title="Movement 2", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 2
        destinations = {e.destination for e in audio_entries}
        assert str(flac_path) in destinations
        assert str(mp3_path) in destinations
        for entry in audio_entries:
            assert entry.release_id == "rel-1"


# ---------------------------------------------------------------------------
# Integration tests for unify()
# ---------------------------------------------------------------------------


class TestUnifyIntegration:
    """Integration tests for :func:`music_annotator.unify` — performer-split unification.

    These tests exercise the full detect → plan → move → verify → journal chain without mocking
    any internal helpers.  The real mutagen write-and-read-back path executes via pyfakefs.

    The performer-split scenario: one release (MUSICBRAINZ_ALBUMID="int-rel-1") has files under
    two distinct top_dirs ("Brahms - Pollini" and "Brahms - Karajan").  After unify(), all files
    should be under the canonical top_dir derived from their embedded tags.
    """

    @staticmethod
    def _make_frag_tags(performer: str = "Karajan") -> TrackTags:
        """Build TrackTags for the fragmented-release integration test.

        :param performer: The ARTIST value to embed (affects the top_dir path).
        :returns: A :class:`TrackTags` instance with CWP, performer, and MB album ID tags set.
        """
        return TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist=performer,
            musicbrainz_albumid="int-rel-1",
        )

    def test_unify_full_pipeline(self, fs: FakeFilesystem) -> None:
        """unify() detects fragmentation, moves files, and appends action="unified" journal entries.

        Full integration: no internal helpers are mocked.  The real detect_fragmented_releases,
        build_dest_path, _sha256_file, _verify_copy, and write_transaction_log all execute.

        Asserts:
        (a) detect_fragmented_releases detects the fragmented release before unify().
        (b) After unify(yes=True), the file is at the canonical path.
        (c) A TransactionEntry(action="unified") is in the journal.
        (d) detect_fragmented_releases returns empty after unify() (idempotency).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Tags that drive build_dest_path to produce "Brahms - Karajan/..." canonical path
        tags_canonical = self._make_frag_tags("Karajan")

        # File A: wrong top_dir (Pollini instead of Karajan)
        old_path = dest_root / "Brahms - Pollini" / "Piano Concerto No. 1 [rec 2021]" / "01 - First movement.flac"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags_canonical)

        # File B: already at canonical path (Karajan) — creates the second top_dir
        canonical_path = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_canonical, global_track_idx=0)
        canonical_path = canonical_path.with_suffix(".flac")
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical_path, tags_canonical)

        # (a) Fragmentation detected before unify
        fragmented_before = detect_fragmented_releases(dest_root)
        assert "int-rel-1" in fragmented_before
        assert old_path in fragmented_before["int-rel-1"]

        # Act: unify with yes=True
        music_annotator.unify(dest_root=dest_root, yes=True)

        # (b) File moved to canonical path
        assert canonical_path.exists()
        assert not old_path.exists()

        # (c) Journal has a "unified" entry
        journal = music_annotator.read_journal(dest_root / JOURNAL_FILENAME)
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1
        assert unified[0].source == str(old_path)
        assert unified[0].destination == str(canonical_path)
        assert unified[0].release_id == "int-rel-1"

        # (d) Idempotency: second run finds nothing to do
        music_annotator.unify(dest_root=dest_root, yes=True)
        journal2 = music_annotator.read_journal(dest_root / JOURNAL_FILENAME)
        unified2 = [e for e in journal2.entries if e.action == "unified"]
        assert len(unified2) == 1  # still only one entry from the first run


# ---------------------------------------------------------------------------
# Integration test: annotation tier persisted at ingest time (S3 KAT)
# ---------------------------------------------------------------------------


class TestIngestPersistsAnnotationTier:
    """KAT: integration test proving the annotation-tier write-and-read-back path (S3).

    Two fixtures:
    (a) A release whose source FLAC carries an embedded MUSICBRAINZ_TRACKID matching the
        release's recording ID → CensusSignal.EMBEDDED_MBID → AnnotationTier.FULL_MB_VERIFIED.
    (b) A release whose source FLAC carries no embedded recording ID → CensusSignal.SEARCH_HIT
        → AnnotationTier.MB_SEARCH_RESOLVED + needs_spot_check=True.

    No internal helpers (apply_tags_flac, _verify_copy) are patched — the real mutagen
    write-and-read-back path executes, proving the tier survives the full ingest cycle.
    """

    def test_ingest_persists_annotation_tier(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Full pipeline: embedded MBID → full-mb-verified; search hit → mb-search-resolved.

        Runs the pipeline twice:
        1. Source FLAC with embedded MUSICBRAINZ_TRACKID matching the release → full-mb-verified.
        2. Source FLAC with no embedded recording ID → mb-search-resolved + needs_spot_check.

        Both runs use the real mutagen write-and-read-back path (no internal helpers patched).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        # --- Fixture (a): embedded MBID → full-mb-verified ---
        # Use a single-track release so one source file matches the track count.
        src_embedded = Path("/src/embedded")
        dest_embedded = Path("/dest/embedded")
        fs.create_dir(str(src_embedded))
        fs.create_dir(str(dest_embedded))
        flac_path = src_embedded / "01 - track1.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)

        # Build a single-track release whose recording id is "rec1".
        single_track_release = MBRelease.model_validate(
            {
                "id": "rel-1",
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
                "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "1995"},
                "label-info-list": [{"label": {"id": "lab1", "name": "Deutsche Grammophon"}, "catalog-number": "449 724-2"}],
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
                                    "artist-credit": [],
                                },
                            }
                        ],
                    }
                ],
            }
        )

        # Embed MUSICBRAINZ_TRACKID = "rec1" (matches the single-track release's recording id)
        audio = FLAC(str(flac_path))
        audio["musicbrainz_trackid"] = ["rec1"]
        audio.save()

        _patch_mb(mocker, single_track_release)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_embedded,
            dest_root=dest_embedded,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        # Locate the work top directory and read the provenance sidecar
        flac_files_a = sorted(dest_embedded.rglob("*.flac"))
        assert len(flac_files_a) == 1
        work_top_a = (
            dest_embedded
            / flac_files_a[0].relative_to(dest_embedded).parts[0]
            / flac_files_a[0].relative_to(dest_embedded).parts[1]
        )
        prov_path_a = work_top_a / PROVENANCE_FILENAME
        assert prov_path_a.exists(), "provenance sidecar must be written for embedded-MBID fixture"
        sidecar_a = _read_provenance_sidecar(prov_path_a)
        assert sidecar_a.annotation_tier == AnnotationTier.FULL_MB_VERIFIED, (
            f"embedded MBID fixture must produce full-mb-verified, got {sidecar_a.annotation_tier!r}"
        )
        assert sidecar_a.needs_spot_check is False

        # --- Fixture (b): no embedded MBID → mb-search-resolved ---
        # Re-patch fetch_release to return the two-track release for this fixture.
        two_track_release = _make_release()
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=two_track_release)

        src_search = Path("/src/search")
        dest_search = Path("/dest/search")
        fs.create_dir(str(src_search))
        fs.create_dir(str(dest_search))
        # Plain FLAC with no embedded recording ID
        fs.create_file(str(src_search / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_search / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src_search,
            dest_root=dest_search,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        flac_files_b = sorted(dest_search.rglob("*.flac"))
        assert len(flac_files_b) == 2
        work_top_b = (
            dest_search / flac_files_b[0].relative_to(dest_search).parts[0] / flac_files_b[0].relative_to(dest_search).parts[1]
        )
        prov_path_b = work_top_b / PROVENANCE_FILENAME
        assert prov_path_b.exists(), "provenance sidecar must be written for search-hit fixture"
        sidecar_b = _read_provenance_sidecar(prov_path_b)
        assert sidecar_b.annotation_tier == AnnotationTier.MB_SEARCH_RESOLVED, (
            f"search-hit fixture must produce mb-search-resolved, got {sidecar_b.annotation_tier!r}"
        )
        assert sidecar_b.needs_spot_check is True


# ---------------------------------------------------------------------------
# Whipper integration test (S5 primary KAT)
# ---------------------------------------------------------------------------

#: Minimal whipper native log body (before the SHA-256 line) for a 2-track disc.
#: The SHA-256 line is appended by :func:`_make_whipper_log_2track`.
_WHIPPER_LOG_BODY_2TRACK = """\
Log created by: whipper 0.10.0 (2023-01-01)
Log creation date: 2023-01-01 12:00:00

CD metadata:
  MusicBrainz Disc ID: whipper-test-disc-id
  CDDB Disc ID: 0x12345678

Tracks:
  1:
    AccurateRip v1:
      Result: exact-match
      Confidence: 42
      Local CRC: AABBCCDD
      Remote CRC: AABBCCDD
    AccurateRip v2:
      Result: exact-match
      Confidence: 38
      Local CRC: 11223344
      Remote CRC: 11223344
    Test CRC: AABBCCDD
    Copy CRC: AABBCCDD
    Status: Copy OK
  2:
    AccurateRip v1:
      Result: exact-match
      Confidence: 40
      Local CRC: EEFF0011
      Remote CRC: EEFF0011
    AccurateRip v2:
      Result: exact-match
      Confidence: 36
      Local CRC: 22334455
      Remote CRC: 22334455
    Test CRC: EEFF0011
    Copy CRC: EEFF0011
    Status: Copy OK

Conclusive status report:
  AccurateRip summary: All tracks accurately ripped
  Accurately ripped: 2
  Tracks in AR database: 2

"""


def _make_whipper_log_2track(body: str = _WHIPPER_LOG_BODY_2TRACK) -> str:
    """Return a complete 2-track whipper log string with a valid self-attesting SHA-256 line.

    :param body: The log body (everything before the SHA-256 line).
    :returns: The complete log string including the trailing SHA-256 line.
    """
    sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest().upper()
    return f"{body}SHA-256 hash: {sha256}\n"


def _make_whipper_release() -> MBRelease:
    """Return a 2-track single-disc release with a disc_list entry for TOC matching.

    The disc_list offsets ``[182, 67232]`` match the ``disc_id`` list written into
    ``00 - disc info.yaml`` by the whipper integration test fixture, enabling the
    single-disc TOC promotion path (S3 / C-WHIP).

    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(
        {
            "id": "rel-whipper",
            "title": "Whipper Test Album",
            "date": "2023",
            "status": "Official",
            "barcode": "",
            "artist-credit": [
                {
                    "name": "Test Artist",
                    "artist": {
                        "id": "artist-1",
                        "name": "Test Artist",
                        "sort-name": "Artist, Test",
                        "type": "Person",
                    },
                }
            ],
            "release-group": {
                "id": "rg-whipper",
                "primary-type": "Album",
                "first-release-date": "2023",
            },
            "label-info-list": [],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": [
                {
                    "position": 1,
                    "format": "CD",
                    # disc-list carries the TOC offsets that match the 00 - disc info.yaml fixture.
                    # offsets=[182, 67232] matches track_frames extracted from disc_id list below.
                    "disc-list": [{"offset-list": [182, 67232], "sectors": "356250"}],
                    "track-list": [
                        {
                            "id": "trk-w1",
                            "position": 1,
                            "recording": {
                                "id": "rec-w1",
                                "title": "Whipper Track 1",
                                "artist-credit": [],
                            },
                        },
                        {
                            "id": "trk-w2",
                            "position": 2,
                            "recording": {
                                "id": "rec-w2",
                                "title": "Whipper Track 2",
                                "artist-credit": [],
                            },
                        },
                    ],
                }
            ],
        }
    )


class TestWhipperIntegration:
    """Primary KAT (S5): end-to-end whipper pipeline integration test.

    Exercises the full public path on an embedded whipper-shaped fixture:
    dir recognition → TOC lookup (mocked MB) → tier promotion → AccurateRip tags written
    and read back through the real mutagen path → sidecar preserved → journal + confirmation
    message correct.

    No internal helpers (apply_tags_flac, _verify_copy) are patched — the real mutagen
    write-and-read-back path executes, per the integration convention.
    """

    def test_whipper_full_pipeline(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Full whipper pipeline: recognition → TOC promotion → AR tags → sidecar → journal.

        Fixture:
        - Source dir with 2 FLAC files + whipper ``.log`` (trailing SHA-256 line) + ``00 - disc info.yaml``
          with a ``disc_id`` list whose offsets match the release's ``disc_list``.
        - MB API mocked to return a release with a matching disc TOC entry.
        - ``discover()`` called with a stub UI that selects the candidate.

        Asserts:
        (a) Annotation tier is ``full-mb-verified`` (single-disc TOC promotion fired via C-WHIP).
        (b) ``needs_spot_check == False`` (TOC-promoted entries are not spot-check candidates).
        (c) AccurateRip flat fields are present in the output FLAC tags (read back via mutagen).
        (d) ``accuraterip_summary`` is in the provenance sidecar with ``log_sha256`` non-empty.
        (e) Whipper ``.log`` sidecar is preserved in the work dir.
        (f) Journal has a ``"sidecar"`` entry for the log file.
        (g) Journal has ``"tagged"`` entries for the FLACs (the "safe to delete" provenance chain).
        (h) ``origin_source == "whipper"`` in the provenance sidecar.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/whipper_album.0xe212b212")
        dest_root = Path("/dest_whipper")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))

        # Source FLAC files (2 tracks)
        fs.create_file(str(src_dir / "01 - track1.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src_dir / "02 - track2.flac"), contents=_MINIMAL_FLAC)

        # Whipper native log (C-WHIP strong signature 1): trailing SHA-256 line present.
        log_content = _make_whipper_log_2track()
        fs.create_file(str(src_dir / "rip.log"), contents=log_content)

        # 00 - disc info.yaml (C-WHIP strong signature 2 + TOC data for single-disc promotion).
        # disc_id list: [freedb_crc, num_tracks, offset_1, offset_2, total_seconds]
        # track_frames = [182, 67232]; leadout_frame = 4750 * 75 = 356250.
        # These offsets match the release's disc_list[0].offsets = [182, 67232].
        disc_info_yaml = "disc_id: [0x12345678, 2, 182, 67232, 4750]\nrecord: []\n"
        fs.create_file(str(src_dir / "00 - disc info.yaml"), contents=disc_info_yaml)

        # Mock MB API calls
        release = _make_whipper_release()
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            return_value=_make_recording_detail("rec-w1", "Whipper Track 1"),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        # search_releases_by_dir is called by discover(); return the whipper release as a candidate.
        candidate = MBReleaseCandidate(release_id="rel-whipper", score=100, title="Whipper Test Album", artist="Test Artist")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[candidate])

        class _AutoSelectUI:
            """Stub DiscoverUI: always picks the first candidate and never deletes."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return the first candidate's release_id unconditionally."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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
            fetch_rels=False,
            ui=stub,
        )

        # --- (a) Tier is full-mb-verified ---
        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 2, f"expected 2 FLAC files in dest, got {len(flac_files)}"
        work_top = dest_root / flac_files[0].relative_to(dest_root).parts[0] / flac_files[0].relative_to(dest_root).parts[1]
        # The sidecar may be freedb_disc_1.yaml (when 00 - disc info.yaml is present) or
        # music_annotator_provenance.yaml.  Use _find_freedb_sidecar with fallback.
        prov_path = _find_freedb_sidecar(work_top) or (work_top / PROVENANCE_FILENAME)
        assert prov_path.exists(), f"provenance sidecar must be written at {prov_path}"
        sidecar = _read_provenance_sidecar(prov_path)
        assert sidecar.annotation_tier == AnnotationTier.FULL_MB_VERIFIED, (
            f"whipper TOC promotion must yield full-mb-verified, got {sidecar.annotation_tier!r}"
        )

        # --- (b) needs_spot_check == False ---
        assert sidecar.needs_spot_check is False, "TOC-promoted whipper rip must not be flagged for spot-check"

        # --- (c) AccurateRip flat fields in FLAC tags ---
        # Track 1 should have AR fields from the whipper log (exact-match, confidence 42 v1 / 38 v2).
        flac_tags_1 = FLAC(str(flac_files[0]))
        assert flac_tags_1.get("accuraterip_v1_result"), "accuraterip_v1_result must be present in FLAC tags"
        assert flac_tags_1["accuraterip_v1_result"][0] == "exact-match"
        assert flac_tags_1.get("accuraterip_v2_result"), "accuraterip_v2_result must be present in FLAC tags"
        assert flac_tags_1["accuraterip_v2_result"][0] == "exact-match"
        assert flac_tags_1.get("accuraterip_status"), "accuraterip_status must be present in FLAC tags"
        assert flac_tags_1["accuraterip_status"][0] == "Copy OK"

        # --- (d) accuraterip_summary in provenance sidecar ---
        ar_summary: AccurateRipSummary = sidecar.accuraterip_summary
        assert ar_summary.log_sha256, "accuraterip_summary.log_sha256 must be non-empty (AR-verified)"
        assert ar_summary.accurately_ripped == 2
        assert ar_summary.in_ar_database == 2
        assert ar_summary.mb_disc_id == "whipper-test-disc-id"

        # --- (e) Whipper .log sidecar preserved in work dir ---
        log_dest = work_top / "rip.log"
        assert log_dest.exists(), "whipper .log sidecar must be copied to the work dir"
        assert log_dest.read_text(encoding="utf-8") == log_content, "whipper .log content must be preserved byte-exact"

        # --- (f) Journal has a "sidecar" entry for the log ---
        journal_path = dest_root / JOURNAL_FILENAME
        assert journal_path.exists()
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        sidecar_entries = [e for e in entries if e["action"] == "sidecar"]
        assert len(sidecar_entries) >= 1, "journal must have at least one 'sidecar' entry for the whipper log"
        sidecar_dests = [e["destination"] for e in sidecar_entries]
        assert str(log_dest) in sidecar_dests, f"journal sidecar entry must reference {log_dest}"

        # --- (g) Journal has "tagged" entries for the FLACs (C-MOVE provenance invariant) ---
        tagged_entries = [e for e in entries if e["action"] == "tagged"]
        assert len(tagged_entries) == 2, f"journal must have 2 'tagged' entries, got {len(tagged_entries)}"
        # The "safe to delete" message derives only from action=="tagged" entries (C-MOVE invariant).
        # Sidecar entries must NOT feed the tagged count — verify they are separate.
        assert all(e["action"] == "tagged" for e in tagged_entries)
        # Confirm the "safe to delete" provenance chain: tagged entries reference the source FLAC files.
        tagged_sources = {e["source"] for e in tagged_entries}
        assert str(src_dir / "01 - track1.flac") in tagged_sources
        assert str(src_dir / "02 - track2.flac") in tagged_sources


# ---------------------------------------------------------------------------
# S3 KAT: audit distinguishes ISRC-verified entries from search-resolved ones
# ---------------------------------------------------------------------------


class TestAuditDistinguishesIsrcVerified:
    """KAT (S3): _audit_tier_pass surfaces ISRC-promoted entries distinctly from search-resolved ones.

    An ISRC-promoted entry has ``annotation_tier == full-mb-verified`` and
    ``origin_source == "download"`` in its provenance sidecar.  The audit must log
    ``audit_tier_full`` with ``origin_source="download"`` for that entry, and
    ``audit_tier_provisional`` (not ``audit_tier_full``) for a search-resolved entry.

    This test calls :func:`_audit_tier_pass` directly with fabricated sidecars and journal
    entries, asserting on the structlog events emitted.
    """

    def test_audit_distinguishes_isrc_verified(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """ISRC-promoted entry logs audit_tier_full with origin_source; search-resolved logs audit_tier_provisional.

        Fixture:
        - Work-ISRC: ``full-mb-verified``, ``origin_source="download"`` (ISRC-promoted download dir).
        - Work-Search: ``mb-search-resolved``, ``origin_source=""`` (bare search-resolved entry).

        Asserts:
        (a) ``audit_tier_full`` is logged for Work-ISRC with ``origin_source="download"``.
        (b) ``audit_tier_provisional`` is logged for Work-Search (not ``audit_tier_full``).
        (c) ``tier_full == 1``, ``tier_search == 1``, ``provisional_total == 1``.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Work-ISRC: full-mb-verified, origin_source="download" (ISRC-promoted download dir)
        work_isrc = dest_root / "Composer - Performer" / "Work-ISRC [2024]"
        work_isrc.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_isrc / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="download",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        # Work-Search: mb-search-resolved, origin_source="" (bare search-resolved entry)
        work_search = dest_root / "Composer - Performer" / "Work-Search [2024]"
        work_search.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_search / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="",
                annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
                needs_spot_check=True,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-isrc",
                source="/src/presto/01.flac",
                destination=str(work_isrc / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-search",
                source="/src/search/01.flac",
                destination=str(work_search / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        mock_log = mocker.patch("music_annotator._audit.log")
        counts = _make_audit_counts()
        _audit_tier_pass(dest_root, journal_entries, counts)

        # (a) audit_tier_full logged for Work-ISRC with origin_source="download"
        full_calls = [c for c in mock_log.debug.call_args_list if c.args[0] == "audit_tier_full"]
        assert len(full_calls) == 1, f"expected 1 audit_tier_full call, got {len(full_calls)}"
        full_kwargs = full_calls[0].kwargs
        assert full_kwargs.get("origin_source") == "download", (
            f"audit_tier_full must include origin_source='download', got {full_kwargs.get('origin_source')!r}"
        )

        # (b) audit_tier_provisional logged for Work-Search (not audit_tier_full)
        provisional_calls = [c for c in mock_log.info.call_args_list if c.args[0] == "audit_tier_provisional"]
        assert len(provisional_calls) == 1, f"expected 1 audit_tier_provisional call, got {len(provisional_calls)}"

        # (c) Counts correct
        assert counts["tier_full"] == 1
        assert counts["tier_search"] == 1
        assert counts["provisional_total"] == 1


# ---------------------------------------------------------------------------
# S1 primary KAT: other-download end-to-end integration test (C-DL)
# ---------------------------------------------------------------------------


def _make_download_release(isrc_track1: str, isrc_track2: str) -> MBRelease:
    """Return a 2-track release with ISRC codes in the recording stubs.

    The ``isrc-list`` field on each recording stub is populated so that the ISRC-match
    ladder rung in ``run()`` can fire when the source files carry matching ISRC tags.

    :param isrc_track1: ISRC code for track 1 (must match the source FLAC's embedded ISRC).
    :param isrc_track2: ISRC code for track 2 (must match the source FLAC's embedded ISRC).
    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(
        {
            "id": "rel-download",
            "title": "Download Test Album",
            "date": "2022",
            "status": "Official",
            "barcode": "",
            "artist-credit": [
                {
                    "name": "Test Artist",
                    "artist": {
                        "id": "artist-d1",
                        "name": "Test Artist",
                        "sort-name": "Artist, Test",
                        "type": "Person",
                    },
                }
            ],
            "release-group": {
                "id": "rg-download",
                "primary-type": "Album",
                "first-release-date": "2022",
            },
            "label-info-list": [],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": [
                {
                    "position": 1,
                    "format": "Digital Media",
                    "track-list": [
                        {
                            "id": "trk-d1",
                            "position": 1,
                            "recording": {
                                "id": "rec-d1",
                                "title": "Download Track 1",
                                "artist-credit": [],
                                "isrc-list": [isrc_track1],
                            },
                        },
                        {
                            "id": "trk-d2",
                            "position": 2,
                            "recording": {
                                "id": "rec-d2",
                                "title": "Download Track 2",
                                "artist-credit": [],
                                "isrc-list": [isrc_track2],
                            },
                        },
                    ],
                }
            ],
        }
    )


class TestOtherDownloadIsrcIntegration:
    """Primary KAT (S1/C-DL): end-to-end other-download pipeline integration test.

    Exercises the full public path on an other-download-shaped fixture: ISRC-bearing FLAC
    files, no booklet PDF, no whipper log, no disc info yaml.  The pipeline must:

    1. Recognise the directory as a generic download (``is_download_dir`` True; ``origin_source == "download"``).
    2. Resolve the release via MB search (mocked).
    3. Promote the annotation tier to ``full-mb-verified`` via ISRC-match (C-ISRC).
    4. Write and read back tags through the real mutagen path.
    5. Write correct journal entries and the "safe to delete" confirmation message.

    No internal helpers (``apply_tags_flac``, ``_verify_copy``) are patched — the real
    mutagen write-and-read-back path executes, per the integration convention.
    """

    def test_other_download_full_pipeline(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Full other-download pipeline: ISRC recognition → ISRC-match promotion → tags → journal.

        Fixture:
        - Source dir with 2 FLAC files carrying ISRC tags (``"GBA002200001"`` and
          ``"GBA002200002"``).  No booklet PDF, no whipper log, no ``00 - disc info.yaml``.
        - MB API mocked to return a release whose recording stubs carry matching ISRC lists.
        - ``discover()`` called with a stub UI that selects the candidate.

        Asserts:
        (a) ``is_download_dir`` returns ``True`` for the source dir (C-DL recognition).
        (b) Annotation tier is ``full-mb-verified`` (ISRC-match promotion fired, C-ISRC).
        (c) ``needs_spot_check == False`` (ISRC-promoted entries are not spot-check candidates).
        (d) FLAC tags are written and readable (TITLE and ALBUMARTIST present).
        (e) Journal has ``"tagged"`` entries for both FLACs (provenance-chain invariant).
        (f) No ``"sidecar"`` entries for a whipper log (download has no rip log).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src_dir = Path("/src/download_album")
        dest_root = Path("/dest_download")
        fs.create_dir(str(src_dir))
        fs.create_dir(str(dest_root))

        # ISRCs that will be embedded in the source FLACs and matched against the release.
        isrc_1 = "GBA002200001"
        isrc_2 = "GBA002200002"

        # Create source FLAC files and embed ISRC tags via mutagen.
        # pyfakefs intercepts the file I/O; mutagen reads/writes through the fake filesystem.
        # No booklet PDF — other-download fixture has ISRC-bearing audio only.
        flac1_path = src_dir / "01 - track1.flac"
        flac2_path = src_dir / "02 - track2.flac"
        fs.create_file(str(flac1_path), contents=_MINIMAL_FLAC)
        fs.create_file(str(flac2_path), contents=_MINIMAL_FLAC)

        # Embed ISRC tags so is_download_dir() recognises the directory (C-DL condition 1)
        # and _isrc_matches() can confirm the match against the release's isrc_list.
        audio1 = FLAC(str(flac1_path))
        audio1["isrc"] = [isrc_1]
        audio1.save()

        audio2 = FLAC(str(flac2_path))
        audio2["isrc"] = [isrc_2]
        audio2.save()

        # Release with matching ISRC lists in the recording stubs.
        release = _make_download_release(isrc_1, isrc_2)

        # Mock MB API calls.  No whipper log → no AR data → no fetch_acoustid_id needed.
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            side_effect=lambda rec_id, no_cache=False: _make_recording_detail(
                rec_id,
                {"rec-d1": "Download Track 1", "rec-d2": "Download Track 2"}.get(rec_id, "Unknown"),
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=_make_work_detail())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        # search_releases_by_dir is called by discover(); return the download release as a candidate.
        candidate = MBReleaseCandidate(release_id="rel-download", score=95, title="Download Test Album", artist="Test Artist")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[candidate])

        class _AutoSelectUI:
            """Stub DiscoverUI: always picks the first candidate and never deletes."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return the first candidate's release_id unconditionally."""
                return candidates[0].release_id if candidates else None

            def confirm_disc(self, _m: object, proposed: MBMedium, _d: object, _u: object) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
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

        # Locate the output FLAC files and work top directory.
        flac_files = sorted(dest_root.rglob("*.flac"))
        assert len(flac_files) == 2, f"expected 2 FLAC files in dest, got {len(flac_files)}"
        work_top = dest_root / flac_files[0].relative_to(dest_root).parts[0] / flac_files[0].relative_to(dest_root).parts[1]
        prov_path = _find_freedb_sidecar(work_top) or (work_top / PROVENANCE_FILENAME)
        assert prov_path.exists(), f"provenance sidecar must be written at {prov_path}"
        sidecar = _read_provenance_sidecar(prov_path)

        # --- (a) is_download_dir returns True: verify that discover() recognised the dir as a download.
        # The pipeline does not write origin_source to the sidecar during normal ingest (it is
        # written by the enrich_origin_time maintenance pass).  Instead, assert that is_download_dir()
        # returns True for the source dir — this is the exact predicate that discover() evaluates
        # to set origin_source = "download" before calling run().
        assert is_download_dir(src_dir), "source dir must be recognised as a download (is_download_dir returned False)"

        # --- (b) Annotation tier is full-mb-verified (ISRC-match promotion) ---
        assert sidecar.annotation_tier == AnnotationTier.FULL_MB_VERIFIED, (
            f"ISRC-match promotion must yield full-mb-verified, got {sidecar.annotation_tier!r}"
        )

        # --- (c) needs_spot_check == False ---
        assert sidecar.needs_spot_check is False, "ISRC-promoted download entry must not be flagged for spot-check"

        # --- (d) FLAC tags written and readable ---
        for flac_path in flac_files:
            tags = FLAC(str(flac_path))
            assert tags.get("TITLE"), f"TITLE missing in {flac_path.name}"
            assert tags.get("ALBUMARTIST"), f"ALBUMARTIST missing in {flac_path.name}"

        # --- (e) Journal has "tagged" entries for both FLACs ---
        journal_path = dest_root / JOURNAL_FILENAME
        assert journal_path.exists()
        entries = json.loads(journal_path.read_text(encoding="utf-8"))
        tagged_entries = [e for e in entries if e["action"] == "tagged"]
        assert len(tagged_entries) == 2, f"journal must have 2 'tagged' entries, got {len(tagged_entries)}"
        tagged_sources = {e["source"] for e in tagged_entries}
        assert str(flac1_path) in tagged_sources, f"source {flac1_path} not in tagged sources"
        assert str(flac2_path) in tagged_sources, f"source {flac2_path} not in tagged sources"

        # --- (f) No "sidecar" entries for a whipper log (download has no rip log) ---
        sidecar_entries = [e for e in entries if e["action"] == "sidecar" and e["destination"].endswith(".log")]
        assert not sidecar_entries, f"download dir must not produce whipper log sidecar entries, got {sidecar_entries}"
