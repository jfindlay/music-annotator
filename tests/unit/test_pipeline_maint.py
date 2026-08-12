"""Unit tests for _pipeline_maint functions: repath, regroup, unify, enrich,
_move_verify_journal, _resolve_current_lib, and related helpers.

Migrated from test_main.py (TestRepath, TestRegroup, TestEnrich, TestUnify, etc.)
and test_pipeline.py (TestMoveVerifyJournal, TestResolveCurrentLib, TestRepathConfirmation).
"""

# pylint: disable=duplicate-code  # test setup patterns are intentionally similar across test modules

from __future__ import annotations

import datetime
import errno
import json
import os
import sys
from pathlib import Path

import pytest
from mutagen._util import MutagenError
from mutagen.flac import FLAC as MutagenFLAC
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    JOURNAL_FILENAME,
    apply_tags_flac,
    apply_tags_mp3,
    build_dest_path,
    read_journal,
    repath,
)
from music_annotator.__main__ import _build_parser, main
from music_annotator._pipeline_io import (
    AudioCompareResult,
    _needs_enrich,
    _read_albumid_tag,
    _read_tags_flac,
    _sha256_file,
)
from music_annotator._pipeline_maint import (
    _hydrate_performer_lists,
    _move_verify_journal,
    _resolve_current_lib,
    _unify_classical_composer_groups,
)
from music_annotator._tags import _work_top_dir
from music_annotator._works import work_group_modal_depth
from music_annotator.models import (
    ArtistEntry,
    MBArtist,
    MBRelease,
    MBTrack,
    TrackTags,
    TransactionEntry,
    TransactionLog,
)
from tests.conftest import _MINIMAL_FLAC, _MINIMAL_MP3


def _write_library_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a journal JSON file to ``dest_root / music_annotator_journal.json``.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / "music_annotator_journal.json"
    journal_path.write_text(json.dumps(entries), encoding="utf-8")


def _make_library_flac(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
    """Create a FLAC file at ``dest_root / rel_path`` with the given tags applied.

    Creates parent directories as needed, writes the minimal FLAC byte sequence, applies tags
    via ``apply_tags_flac``, and returns the full path.

    :param dest_root: Library root directory.
    :param rel_path: Relative path within the library (e.g. ``"Composer/Work/01 - Title.flac"``).
    :param tags: Tags to embed in the FLAC file via :func:`apply_tags_flac`.
    :returns: The full absolute path of the created FLAC file.
    """
    full_path = dest_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(_MINIMAL_FLAC)
    apply_tags_flac(full_path, tags)
    return full_path


def _make_library_mp3(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
    """Create an MP3 file at ``dest_root / rel_path`` with the given tags applied.

    Creates parent directories as needed, writes the minimal MP3 byte sequence, applies tags
    via ``apply_tags_mp3``, and returns the full path.

    :param dest_root: Library root directory.
    :param rel_path: Relative path within the library (e.g. ``"Composer/Work/01 - Title.mp3"``).
    :param tags: Tags to embed in the MP3 file via :func:`apply_tags_mp3`.
    :returns: The full absolute path of the created MP3 file.
    """
    full_path = dest_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(_MINIMAL_MP3)
    apply_tags_mp3(full_path, tags)
    return full_path


class TestRepath:
    """Tests for :func:`music_annotator.repath` — the library re-path maintenance mode.

    Exercises the full move + verify + journal provenance chain without mocking
    ``_read_tags_*``, ``_verify_copy``, or ``build_dest_path`` (real round-trip, only the
    filesystem is fake via pyfakefs).  Network boundaries and MusicBrainz lookups are not
    involved (repath is fully offline).
    """

    # Tags shared across tests: a two-movement symphony with CWP hierarchy.
    # build_dest_path produces:
    #   <dest_root>/Beethoven - Karajan/Symphony No. 5 [rec 2020]/01 - Allegro con brio.flac
    # (ARTIST fallback: CEA_ENSEMBLE_NAMES and all performer lists are absent, so ARTIST is used)
    #
    # Dynamic extra CWP_WORK_0 is included to ensure the extras branch of _tags_from_file_dict
    # (storing non-named-field keys) is exercised by the real repath round-trip.
    @staticmethod
    def _make_tags_mvt1() -> TrackTags:
        """Build TrackTags for movement 1, including a dynamic CWP_WORK_0 extra field.

        The dynamic extra exercises the extras code path in _tags_from_file_dict.

        :returns: A :class:`TrackTags` instance with CWP and per-level extra tags set.
        """
        tags = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
        )
        # CWP_WORK_0 is a dynamic per-level extra field (not in model_fields)
        if tags.model_extra is None:  # pragma: no cover
            pass
        else:
            tags.model_extra["cwp_work_0"] = "Symphony No. 5"
        return tags

    @staticmethod
    def _make_tags_mvt2() -> TrackTags:
        """Build TrackTags for movement 2.

        :returns: A :class:`TrackTags` instance for movement 2.
        """
        return TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="2",
            movementtotal="2",
            cwp_part_levels="1",
            title="Andante con moto",
            artist="Karajan",
        )

    # Legacy paths: same composer dir + performer but old work dir name "OldSymphony"
    _OLD_REL_MVT1 = "Beethoven - Karajan/OldSymphony [rec 2020]/01 - Allegro con brio.flac"
    _OLD_REL_MVT2 = "Beethoven - Karajan/OldSymphony [rec 2020]/02 - Andante con moto.flac"

    @staticmethod
    def _new_path(dest_root: Path, tags: TrackTags, ext: str = ".flac") -> Path:
        """Compute the repathed destination for a given set of tags.

        :param dest_root: Library root.
        :param tags: Tags to drive ``build_dest_path``.
        :param ext: File extension (default ``".flac"``).
        :returns: Full absolute path after repathing.
        """
        base = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0)
        return base.with_suffix(ext)

    def test_repath_moves_and_journals_legacy_layout(self, fs: FakeFilesystem) -> None:
        """repath() moves files from legacy paths to recomputed paths and journals each move.

        Fabricates a two-track library under old per-work-key paths (OldSymphony as the work dir),
        with embedded CWP tags that recompute to the correct Symphony No. 5 path.  Asserts that:
        (a) files exist at the new paths and no longer at the old paths, and
        (b) the journal gained action="repathed" entries recording old → new destination.

        Uses the real tagger + _verify_copy + build_dest_path (no mocking of internals).
        Includes a dynamic CWP_WORK_0 extra tag to exercise the extras branch of
        _tags_from_file_dict.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        tags_mvt2 = self._make_tags_mvt2()

        # Create FLAC files at old (legacy) paths with correct embedded tags
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        old_path2 = _make_library_flac(dest_root, self._OLD_REL_MVT2, tags_mvt2)

        new_path1 = self._new_path(dest_root, tags_mvt1)
        new_path2 = self._new_path(dest_root, tags_mvt2)

        # New paths must differ from old paths (test precondition)
        assert new_path1 != old_path1
        assert new_path2 != old_path2

        # Write journal with "tagged" entries at the legacy (old) paths
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/02.flac",
                    "destination": str(old_path2),
                    "action": "tagged",
                },
            ],
        )

        # Act: repath without dry_run
        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # (a) Files are at new paths, not at old paths
        assert new_path1.exists()
        assert new_path2.exists()
        assert not old_path1.exists()
        assert not old_path2.exists()

        # (b) Journal gained "repathed" entries
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 2
        repathed_dests = {e.destination for e in repathed}
        assert str(new_path1) in repathed_dests
        assert str(new_path2) in repathed_dests
        repathed_srcs = {e.source for e in repathed}
        assert str(old_path1) in repathed_srcs
        assert str(old_path2) in repathed_srcs

    def test_repath_dry_run_no_move_no_journal(self, fs: FakeFilesystem) -> None:
        """repath(dry_run=True) logs planned moves but writes nothing to disk or journal.

        Asserts that (a) files remain at their old paths, and (b) no "repathed" journal entries
        are added.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        tags_mvt2 = self._make_tags_mvt2()

        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        old_path2 = _make_library_flac(dest_root, self._OLD_REL_MVT2, tags_mvt2)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/02.flac",
                    "destination": str(old_path2),
                    "action": "tagged",
                },
            ],
        )

        # Act: dry run
        music_annotator.repath(dest_root=dest_root, dry_run=True)

        # (a) Files remain at old paths
        assert old_path1.exists()
        assert old_path2.exists()

        # (b) No "repathed" entries in journal
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0

    def test_repath_noop_when_paths_already_current(self, fs: FakeFilesystem) -> None:
        """repath() is a no-op when files already live at the recomputed paths.

        When a file's current path matches the path that build_dest_path recomputes from its tags,
        no move is performed and no journal entry is written.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()

        # Compute the "correct" path first, then place the file there
        new_path1 = self._new_path(dest_root, tags_mvt1)
        _make_library_flac(dest_root, str(new_path1.relative_to(dest_root)), tags_mvt1)
        assert new_path1.exists()

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(new_path1),
                    "action": "tagged",
                },
            ],
        )

        journal_before = dest_root / "music_annotator_journal.json"
        contents_before = journal_before.read_text(encoding="utf-8")

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # File still exists at the same path
        assert new_path1.exists()
        # Journal is unchanged (no "repathed" entries added)
        journal = music_annotator.read_journal(journal_before)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0
        # Journal file bytes unchanged (repath_all_current path: no write_transaction_log call)
        assert journal_before.read_text(encoding="utf-8") == contents_before

    def test_repath_empty_journal_no_error(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """repath() handles an empty (no-entry) journal gracefully.

        No files exist to repath and no error is raised.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        _write_library_journal(dest_root, [])
        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)  # should not raise

    def test_repath_skips_journalled_file_not_on_disk(self, fs: FakeFilesystem) -> None:
        """repath() silently skips journal destinations that no longer exist on disk.

        A journal entry with action="tagged" pointing at a path that was deleted outside the
        tool is silently skipped (file-was-moved/deleted-outside case).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # Journal says the file is at old_path1 but we never create it
        old_path1 = dest_root / self._OLD_REL_MVT1
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )
        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)  # should not raise
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0

    def test_repath_repathed_entry_supersedes_tagged(self, fs: FakeFilesystem) -> None:
        """repath() uses the latest destination when a file has already been repathed once.

        When the journal contains a "tagged" entry followed by a "repathed" entry for the same
        logical file (simulating a prior repath pass), only the current (latest) destination is
        considered.  The file at the old "tagged" destination is gone; the file at the "repathed"
        destination is treated as the current library file.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()

        # The file was previously repathed from old_path → intermediate_path, but now the
        # recomputed path from tags points somewhere new: new_path.
        # We only create intermediate_path on disk (old_path is gone after the prior repath).
        intermediate_path = dest_root / "Beethoven - Karajan/IntermediateWork [rec 2020]/01 - Allegro con brio.flac"
        _make_library_flac(
            dest_root,
            str(intermediate_path.relative_to(dest_root)),
            tags_mvt1,
        )
        new_path = self._new_path(dest_root, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(dest_root / self._OLD_REL_MVT1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T01:00:00+00:00",
                    "release_id": "",
                    "source": str(dest_root / self._OLD_REL_MVT1),
                    "destination": str(intermediate_path),
                    "action": "repathed",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        assert new_path.exists()
        assert not intermediate_path.exists()
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        new_repathed = [e for e in journal.entries if e.action == "repathed" and e.destination == str(new_path)]
        assert len(new_repathed) == 1

    def test_repath_collision_gets_suffix(self, fs: FakeFilesystem) -> None:
        """repath() appends a disambiguating suffix when a file's recomputed path already exists.

        This covers the _assess_collisions / _apply_collision_suffix branch in repath().
        A file at a legacy path recomputes to a destination that already exists on disk with
        a different AcoustID tag (confirmed non-match) → repath() appends a release-identifying
        suffix to disambiguate the destination.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # The incoming file has AcoustID "aaa..." (simulating a distinct recording)
        tags_mvt1 = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

        # The file at the legacy path will recompute to new_path_raw.
        new_path_raw = self._new_path(dest_root, tags_mvt1)
        old_path = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)

        # Pre-create new_path_raw as a FLAC with a DIFFERENT AcoustID tag so _assess_collisions
        # gets a confirmed non-match (different AcoustID) → triggers the collision suffix block.
        tags_existing = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )
        new_path_raw.parent.mkdir(parents=True, exist_ok=True)
        new_path_raw.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_path_raw, tags_existing)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        # Repath: old_path's recomputed dest (new_path_raw) is occupied with a different
        # AcoustID → confirmed non-match → suffix appended to disambiguate.
        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # old_path was moved (not still at legacy location)
        assert not old_path.exists()

        # Journal has a "repathed" entry; destination has a disambiguating suffix (not raw)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        # The destination should NOT be new_path_raw (it would collide); it has a suffix
        assert repathed[0].destination != str(new_path_raw)

    def test_repath_exdev_fallback(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath() falls back to shutil.copy2 + unlink when os.replace raises EXDEV.

        EXDEV (errno 18) signals a cross-filesystem move.  The fallback path should:
        (a) still result in the file existing at the new path,
        (b) still write a "repathed" journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        new_path1 = self._new_path(dest_root, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        def _failing_replace(src: str, dst: str) -> None:
            raise OSError(errno.EXDEV, "cross-device link", src)

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=_failing_replace)

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # (a) File exists at new path
        assert new_path1.exists()
        assert not old_path1.exists()

        # (b) Journal has a "repathed" entry
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].source == str(old_path1)
        assert repathed[0].destination == str(new_path1)

    def test_repath_non_exdev_oserror_propagates(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath() re-raises OSError when errno is not EXDEV.

        Non-EXDEV errors (e.g. EPERM, EIO) indicate a real I/O failure that should not be
        silently swallowed; repath() must propagate them.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint.os.replace",
            side_effect=OSError(errno.EPERM, "operation not permitted", str(old_path1)),
        )

        with pytest.raises(OSError) as exc_info:
            music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)
        assert exc_info.value.errno == errno.EPERM

    def test_repath_exdev_copy_integrity_failure_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath() raises RuntimeError on SHA mismatch after cross-fs copy.

        When the cross-filesystem copy produces a byte-different file (corrupted copy),
        repath raises RuntimeError and does NOT write a journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        new_path1 = self._new_path(dest_root, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        # Make os.replace raise EXDEV, then make shutil.copy2 produce a corrupted file
        # by writing garbage bytes to the destination instead.
        def _failing_replace(src: str, dst: str) -> None:
            raise OSError(errno.EXDEV, "cross-device link", src)

        def _corrupt_copy(_src: str, dst: str) -> None:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b"\x00" * 100)  # write garbage

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=_failing_replace)
        mocker.patch("music_annotator._pipeline_maint.shutil.copy2", side_effect=_corrupt_copy)

        with pytest.raises(RuntimeError, match="cross-fs copy integrity failure"):
            music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # No journal entry was added (integrity failure before journal write)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0

        # Clean up destination to avoid polluting other assertions
        if new_path1.exists():
            new_path1.unlink()

    def test_repath_sha_mismatch_after_rename_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath() raises RuntimeError when destination SHA differs from source SHA after rename.

        This covers the hash-check after os.replace in the same-filesystem path.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        new_path1 = self._new_path(dest_root, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        # Patch os.replace to actually move the file (normal rename) BUT then corrupt
        # the new file so the post-rename SHA check fails.
        real_replace = os.replace

        def _replace_then_corrupt(src: str, dst: str) -> None:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            real_replace(src, dst)
            Path(dst).write_bytes(b"\x00" * 100)  # corrupt after move

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=_replace_then_corrupt)

        with pytest.raises(RuntimeError, match="repathed integrity failure"):
            music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # No journal entry was added (hash mismatch before journal write)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0

        # Clean up
        if new_path1.exists():
            new_path1.unlink()

    def test_repath_tag_read_error_skips_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath() logs a warning and skips a file when reading its tags fails.

        This covers the except handler in the tag-read loop.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        # Patch _read_tags_flac to raise an error (simulates corrupted tag block)
        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=Exception("corrupt tags"))

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)  # should not raise

        # File was not moved (read error caused skip)
        assert old_path1.exists()
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0

    def test_repath_length_tag_invalid_value_uses_zero(self, fs: FakeFilesystem) -> None:
        """repath() treats LENGTH tag as 0 when it contains a non-integer value.

        This covers the ValueError branch in the length_ms parsing.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Tags with an invalid LENGTH value (non-integer)
        tags_mvt1 = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            length="not-a-number",
        )
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        new_path1 = self._new_path(dest_root, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        # repath should succeed; the invalid LENGTH is silently treated as 0
        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        assert new_path1.exists()
        assert not old_path1.exists()

    def test_repath_ignores_non_library_journal_entries(self, fs: FakeFilesystem) -> None:
        """repath() skips journal entries with actions other than "tagged" or "repathed".

        Entries with action "skipped", "dry_run", "downloaded", or "sidecar" must not be
        treated as current library files.  This covers the `continue` branch in the journal
        scan loop.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()

        # Only the "tagged" entry points to a real file on disk; the "skipped" entry does not.
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)
        new_path1 = self._new_path(dest_root, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": "/lib/nonexistent/path.flac",
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/art.jpg",
                    "destination": "/lib/Beethoven - Karajan/Symphony No. 5/cover.jpg",
                    "action": "downloaded",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # The "tagged" file was moved; skipped/downloaded entries did not cause errors
        assert new_path1.exists()
        assert not old_path1.exists()

    def test_repath_mp3_moves_and_journals(self, fs: FakeFilesystem) -> None:
        """repath() handles MP3 files correctly (exercises the .mp3 tag-read branches).

        This covers the ``case ".mp3"`` branches in the plan-build and post-move tag-read
        loops, as well as the post-move tag round-trip via ``_verify_copy``.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Use a simpler MP3 tag set: only TXXX-writable fields so _verify_copy round-trips OK.
        tags_mp3 = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
        )
        old_rel_mp3 = "Beethoven - Karajan/OldSymphony [rec 2020]/01 - Allegro con brio.mp3"
        old_path = _make_library_mp3(dest_root, old_rel_mp3, tags_mp3)
        new_path = self._new_path(dest_root, tags_mp3, ext=".mp3")

        assert new_path != old_path

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.mp3",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].destination == str(new_path)

    def test_repath_post_move_tag_read_failure_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath() raises RuntimeError when the post-move tag re-read fails.

        This covers the except handler in the post-move tag-read block (after the file has
        already been moved).  A RuntimeError is raised so the operator knows the journal entry
        was NOT written and the provenance chain is intact.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_mvt1 = self._make_tags_mvt1()
        old_path1 = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_mvt1)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
            ],
        )

        # Patch _read_tags_flac: succeed on the first call (pre-move plan build) but
        # fail on the second call (post-move verify read).
        call_count: list[int] = [0]

        def _read_tags_side_effect(path: Path) -> dict[str, str]:
            call_count[0] += 1
            if call_count[0] == 1:
                return _read_tags_flac(path)
            raise OSError("tag read failed after move")

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=_read_tags_side_effect)

        with pytest.raises(RuntimeError, match="repathed tag re-read failure"):
            music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

    def test_repath_cleans_up_empty_dirs_to_root(self, fs: FakeFilesystem) -> None:
        """repath() removes empty parent directories all the way to dest_root.

        When a file is moved from a path whose entire ancestor chain becomes empty
        (different top-level composer directory), repath() removes the now-empty dirs until
        it reaches dest_root.  This covers the while-loop exit branch (src_dir == dest_root).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File under a DIFFERENT old composer dir than what the tags recompute to.
        # Old path: /lib/TempComp - TempPerf/TempWork [rec 2020]/01 - Track.flac
        # Tags: cwp_composer_lastnames=Beethoven → new path under /lib/Beethoven - Karajan/...
        old_rel = "TempComp - TempPerf/TempWork [rec 2020]/01 - Allegro con brio.flac"
        tags_mvt1 = self._make_tags_mvt1()
        old_path = _make_library_flac(dest_root, old_rel, tags_mvt1)
        new_path = self._new_path(dest_root, tags_mvt1)

        assert new_path != old_path
        # The old dir hierarchy should NOT overlap with the new path
        assert not str(new_path).startswith(str(dest_root / "TempComp - TempPerf"))

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        assert new_path.exists()
        assert not old_path.exists()
        # The old empty parent directories should have been removed
        assert not (dest_root / "TempComp - TempPerf").exists()

    # ------------------------------------------------------------------
    # W3a fossil-shape tests
    # ------------------------------------------------------------------

    def test_repath_leaf_collision_suffix_preserved(self, fs: FakeFilesystem) -> None:
        """W3a fossil shape 1: legitimate collision-suffix files are not overwritten.

        Two recordings of the same work live at paths with distinct collision suffixes
        (e.g. ``Work [rec 2020] [CAT-001]`` and ``Work [rec 2020] [CAT-002]``).  Both
        recompute to the same clean path via ``build_dest_path``.  ``repath`` must NOT
        move either file — doing so would cause one to overwrite the other.

        Asserts that both files still exist after ``repath`` and that no "repathed"
        journal entries were written (the intra-plan collision guard skips both files).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Both recordings share the same work/movement tags — they recompute to the same
        # clean destination.  The collision suffix distinguishes them on disk.
        tags_a = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
        tags_b = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )

        # Place both files at paths with collision suffixes on the work_dir.
        path_a = _make_library_flac(
            dest_root,
            "Beethoven - Karajan/Symphony No. 5 [rec 2020] [CAT-001]/01 - Allegro con brio.flac",
            tags_a,
        )
        path_b = _make_library_flac(
            dest_root,
            "Beethoven - Karajan/Symphony No. 5 [rec 2020] [CAT-002]/01 - Allegro con brio.flac",
            tags_b,
        )

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r2",
                    "source": "/src/02.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # Both files must still exist — neither was overwritten.
        assert path_a.exists(), "File A (CAT-001 suffix) was lost after repath"
        assert path_b.exists(), "File B (CAT-002 suffix) was lost after repath"

        # No "repathed" journal entries: the intra-plan collision guard skipped both files.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0, f"Expected 0 repathed entries (collision guard should skip both files), got {len(repathed)}"

    def test_repath_intra_collision_mixed_with_moveable_file(self, fs: FakeFilesystem) -> None:
        """W3a: intra-plan collision guard skips colliding files but still moves others.

        When the plan contains both intra-plan-colliding files (same recomputed destination)
        and a file that needs to be moved to a unique destination, the guard must skip the
        colliding files while still moving the non-colliding file.

        This covers the branch where ``plan_pairs`` is non-empty after filtering out the
        intra-plan collision group.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two files that recompute to the same clean destination (intra-plan collision).
        tags_a = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
        tags_b = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )

        # A third file that recomputes to a DIFFERENT destination (no intra-plan collision).
        tags_c = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Symphony No. 1",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Karajan",
        )

        path_a = _make_library_flac(
            dest_root,
            "Beethoven - Karajan/Symphony No. 5 [rec 2020] [CAT-001]/01 - Allegro con brio.flac",
            tags_a,
        )
        path_b = _make_library_flac(
            dest_root,
            "Beethoven - Karajan/Symphony No. 5 [rec 2020] [CAT-002]/01 - Allegro con brio.flac",
            tags_b,
        )
        # File C is at a stale path (different old work name) — should be moved.
        path_c_old = _make_library_flac(
            dest_root,
            "Brahms - Karajan/OldSym1 [rec 2019]/01 - Allegro.flac",
            tags_c,
        )
        path_c_new = self._new_path(dest_root, tags_c)
        assert path_c_new != path_c_old

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r2",
                    "source": "/src/02.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r3",
                    "source": "/src/03.flac",
                    "destination": str(path_c_old),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # Colliding files A and B must still exist at their original paths.
        assert path_a.exists(), "File A (collision) was lost"
        assert path_b.exists(), "File B (collision) was lost"

        # File C must have been moved to its correct path.
        assert path_c_new.exists(), "File C was not moved to its correct path"
        assert not path_c_old.exists(), "File C still exists at old path"

        # Journal has exactly one "repathed" entry (for file C only).
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].destination == str(path_c_new)

    def test_repath_stale_collision_suffix_removed(self, fs: FakeFilesystem) -> None:
        """W3a fossil shape 2: stale collision suffix (dd.dd over-application) is removed.

        A single file lives at a path with a collision suffix on the work_dir (e.g.
        ``Work [rec 2020] [CAT-001]``) but it is the ONLY recording of that work — no
        other file recomputes to the same clean destination.  ``repath`` must move it to
        the clean path (removing the stale suffix).

        This is the ``dd.dd`` over-application case from the 2026-06 library audit
        (4 work_dirs / 79 files).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
        )

        # File at a path with a stale collision suffix — only one recording of this work.
        old_path = _make_library_flac(
            dest_root,
            "Beethoven - Karajan/Symphony No. 5 [rec 2020] [CAT-001]/01 - Allegro con brio.flac",
            tags,
        )

        # The clean path (without the stale suffix) is what build_dest_path computes.
        clean_path = self._new_path(dest_root, tags)
        assert clean_path != old_path, "Test precondition: clean path must differ from old path"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # File must be at the clean path (stale suffix removed).
        assert clean_path.exists(), f"File was not moved to clean path {clean_path}"
        assert not old_path.exists(), "File still exists at old (stale-suffix) path"

        # Journal has a "repathed" entry recording the move.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].source == str(old_path)
        assert repathed[0].destination == str(clean_path)

    def test_repath_missing_inter_index_added(self, fs: FakeFilesystem) -> None:
        """W3a fossil shape 3: missing CWP_INTER_INDEX_{i} is added to the path.

        A file lives at a 2-level path (no intermediate directory) but its tags carry
        ``CWP_PART_LEVELS=2`` and ``CWP_INTER_INDEX_1`` — indicating a 3-level hierarchy
        with an intermediate directory.  ``repath`` must move it to the correct 3-level
        path that includes the intermediate directory.

        This covers the missing-inter-index fossil shape from the 2026-06 library audit.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Tags for a 3-level hierarchy: Opera > Act I > Aria.
        # CWP_PART_LEVELS=2 means 3 levels total (root + 1 intermediate + leaf).
        # CWP_INTER_INDEX_1=1 is the gap-free sibling index for the intermediate level.
        # cwp_worktype_genres_top="Classical" routes to the Classical class (C-CLASS predicate).
        tags = TrackTags(
            cwp_composer_lastnames="Mozart",
            cwp_work_top="Don Giovanni",
            cwp_worktype_genres_top="Classical",
            recording_date="1985",
            cwp_movt_num="1",
            movementtotal="3",
            cwp_part_levels="2",
            title="Madamina, il catalogo",
            artist="Karajan",
        )
        # Add the intermediate-level fields as model_extra (dynamic per-level fields).
        if tags.model_extra is not None:
            tags.model_extra["cwp_part_1"] = "Act I"
            tags.model_extra["cwp_inter_index_1"] = "1"
            tags.model_extra["cwp_ordering_key_1"] = "1"

        # Compute the correct (3-level) destination path.
        correct_path = self._new_path(dest_root, tags)
        # The correct path must include an intermediate directory.
        # With C-CLASS: class/top_dir/work_dir/inter_dir/leaf = 5 parts below dest_root.
        assert len(correct_path.relative_to(dest_root).parts) == 5, (
            f"Expected 5 path parts (class/top_dir/work_dir/inter_dir/leaf), got {correct_path.relative_to(dest_root).parts!r}"
        )

        # Place the file at a stale 2-level path (missing the intermediate directory).
        # This simulates the fossil: the file was annotated before the inter-index was computed.
        stale_rel = "Mozart - Karajan/Don Giovanni [rec 1985]/01 - Madamina, il catalogo.flac"
        old_path = _make_library_flac(dest_root, stale_rel, tags)
        assert old_path != correct_path, "Test precondition: stale path must differ from correct path"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # File must be at the correct 3-level path (intermediate directory added).
        assert correct_path.exists(), f"File was not moved to correct path {correct_path.relative_to(dest_root)}"
        assert not old_path.exists(), "File still exists at stale (missing-inter-index) path"

        # Journal has a "repathed" entry recording the move.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].source == str(old_path)
        assert repathed[0].destination == str(correct_path)


class TestRegroup:
    """Tests for :func:`music_annotator.regroup` — the library regroup maintenance move.

    The KAT ``test_regroup_appends_journal_entry`` is the load-bearing assertion: it drives a
    full confirmed split-release scenario (two work_dirs for one release_id) through ``regroup()``
    and asserts that a ``TransactionEntry(action="regrouped")`` is appended to the journal,
    with source=old path, destination=new path, release_id=the split release's MBID.

    All other tests cover the required branches for 100% branch coverage:
    dry_run, prompt-accepted, prompt-declined, yes=True, empty-plan, SHA-mismatch
    (provenance invariant: NO journal entry on mismatch), EXDEV cross-fs fallback,
    collision suffix, and the main() dispatch arms.

    Uses real FLAC bytes and :func:`apply_tags_flac` so that :func:`_read_tags_flac` executes
    the real mutagen round-trip rather than a mock.  Network and MusicBrainz lookups are not
    involved (regroup is fully offline like repath).
    """

    # Tags for a two-movement work.  The MUSICBRAINZ_ALBUMID tag is set to "split-rel-1" so that
    # _confirm_fragmentation reads it back and marks the candidate confirmed=True.
    #
    # build_dest_path (with these tags) produces:
    #   <dest_root>/Brahms - Vienna PO/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac
    # (ARTIST fallback: CEA_ENSEMBLE_NAMES absent, ARTIST="Vienna PO" is used as performer)
    #
    # The split scenario: same release_id "split-rel-1" has entries under TWO different work_dirs:
    #   "OldWork [2021]"  (where the file currently lives)
    #   "Piano Concerto No. 1 [rec 2021]"  (the canonical path from tags, in the journal via a second file)
    # After regroup(), the file from "OldWork [2021]" should move to "Piano Concerto No. 1 [rec 2021]".

    @staticmethod
    def _make_split_tags() -> TrackTags:
        """Build TrackTags for the split-release test file.

        Sets MUSICBRAINZ_ALBUMID so _confirm_fragmentation confirms the candidate via tag match.

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
            artist="Vienna PO",
            musicbrainz_albumid="split-rel-1",
        )

    @staticmethod
    def _canonical_path(dest_root: Path, tags: TrackTags, ext: str = ".flac") -> Path:
        """Compute the canonical destination path for given tags.

        :param dest_root: Library root.
        :param tags: Tags to drive build_dest_path.
        :param ext: File extension.
        :returns: Full absolute canonical path.
        """
        base = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0)
        return base.with_suffix(ext)

    def _build_split_scenario(self, dest_root: Path) -> tuple[Path, Path]:
        """Create a confirmed case-(b) split-release scenario under dest_root.

        Two "tagged" journal entries for release_id "split-rel-1" land under different work_dirs:
        - File A lives at a legacy path "OldWork [2021]" with the MUSICBRAINZ_ALBUMID tag set to
          "split-rel-1" (so _confirm_fragmentation marks it confirmed=True).
        - A phantom entry (no file on disk) for the canonical work_dir "Piano Concerto No. 1 [rec 2021]"
          ensures the release_id has two distinct work_dirs in the journal.

        Returns (old_path, new_path) where old_path is File A's current location and new_path is
        the recomputed canonical destination from the embedded tags.

        :param dest_root: Library root (must already exist).
        :returns: Tuple of (current file path, expected canonical path after regroup).
        """
        tags = self._make_split_tags()

        # File A: lives at legacy path with correct MUSICBRAINZ_ALBUMID
        old_path = _make_library_flac(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac", tags)

        # Canonical path (recomputed from tags by build_dest_path)
        new_path = self._canonical_path(dest_root, tags)

        # The old and canonical paths must differ for the scenario to be non-trivial
        assert old_path != new_path, "test setup error: old and canonical paths must differ"

        # Write journal: two entries for "split-rel-1" under different work_dirs, plus an
        # irrelevant "skipped" entry so the elif-False branch (action not "tagged"/"repathed"/
        # "regrouped") is covered in the current_lib-building loop.
        phantom = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02 - phantom.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
                # An "action='skipped'" entry triggers neither the tagged-if nor the
                # repathed/regrouped-elif in the current_lib loop, covering that False branch.
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/03.flac",
                    "destination": "/lib/some/skipped/file.flac",
                    "action": "skipped",
                },
            ],
        )

        return old_path, new_path

    # ------------------------------------------------------------------
    # KAT: test_regroup_appends_journal_entry
    # ------------------------------------------------------------------

    def test_regroup_appends_journal_entry(self, fs: FakeFilesystem) -> None:
        """regroup() moves the file and appends a TransactionEntry(action="regrouped") to the journal.

        Constructs a confirmed case-(b) split-release scenario (one
        release_id scattered across two work_dirs) and drives regroup(yes=True) through the full
        move+verify+journal provenance chain.  Asserts:

        (a) A TransactionEntry with action="regrouped", source=old path, destination=new path,
            release_id="split-rel-1" is appended to the journal.
        (b) The file exists at the new canonical path and no longer at the old path.
        (c) The file bytes are intact (re-read MUSICBRAINZ_ALBUMID via _read_albumid_tag).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_split_scenario(dest_root)

        # Act: regroup with yes=True to skip the interactive prompt
        music_annotator.regroup(dest_root=dest_root, yes=True)

        # (a) Journal gained a "regrouped" entry with correct fields
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        entry = regrouped[0]
        assert entry.source == str(old_path)
        assert entry.destination == str(new_path)
        assert entry.release_id == "split-rel-1"

        # (b) File moved: new path exists, old path gone
        assert new_path.exists()
        assert not old_path.exists()

        # (c) Bytes intact: MUSICBRAINZ_ALBUMID readable at new path
        assert _read_albumid_tag(new_path) == "split-rel-1"

    # ------------------------------------------------------------------
    # dry_run: no move, no journal write
    # ------------------------------------------------------------------

    def test_regroup_dry_run_no_move_no_journal(self, fs: FakeFilesystem) -> None:
        """regroup(dry_run=True) logs planned moves but writes nothing to disk or journal.

        Asserts that (a) files remain at their old paths and (b) no "regrouped" journal entries
        are added.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_split_scenario(dest_root)

        music_annotator.regroup(dest_root=dest_root, dry_run=True)

        # (a) File still at old path
        assert old_path.exists()
        assert not new_path.exists()

        # (b) No "regrouped" entries
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # Confirmation prompt: accepted (y) and declined (n)
    # ------------------------------------------------------------------

    def test_regroup_prompt_accepted_moves_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() with yes=False prompts; answering 'y' proceeds with the move.

        Mocks input() to return "y" and asserts the file is moved and journalled.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_split_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.input", return_value="y")

        music_annotator.regroup(dest_root=dest_root, yes=False)

        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        assert regrouped[0].release_id == "split-rel-1"

    def test_regroup_prompt_declined_no_move(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() with yes=False prompts; answering 'n' aborts with no move and no journal entry.

        Mocks input() to return "n" and asserts the file remains at its old path and no
        "regrouped" journal entry is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_split_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.input", return_value="n")

        music_annotator.regroup(dest_root=dest_root, yes=False)

        # File stays at old path; new path does not exist
        assert old_path.exists()
        assert not new_path.exists()

        # No "regrouped" entries
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # yes=True: prompt skipped
    # ------------------------------------------------------------------

    def test_regroup_yes_skips_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup(yes=True) does not call input() at all.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_split_scenario(dest_root)

        mock_input = mocker.patch("music_annotator._pipeline_maint.input")

        music_annotator.regroup(dest_root=dest_root, yes=True)

        mock_input.assert_not_called()

    # ------------------------------------------------------------------
    # Empty plan / nothing-to-regroup paths
    # ------------------------------------------------------------------

    def test_regroup_nothing_to_regroup_empty_journal(self, fs: FakeFilesystem) -> None:
        """regroup() on an empty journal returns immediately (no plan, no prompt).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # No journal file → empty journal → no confirmed candidates
        music_annotator.regroup(dest_root=dest_root, yes=True)
        # No exception raised; no journal created

    def test_regroup_nothing_to_regroup_no_confirmed_candidates(self, fs: FakeFilesystem) -> None:
        """regroup() returns immediately when there are no confirmed case-(b) candidates.

        A journal with only clean (one work_dir per release) entries produces no candidates.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single entry: release_id "r1" under one work_dir → no fragmentation
        dest_file = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "01.flac"
        _make_library_flac(
            dest_root,
            "Brahms - Vienna PO/Piano Concerto No. 1 [rec 2021]/01.flac",
            TrackTags(musicbrainz_albumid="r1"),
        )
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(dest_file),
                    "action": "tagged",
                }
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # No regrouped entries written
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    def test_regroup_nothing_to_regroup_when_all_paths_already_canonical(self, fs: FakeFilesystem) -> None:
        """regroup() is a no-op when all files already live at their canonical paths.

        The plan is empty even for a confirmed split-release if every file already lives at its
        canonical destination (build_dest_path returns the same path as the current path).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_split_tags()
        # Place File A at EXACTLY the canonical path (not old legacy path)
        canonical_path = self._canonical_path(dest_root, tags)
        canonical_rel = str(canonical_path.relative_to(dest_root))
        _make_library_flac(dest_root, canonical_rel, tags)

        # Phantom entry in another work_dir to force the two-work_dir case-b fragmentation
        phantom = dest_root / "Brahms - Vienna PO" / "OtherWork [2021]" / "02.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(canonical_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # File stays at canonical path; no journal entries added
        assert canonical_path.exists()
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # SHA mismatch after move → RuntimeError, NO journal entry
    # (Provenance invariant: the most critical test)
    # ------------------------------------------------------------------

    def test_regroup_sha_mismatch_raises_no_journal_entry(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() raises RuntimeError on SHA-256 mismatch and writes NO journal entry.

        This is the provenance-invariant test.  Patches _sha256_file to return a mismatched hash
        for the destination check (simulating silent corruption during the move), and asserts that:
        (a) RuntimeError is raised, and
        (b) no "regrouped" journal entry is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_split_scenario(dest_root)

        # Patch _sha256_file: first call returns "aaa..." (src), second returns "bbb..." (dest ≠ src)
        call_count = {"n": 0}

        def _fake_sha256(path: Path) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "a" * 64  # src hash
            if call_count["n"] == 2:
                return "b" * 64  # dest hash ≠ src → triggers RuntimeError
            return _sha256_file(path)  # subsequent calls use real implementation

        mocker.patch("music_annotator._pipeline_maint._sha256_file", side_effect=_fake_sha256)

        with pytest.raises(RuntimeError, match="regrouped integrity failure"):
            music_annotator.regroup(dest_root=dest_root, yes=True)

        # No "regrouped" journal entry must have been written
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0, "provenance invariant violated: journal entry written before verification passed"

    # ------------------------------------------------------------------
    # EXDEV cross-filesystem fallback
    # ------------------------------------------------------------------

    def test_regroup_exdev_fallback(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() falls back to copy2+unlink on EXDEV and still journals the move.

        Patches os.replace to raise OSError(EXDEV) on the first call, forcing the copy2+unlink
        path.  Asserts the file is moved and a "regrouped" journal entry is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_split_scenario(dest_root)

        exdev_raised = {"done": False}
        real_replace = os.replace

        def _fake_replace(src: str, dst: str) -> None:
            if not exdev_raised["done"]:
                exdev_raised["done"] = True
                raise OSError(errno.EXDEV, "cross-device link not permitted", str(src))
            real_replace(src, dst)

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=_fake_replace)

        music_annotator.regroup(dest_root=dest_root, yes=True)

        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        assert regrouped[0].action == "regrouped"
        assert regrouped[0].release_id == "split-rel-1"

    def test_regroup_exdev_copy_integrity_failure_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() raises RuntimeError when the EXDEV copy+verify produces a hash mismatch.

        The unlink of the destination is attempted and then RuntimeError is raised.  No journal
        entry is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_split_scenario(dest_root)

        exdev_raised = {"done": False}

        def _fake_replace(src: str, _dst: str) -> None:
            if not exdev_raised["done"]:
                exdev_raised["done"] = True
                raise OSError(errno.EXDEV, "cross-device link", str(src))

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=_fake_replace)

        # After the EXDEV copy, the cross-hash check will compare real hashes (which match).
        # To force a failure we additionally patch _sha256_file to return mismatched values on the
        # cross-fs verification call (the second sha256 call within the EXDEV branch).
        sha_calls = {"n": 0}

        def _fake_sha(_path: Path) -> str:
            sha_calls["n"] += 1
            if sha_calls["n"] <= 2:  # noqa: PLR2004
                return "x" * 64 if sha_calls["n"] == 2 else "a" * 64  # mismatch on second call
            return "a" * 64

        mocker.patch("music_annotator._pipeline_maint._sha256_file", side_effect=_fake_sha)

        with pytest.raises(RuntimeError, match="cross-fs copy integrity failure"):
            music_annotator.regroup(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # non-EXDEV OSError propagates
    # ------------------------------------------------------------------

    def test_regroup_non_exdev_oserror_propagates(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() re-raises OSError when the error is not EXDEV (e.g. permission denied).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_split_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=OSError(errno.EPERM, "permission denied"))

        with pytest.raises(OSError):
            music_annotator.regroup(dest_root=dest_root, yes=True)

    # ------------------------------------------------------------------
    # Collision handling
    # ------------------------------------------------------------------

    def test_regroup_collision_gets_suffix(self, fs: FakeFilesystem) -> None:
        """regroup() applies a collision suffix when the recomputed path already exists with different audio.

        Mirrors the repath collision test: a file at a legacy path recomputes to a canonical
        destination that ALREADY EXISTS on disk with a different AcoustID (confirmed non-match) →
        regroup() appends a release-identifying suffix to disambiguate.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # The incoming file (old_path) has AcoustID "aaa..." (distinct recording)
        tags_incoming = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid="split-rel-1",
            acoustid_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

        # Pre-compute the canonical path so we can pre-create a competing file there
        canonical = self._canonical_path(dest_root, tags_incoming)

        # File at a legacy (non-canonical) path — the one regroup will try to move
        old_path = _make_library_flac(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac", tags_incoming)
        assert old_path != canonical

        # Pre-create a DIFFERENT file at the canonical path with a different AcoustID so
        # _assess_collisions gets a confirmed non-match → collision suffix applied.
        tags_existing = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid="split-rel-1",
            acoustid_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical, tags_existing)

        # Journal: old_path under "OldWork [2021]" and canonical under its work_dir give
        # "split-rel-1" two distinct work_dirs → case-(b) fragmentation.
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(canonical),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # old_path was moved (not still at legacy location)
        assert not old_path.exists()

        # Journal has a "regrouped" entry; destination has a disambiguating suffix (not raw canonical)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        assert regrouped[0].release_id == "split-rel-1"
        # The destination must be different from the pre-existing canonical path (collision was resolved)
        assert regrouped[0].destination != str(canonical)

    # ------------------------------------------------------------------
    # Confirmed release, all files gone from disk → nothing to regroup (line 1799-1800)
    # ------------------------------------------------------------------

    def test_regroup_confirmed_but_all_files_missing_from_disk(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() returns 'nothing to regroup' when confirmed candidates have no on-disk files.

        Patches _confirm_fragmentation to return a confirmed case-(b) candidate, while the
        corresponding journal entries point to paths that do not exist on disk.  regroup() builds
        an empty existing_files list (all p.exists() are False) and returns immediately.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Patch _confirm_fragmentation to report "split-rel-1" as confirmed case-(b)
        mocker.patch(
            "music_annotator._pipeline_maint._confirm_fragmentation",
            return_value=(
                {},  # case_a: empty
                {"split-rel-1": (["WorkA [2021]", "WorkB [2021]"], True)},  # case_b: confirmed
            ),
        )

        # Journal with "tagged" entries pointing to non-existent paths
        missing_path_1 = dest_root / "Brahms - Vienna PO" / "WorkA [2021]" / "01.flac"
        missing_path_2 = dest_root / "Brahms - Vienna PO" / "WorkB [2021]" / "02.flac"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(missing_path_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(missing_path_2),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # No regrouped entries: nothing existed on disk to move
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # Repathed entry for unrelated file (old_path not in current_lib branch)
    # ------------------------------------------------------------------

    def test_regroup_ignores_repathed_entry_for_unconfirmed_release(self, fs: FakeFilesystem) -> None:
        """regroup() silently skips 'repathed' entries whose source is not a tracked confirmed file.

        When the journal contains a "repathed" entry whose source path does NOT appear in
        current_lib (because the file belongs to a different release or was already moved before
        the confirmed "tagged" entries were processed), regroup() correctly ignores it.

        This covers the ``if old_path in current_lib:`` False branch (1791->1785).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_split_tags()
        old_path = _make_library_flac(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac", tags)

        # An unrelated "repathed" entry for a completely different release
        unrelated_old = dest_root / "Mozart - Berlin PO" / "OldWork [2000]" / "01.flac"
        unrelated_new = dest_root / "Mozart - Berlin PO" / "NewWork [2000]" / "01.flac"

        phantom = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
                # This "repathed" entry's source is unrelated_old — NOT in current_lib
                # (current_lib only tracks confirmed release "split-rel-1" files)
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "",
                    "source": str(unrelated_old),
                    "destination": str(unrelated_new),
                    "action": "repathed",
                },
            ],
        )

        new_path = self._canonical_path(dest_root, tags)
        music_annotator.regroup(dest_root=dest_root, yes=True)

        # The confirmed split-release file was still moved correctly
        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1

    # ------------------------------------------------------------------
    # Planning-pass tag read failure → skip file (lines 1816-1818)
    # ------------------------------------------------------------------

    def test_regroup_planning_tag_read_failure_skips_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() skips a file whose tags cannot be read during the planning pass.

        Patches _read_tags_flac to raise on the FIRST call (the planning pass read) so the file
        is skipped.  The plan is empty → regroup logs "nothing to regroup" and returns.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_split_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=OSError("unreadable"))

        # The only file's tags can't be read → plan is empty → nothing to regroup
        music_annotator.regroup(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # Invalid LENGTH tag → length_ms falls back to 0 (lines 1836-1837)
    # ------------------------------------------------------------------

    def test_regroup_invalid_length_tag_uses_zero(self, fs: FakeFilesystem) -> None:
        """regroup() uses length_ms=0 when the LENGTH tag cannot be parsed as an integer.

        Constructs a scenario where FLAC bytes carry an invalid LENGTH tag value (non-numeric).
        regroup() must not raise; it falls back to length_ms=0 and proceeds with the move.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # We need a non-numeric LENGTH tag.  TrackTags.length is a str field; apply_tags_flac
        # writes it as "LENGTH".  We apply a tag with a non-numeric length value.
        tags = self._make_split_tags()
        # Manually patch a non-numeric length into the FLAC file after apply_tags_flac
        old_path = _make_library_flac(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac", tags)

        # Inject a non-numeric LENGTH tag directly via mutagen
        audio = MutagenFLAC(str(old_path))
        audio["LENGTH"] = ["not-a-number"]
        audio.save()

        phantom = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        # Should succeed despite invalid LENGTH — falls back to length_ms=0
        music_annotator.regroup(dest_root=dest_root, yes=True)

        new_path = self._canonical_path(dest_root, tags)
        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1

    # ------------------------------------------------------------------
    # post-move tag re-read failure raises RuntimeError (no journal entry)
    # ------------------------------------------------------------------

    def test_regroup_post_move_tag_read_failure_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup() raises RuntimeError and writes no journal entry when the post-move tag re-read fails.

        Patches _read_tags_flac to succeed for the planning pass but raise on the post-move
        re-read (the second call on the same path, now at new_dest).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_split_scenario(dest_root)

        call_count = {"n": 0}

        def _fake_read(path: Path) -> dict[str, str]:
            call_count["n"] += 1
            # First call is from the planning pass on old_path; second is the post-move re-read
            if call_count["n"] >= 2:  # noqa: PLR2004
                raise OSError("simulated post-move tag read failure")
            return _read_tags_flac(path)

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=_fake_read)

        with pytest.raises(RuntimeError, match="regrouped tag re-read failure"):
            music_annotator.regroup(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0

    # ------------------------------------------------------------------
    # MP3 file: regroup moves and journals correctly
    # ------------------------------------------------------------------

    def test_regroup_mp3_moves_and_journals(self, fs: FakeFilesystem) -> None:
        """regroup() handles MP3 files the same as FLAC: moves and appends a journal entry.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_split_tags()

        old_path = _make_library_mp3(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.mp3", tags)
        new_path = self._canonical_path(dest_root, tags, ext=".mp3")

        assert old_path != new_path

        phantom = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02.mp3"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.mp3",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.mp3",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        assert regrouped[0].source == str(old_path)
        assert regrouped[0].destination == str(new_path)
        assert regrouped[0].release_id == "split-rel-1"

    # ------------------------------------------------------------------
    # Empty-dir cleanup after move
    # ------------------------------------------------------------------

    def test_regroup_cleans_up_empty_dirs(self, fs: FakeFilesystem) -> None:
        """regroup() removes empty parent directories all the way to dest_root after moving.

        The old path uses a completely different top-level composer directory than the canonical
        path.  After the move, the entire old ancestor chain empties and gets removed, so the
        while loop exits normally when src_dir reaches dest_root (covering the while-False branch).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_split_tags()
        # Use a DIFFERENT top-level dir (TempComp) so the entire old ancestor chain empties
        # after the move and regroup() removes dirs all the way to dest_root.
        old_path = _make_library_flac(dest_root, "TempComp - TempPerf/OldWork [2021]/01 - First movement.flac", tags)
        new_path = self._canonical_path(dest_root, tags)
        assert old_path != new_path
        # The new path must be under a different top-level dir than TempComp - TempPerf
        assert not str(new_path).startswith(str(dest_root / "TempComp - TempPerf"))

        phantom = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        assert new_path.exists()
        # Entire old ancestor chain should have been removed (while loop walked to dest_root)
        assert not (dest_root / "TempComp - TempPerf").exists()

    # ------------------------------------------------------------------
    # repathed / regrouped entries update file lineage for regroup
    # ------------------------------------------------------------------

    def test_regroup_follows_repathed_lineage(self, fs: FakeFilesystem) -> None:
        """regroup() resolves the current location of a file that was previously repathed.

        When a "tagged" entry's file was subsequently moved by repath (creating a "repathed"
        journal entry), regroup() tracks the lineage and acts on the file at its current location.

        Scenario:
        - File A was originally "tagged" at ``orig_path`` then "repathed" to ``mid_path``.
          The current on-disk location is ``mid_path``.
        - File B is a real confirmed file at ``confirm_path`` (a different work_dir for the same
          release_id "split-rel-1"), which makes ``_confirm_fragmentation`` mark the candidate
          as confirmed=True (because its MUSICBRAINZ_ALBUMID tag matches the journal's release_id).

        Asserts that regroup() correctly identifies ``mid_path`` (not ``orig_path``) as the
        current file to move, and journals the move with source=mid_path.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_split_tags()

        # File A: originally "tagged" at orig_path, then "repathed" to mid_path.
        # The current on-disk location is mid_path (tags with MUSICBRAINZ_ALBUMID).
        mid_path = _make_library_flac(dest_root, "Brahms - Vienna PO/MidWork [2021]/01 - First movement.flac", tags)
        orig_path = dest_root / "Brahms - Vienna PO" / "OldWork [2021]" / "01 - First movement.flac"

        # File B: a second real file in another work_dir for the same release, confirming the
        # candidate via its MUSICBRAINZ_ALBUMID tag so _confirm_fragmentation returns confirmed=True.
        confirm_path = _make_library_flac(
            dest_root,
            "Brahms - Vienna PO/AnotherWork [2021]/02 - Second movement.flac",
            tags,
        )

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(orig_path),  # original tagged destination (file gone)
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "",
                    "source": str(orig_path),
                    "destination": str(mid_path),
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(confirm_path),  # real file; confirms the candidate
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # Both mid_path and confirm_path recompute to the same canonical path (same tags).
        # After collision handling both get unique destinations, or if they hash-match they land
        # at the same canonical path (one "wins" by being a noop for the already-moved file).
        # For the lineage assertion: the move source for File A must be mid_path, not orig_path.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        regrouped_sources = {e.source for e in regrouped}
        # mid_path must appear as a source (lineage correctly resolved from the "repathed" entry)
        assert str(mid_path) in regrouped_sources, (
            f"expected mid_path {mid_path} in regrouped sources {regrouped_sources}; regroup did not follow repathed lineage"
        )
        # orig_path must NOT appear as a source (it's stale — the file actually lived at mid_path)
        assert str(orig_path) not in regrouped_sources, (
            "regroup incorrectly used the stale orig_path instead of tracking via the repathed entry"
        )
        for e in regrouped:
            assert e.release_id == "split-rel-1"


# ---------------------------------------------------------------------------
class TestNeedsEnrich:
    """Unit tests for :func:`music_annotator._pipeline_io._needs_enrich`.

    Exercises all combinations of empty/present audio_hash, chromaprint_fp, and acoustid_id,
    plus the re_resolve=True path.
    """

    def test_all_empty_returns_all_fields(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich returns audio_hash and chromaprint_fp when both are absent.

        acoustid_id is absent so it is not included; the inconclusive log is emitted.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(title="No Fingerprints")
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")
        mock_log = mocker.patch("music_annotator._pipeline_io.log")

        result = _needs_enrich(path, re_resolve=False)

        assert "audio_hash" in result
        assert result["audio_hash"].startswith("flac-md5:")
        assert result["chromaprint_fp"] == "AQADtMmybckm"
        assert "acoustid_id" not in result
        mock_log.info.assert_called_once_with("enrich_acoustid_inconclusive", path=str(path))

    def test_all_present_returns_empty_dict(self, fs: FakeFilesystem) -> None:
        """_needs_enrich returns {} when all three fields are already present.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(audio_hash="flac-md5:aabb", chromaprint_fp="AQADtMmybckm", acoustid_id="test-uuid")
        apply_tags_flac(path, tags)

        result = _needs_enrich(path, re_resolve=False)

        assert result == {"acoustid_id": "test-uuid"}

    def test_re_resolve_true_recomputes_chromaprint_fp(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich recomputes chromaprint_fp when re_resolve=True even if already present.

        audio_hash is NOT recomputed (anchor rule).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(
            audio_hash="flac-md5:existing",
            chromaprint_fp="OldFingerprint",
            acoustid_id="test-uuid",
        )
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")

        result = _needs_enrich(path, re_resolve=True)

        # audio_hash is present → anchor rule: not recomputed
        assert "audio_hash" not in result
        # chromaprint_fp is recomputed under re_resolve=True
        assert result["chromaprint_fp"] == "NewFingerprint"
        # acoustid_id is copied from tag
        assert result["acoustid_id"] == "test-uuid"

    def test_audio_hash_present_not_overwritten(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich skips audio_hash when already present (anchor rule).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(audio_hash="flac-md5:existing", acoustid_id="test-uuid")
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="FP")

        result = _needs_enrich(path, re_resolve=False)

        assert "audio_hash" not in result
        assert result["chromaprint_fp"] == "FP"

    def test_fpcalc_returns_empty_not_included(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich omits chromaprint_fp when fpcalc returns empty string.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(title="No FP")
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline_io.log")

        result = _needs_enrich(path, re_resolve=False)

        assert "chromaprint_fp" not in result

    def test_acoustid_present_copied_to_result(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich copies acoustid_id from tag into result when present.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(acoustid_id="my-acoustid-uuid")
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="FP")

        result = _needs_enrich(path, re_resolve=False)

        assert result["acoustid_id"] == "my-acoustid-uuid"


def _make_enrichable_flac(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
    """Create a FLAC file at ``dest_root / rel_path`` with the given tags applied.

    Helper for enrich() tests: creates parent directories, writes minimal FLAC bytes, applies tags.

    :param dest_root: Library root directory.
    :param rel_path: Relative path within the library.
    :param tags: Tags to embed via :func:`apply_tags_flac`.
    :returns: The full absolute path of the created FLAC file.
    """
    full_path = dest_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(_MINIMAL_FLAC)
    apply_tags_flac(full_path, tags)
    return full_path


def _make_enrichable_mp3(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
    """Create an MP3 file at ``dest_root / rel_path`` with the given tags applied.

    Helper for enrich() tests: creates parent directories, writes minimal MP3 bytes, applies tags.

    :param dest_root: Library root directory.
    :param rel_path: Relative path within the library.
    :param tags: Tags to embed via :func:`apply_tags_mp3`.
    :returns: The full absolute path of the created MP3 file.
    """
    full_path = dest_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(_MINIMAL_MP3)
    apply_tags_mp3(full_path, tags)
    return full_path


class TestEnrich:
    """Tests for :func:`music_annotator.enrich` — the F4 fingerprint backfill mode.

    Exercises the full write + verify + journal provenance chain without mocking
    ``apply_tags_flac``, ``_verify_copy``, or ``_read_tags_flac`` (real round-trip, only the
    filesystem is fake via pyfakefs).  ``_run_fpcalc`` is mocked because fpcalc is not available
    in the test environment.
    """

    # ------------------------------------------------------------------
    # Core idempotency test (the most important test)
    # ------------------------------------------------------------------

    def test_enrich_backfills_triple_idempotently(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() backfills audio_hash + chromaprint_fp and is idempotent on a second run.

        Run 1: file has acoustid_id but no audio_hash or chromaprint_fp.  enrich() writes both
        missing fields and appends an "enriched" journal entry.

        Run 2: file is now fully enriched.  enrich() is a no-op: no new journal entry is written
        and the tags are unchanged.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")

        # --- Run 1: backfill ---
        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        audio = MutagenFLAC(str(path))
        hash_vals = audio.get("audio_hash") or []
        fp_vals = audio.get("chromaprint_fp") or []
        acoustid_vals = audio.get("acoustid_id") or []

        assert hash_vals and hash_vals[0].startswith("flac-md5:")
        assert fp_vals and fp_vals[0] == "AQADtMmybckm"
        assert acoustid_vals and acoustid_vals[0] == "test-acoustid-id"

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].audio_hash.startswith("flac-md5:")
        assert enriched[0].chromaprint_fp == "AQADtMmybckm"
        assert enriched[0].acoustid_id == "test-acoustid-id"
        assert enriched[0].source == str(path)
        assert enriched[0].destination == str(path)

        # --- Run 2: idempotency ---
        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        journal2 = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched2 = [e for e in journal2.entries if e.action == "enriched"]
        assert len(enriched2) == 1, "second run must not append a new enriched entry"

        audio2 = MutagenFLAC(str(path))
        assert (audio2.get("audio_hash") or []) == hash_vals
        assert (audio2.get("chromaprint_fp") or []) == fp_vals

    # ------------------------------------------------------------------
    # dry_run: no tags written, no journal entry
    # ------------------------------------------------------------------

    def test_enrich_dry_run_no_writes(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich(dry_run=True) logs planned backfills but writes no tags and no journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")
        mock_log = mocker.patch("music_annotator._pipeline_maint.log")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=True)

        # No tags written
        audio = MutagenFLAC(str(path))
        assert not (audio.get("audio_hash") or [])
        assert not (audio.get("chromaprint_fp") or [])

        # No journal entry
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 0

        # dry_run log event emitted
        dry_run_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_dry_run"]
        assert len(dry_run_calls) == 1

    # ------------------------------------------------------------------
    # re_resolve: recomputes chromaprint_fp; audio_hash NOT overwritten
    # ------------------------------------------------------------------

    def test_enrich_re_resolve_recomputes_fp_not_hash(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich(re_resolve=True) recomputes chromaprint_fp but never overwrites audio_hash.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            audio_hash="flac-md5:original",
            chromaprint_fp="OldFingerprint",
            acoustid_id="test-acoustid-id",
        )
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")

        music_annotator.enrich(dest_root=dest_root, re_resolve=True, dry_run=False)

        audio = MutagenFLAC(str(path))
        hash_vals = audio.get("audio_hash") or []
        fp_vals = audio.get("chromaprint_fp") or []

        # audio_hash must not be overwritten
        assert hash_vals and hash_vals[0] == "flac-md5:original"
        # chromaprint_fp must be updated
        assert fp_vals and fp_vals[0] == "NewFingerprint"

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].chromaprint_fp == "NewFingerprint"

    # ------------------------------------------------------------------
    # empty journal → nothing to enrich
    # ------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_enrich_empty_journal_is_noop(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() is a no-op when the journal has no entries.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        _write_library_journal(dest_root, [])

        mock_log = mocker.patch("music_annotator._pipeline_maint.log")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        nothing_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_nothing_to_enrich"]
        assert len(nothing_calls) == 1

    # ------------------------------------------------------------------
    # file not on disk → skipped gracefully
    # ------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_enrich_skips_file_not_on_disk(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() skips files that are in the journal but do not exist on disk.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/Artist/Album/01 - Track.flac",
                    "action": "tagged",
                }
            ],
        )

        mock_log = mocker.patch("music_annotator._pipeline_maint.log")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        nothing_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_nothing_to_enrich"]
        assert len(nothing_calls) == 1

    # ------------------------------------------------------------------
    # lineage resolution: repathed entry updates current path
    # ------------------------------------------------------------------

    def test_enrich_follows_repathed_lineage(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() resolves the current path via repathed journal entries.

        A file originally "tagged" at orig_path was subsequently "repathed" to new_path.
        enrich() must act on new_path, not orig_path.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        new_path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)
        orig_path = dest_root / "Artist" / "OldAlbum" / "01 - Track.flac"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(orig_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "",
                    "source": str(orig_path),
                    "destination": str(new_path),
                    "action": "repathed",
                },
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].destination == str(new_path)

    # ------------------------------------------------------------------
    # enriched entry in journal re-registers path (lineage completeness)
    # ------------------------------------------------------------------

    def test_enrich_enriched_entry_updates_current_lib(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() processes an "enriched" journal entry to keep current_lib up to date.

        When a prior "enriched" entry exists for a path, enrich() re-registers it in current_lib
        (source == destination for enriched entries).  A second run is then a noop.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(audio_hash="flac-md5:aabb", chromaprint_fp="FP", acoustid_id="uuid")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "rel-1",
                    "source": str(path),
                    "destination": str(path),
                    "action": "enriched",
                    "audio_hash": "flac-md5:aabb",
                    "chromaprint_fp": "FP",
                    "acoustid_id": "uuid",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._pipeline_maint.log")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        # File is already fully enriched → noop
        noop_calls = [c for c in mock_log.debug.call_args_list if c.args and c.args[0] == "enrich_noop"]
        assert len(noop_calls) == 1

    # ------------------------------------------------------------------
    # acoustid_id absent → logged as inconclusive, not written
    # ------------------------------------------------------------------

    def test_enrich_inconclusive_acoustid_logged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() logs enrich_acoustid_inconclusive when acoustid_id is absent from the file.

        The inconclusive count is incremented and the journal entry omits acoustid_id.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # No acoustid_id tag
        tags = TrackTags(title="No AcoustID")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].acoustid_id == ""

    # ------------------------------------------------------------------
    # MP3 file: enrich writes and journals correctly
    # ------------------------------------------------------------------

    def test_enrich_mp3_backfills_and_journals(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() handles MP3 files the same as FLAC: writes tags and appends a journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="mp3-acoustid-id")
        path = _make_enrichable_mp3(dest_root, "Artist/Album/01 - Track.mp3", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.mp3",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].audio_hash.startswith("mp3-stream-sha256:")
        assert enriched[0].chromaprint_fp == "AQADtMmybckm"
        assert enriched[0].acoustid_id == "mp3-acoustid-id"

    # ------------------------------------------------------------------
    # tag read error → skip file gracefully
    # ------------------------------------------------------------------

    def test_enrich_tag_read_error_skips_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() skips a file and writes no journal entry when the tag read raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")
        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=OSError("corrupt"))
        mock_log = mocker.patch("music_annotator._pipeline_maint.log")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 0

        warning_calls = [c for c in mock_log.warning.call_args_list if c.args and c.args[0] == "enrich_tag_read_error"]
        assert len(warning_calls) == 1

    # ------------------------------------------------------------------
    # regrouped entry updates lineage
    # ------------------------------------------------------------------

    def test_enrich_follows_regrouped_lineage(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() resolves the current path via regrouped journal entries.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        new_path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)
        orig_path = dest_root / "Artist" / "OldAlbum" / "01 - Track.flac"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(orig_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "rel-1",
                    "source": str(orig_path),
                    "destination": str(new_path),
                    "action": "regrouped",
                },
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].destination == str(new_path)


# ---------------------------------------------------------------------------
class TestAuditEnrichCLI:
    """Tests for the ``enrich`` top-level subcommand CLI dispatch path.

    Verifies that ``main()`` routes ``enrich`` to :func:`music_annotator.enrich` with the
    correct arguments, and that the standard error-handling paths (exception, KeyboardInterrupt)
    still exit with code 1.
    """

    def _patch_common(self, mocker: MockerFixture) -> None:
        """Patch logging and structlog so tests don't reconfigure the process logger.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")

    _ENRICH_ARGV = ["music-annotator", "enrich", "/d"]

    # pylint: disable-next=unused-argument
    def test_audit_enrich_dispatches_to_enrich(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich calls music_annotator.enrich with dest_root, re_resolve, dry_run.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        mocker.patch.object(sys, "argv", new=self._ENRICH_ARGV)
        main()
        mock_enrich.assert_called_once_with(dest_root=Path("/d"), re_resolve=False, dry_run=False, acoustid_key="")

    # pylint: disable-next=unused-argument
    def test_audit_enrich_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich --dry-run passes dry_run=True to enrich().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        mocker.patch.object(sys, "argv", new=[*self._ENRICH_ARGV, "--dry-run"])
        main()
        _, kwargs = mock_enrich.call_args
        assert kwargs["dry_run"] is True

    # pylint: disable-next=unused-argument
    def test_audit_enrich_re_resolve_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich --re-resolve passes re_resolve=True to enrich().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        mocker.patch.object(sys, "argv", new=[*self._ENRICH_ARGV, "--re-resolve"])
        main()
        _, kwargs = mock_enrich.call_args
        assert kwargs["re_resolve"] is True

    # pylint: disable-next=unused-argument
    def test_audit_enrich_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich exits with code 1 when enrich() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._ENRICH_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_audit_enrich_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._ENRICH_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_audit_enrich_parser_flags(self) -> None:
        """enrich parser accepts --re-resolve and --dry-run flags.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["enrich", "/dest", "--re-resolve", "--dry-run"])
        assert ns.re_resolve is True
        assert ns.dry_run is True


# ---------------------------------------------------------------------------
class TestNeedsEnrichMissingBranches:
    """Additional _needs_enrich tests to cover branches missed by TestNeedsEnrich.

    Covers:
    - audio_hash computed but returns empty (branch 258->262: ``if computed_hash:`` is False)
    - re_resolve=True but fpcalc returns empty (branch 269->273: ``if computed_fp:`` is False)
    """

    def test_audio_hash_computed_empty_not_included(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich omits audio_hash when _audio_hash returns empty string.

        Covers the ``if computed_hash:`` False branch (line 258->262).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(title="No Hash")
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._audio_hash", return_value="")
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="FP")
        mocker.patch("music_annotator._pipeline_io.log")

        result = _needs_enrich(path, re_resolve=False)

        assert "audio_hash" not in result

    def test_re_resolve_fpcalc_empty_not_included(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich omits chromaprint_fp when re_resolve=True but fpcalc returns empty.

        Covers the ``elif re_resolve: if computed_fp:`` False branch (line 269->273).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(audio_hash="flac-md5:existing", chromaprint_fp="OldFP", acoustid_id="uuid")
        apply_tags_flac(path, tags)

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")

        result = _needs_enrich(path, re_resolve=True)

        assert "chromaprint_fp" not in result


# ---------------------------------------------------------------------------
class TestEnrichUnrecognisedAction:
    """Test that enrich() ignores journal entries with actions other than tagged/repathed/regrouped/enriched."""

    def test_enrich_ignores_skipped_action(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() ignores journal entries with action="skipped" (covers the elif-enriched False branch).

        A "skipped" entry does not seed current_lib, so the file is not enriched.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "skipped",
                },
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        # The "tagged" entry seeds current_lib; the "skipped" entry is ignored.
        # The file should still be enriched (from the "tagged" entry).
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1


# ---------------------------------------------------------------------------
class TestRegroupEnrichedLineage:
    """Test that regroup() processes "enriched" journal entries to keep current_lib up to date.

    Covers lines 1813-1814 in _pipeline.py: the ``if dest_path in current_lib:`` branch inside
    the ``elif entry.action == "enriched":`` arm of regroup()'s journal-walk loop.
    """

    def test_regroup_enriched_entry_updates_current_lib(self, fs: FakeFilesystem) -> None:
        """regroup() re-registers a path when an "enriched" entry exists for a confirmed release.

        Scenario: a file was "tagged", then "enriched" (in-place, same path).  regroup() must
        process the "enriched" entry and keep the path registered in current_lib so that the
        file is still considered for regrouping.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Build a split-release scenario: two files in different work dirs for the same release.
        # File A is at a legacy path and has been enriched in-place.
        # File B is a phantom (confirms the release via its MUSICBRAINZ_ALBUMID tag).
        tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid="split-rel-enrich",
        )

        old_path = _make_library_flac(
            dest_root,
            "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac",
            tags,
        )
        phantom = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02.flac"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-enrich",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "split-rel-enrich",
                    "source": str(old_path),
                    "destination": str(old_path),
                    "action": "enriched",
                    "audio_hash": "flac-md5:aabb",
                    "chromaprint_fp": "FP",
                    "acoustid_id": "uuid",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "split-rel-enrich",
                    "source": "/src/02.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # old_path should have been moved (it was registered via the "enriched" entry)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        regrouped_sources = {e.source for e in regrouped}
        assert str(old_path) in regrouped_sources

    def test_regroup_enriched_entry_for_unconfirmed_path_is_ignored(self, fs: FakeFilesystem) -> None:
        """regroup() ignores an "enriched" entry when its path is not in current_lib.

        Covers the ``if dest_path in current_lib:`` False branch (line 1813->1801): when an
        "enriched" entry refers to a path that was tagged for a release NOT in
        ``confirmed_release_ids``, the ``if`` body is skipped.

        Scenario:
        - Release "confirmed-rel" has two files in different work_dirs → confirmed case-b →
          ``confirmed_release_ids = {"confirmed-rel"}``.  This prevents the early-return so the
          journal-walk loop is reached.
        - Release "other-rel" has a file that was "tagged" and then "enriched".  Because
          "other-rel" is NOT in ``confirmed_release_ids``, the "tagged" entry does NOT seed
          ``current_lib``.  The subsequent "enriched" entry therefore hits the
          ``if dest_path in current_lib:`` False branch and is silently skipped.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # --- confirmed-rel: two files in different work_dirs (case-b split) ---
        confirmed_tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid="confirmed-rel",
        )
        confirmed_path_a = _make_library_flac(
            dest_root,
            "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac",
            confirmed_tags,
        )
        phantom_b = dest_root / "Brahms - Vienna PO" / "Piano Concerto No. 1 [rec 2021]" / "02.flac"

        # --- other-rel: a file that was tagged then enriched (NOT confirmed) ---
        other_path = dest_root / "OtherArtist" / "OtherAlbum" / "01 - Track.flac"
        other_path.parent.mkdir(parents=True, exist_ok=True)
        other_path.write_bytes(_MINIMAL_FLAC)

        _write_library_journal(
            dest_root,
            [
                # confirmed-rel: two work_dirs → confirmed case-b
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "confirmed-rel",
                    "source": "/src/01.flac",
                    "destination": str(confirmed_path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "confirmed-rel",
                    "source": "/src/02.flac",
                    "destination": str(phantom_b),
                    "action": "tagged",
                },
                # other-rel: tagged (not confirmed) then enriched
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "other-rel",
                    "source": "/src/other.flac",
                    "destination": str(other_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:01:00+00:00",
                    "release_id": "other-rel",
                    "source": str(other_path),
                    "destination": str(other_path),
                    "action": "enriched",
                    "audio_hash": "flac-md5:aabb",
                    "chromaprint_fp": "FP",
                    "acoustid_id": "uuid",
                },
            ],
        )

        # regroup() must not crash; the "other-rel" enriched entry is silently ignored
        music_annotator.regroup(dest_root=dest_root, yes=True)

        # Only confirmed-rel files should appear in regrouped sources
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        regrouped_sources = {e.source for e in regrouped}
        assert str(other_path) not in regrouped_sources


# ---------------------------------------------------------------------------
class TestEnrichTagWriteError:
    """Test that enrich() raises RuntimeError when apply_tags_flac raises MutagenError."""

    def test_enrich_mutagen_error_on_write_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich() raises RuntimeError when apply_tags_flac raises MutagenError.

        Covers lines 2110-2111: the ``except MutagenError`` handler in the tag-write block.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(acoustid_id="test-acoustid-id")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm")
        mocker.patch("music_annotator._pipeline_maint.apply_tags_flac", side_effect=MutagenError("write failed"))

        with pytest.raises(RuntimeError, match="enrich tag write failure"):
            music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)


# ---------------------------------------------------------------------------
class TestEnrichAcoustidReResolve:
    """Tests for the C-F6d acoustid_id re-resolve in enrich().

    When re_resolve=True and acoustid_key is non-empty, enrich() calls
    _fetch_acoustid_lookup_raw after recomputing chromaprint_fp and backfills
    acoustid_id with the top AcoustID cluster UUID.
    """

    def test_re_resolve_with_acoustid_key_updates_acoustid_id(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """--re-resolve + acoustid_key → acoustid_id updated in FLAC tag and journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File has an old acoustid_id and an existing chromaprint_fp (will be re-resolved)
        tags = TrackTags(
            audio_hash="flac-md5:existing",
            chromaprint_fp="OldFingerprint",
            acoustid_id="old-acoustid-uuid",
        )
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")
        mocker.patch(
            "music_annotator._pipeline_maint._fetch_acoustid_lookup_raw",
            return_value=(["rec-mbid"], "new-acoustid-uuid"),
        )
        mocker.patch("music_annotator._pipeline_maint._read_duration_ms", return_value=180000)

        music_annotator.enrich(dest_root=dest_root, re_resolve=True, dry_run=False, acoustid_key="my-api-key")

        # FLAC tag should have the new acoustid_id written by the re-resolve lookup
        audio = MutagenFLAC(str(path))
        acoustid_vals = audio.get("acoustid_id") or []
        assert acoustid_vals and acoustid_vals[0] == "new-acoustid-uuid"

        # Journal entry is written; an enriched entry exists
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        # The chromaprint_fp was re-resolved
        assert enriched[0].chromaprint_fp == "NewFingerprint"

    def test_re_resolve_without_acoustid_key_does_not_call_lookup(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """--re-resolve without acoustid_key does NOT call _fetch_acoustid_lookup_raw (F4 behaviour preserved).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            audio_hash="flac-md5:existing",
            chromaprint_fp="OldFingerprint",
            acoustid_id="old-acoustid-uuid",
        )
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")
        mock_lookup = mocker.patch("music_annotator._pipeline_maint._fetch_acoustid_lookup_raw")

        music_annotator.enrich(dest_root=dest_root, re_resolve=True, dry_run=False, acoustid_key="")

        mock_lookup.assert_not_called()

    def test_re_resolve_with_acoustid_key_but_empty_lookup_leaves_acoustid_id_unchanged(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """--re-resolve + acoustid_key but lookup returns [] → acoustid_id unchanged (inconclusive).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            audio_hash="flac-md5:existing",
            chromaprint_fp="OldFingerprint",
            acoustid_id="old-acoustid-uuid",
        )
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")
        mocker.patch(
            "music_annotator._pipeline_maint._fetch_acoustid_lookup_raw",
            return_value=([], ""),
        )
        mocker.patch("music_annotator._pipeline_maint._read_duration_ms", return_value=180000)

        music_annotator.enrich(dest_root=dest_root, re_resolve=True, dry_run=False, acoustid_key="my-api-key")

        # acoustid_id should remain unchanged (lookup returned no results)
        audio = MutagenFLAC(str(path))
        acoustid_vals = audio.get("acoustid_id") or []
        assert acoustid_vals and acoustid_vals[0] == "old-acoustid-uuid"

    def test_re_resolve_acoustid_lookup_failure_is_logged_and_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When _fetch_acoustid_lookup_raw raises, the error is logged and acoustid_id is left unchanged.

        Covers the ``except (OSError, RuntimeError, ValueError)`` branch in enrich() that handles
        cannot-determine AcoustID failures (5xx exhaustion, malformed JSON) without aborting the run.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            audio_hash="flac-md5:existing",
            chromaprint_fp="OldFingerprint",
            acoustid_id="old-acoustid-uuid",
        )
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")
        mocker.patch(
            "music_annotator._pipeline_maint._fetch_acoustid_lookup_raw",
            side_effect=OSError("acoustid network failure"),
        )
        mocker.patch("music_annotator._pipeline_maint._read_duration_ms", return_value=180000)

        # Should not raise — the error is caught and logged.
        music_annotator.enrich(dest_root=dest_root, re_resolve=True, dry_run=False, acoustid_key="my-api-key")

        # acoustid_id should remain unchanged (lookup failed, not overwritten).
        audio = MutagenFLAC(str(path))
        acoustid_vals = audio.get("acoustid_id") or []
        assert acoustid_vals and acoustid_vals[0] == "old-acoustid-uuid"


# ---------------------------------------------------------------------------
class TestAuditOriginTimeCLI:
    """Tests for the ``origin-time`` top-level subcommand CLI dispatch path.

    Verifies that ``main()`` routes ``origin-time`` to
    :func:`music_annotator.enrich_origin_time` with the correct arguments, and that the standard
    error-handling paths (exception, KeyboardInterrupt) still exit with code 1.
    """

    def _patch_common(self, mocker: MockerFixture) -> None:
        """Patch logging and structlog so tests don't reconfigure the process logger.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")

    _ORIGIN_TIME_ARGV = ["music-annotator", "origin-time", "/d"]

    # pylint: disable-next=unused-argument
    def test_origin_time_dispatches_to_enrich_origin_time(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() origin-time calls music_annotator.enrich_origin_time with dest_root and dry_run=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_fn = mocker.patch("music_annotator.enrich_origin_time")
        mocker.patch.object(sys, "argv", new=self._ORIGIN_TIME_ARGV)
        main()
        mock_fn.assert_called_once_with(dest_root=Path("/d"), dry_run=False)

    # pylint: disable-next=unused-argument
    def test_origin_time_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() origin-time --dry-run passes dry_run=True to enrich_origin_time().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_fn = mocker.patch("music_annotator.enrich_origin_time")
        mocker.patch.object(sys, "argv", new=[*self._ORIGIN_TIME_ARGV, "--dry-run"])
        main()
        _, kwargs = mock_fn.call_args
        assert kwargs["dry_run"] is True

    # pylint: disable-next=unused-argument
    def test_origin_time_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() origin-time exits with code 1 when enrich_origin_time() raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich_origin_time", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._ORIGIN_TIME_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_origin_time_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() origin-time exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich_origin_time", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._ORIGIN_TIME_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_origin_time_parser_flag(self) -> None:
        """origin-time parser accepts dest_dir and --dry-run flag.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["origin-time", "/dest", "--dry-run"])
        assert ns.subcommand == "origin-time"
        assert ns.dry_run is True

    def test_origin_time_default_false(self) -> None:
        """origin-time parser sets dry_run=False by default.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["origin-time", "/dest"])
        assert ns.dry_run is False

    # ------------------------------------------------------------------
    # rebuild dispatch tests
    # ------------------------------------------------------------------

    _REBUILD_ARGV = ["music-annotator", "rebuild", "/d"]

    # pylint: disable-next=unused-argument
    def test_rebuild_dispatches_to_rebuild_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() rebuild calls music_annotator.rebuild_journal with dest_root and dry_run=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_rebuild = mocker.patch("music_annotator.rebuild_journal")
        mocker.patch.object(sys, "argv", new=self._REBUILD_ARGV)
        main()
        mock_rebuild.assert_called_once_with(dest_root=Path("/d"), dry_run=True)

    # pylint: disable-next=unused-argument
    def test_rebuild_apply_flag_passes_dry_run_false(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() rebuild --apply passes dry_run=False to rebuild_journal().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_rebuild = mocker.patch("music_annotator.rebuild_journal")
        mocker.patch.object(sys, "argv", new=[*self._REBUILD_ARGV, "--apply"])
        main()
        _, kwargs = mock_rebuild.call_args
        assert kwargs["dry_run"] is False

    # pylint: disable-next=unused-argument
    def test_rebuild_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() rebuild exits with code 1 when rebuild_journal() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.rebuild_journal", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._REBUILD_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_rebuild_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() rebuild exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.rebuild_journal", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._REBUILD_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_rebuild_parser_dry_run_default(self) -> None:
        """rebuild parser defaults to dry_run=True when no mode flag is given.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["rebuild", "/dest"])
        assert ns.dry_run is True
        assert ns.apply is False

    def test_rebuild_parser_apply_flag(self) -> None:
        """rebuild parser accepts --apply flag and sets apply=True.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["rebuild", "/dest", "--apply"])
        assert ns.apply is True


# ---------------------------------------------------------------------------
class TestUnify:
    """Tests for :func:`music_annotator.unify` — performer-split fragmentation consolidation.

    The KAT ``test_unify_appends_journal_entry`` is the load-bearing assertion: it drives a
    full performer-split fragmented release scenario (two top_dirs for one MUSICBRAINZ_ALBUMID)
    through ``unify()`` and asserts that a ``TransactionEntry(action="unified")`` is appended to
    the journal, with source=old path, destination=new path, release_id=the release's MBID.

    All other tests cover the required branches for 100% branch coverage:
    dry_run, prompt-accepted, prompt-declined, yes=True, empty-plan (nothing to unify),
    idempotency (second run is a no-op), SHA-mismatch (provenance invariant: NO journal entry
    on mismatch), EXDEV cross-fs fallback, and the main() dispatch arms.

    Uses real FLAC bytes and :func:`apply_tags_flac` so that :func:`_read_tags_flac` executes
    the real mutagen round-trip rather than a mock.  No MusicBrainz network calls are involved
    (unify is fully offline — detection is by embedded tag, not journal).
    """

    # Tags for a file that should be unified.
    # The MUSICBRAINZ_ALBUMID tag is set to "frag-rel-1" so detect_fragmented_releases picks it up.
    # build_dest_path (with these tags) produces:
    #   <dest_root>/Brahms - Karajan/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac
    # The split scenario: same MUSICBRAINZ_ALBUMID "frag-rel-1" has files under TWO different
    # top_dirs:
    #   "Brahms - Pollini"  (where file A currently lives — wrong performer in path)
    #   "Brahms - Karajan"  (the canonical path from tags)
    # After unify(), file A should move to "Brahms - Karajan".

    @staticmethod
    def _make_frag_tags() -> TrackTags:
        """Build TrackTags for the fragmented-release test file.

        Sets MUSICBRAINZ_ALBUMID so detect_fragmented_releases detects the fragmentation.
        The tags drive build_dest_path to produce the canonical path under "Brahms - Karajan".

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
            artist="Karajan",
            musicbrainz_albumid="frag-rel-1",
        )

    @staticmethod
    def _canonical_path(dest_root: Path, tags: TrackTags, ext: str = ".flac") -> Path:
        """Compute the canonical destination path for given tags.

        :param dest_root: Library root.
        :param tags: Tags to drive build_dest_path.
        :param ext: File extension.
        :returns: Full absolute canonical path.
        """
        base = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0)
        return base.with_suffix(ext)

    def _build_frag_scenario(self, dest_root: Path) -> tuple[Path, Path]:
        """Create a performer-split fragmented release scenario under dest_root.

        Two FLAC files for release_id "frag-rel-1" land under different top_dirs:
        - File A lives at "Brahms - Pollini/..." (wrong performer in path) with
          MUSICBRAINZ_ALBUMID="frag-rel-1" embedded.
        - File B lives at the canonical path "Brahms - Karajan/..." (already correct).

        detect_fragmented_releases will detect two distinct top_dirs for "frag-rel-1".
        unify() should move File A to the canonical path.

        Returns (old_path, new_path) where old_path is File A's current location and new_path is
        the recomputed canonical destination from the embedded tags.

        :param dest_root: Library root (must already exist).
        :returns: Tuple of (current file path, expected canonical path after unify).
        """
        tags = self._make_frag_tags()

        # File A: lives at wrong top_dir (Pollini instead of Karajan)
        old_path = _make_library_flac(
            dest_root, "Brahms - Pollini/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac", tags
        )

        # File B: already at canonical path (Karajan) — ensures two distinct top_dirs
        canonical_path = self._canonical_path(dest_root, tags)
        canonical_rel = str(canonical_path.relative_to(dest_root))
        _make_library_flac(dest_root, canonical_rel, tags)

        # The old and canonical paths must differ for the scenario to be non-trivial
        assert old_path != canonical_path, "test setup error: old and canonical paths must differ"

        return old_path, canonical_path

    # ------------------------------------------------------------------
    # KAT: test_unify_appends_journal_entry
    # ------------------------------------------------------------------

    def test_unify_appends_journal_entry(self, fs: FakeFilesystem) -> None:
        """unify() moves the file and appends a TransactionEntry(action="unified") to the journal.

        This is the KAT for W2a.  Constructs a performer-split fragmented release scenario (one
        MUSICBRAINZ_ALBUMID scattered across two top_dirs) and drives unify(yes=True) through the
        full move+verify+journal provenance chain.  Asserts:

        (a) A TransactionEntry with action="unified", source=old path, destination=new path,
            release_id="frag-rel-1" is appended to the journal.
        (b) The file exists at the new canonical path and no longer at the old path.
        (c) The file bytes are intact (re-read MUSICBRAINZ_ALBUMID via _read_albumid_tag).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_frag_scenario(dest_root)

        # Act: unify with yes=True to skip the interactive prompt
        music_annotator.unify(dest_root=dest_root, yes=True)

        # (a) Journal gained a "unified" entry with correct fields
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1
        entry = unified[0]
        assert entry.source == str(old_path)
        assert entry.destination == str(new_path)
        assert entry.release_id == "frag-rel-1"

        # (b) File moved: new path exists, old path gone
        assert new_path.exists()
        assert not old_path.exists()

        # (c) Bytes intact: MUSICBRAINZ_ALBUMID readable at new path
        assert _read_albumid_tag(new_path) == "frag-rel-1"

    # ------------------------------------------------------------------
    # dry_run: no move, no journal write
    # ------------------------------------------------------------------

    def test_unify_dry_run_no_move_no_journal(self, fs: FakeFilesystem) -> None:
        """unify(dry_run=True) logs planned moves but writes nothing to disk or journal.

        Asserts that (a) File A remains at its old (wrong) path and (b) no "unified" journal
        entries are added.  Note: File B already exists at the canonical path (new_path) as part
        of the fragmented scenario setup, so we only check that old_path is not moved.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, _new_path = self._build_frag_scenario(dest_root)

        music_annotator.unify(dest_root=dest_root, dry_run=True)

        # (a) File A still at old (wrong) path — not moved
        assert old_path.exists()

        # (b) No "unified" entries
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    # ------------------------------------------------------------------
    # Confirmation prompt: accepted (y) and declined (n)
    # ------------------------------------------------------------------

    def test_unify_prompt_accepted_moves_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() with yes=False prompts; answering 'y' proceeds with the move.

        Mocks input() to return "y" and asserts the file is moved and journalled.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_frag_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.input", return_value="y")

        music_annotator.unify(dest_root=dest_root, yes=False)

        assert new_path.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1
        assert unified[0].release_id == "frag-rel-1"

    def test_unify_prompt_declined_no_move(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() with yes=False prompts; answering 'n' aborts with no move and no journal entry.

        Mocks input() to return "n" and asserts File A remains at its old path and no
        "unified" journal entry is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, _new_path = self._build_frag_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.input", return_value="n")

        music_annotator.unify(dest_root=dest_root, yes=False)

        # File A stays at old path — not moved
        assert old_path.exists()

        # No "unified" entries
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    # ------------------------------------------------------------------
    # yes=True: prompt skipped
    # ------------------------------------------------------------------

    def test_unify_yes_skips_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify(yes=True) does not call input() at all.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario(dest_root)

        mock_input = mocker.patch("music_annotator._pipeline_maint.input")

        music_annotator.unify(dest_root=dest_root, yes=True)

        mock_input.assert_not_called()

    # ------------------------------------------------------------------
    # Empty plan / nothing-to-unify paths
    # ------------------------------------------------------------------

    def test_unify_nothing_to_unify_empty_library(self, fs: FakeFilesystem) -> None:
        """unify() on an empty library returns immediately (no plan, no prompt).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # No files → no fragmented releases
        music_annotator.unify(dest_root=dest_root, yes=True)
        # No exception raised; no journal created

    def test_unify_nothing_to_unify_when_all_paths_already_canonical(self, fs: FakeFilesystem) -> None:
        """unify() is a no-op when all files already live at their canonical paths.

        The plan is empty even for a fragmented release if every file already lives at its
        canonical destination (build_dest_path returns the same path as the current path).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_frag_tags()
        # Place both files at canonical paths (same top_dir → not fragmented)
        canonical_path = self._canonical_path(dest_root, tags)
        canonical_rel = str(canonical_path.relative_to(dest_root))
        _make_library_flac(dest_root, canonical_rel, tags)

        # Only one top_dir → detect_fragmented_releases returns empty → nothing to unify
        music_annotator.unify(dest_root=dest_root, yes=True)

        # File stays at canonical path; no journal entries added
        assert canonical_path.exists()
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    # ------------------------------------------------------------------
    # Idempotency: second run finds nothing to do
    # ------------------------------------------------------------------

    def test_unify_idempotent_second_run_noop(self, fs: FakeFilesystem) -> None:
        """A second unify() run after the first is a complete no-op.

        After the first run moves all fragments to canonical paths, all files share the same
        top_dir, so detect_fragmented_releases returns empty and unify() returns immediately.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_frag_scenario(dest_root)

        # First run: moves the file
        music_annotator.unify(dest_root=dest_root, yes=True)
        assert new_path.exists()
        assert not old_path.exists()

        # Second run: no-op
        music_annotator.unify(dest_root=dest_root, yes=True)

        # Still only one "unified" entry from the first run
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1

    # ------------------------------------------------------------------
    # SHA mismatch after move → RuntimeError, NO journal entry
    # (Provenance invariant: the most critical test)
    # ------------------------------------------------------------------

    def test_unify_sha_mismatch_raises_no_journal_entry(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() raises RuntimeError on SHA-256 mismatch and writes NO journal entry.

        This is the provenance-invariant test.  Patches _sha256_file to return a mismatched hash
        for the destination check (simulating silent corruption during the move), and asserts that:
        (a) RuntimeError is raised, and
        (b) no "unified" journal entry is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario(dest_root)

        # Patch _sha256_file: first call returns "aaa..." (src), second returns "bbb..." (dest ≠ src)
        call_count = {"n": 0}

        def _fake_sha256(path: Path) -> str:
            """Return mismatched hash on second call to simulate corruption.

            :param path: File path (unused).
            :returns: Hash string.
            """
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "a" * 64  # src hash
            if call_count["n"] == 2:
                return "b" * 64  # dest hash ≠ src → triggers RuntimeError
            return _sha256_file(path)  # subsequent calls use real implementation

        mocker.patch("music_annotator._pipeline_maint._sha256_file", side_effect=_fake_sha256)

        with pytest.raises(RuntimeError, match="unified integrity failure"):
            music_annotator.unify(dest_root=dest_root, yes=True)

        # No "unified" entry written
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    # ------------------------------------------------------------------
    # EXDEV cross-filesystem fallback
    # ------------------------------------------------------------------

    def test_unify_exdev_cross_fs_fallback(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() falls back to copy+unlink when os.replace raises EXDEV.

        Patches os.replace to raise OSError(EXDEV) on the first call (the atomic rename attempt)
        and asserts the file is still moved correctly via the shutil.copy2 + os.unlink fallback.
        The real shutil.copy2 is used (not mocked) so the file is actually copied.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_frag_scenario(dest_root)

        # Patch os.replace to raise EXDEV only on the first call (the atomic rename attempt).
        # Subsequent calls (e.g. from shutil.copy2 internals) use the real implementation.
        original_replace = os.replace
        call_count = {"n": 0}

        def _fake_replace(src: str, dst: str) -> None:
            """Raise EXDEV on first call to simulate cross-filesystem move.

            :param src: Source path.
            :param dst: Destination path.
            """
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError(errno.EXDEV, "Cross-device link")
            original_replace(src, dst)

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=_fake_replace)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # File A was removed (unlinked after copy); canonical path still exists
        assert not old_path.exists()
        assert new_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1

    # ------------------------------------------------------------------
    # main() dispatch: unify subcommand
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_common(mocker: MockerFixture) -> None:
        """Patch structlog and configure_color for main() dispatch tests.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.configure_color")
        mocker.patch("structlog.configure")

    _UNIFY_ARGV = ["music-annotator", "unify", "/dest"]

    def test_main_unify_dispatches(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() dispatches 'unify' subcommand to music_annotator.unify.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/dest")
        mock_unify = mocker.patch("music_annotator.unify")
        mocker.patch.object(sys, "argv", new=self._UNIFY_ARGV)
        main()
        mock_unify.assert_called_once_with(dest_root=Path("/dest"), yes=False, dry_run=False)

    def test_main_unify_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() unify exits with code 1 on unhandled exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/dest")
        mocker.patch("music_annotator.unify", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._UNIFY_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_main_unify_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() unify exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/dest")
        mocker.patch("music_annotator.unify", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._UNIFY_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_unify_parser_dry_run_flag(self) -> None:
        """unify parser accepts --dry-run flag.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["unify", "/dest", "--dry-run"])
        assert ns.dry_run is True

    def test_unify_parser_yes_flag(self) -> None:
        """unify parser accepts -y/--yes flag.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["unify", "/dest", "--yes"])
        assert ns.yes is True

    # ------------------------------------------------------------------
    # MP3 file path in unify
    # ------------------------------------------------------------------

    def test_unify_moves_mp3_file(self, fs: FakeFilesystem) -> None:
        """unify() handles MP3 files correctly (exercises the .mp3 tag-read branch).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_frag_tags()

        # File A: MP3 at wrong top_dir
        old_path = dest_root / "Brahms - Pollini" / "Piano Concerto No. 1 [rec 2021]" / "01 - First movement.mp3"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(old_path, tags)

        # File B: FLAC at canonical path (creates second top_dir)
        canonical_path = self._canonical_path(dest_root, tags)
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        canonical_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical_path, tags)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # MP3 file moved to canonical path
        new_mp3 = canonical_path.with_suffix(".mp3")
        assert new_mp3.exists()
        assert not old_path.exists()

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1

    # ------------------------------------------------------------------
    # group_tags empty → continue (all files in a release group fail to read)
    # ------------------------------------------------------------------

    def test_unify_skips_release_when_all_files_unreadable(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() skips a release when all its files fail tag-read (group_tags empty).

        Patches _read_tags_flac to raise an exception for all files, exercising the
        ``if not group_tags: continue`` branch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=RuntimeError("unreadable"))

        # Should not raise; just skips the release
        music_annotator.unify(dest_root=dest_root, yes=True)

        # No "unified" entries since all files were unreadable
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    # ------------------------------------------------------------------
    # ValueError in length_ms parsing
    # ------------------------------------------------------------------

    def test_unify_handles_invalid_length_tag(self, fs: FakeFilesystem) -> None:
        """unify() handles a non-integer LENGTH tag gracefully (exercises ValueError branch).

        Creates a file with a non-integer LENGTH tag so the ``except ValueError: length_ms = 0``
        branch is exercised.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_frag_tags()
        # Add an invalid LENGTH tag via model_extra
        if tags.model_extra is None:  # pragma: no cover
            pass
        else:
            tags.model_extra["length"] = "not-a-number"

        # File A: wrong top_dir
        old_path = _make_library_flac(
            dest_root, "Brahms - Pollini/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac", tags
        )

        # File B: canonical path
        canonical_path = self._canonical_path(dest_root, tags)
        canonical_rel = str(canonical_path.relative_to(dest_root))
        _make_library_flac(dest_root, canonical_rel, tags)

        # Should not raise; length_ms defaults to 0
        music_annotator.unify(dest_root=dest_root, yes=True)

        assert not old_path.exists()
        assert canonical_path.exists()

    # ------------------------------------------------------------------
    # Collision suffix applied
    # ------------------------------------------------------------------

    def test_unify_collision_suffix_applied(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() applies a collision suffix when two files recompute to the same destination.

        Patches _assess_collisions to return a confirmed non-match, exercising the
        collision-suffix branch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        old_path, new_path = self._build_frag_scenario(dest_root)

        # Simulate a confirmed non-match collision at the destination
        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_path, match=False, method="sha256", detail="different")],
        )

        music_annotator.unify(dest_root=dest_root, yes=True)

        # A journal entry should exist (file moved with suffix)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1

    # ------------------------------------------------------------------
    # EXDEV cross-fs fallback with cross-hash mismatch
    # ------------------------------------------------------------------

    def test_unify_non_exdev_oserror_propagates(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() re-raises OSError when it is not EXDEV (e.g. EACCES permission denied).

        Patches os.replace to raise OSError(EACCES), exercising the ``if exc.errno != errno.EXDEV: raise``
        branch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=OSError(errno.EACCES, "Permission denied"))

        with pytest.raises(OSError):
            music_annotator.unify(dest_root=dest_root, yes=True)

    def test_unify_exdev_cross_hash_mismatch_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() raises RuntimeError when cross-fs copy produces a hash mismatch.

        Patches os.replace to raise EXDEV and _sha256_file to return mismatched hashes
        for the cross-fs copy verification, exercising the cross-hash-mismatch branch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario(dest_root)

        # Patch os.replace to always raise EXDEV
        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=OSError(errno.EXDEV, "Cross-device link"))

        # Patch _sha256_file: first call (src) returns "aaa...", second call (cross-fs dest) returns "bbb..."
        call_count = {"n": 0}

        def _fake_sha256(_path: Path) -> str:
            """Return mismatched hash on second call.

            :param _path: File path (unused; hash is determined by call count).
            :returns: Hash string.
            """
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "a" * 64  # src hash
            return "b" * 64  # cross-fs dest hash ≠ src → triggers RuntimeError

        mocker.patch("music_annotator._pipeline_maint._sha256_file", side_effect=_fake_sha256)

        with pytest.raises(RuntimeError, match="cross-fs copy integrity failure"):
            music_annotator.unify(dest_root=dest_root, yes=True)

        # No "unified" entry written
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    # ------------------------------------------------------------------
    # Tag re-read failure after move
    # ------------------------------------------------------------------

    def test_unify_tag_reread_failure_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() raises RuntimeError when tag re-read fails after the move.

        Patches _read_tags_flac to raise on the second call (post-move re-read), exercising
        the ``except Exception: raise RuntimeError(...)`` branch in the tag re-read step.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario(dest_root)

        original_read = _read_tags_flac
        call_count = {"n": 0}

        def _fake_read(path: Path) -> dict[str, str]:
            """Raise on second call to simulate post-move tag read failure.

            :param path: File path.
            :returns: Tag dict.
            """
            call_count["n"] += 1
            if call_count["n"] >= 3:  # First two calls are during plan-building; third is post-move
                raise RuntimeError("tag read failed")
            return original_read(path)

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=_fake_read)

        with pytest.raises(RuntimeError, match="unified tag re-read failure"):
            music_annotator.unify(dest_root=dest_root, yes=True)

    # ------------------------------------------------------------------
    # OSError in directory cleanup (non-empty dir)
    # ------------------------------------------------------------------

    def test_unify_cleanup_skips_nonempty_dir(self, fs: FakeFilesystem) -> None:
        """unify() skips directory cleanup when the source dir is not empty.

        Creates a second file in the source directory so rmdir() raises OSError, exercising
        the ``except OSError: break`` branch in the cleanup loop.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_frag_tags()

        # File A: wrong top_dir
        old_path = _make_library_flac(
            dest_root, "Brahms - Pollini/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac", tags
        )

        # Extra file in the same directory — prevents rmdir() from succeeding
        extra = old_path.parent / "extra.txt"
        extra.write_text("extra", encoding="utf-8")

        # File B: canonical path
        canonical_path = self._canonical_path(dest_root, tags)
        canonical_rel = str(canonical_path.relative_to(dest_root))
        _make_library_flac(dest_root, canonical_rel, tags)

        # Should not raise; cleanup just breaks out of the loop
        music_annotator.unify(dest_root=dest_root, yes=True)

        # File A moved; source dir still exists (not empty due to extra.txt)
        assert not old_path.exists()
        assert old_path.parent.exists()  # dir not removed (still has extra.txt)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1

    # ------------------------------------------------------------------
    # W2b: Composer-split unification (Benny Goodman shape)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_composer_split_tags(composer: str, album_artist_sort: str = "Goodman, Benny") -> TrackTags:
        """Build TrackTags for a non-classical multi-composer compilation track.

        The release has no CWP_WORK_TOP (non-classical) and a varying CEA_COMPOSER_LASTNAMES
        across tracks.  ALBUMARTISTSORT is set to ``album_artist_sort`` so the canonical
        composer component can be derived from it.  ``releasetype="Album"`` routes the release
        to the ``Popular`` C-CLASS so the top_dir uses ``<ALBUMARTIST> - <ALBUM>`` shape.

        :param composer: The per-track CEA_COMPOSER_LASTNAMES value.
        :param album_artist_sort: The ALBUMARTISTSORT value (uniform across the release).
        :returns: A :class:`TrackTags` instance.
        """
        return TrackTags(
            cea_composer_lastnames=composer,
            albumartistsort=album_artist_sort,
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="",  # non-classical: no MB work link
            cwp_worktype_genres_top="",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Track 1",
            artist="Benny Goodman",
            musicbrainz_albumid="goodman-rel-1",
        )

    def test_composer_split_detected_and_unified(self, fs: FakeFilesystem) -> None:
        """unify() detects a composer-split release and unifies all tracks under ALBUMARTISTSORT.

        Creates two FLAC files for the same release_id under different top_dirs due to varying
        CEA_COMPOSER_LASTNAMES ("Goodman" vs "Berlin").  After unify(), both files should land
        under the canonical top_dir derived from ALBUMARTISTSORT ("Goodman, Benny").

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File A: CEA_COMPOSER_LASTNAMES="Goodman" → top_dir "Goodman - Benny Goodman"
        tags_a = self._make_composer_split_tags("Goodman")
        path_a = _make_library_flac(dest_root, "Goodman - Benny Goodman/The Story [rel 1956]/01 - Track 1.flac", tags_a)

        # File B: CEA_COMPOSER_LASTNAMES="Berlin" → different top_dir (triggers fragmentation)
        tags_b = self._make_composer_split_tags("Berlin")
        path_b = _make_library_flac(dest_root, "Berlin - Benny Goodman/The Story [rel 1956]/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # Both files should now be under the canonical composer component "Goodman"
        # (last_name("Goodman, Benny") == "Goodman" — strips the given-name suffix)
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        # At least one file was moved (the one not already at the canonical path)
        assert len(unified) >= 1

        # The file that was at the wrong path should have moved
        moved_dests = {e.destination for e in unified}
        # All moved destinations should be under the canonical top_dir.
        # With C-CLASS, Popular releases use <ALBUMARTIST> - <ALBUM> as top_dir (parts[1]).
        for dest_str in moved_dests:
            dest_path = Path(dest_str)
            rel = dest_path.relative_to(dest_root)
            # parts[0] = class ("Popular"), parts[1] = top_dir ("<albumartist> - <album>")
            assert rel.parts[0] == "Popular", f"Expected 'Popular', got {rel.parts[0]!r}"
            assert rel.parts[1].startswith("Benny Goodman"), (
                f"Expected top_dir starting with 'Benny Goodman', got {rel.parts[1]!r}"
            )

        # Original paths should no longer exist (they were moved)
        assert not path_a.exists() or not path_b.exists()

    def test_composer_split_canonical_path_uses_albumartistsort(self, fs: FakeFilesystem) -> None:
        """unify() uses ALBUMARTISTSORT to derive the canonical composer component.

        Creates a composer-split scenario where ALBUMARTISTSORT="Goodman, Benny".
        Asserts that the moved file lands in a top_dir derived from last_name("Goodman, Benny").

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File A: wrong composer in path
        tags_a = self._make_composer_split_tags("Berlin", album_artist_sort="Goodman, Benny")
        path_a = _make_library_flac(dest_root, "Berlin - Benny Goodman/The Story [rel 1956]/01 - Track 1.flac", tags_a)

        # File B: also wrong composer, different top_dir (triggers fragmentation detection)
        tags_b = self._make_composer_split_tags("Goodman", album_artist_sort="Goodman, Benny")
        path_b = _make_library_flac(dest_root, "Goodman - Benny Goodman/The Story [rel 1956]/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # All moved destinations must be under a top_dir starting with "Benny Goodman"
        # (albumartist="Benny Goodman", album="The Story" → top_dir "Benny Goodman - The Story")
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Popular", f"Expected 'Popular', got {rel.parts[0]!r}"
            assert rel.parts[1].startswith("Benny Goodman"), (
                f"Expected top_dir starting with 'Benny Goodman', got {rel.parts[1]!r}"
            )

        # At least one file was moved
        assert not path_a.exists() or not path_b.exists()

    def test_composer_split_albumartistsort_empty_uses_various(self, fs: FakeFilesystem) -> None:
        """unify() falls back to 'Various' when ALBUMARTISTSORT is empty.

        Creates a composer-split release with ALBUMARTISTSORT="" and asserts that the canonical
        composer component is "Various".

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File A: CEA_COMPOSER_LASTNAMES="Goodman", ALBUMARTISTSORT="" → fallback to "Various"
        tags_a = self._make_composer_split_tags("Goodman", album_artist_sort="")
        path_a = _make_library_flac(dest_root, "Goodman - Various/The Story [rel 1956]/01 - Track 1.flac", tags_a)

        # File B: CEA_COMPOSER_LASTNAMES="Berlin", ALBUMARTISTSORT="" → same fallback
        tags_b = self._make_composer_split_tags("Berlin", album_artist_sort="")
        path_b = _make_library_flac(dest_root, "Berlin - Various/The Story [rel 1956]/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # All moved destinations must be under the same canonical top_dir.
        # With C-CLASS, Popular releases use <ALBUMARTIST> - <ALBUM> as top_dir (parts[1]).
        # albumartist="Benny Goodman" (set by _make_composer_split_tags), album="The Story".
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Popular", f"Expected 'Popular', got {rel.parts[0]!r}"
            assert rel.parts[1].startswith("Benny Goodman"), (
                f"Expected top_dir starting with 'Benny Goodman', got {rel.parts[1]!r}"
            )

        assert not path_a.exists() or not path_b.exists()

    def test_composer_split_various_artists_albumartistsort_uses_various(self, fs: FakeFilesystem) -> None:
        """unify() falls back to 'Various' when ALBUMARTISTSORT is 'Various Artists'.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = self._make_composer_split_tags("Goodman", album_artist_sort="Various Artists")
        path_a = _make_library_flac(dest_root, "Goodman - Various/The Story [rel 1956]/01 - Track 1.flac", tags_a)

        tags_b = self._make_composer_split_tags("Berlin", album_artist_sort="Various Artists")
        path_b = _make_library_flac(dest_root, "Berlin - Various/The Story [rel 1956]/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # With C-CLASS, Popular releases use <ALBUMARTIST> - <ALBUM> as top_dir (parts[1]).
        # albumartist="Benny Goodman" (set by _make_composer_split_tags), album="The Story".
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Popular", f"Expected 'Popular', got {rel.parts[0]!r}"
            assert rel.parts[1].startswith("Benny Goodman"), (
                f"Expected top_dir starting with 'Benny Goodman', got {rel.parts[1]!r}"
            )

        assert not path_a.exists() or not path_b.exists()

    def test_composer_split_non_classical_with_work_top_triggers_rule(self, fs: FakeFilesystem) -> None:
        """unify() applies composer-split rule when CWP_WORK_TOP is set but genre is not Classical.

        Exercises the ``"Classical" not in tags.cwp_worktype_genres_top`` branch of the scope gate:
        a release with a MB work link (CWP_WORK_TOP non-empty) but a non-classical genre (e.g.
        "Jazz") is still treated as a non-classical multi-composer compilation.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Non-classical release with a work link but genre "Jazz" (not "Classical").
        # releasetype="Album" routes to Popular class; albumartist/album set the top_dir shape.
        tags_a = TrackTags(
            cea_composer_lastnames="Goodman",
            albumartistsort="Goodman, Benny",
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="The Benny Goodman Story",  # has MB work link
            cwp_worktype_genres_top="Jazz",  # NOT "Classical" → composer-split rule applies
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Track 1",
            artist="Benny Goodman",
            musicbrainz_albumid="jazz-rel-1",
        )
        tags_b = TrackTags(
            cea_composer_lastnames="Berlin",  # different composer → composer-split
            albumartistsort="Goodman, Benny",
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="The Benny Goodman Story",
            cwp_worktype_genres_top="Jazz",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Track 2",
            artist="Benny Goodman",
            musicbrainz_albumid="jazz-rel-1",
        )

        _make_library_flac(dest_root, "Goodman - Benny Goodman/The Story [rel 1956]/01 - Track 1.flac", tags_a)
        _make_library_flac(dest_root, "Berlin - Benny Goodman/The Story [rel 1956]/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # Composer-split rule should have fired: canonical composer = last_name("Goodman, Benny") = "Goodman"
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # With C-CLASS, Popular releases use <ALBUMARTIST> - <ALBUM> as top_dir (parts[1]).
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Popular", f"Expected class 'Popular', got {rel.parts[0]!r}"
            assert rel.parts[1].startswith("Benny Goodman"), (
                f"Expected composer-split rule to fire for Jazz genre; got top_dir={rel.parts[1]!r}"
            )

    def test_composer_split_classical_release_not_affected(self, fs: FakeFilesystem) -> None:
        """unify() does not apply composer-split rule to classical releases.

        A classical release (CWP_WORK_TOP non-empty AND CWP_WORKTYPE_GENRES_TOP contains
        "Classical") with varying CEA_COMPOSER_LASTNAMES is NOT treated as a composer-split
        compilation.  The performer-split unification still runs, but the composer component
        is not patched.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Classical release: CWP_WORK_TOP set, CWP_WORKTYPE_GENRES_TOP contains "Classical"
        tags_a = TrackTags(
            cea_composer_lastnames="Mozart",
            albumartistsort="Goodman, Benny",  # would be used if composer-split applied
            cwp_work_top="Symphony No. 40",  # classical: has MB work link
            cwp_worktype_genres_top="Classical",  # classical genre
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Mvt 1",
            artist="Karajan",
            musicbrainz_albumid="classical-rel-1",
        )
        tags_b = TrackTags(
            cea_composer_lastnames="Haydn",  # different composer — but classical → not composer-split
            albumartistsort="Goodman, Benny",
            cwp_work_top="Symphony No. 40",
            cwp_worktype_genres_top="Classical",
            cwp_movt_num="2",
            movementtotal="2",
            cwp_part_levels="1",
            title="Mvt 2",
            artist="Karajan",
            musicbrainz_albumid="classical-rel-1",
        )

        # Place files under two different top_dirs (triggers fragmentation detection)
        _make_library_flac(dest_root, "Mozart - Karajan/Symphony No. 40 [rec 2020]/01 - Mvt 1.flac", tags_a)
        _make_library_flac(dest_root, "Haydn - Karajan/Symphony No. 40 [rec 2020]/02 - Mvt 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # The composer-split rule must NOT have fired: no "Goodman, Benny" top_dir should appear.
        # With C-CLASS, Classical releases use parts[0]="Classical", parts[1]=<composer>-<performers>.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Classical", f"Expected class 'Classical', got {rel.parts[0]!r}"
            assert "Goodman" not in rel.parts[1], (
                f"Composer-split rule must not fire for classical releases; got top_dir={rel.parts[1]!r}"
            )

    def test_composer_split_idempotent_second_run_noop(self, fs: FakeFilesystem) -> None:
        """A second unify() run after composer-split unification is a complete no-op.

        After the first run moves all fragments to the canonical path, all files share the same
        top_dir, so detect_fragmented_releases returns empty and unify() returns immediately.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = self._make_composer_split_tags("Goodman")
        tags_b = self._make_composer_split_tags("Berlin")

        _make_library_flac(dest_root, "Goodman - Benny Goodman/The Story [rel 1956]/01 - Track 1.flac", tags_a)
        _make_library_flac(dest_root, "Berlin - Benny Goodman/The Story [rel 1956]/02 - Track 2.flac", tags_b)

        # First run: unifies the composer-split release
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal_after_first = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_first = [e for e in journal_after_first.entries if e.action == "unified"]
        first_count = len(unified_after_first)
        assert first_count >= 1

        # Second run: no-op (all files already at canonical paths)
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal_after_second = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_second = [e for e in journal_after_second.entries if e.action == "unified"]
        # No new "unified" entries from the second run
        assert len(unified_after_second) == first_count

    # ------------------------------------------------------------------
    # W2c: Classical arranger/finisher work-level path credit
    # ------------------------------------------------------------------

    @staticmethod
    def _make_classical_arranger_tags(
        composer_lastnames: str,
        work_id: str = "work-k626",
        movt_num: str = "1",
        title: str = "Mvt 1",
    ) -> TrackTags:
        """Build TrackTags for a classical movement with the given composer credit.

        Sets CWP_WORKID_TOP so :func:`_unify_classical_composer_groups` groups movements by work.
        Sets CWP_WORK_TOP and CWP_WORKTYPE_GENRES_TOP="Classical" so the W2b scope gate does not
        fire (classical releases route through W2c, not W2b).

        :param composer_lastnames: The CEA_COMPOSER_LASTNAMES value to embed.
        :param work_id: The CWP_WORKID_TOP value (shared across movements of the same work).
        :param movt_num: The CWP_MOVT_NUM value (1-based movement index).
        :param title: The track title.
        :returns: A :class:`TrackTags` instance.
        """
        tags = TrackTags(
            cea_composer_lastnames=composer_lastnames,
            cwp_composer_lastnames=composer_lastnames,
            cwp_workid_top=work_id,
            cwp_work_top="Requiem K. 626",
            cwp_worktype_genres_top="Classical",
            cwp_movt_num=movt_num,
            movementtotal="2",
            cwp_part_levels="1",
            title=title,
            artist="Karajan",
            recording_date="1962",
            musicbrainz_albumid="classical-arranger-rel-1",
        )
        return tags

    def test_w2c_kat_arranger_only_movement_same_top_dir(self, fs: FakeFilesystem) -> None:
        """KAT (W2c): arranger-only movement produces the same top_dir as composer-credited movement.

        Constructs a classical release where two movements of the same CWP_WORKID_TOP group have
        different CEA_COMPOSER_LASTNAMES: movement 1 has "Mozart" (the majority/primary composer)
        and movement 2 has "Mozart; Süßmayr" (the arranger-credited minority value, as produced
        when an arranger is credited as "composer" with the "additional" attribute on only some
        movements).

        After unify(), the W2c pass propagates the plurality value ("Mozart") to movement 2,
        so both movements land in the same top_dir.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Movement 1: primary composer "Mozart" (majority value — 1 occurrence)
        tags_mvt1 = self._make_classical_arranger_tags("Mozart", movt_num="1", title="Introitus")

        # Movement 2: arranger-credited "Mozart; Süßmayr" (minority value — 1 occurrence)
        # In a real library this would be the only movement with the arranger in the path.
        # We give Mozart 2 occurrences by adding a third movement so Mozart is the plurality.
        tags_mvt3 = self._make_classical_arranger_tags("Mozart", movt_num="3", title="Lacrimosa")
        tags_mvt2 = self._make_classical_arranger_tags("Mozart; Süßmayr", movt_num="2", title="Kyrie")

        # Place files under different top_dirs (fragmentation: two distinct top_dirs for same albumid)
        _make_library_flac(dest_root, "Mozart - Karajan/Requiem K. 626 [rec 1962]/01 - Introitus.flac", tags_mvt1)
        path_mvt2 = _make_library_flac(
            dest_root, "Mozart; Süßmayr - Karajan/Requiem K. 626 [rec 1962]/02 - Kyrie.flac", tags_mvt2
        )
        _make_library_flac(dest_root, "Mozart - Karajan/Requiem K. 626 [rec 1962]/03 - Lacrimosa.flac", tags_mvt3)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # After W2c unification, movement 2 should have moved to the "Mozart" top_dir
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # The moved file (mvt2) should now be under the "Mozart" top_dir.
        # With C-CLASS, Classical releases use <composer> - <performers> as top_dir (parts[1]).
        moved_dests = {e.destination for e in unified}
        for dest_str in moved_dests:
            dest_path = Path(dest_str)
            rel = dest_path.relative_to(dest_root)
            assert rel.parts[0] == "Classical", f"Expected class 'Classical', got {rel.parts[0]!r}"
            assert rel.parts[1].startswith("Mozart"), (
                f"W2c KAT: arranger-only movement in top_dir={rel.parts[1]!r}, expected 'Mozart'"
            )

        # The "Mozart; Süßmayr" top_dir should no longer contain the movement 2 file
        assert not path_mvt2.exists()

        # Movements 1 and 3 were at legacy two-level paths; unify() moved them to the new
        # class-prefixed paths (Classical/Mozart - Karajan/...).  The original paths no longer exist.
        # Verify the new paths exist.
        new_mvt1 = dest_root / "Classical" / "Mozart - Karajan" / "Requiem K. 626 [rec 1962]" / "01 - Introitus.flac"
        new_mvt3 = dest_root / "Classical" / "Mozart - Karajan" / "Requiem K. 626 [rec 1962]" / "03 - Lacrimosa.flac"
        assert new_mvt1.exists(), f"Movement 1 not found at new canonical path {new_mvt1}"
        assert new_mvt3.exists(), f"Movement 3 not found at new canonical path {new_mvt3}"

    def test_w2c_classical_uniform_composer_is_noop(self, fs: FakeFilesystem) -> None:
        """W2c: classical release where all movements agree on CEA_COMPOSER_LASTNAMES is a no-op.

        When all movements of a work group have the same non-empty CEA_COMPOSER_LASTNAMES,
        ``_unify_classical_composer_groups`` finds ``len(composer_counts) < 2`` and skips the
        group.  No files are moved.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Both movements have the same CEA_COMPOSER_LASTNAMES — no variation to unify
        tags_mvt1 = self._make_classical_arranger_tags("Mozart", movt_num="1", title="Introitus")
        tags_mvt2 = self._make_classical_arranger_tags("Mozart", movt_num="2", title="Kyrie")

        # Place both files under the same top_dir (not fragmented — only one top_dir)
        canonical_path_1 = self._canonical_path(dest_root, tags_mvt1)
        canonical_path_2 = self._canonical_path(dest_root, tags_mvt2)
        _make_library_flac(dest_root, str(canonical_path_1.relative_to(dest_root)), tags_mvt1)
        _make_library_flac(dest_root, str(canonical_path_2.relative_to(dest_root)), tags_mvt2)

        # Not fragmented (same top_dir) → unify() returns immediately with nothing to do
        music_annotator.unify(dest_root=dest_root, yes=True)

        # No "unified" entries — nothing was moved
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0

    def test_w2c_unify_classical_composer_groups_skips_no_work_id(self) -> None:
        """_unify_classical_composer_groups skips tracks with no CWP_WORKID_TOP or MUSICBRAINZ_WORKID.

        Exercises the ``if not work_id: continue`` branch: a track with both fields empty is
        grouped under ``""`` and skipped, so its CEA_COMPOSER_LASTNAMES is never patched.

        :returns: None.
        """
        # Build a group_tags list with one track that has no work ID
        tags_no_work = TrackTags(
            cea_composer_lastnames="Mozart; Süßmayr",
            cwp_workid_top="",
            musicbrainz_workid="",
        )
        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = [
            (Path("/lib/Mozart; Süßmayr - Karajan/Requiem/01 - Introitus.flac"), tags_no_work, {}),
        ]

        _unify_classical_composer_groups(group_tags)

        # The track with no work ID must not be patched
        assert tags_no_work.cea_composer_lastnames == "Mozart; Süßmayr"

    def test_w2c_unify_classical_composer_groups_skips_uniform_group(self) -> None:
        """_unify_classical_composer_groups skips a work group where all movements agree.

        Exercises the ``if len(composer_counts) < 2: continue`` branch: when all movements of a
        work group carry the same non-empty ``cea_composer_lastnames``, the function finds only one
        distinct value and skips the group without patching anything.

        :returns: None.
        """
        work_id = "work-k626"

        tags_a = TrackTags(cea_composer_lastnames="Mozart", cwp_workid_top=work_id)
        tags_b = TrackTags(cea_composer_lastnames="Mozart", cwp_workid_top=work_id)

        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = [
            (Path("/lib/Mozart - Karajan/Requiem/01 - Introitus.flac"), tags_a, {}),
            (Path("/lib/Mozart - Karajan/Requiem/02 - Kyrie.flac"), tags_b, {}),
        ]

        _unify_classical_composer_groups(group_tags)

        # Both tracks already agree — no patching needed
        assert tags_a.cea_composer_lastnames == "Mozart"
        assert tags_b.cea_composer_lastnames == "Mozart"

    def test_w2c_unify_classical_composer_groups_empty_composer_not_counted(self) -> None:
        """_unify_classical_composer_groups ignores tracks with empty CEA_COMPOSER_LASTNAMES.

        Exercises the ``if val:`` False branch: a track with an empty ``cea_composer_lastnames``
        contributes nothing to the count, so the plurality is determined by the non-empty values
        only.  The empty-composer track is still patched to the canonical value.

        :returns: None.
        """
        work_id = "work-k626"

        tags_a = TrackTags(cea_composer_lastnames="Mozart", cwp_workid_top=work_id)
        tags_b = TrackTags(cea_composer_lastnames="Mozart; Süßmayr", cwp_workid_top=work_id)
        # tags_c has empty cea_composer_lastnames — should not be counted but should be patched
        tags_c = TrackTags(cea_composer_lastnames="", cwp_workid_top=work_id)

        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = [
            (Path("/lib/Mozart - Karajan/Requiem/01 - Introitus.flac"), tags_a, {}),
            (Path("/lib/Mozart; Süßmayr - Karajan/Requiem/02 - Kyrie.flac"), tags_b, {}),
            (Path("/lib/Mozart - Karajan/Requiem/03 - Lacrimosa.flac"), tags_c, {}),
        ]

        _unify_classical_composer_groups(group_tags)

        # "Mozart" and "Mozart; Süßmayr" are both counted (1 each); tie broken by first appearance
        # → canonical is "Mozart" (appears first in dict insertion order)
        assert tags_a.cea_composer_lastnames == "Mozart"
        assert tags_b.cea_composer_lastnames == "Mozart"
        # tags_c had empty composer — empty values are not counted but the track is still patched
        # to the canonical value (the condition ``tags.cea_composer_lastnames != canonical`` fires
        # because ``"" != "Mozart"``).
        assert tags_c.cea_composer_lastnames == "Mozart"

    def test_w2c_unify_classical_composer_groups_patches_minority_value(self) -> None:
        """_unify_classical_composer_groups patches the minority CEA_COMPOSER_LASTNAMES to the plurality.

        Directly exercises the function with a group where "Mozart" appears twice and
        "Mozart; Süßmayr" appears once.  The minority value should be patched to "Mozart".

        :returns: None.
        """
        work_id = "work-k626"

        tags_a = TrackTags(cea_composer_lastnames="Mozart", cwp_workid_top=work_id)
        tags_b = TrackTags(cea_composer_lastnames="Mozart", cwp_workid_top=work_id)
        tags_c = TrackTags(cea_composer_lastnames="Mozart; Süßmayr", cwp_workid_top=work_id)

        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = [
            (Path("/lib/Mozart - Karajan/Requiem/01 - Introitus.flac"), tags_a, {}),
            (Path("/lib/Mozart - Karajan/Requiem/02 - Kyrie.flac"), tags_b, {}),
            (Path("/lib/Mozart; Süßmayr - Karajan/Requiem/03 - Lacrimosa.flac"), tags_c, {}),
        ]

        _unify_classical_composer_groups(group_tags)

        # Plurality is "Mozart" (2 occurrences); minority "Mozart; Süßmayr" must be patched
        assert tags_a.cea_composer_lastnames == "Mozart"
        assert tags_b.cea_composer_lastnames == "Mozart"
        assert tags_c.cea_composer_lastnames == "Mozart"

    def test_w2c_unify_classical_composer_groups_already_canonical_not_patched(self) -> None:
        """_unify_classical_composer_groups does not re-patch tracks already at the canonical value.

        Exercises the ``if tags.cea_composer_lastnames != canonical`` branch: tracks that already
        carry the plurality value are not mutated.

        :returns: None.
        """
        work_id = "work-k626"

        tags_a = TrackTags(cea_composer_lastnames="Mozart", cwp_workid_top=work_id)
        tags_b = TrackTags(cea_composer_lastnames="Mozart; Süßmayr", cwp_workid_top=work_id)

        original_a_id = id(tags_a)

        group_tags: list[tuple[Path, TrackTags, dict[str, str]]] = [
            (Path("/lib/Mozart - Karajan/Requiem/01 - Introitus.flac"), tags_a, {}),
            (Path("/lib/Mozart; Süßmayr - Karajan/Requiem/02 - Kyrie.flac"), tags_b, {}),
        ]

        _unify_classical_composer_groups(group_tags)

        # tags_a already had the canonical value; its identity (object id) is unchanged
        assert id(tags_a) == original_a_id
        assert tags_a.cea_composer_lastnames == "Mozart"
        # tags_b was patched to the canonical value
        assert tags_b.cea_composer_lastnames == "Mozart"


# ---------------------------------------------------------------------------
# _resolve_current_lib
# ---------------------------------------------------------------------------


class TestResolveCurrentLib:
    """Tests for :func:`_resolve_current_lib`.

    Verifies that the lineage walk correctly resolves the current on-disk path for each logical
    library file from the journal, handling tagged, repathed, regrouped, and enriched entries.
    """

    def test_tagged_entry_seeds_map(self) -> None:
        """A 'tagged' entry seeds the map with destination → release_id.

        :returns: None.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/Composer/Work/01.flac",
                    action="tagged",
                )
            ]
        )
        result = _resolve_current_lib(journal)
        assert result == {Path("/lib/Composer/Work/01.flac"): "r1"}

    def test_repathed_entry_updates_path(self) -> None:
        """A 'repathed' entry removes the old path and registers the new path with the same release_id.

        :returns: None.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/Old/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="",
                    source="/lib/Old/01.flac",
                    destination="/lib/New/01.flac",
                    action="repathed",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert Path("/lib/Old/01.flac") not in result
        assert result == {Path("/lib/New/01.flac"): "r1"}

    def test_regrouped_entry_updates_path(self) -> None:
        """A 'regrouped' entry removes the old path and registers the new path with the same release_id.

        :returns: None.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/Old/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="r1",
                    source="/lib/Old/01.flac",
                    destination="/lib/New/01.flac",
                    action="regrouped",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert Path("/lib/Old/01.flac") not in result
        assert result == {Path("/lib/New/01.flac"): "r1"}

    def test_enriched_entry_reregisters_release_id(self) -> None:
        """An 'enriched' entry re-registers the path with the current release_id.

        :returns: None.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/Composer/Work/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="r1",
                    source="/lib/Composer/Work/01.flac",
                    destination="/lib/Composer/Work/01.flac",
                    action="enriched",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert result == {Path("/lib/Composer/Work/01.flac"): "r1"}

    def test_multi_hop_chain_resolves(self) -> None:
        """A file that is repathed and then regrouped resolves to the final destination.

        :returns: None.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/A/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="",
                    source="/lib/A/01.flac",
                    destination="/lib/B/01.flac",
                    action="repathed",
                ),
                TransactionEntry(
                    timestamp="2024-01-03T00:00:00+00:00",
                    release_id="r1",
                    source="/lib/B/01.flac",
                    destination="/lib/C/01.flac",
                    action="regrouped",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert Path("/lib/A/01.flac") not in result
        assert Path("/lib/B/01.flac") not in result
        assert result == {Path("/lib/C/01.flac"): "r1"}

    def test_non_move_actions_ignored(self) -> None:
        """Actions other than tagged/repathed/regrouped/enriched are ignored.

        :returns: None.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/Composer/Work/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="r1",
                    source="/src/cover.jpg",
                    destination="/lib/Composer/Work/cover.jpg",
                    action="downloaded",
                ),
                TransactionEntry(
                    timestamp="2024-01-03T00:00:00+00:00",
                    release_id="r1",
                    source="/src/01.flac",
                    destination="/lib/Composer/Work/01.flac",
                    action="skipped",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        # Only the tagged entry should be in the result.
        assert result == {Path("/lib/Composer/Work/01.flac"): "r1"}

    def test_empty_journal_returns_empty(self) -> None:
        """An empty journal returns an empty dict.

        :returns: None.
        """
        journal = TransactionLog(entries=[])
        result = _resolve_current_lib(journal)
        assert result == {}


# ---------------------------------------------------------------------------
# _move_verify_journal
# ---------------------------------------------------------------------------


class TestMoveVerifyJournal:
    """Tests for :func:`_move_verify_journal`.

    Covers the C-PROV provenance-chain invariant (no journal entry on verify failure), the
    EXDEV cross-filesystem fallback, and the basic success path.
    """

    # Tags that produce a valid FLAC file for _verify_copy.
    _TAGS = TrackTags(
        cwp_composer_lastnames="Beethoven",
        cwp_work_top="Symphony No. 5",
        recording_date="2020",
        cwp_movt_num="1",
        movementtotal="1",
        cwp_part_levels="1",
        title="Allegro con brio",
        artist="Karajan",
    )

    def _make_flac(self, path: Path) -> None:
        """Write a minimal FLAC with embedded tags to ``path``.

        :param path: Destination path (parent must exist).
        """
        path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(path, self._TAGS)

    def test_move_verify_journal_no_entry_on_verify_failure(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """C-PROV regression: no journal entry is appended when _verify_copy raises RuntimeError.

        Forces _verify_copy to raise RuntimeError after the file has been moved and the SHA-256
        check has passed.  Asserts that the journal file contains no entries with the move action.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src = src_dir / "01.flac"
        dest = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro con brio.flac"
        self._make_flac(src)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        # Force _verify_copy to raise RuntimeError after the move succeeds.
        mocker.patch("music_annotator._pipeline_maint._verify_copy", side_effect=RuntimeError("verify failed"))

        now = datetime.datetime.now(datetime.UTC)
        with pytest.raises(RuntimeError, match="verify failed"):
            _move_verify_journal(
                [(src, dest)],
                journal_path=journal_path,
                action="repathed",
                dest_root=dest_root,
                now=now,
                release_id="",
            )

        # C-PROV: no journal entry must have been appended.
        journal = read_journal(journal_path)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert repathed == [], "C-PROV violated: journal entry written despite _verify_copy failure"

    def test_move_verify_journal_success_appends_entry(self, fs: FakeFilesystem) -> None:
        """A successful move appends exactly one journal entry with the correct fields.

        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src = src_dir / "01.flac"
        dest = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro con brio.flac"
        self._make_flac(src)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        now = datetime.datetime.now(datetime.UTC)
        moved = _move_verify_journal(
            [(src, dest)],
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        assert moved == 1
        assert dest.exists()
        assert not src.exists()

        journal = read_journal(journal_path)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].source == str(src)
        assert repathed[0].destination == str(dest)
        assert repathed[0].release_id == ""

    def test_move_verify_journal_exdev_fallback(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """EXDEV cross-filesystem fallback: shutil.copy2 + os.unlink is used when os.replace raises EXDEV.

        Patches os.replace to raise OSError(EXDEV) and verifies that the file is moved via
        shutil.copy2 + os.unlink, and that a journal entry is appended.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src = src_dir / "01.flac"
        dest = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro con brio.flac"
        self._make_flac(src)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        # Patch os.replace to raise EXDEV so the cross-fs fallback is exercised.
        exdev_error = OSError(errno.EXDEV, "Cross-device link")
        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=exdev_error)

        now = datetime.datetime.now(datetime.UTC)
        moved = _move_verify_journal(
            [(src, dest)],
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        assert moved == 1
        assert dest.exists()
        # Source should be unlinked after cross-fs copy.
        assert not src.exists()

        journal = read_journal(journal_path)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1

    def test_move_verify_journal_non_exdev_oserror_propagates(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A non-EXDEV OSError from os.replace propagates without journalling.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src = src_dir / "01.flac"
        dest = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro con brio.flac"
        self._make_flac(src)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        # Patch os.replace to raise a non-EXDEV OSError (e.g. EPERM).
        perm_error = OSError(errno.EPERM, "Operation not permitted")
        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=perm_error)

        now = datetime.datetime.now(datetime.UTC)
        with pytest.raises(OSError, match="Operation not permitted"):
            _move_verify_journal(
                [(src, dest)],
                journal_path=journal_path,
                action="repathed",
                dest_root=dest_root,
                now=now,
                release_id="",
            )

        # No journal entry should have been written.
        journal = read_journal(journal_path)
        assert journal.entries == []

    def test_move_verify_journal_empty_plan_returns_zero(self, fs: FakeFilesystem) -> None:
        """An empty plan_pairs list returns 0 without touching the journal.

        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        now = datetime.datetime.now(datetime.UTC)
        moved = _move_verify_journal(
            [],
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
        )

        assert moved == 0
        journal = read_journal(journal_path)
        assert journal.entries == []

    def test_move_verify_journal_release_id_recorded(self, fs: FakeFilesystem) -> None:
        """The release_id parameter is recorded in the journal entry.

        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src = src_dir / "01.flac"
        dest = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro con brio.flac"
        self._make_flac(src)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        now = datetime.datetime.now(datetime.UTC)
        _move_verify_journal(
            [(src, dest)],
            journal_path=journal_path,
            action="regrouped",
            dest_root=dest_root,
            now=now,
            release_id="release-mbid-abc123",
        )

        journal = read_journal(journal_path)
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        assert regrouped[0].release_id == "release-mbid-abc123"

    def test_move_verify_journal_empty_dir_cleaned_up(self, fs: FakeFilesystem) -> None:
        """After a successful move, now-empty source directories are removed.

        :param fs: pyfakefs fixture.
        """

        dest_root = Path("/lib")
        src_dir = Path("/lib/OldComposer/OldWork")
        fs.create_dir(str(src_dir))

        src = src_dir / "01.flac"
        dest = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro con brio.flac"
        self._make_flac(src)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        now = datetime.datetime.now(datetime.UTC)
        _move_verify_journal(
            [(src, dest)],
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
        )

        # The now-empty source directories should have been removed.
        assert not src_dir.exists()
        assert not (dest_root / "OldComposer").exists()


# ---------------------------------------------------------------------------
# repath() confirmation prompt
# ---------------------------------------------------------------------------


def _write_repath_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a journal JSON file to ``dest_root / music_annotator_journal.json``.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / "music_annotator_journal.json"
    journal_path.write_text(json.dumps(entries), encoding="utf-8")


def _make_repath_flac(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
    """Create a FLAC file at ``dest_root / rel_path`` with the given tags applied.

    Creates parent directories as needed, writes the minimal FLAC byte sequence, applies tags
    via :func:`apply_tags_flac`, and returns the full path.

    :param dest_root: Library root directory.
    :param rel_path: Relative path within the library.
    :param tags: Tags to embed in the FLAC file.
    :returns: The full absolute path of the created FLAC file.
    """
    full_path = dest_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(_MINIMAL_FLAC)
    apply_tags_flac(full_path, tags)
    return full_path


class TestRepathConfirmation:
    """Tests for the ``repath()`` confirmation prompt (``yes`` parameter).

    Covers the three new branches introduced by Q2:

    * ``yes=True`` — prompt is skipped entirely (``input()`` never called).
    * ``yes=False``, user answers ``"y"`` — move proceeds.
    * ``yes=False``, user answers ``"n"`` — move is aborted; no files moved, no journal entry.

    Uses real FLAC bytes and :func:`apply_tags_flac` so that :func:`_read_tags_flac` executes
    the real mutagen round-trip.  The filesystem is fake via pyfakefs.
    """

    # Tags that produce a deterministic canonical path different from the legacy path.
    # build_dest_path produces:
    #   <dest_root>/Beethoven - Karajan/Symphony No. 5 [rec 2020]/01 - Allegro con brio.flac
    _TAGS = TrackTags(
        cwp_composer_lastnames="Beethoven",
        cwp_work_top="Symphony No. 5",
        recording_date="2020",
        cwp_movt_num="1",
        movementtotal="1",
        cwp_part_levels="1",
        title="Allegro con brio",
        artist="Karajan",
    )
    _OLD_REL = "Beethoven - Karajan/OldSymphony [rec 2020]/01 - Allegro con brio.flac"

    @staticmethod
    def _canonical_path(dest_root: Path) -> Path:
        """Compute the canonical destination path for the shared test tags.

        :param dest_root: Library root.
        :returns: Full absolute canonical path after repathing.
        """
        base = build_dest_path(dest_root, MBRelease(), MBTrack(), TestRepathConfirmation._TAGS, global_track_idx=0)
        return base.with_suffix(".flac")

    def _build_repath_scenario(self, dest_root: Path) -> tuple[Path, Path]:
        """Create a single-file repath scenario under ``dest_root``.

        Places a FLAC file at the legacy path with embedded tags that recompute to a different
        canonical path, and writes a journal entry so ``repath()`` picks it up.

        :param dest_root: Library root (must already exist).
        :returns: Tuple of (old_path, new_path).
        """
        old_path = _make_repath_flac(dest_root, self._OLD_REL, self._TAGS)
        new_path = self._canonical_path(dest_root)
        assert old_path != new_path, "test setup error: old and canonical paths must differ"
        _write_repath_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                }
            ],
        )
        return old_path, new_path

    def test_repath_yes_skips_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath(yes=True) does not call input() at all.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._build_repath_scenario(dest_root)

        mock_input = mocker.patch("music_annotator._pipeline_maint.input")

        repath(dest_root=dest_root, yes=True)

        mock_input.assert_not_called()

    def test_repath_prompt_accepted_moves_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath(yes=False) prompts; answering 'y' proceeds with the move and journals it.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        old_path, new_path = self._build_repath_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.input", return_value="y")

        repath(dest_root=dest_root, yes=False)

        assert new_path.exists()
        assert not old_path.exists()

        journal = read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].source == str(old_path)
        assert repathed[0].destination == str(new_path)

    def test_repath_prompt_declined_no_move(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """repath(yes=False) prompts; answering 'n' aborts with no move and no journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        old_path, new_path = self._build_repath_scenario(dest_root)

        mocker.patch("music_annotator._pipeline_maint.input", return_value="n")

        repath(dest_root=dest_root, yes=False)

        assert old_path.exists()
        assert not new_path.exists()

        journal = read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0


# ---------------------------------------------------------------------------
# C-CLASS KATs: repath reconstructs class from tags; _work_top_dir depth invariant
# ---------------------------------------------------------------------------


class TestCClassKATs:
    """C-CLASS KATs for the tag-derivable class routing and the work_top_dir depth invariant.

    These tests pin the substrate correctness core: the class must be derivable from embedded tags
    alone (so repath/regroup/unify reconstruct the correct class without a live MBRelease), and
    the _work_top_dir helper must handle both legacy two-level and class-prefixed three-level paths.
    """

    def test_repath_reconstructs_class_from_tags(self, fs: FakeFilesystem) -> None:
        """Empty-stub build_dest_path derives the class from RELEASETYPE/RELEASETYPE_SECONDARY tags.

        Verifies the substrate correctness core (R-2): repath/regroup/unify call build_dest_path
        with empty MBRelease()/MBTrack() stubs.  The class must be derivable from embedded tags
        alone, not from release.release_group.

        Creates a FLAC file with RELEASETYPE="Album" embedded (Popular class) and verifies that
        build_dest_path with an empty stub produces a path under "Popular/".

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Tags with releasetype="Album" → Popular class (no cwp_work_top, no classical predicate).
        tags = TrackTags(
            releasetype="Album",
            albumartist="Test Artist",
            album="Test Album",
            title="Track 1",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="",
            cwp_worktype_genres_top="",
        )

        # Call build_dest_path with empty stubs (as repath/regroup/unify do).
        stub_release = MBRelease()
        stub_track = MBTrack()
        result = build_dest_path(dest_root, stub_release, stub_track, tags)

        rel = result.relative_to(dest_root)
        # The class must be derived from the RELEASETYPE tag, not from release.release_group.
        assert rel.parts[0] == "Popular", f"Expected class 'Popular' from RELEASETYPE='Album' tag, got {rel.parts[0]!r}"

    def test_repath_reconstructs_classical_class_from_tags(self, fs: FakeFilesystem) -> None:
        """Empty-stub build_dest_path derives the Classical class from CWP_WORK_TOP and CWP_WORKTYPE_GENRES_TOP tags.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            cwp_work_top="Symphony No. 9",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Beethoven",
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
        )

        stub_release = MBRelease()
        stub_track = MBTrack()
        result = build_dest_path(dest_root, stub_release, stub_track, tags)

        rel = result.relative_to(dest_root)
        assert rel.parts[0] == "Classical", (
            f"Expected class 'Classical' from CWP_WORK_TOP + CWP_WORKTYPE_GENRES_TOP tags, got {rel.parts[0]!r}"
        )

    def test_work_top_dir_depth_invariant(self, fs: FakeFilesystem) -> None:
        """_work_top_dir returns the correct work dir for BOTH legacy two-level AND class-prefixed three-level paths.

        Pins the dual-shape behaviour required during R4a: the library is a mix of legacy
        two-level (old annotated releases) and new three-level (class-prefixed) paths.  The
        _work_top_dir helper must handle both shapes by testing whether parts[0] is a known
        class name from the closed C-CLASS vocabulary.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Legacy two-level path: dest_root / <top_dir> / <work_dir> / leaf
        legacy_file = dest_root / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]" / "01 - Allegro.flac"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.touch()

        # Class-prefixed three-level path: dest_root / <class> / <top_dir> / <work_dir> / leaf
        class_file = dest_root / "Classical" / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]" / "01 - Allegro.flac"
        class_file.parent.mkdir(parents=True, exist_ok=True)
        class_file.touch()

        # Legacy path: work_top_dir = dest_root / parts[0] / parts[1]
        legacy_work_top = _work_top_dir(legacy_file, dest_root)
        assert legacy_work_top == dest_root / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]", (
            f"Legacy path: expected work_top_dir at depth 2, got {legacy_work_top.relative_to(dest_root)}"
        )

        # Class-prefixed path: work_top_dir = dest_root / parts[1] / parts[2]
        class_work_top = _work_top_dir(class_file, dest_root)
        assert class_work_top == dest_root / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]", (
            f"Class-prefixed path: expected work_top_dir at depth 2 (below class), got {class_work_top.relative_to(dest_root)}"
        )

    def test_repath_reconstructs_classical_top_dir_from_tags(self, fs: FakeFilesystem) -> None:
        """Empty-stub build_dest_path derives the within-classical top_dir from tags (C-INIT KAT).

        Verifies the C-INIT substrate correctness core: repath/regroup/unify call build_dest_path
        with empty MBRelease()/MBTrack() stubs.  The within-classical top_dir must be derivable
        from embedded tags alone, not from release.release_group or any live release data.

        Tests the recital branch: a FLAC with CWP_COMPOSER_LASTNAMES="" and ALBUMARTIST set
        must produce a performer-first top_dir under Classical/ when called with empty stubs.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Recital tags: cwp_work_top set (→ Classical class), but no composer linked.
        # The within-classical top_dir must use albumartist (performer-first).
        tags = TrackTags(
            cwp_work_top="Sonata in B minor",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            albumartist="Mitsuko Uchida",
            albumartistsort="Uchida, Mitsuko",
            album="Schubert: Piano Sonatas",
            title="Sonata in B minor",
            movementnumber="1",
            movementtotal="1",
        )

        # Call build_dest_path with empty stubs (as repath/regroup/unify do).
        stub_release = MBRelease()
        stub_track = MBTrack()
        result = build_dest_path(dest_root, stub_release, stub_track, tags)

        rel = result.relative_to(dest_root)
        assert rel.parts[0] == "Classical", (
            f"Expected class 'Classical' from CWP_WORK_TOP + CWP_WORKTYPE_GENRES_TOP tags, got {rel.parts[0]!r}"
        )
        # C-INIT recital branch: top_dir must be performer-first (albumartist), not composer-first.
        assert "Mitsuko Uchida" in rel.parts[1], (
            f"Expected albumartist 'Mitsuko Uchida' in recital top_dir (C-INIT), got {rel.parts[1]!r}"
        )


# ---------------------------------------------------------------------------
# _hydrate_performer_lists — KAT for ingest/repath canonical-form parity
# ---------------------------------------------------------------------------


class TestHydratePerformerLists:
    """KAT: _hydrate_performer_lists reconstructs ArtistEntry lists so repath renders canonical forms.

    The ``cea_album_conductors_list``, ``cea_album_ensembles_list``, ``cea_conductors_list``, and
    ``cea_ensembles_list`` fields are excluded from :meth:`~music_annotator.models.TrackTags.to_file_dict`
    and therefore absent from the embedded tag dict read back from an audio file.  Without
    :func:`~music_annotator._pipeline_maint._hydrate_performer_lists`, the repath path falls back
    to the raw ``CEA_ENSEMBLE_NAMES`` / ``ARTIST`` string and cannot render canonical name-forms.

    Two behavioural witnesses:

    1. **Alias-present (ingest/repath parity)**: an ensemble whose hydrated ``MBArtist`` has a
       primary native-Latin alias — after hydration, :func:`~music_annotator._tags.build_dest_path`
       renders the alias form in the path, matching the ingest render byte-for-byte.
    2. **Alias-absent (no-regression)**: an ensemble with no primary alias — the path carries the
       as-credited display name unchanged, proving the resolver does not corrupt the no-alias case.

    Both witnesses also verify that the ``ARTIST`` / ``ALBUMARTIST`` preserved tag surfaces are
    unchanged (the compact-path-only scope of canonical-form rendering).
    """

    def _make_file_dict(
        self,
        *,
        ensemble_name: str,
        ensemble_sort: str,
        album_artist_mbid: str,
    ) -> dict[str, str]:
        """Build a minimal embedded tag dict simulating a read-back from an audio file.

        Populates the string tags that :func:`~music_annotator._pipeline_maint._hydrate_performer_lists`
        reads to reconstruct performer :class:`~music_annotator.models.ArtistEntry` lists.

        :param ensemble_name: As-credited ensemble display name (``CEA_ALBUM_ENSEMBLES``).
        :param ensemble_sort: Ensemble sort name (``CEA_ALBUM_ENSEMBLES_SORT``).
        :param album_artist_mbid: Value for ``MUSICBRAINZ_ALBUMARTISTID`` (the ensemble MBID when
            no conductor is present, since ensemble MBIDs = album artist MBIDs minus conductor MBIDs).
        :returns: An uppercase ``{KEY: value}`` dict as returned by ``_read_tags_flac``.
        """
        return {
            "CEA_ALBUM_ENSEMBLES": ensemble_name,
            "CEA_ALBUM_ENSEMBLES_SORT": ensemble_sort,
            "MUSICBRAINZ_ALBUMARTISTID": album_artist_mbid,
            "MUSICBRAINZ_CONDUCTORID": "",  # no conductor — ensemble MBIDs = album artist MBIDs
        }

    def test_hydrate_sets_album_ensembles_list_with_mbid(self) -> None:
        """_hydrate_performer_lists populates cea_album_ensembles_list with an ArtistEntry carrying the MBID.

        When ``CEA_ALBUM_ENSEMBLES`` has one name and ``MUSICBRAINZ_ALBUMARTISTID`` has one MBID
        (with no conductor MBIDs to subtract), the two are zipped positionally and the resulting
        :class:`~music_annotator.models.ArtistEntry` carries the MBID.

        :raises AssertionError: If the list is not populated or the MBID is absent.
        """
        tags = TrackTags()
        file_dict = self._make_file_dict(
            ensemble_name="Vienna Philharmonic",
            ensemble_sort="Vienna Philharmonic",
            album_artist_mbid="vp-1",
        )
        _hydrate_performer_lists(tags, file_dict)

        assert len(tags.cea_album_ensembles_list) == 1
        entry = tags.cea_album_ensembles_list[0]
        assert entry.name == "Vienna Philharmonic"
        assert entry.mbid == "vp-1"

    def test_hydrate_idempotent_when_list_already_populated(self) -> None:
        """_hydrate_performer_lists is a no-op when cea_album_ensembles_list is already non-empty.

        Ensures the function does not overwrite lists that were set by the ingest pipeline
        (e.g. when called on a TrackTags that was not reconstructed from embedded tags).

        :raises AssertionError: If the pre-existing list is overwritten.
        """
        existing_entry = ArtistEntry(name="Berlin Philharmonic", sort="Berlin Philharmonic", mbid="bp-1")
        tags = TrackTags(cea_album_ensembles_list=[existing_entry])
        file_dict = self._make_file_dict(
            ensemble_name="Vienna Philharmonic",
            ensemble_sort="Vienna Philharmonic",
            album_artist_mbid="vp-1",
        )
        _hydrate_performer_lists(tags, file_dict)

        # The pre-existing list must not be overwritten.
        assert len(tags.cea_album_ensembles_list) == 1
        assert tags.cea_album_ensembles_list[0].mbid == "bp-1"

    def test_repath_renders_canonical_alias_form(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Repath path renders the primary-flagged MB alias, not the as-credited display name.

        KAT (ingest/repath parity, alias-present): the ensemble "Vienna Philharmonic" has a
        primary native-Latin alias "Wiener Philharmoniker".  After :func:`_hydrate_performer_lists`
        reconstructs the ``cea_album_ensembles_list`` with the ensemble MBID, and
        :func:`~music_annotator._tags.build_dest_path` calls
        :func:`~music_annotator._mb_api.fetch_artist_aliases` on that MBID, the resolver selects
        the alias.  The path must contain "Wiener Philharmoniker", not "Vienna Philharmonic" —
        matching the ingest render byte-for-byte.

        Preserved tag surfaces (``ARTIST``, ``ALBUMARTIST``) are asserted unchanged, freezing the
        compact-path-only scope of canonical-form rendering.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        hydrated = MBArtist.model_validate(
            {
                "id": "vp-1",
                "name": "Vienna Philharmonic",
                "alias-list": [
                    {"alias": "Wiener Philharmoniker", "type": "Artist name", "primary": "primary", "locale": "de"},
                ],
            }
        )
        mocker.patch("music_annotator._tags.fetch_artist_aliases", return_value=hydrated)

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            artist="Vienna Philharmonic",
            albumartist="Vienna Philharmonic",
        )
        file_dict = {
            "CEA_ALBUM_ENSEMBLES": "Vienna Philharmonic",
            "CEA_ALBUM_ENSEMBLES_SORT": "Vienna Philharmonic",
            "MUSICBRAINZ_ALBUMARTISTID": "vp-1",
            "MUSICBRAINZ_CONDUCTORID": "",
        }

        _hydrate_performer_lists(tags, file_dict)

        result = build_dest_path(
            dest_root,
            MBRelease(),
            MBTrack(),
            tags,
            global_track_idx=0,
        )
        path_str = str(result)

        # Path performers component must carry the canonical alias form.
        assert "Wiener Philharmoniker" in path_str, f"Expected canonical alias 'Wiener Philharmoniker' in path '{path_str}'"
        # The anglicised display name must not appear in the path.
        assert "Vienna Philharmonic" not in path_str, (
            f"Display name 'Vienna Philharmonic' must not appear in path '{path_str}' (alias should replace it)"
        )
        # Preserved tag surfaces are unchanged — ARTIST and ALBUMARTIST stay as-credited.
        assert tags.artist == "Vienna Philharmonic", "ARTIST tag must remain as-credited"
        assert tags.albumartist == "Vienna Philharmonic", "ALBUMARTIST tag must remain as-credited"

    def test_repath_unchanged_when_no_primary_alias(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Repath path is unchanged when the ensemble has no primary alias.

        KAT (alias-absent, no-regression): the ensemble "Berlin Philharmonic" has no primary alias.
        After :func:`_hydrate_performer_lists` reconstructs the ``cea_album_ensembles_list`` with
        the ensemble MBID, :func:`~music_annotator._tags.build_dest_path` calls
        :func:`~music_annotator._mb_api.fetch_artist_aliases` on that MBID and the resolver falls
        back to ``MBArtist.name``.  The path must carry "Berlin Philharmonic" unchanged.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        hydrated = MBArtist.model_validate({"id": "bp-1", "name": "Berlin Philharmonic"})
        mocker.patch("music_annotator._tags.fetch_artist_aliases", return_value=hydrated)

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            artist="Berlin Philharmonic",
            albumartist="Berlin Philharmonic",
        )
        file_dict = {
            "CEA_ALBUM_ENSEMBLES": "Berlin Philharmonic",
            "CEA_ALBUM_ENSEMBLES_SORT": "Berlin Philharmonic",
            "MUSICBRAINZ_ALBUMARTISTID": "bp-1",
            "MUSICBRAINZ_CONDUCTORID": "",
        }

        _hydrate_performer_lists(tags, file_dict)

        result = build_dest_path(
            dest_root,
            MBRelease(),
            MBTrack(),
            tags,
            global_track_idx=0,
        )
        path_str = str(result)

        # No alias → resolver falls back to MBArtist.name → path unchanged from as-credited form.
        assert "Berlin Philharmonic" in path_str, (
            f"Expected as-credited name 'Berlin Philharmonic' in path '{path_str}' (no-alias fallback)"
        )
        # Preserved tag surfaces are unchanged.
        assert tags.artist == "Berlin Philharmonic", "ARTIST tag must remain as-credited"
        assert tags.albumartist == "Berlin Philharmonic", "ALBUMARTIST tag must remain as-credited"

    def test_hydrate_sets_album_conductors_list_with_mbid(self) -> None:
        """_hydrate_performer_lists populates cea_album_conductors_list with an ArtistEntry carrying the MBID.

        When ``CEA_ALBUM_CONDUCTORS`` has one name and ``MUSICBRAINZ_CONDUCTORID`` has one MBID,
        the two are zipped positionally and the resulting :class:`~music_annotator.models.ArtistEntry`
        carries the MBID.

        :raises AssertionError: If the list is not populated or the MBID is absent.
        """
        tags = TrackTags()
        file_dict = {
            "CEA_ALBUM_CONDUCTORS": "Herbert von Karajan",
            "CEA_ALBUM_CONDUCTORS_SORT": "Karajan, Herbert von",
            "MUSICBRAINZ_CONDUCTORID": "hvk-1",
            "MUSICBRAINZ_ALBUMARTISTID": "hvk-1",
        }
        _hydrate_performer_lists(tags, file_dict)

        assert len(tags.cea_album_conductors_list) == 1
        entry = tags.cea_album_conductors_list[0]
        assert entry.name == "Herbert von Karajan"
        assert entry.mbid == "hvk-1"

    def test_hydrate_sets_per_track_conductors_list(self) -> None:
        """_hydrate_performer_lists populates cea_conductors_list from CEA_CONDUCTORS.

        When ``CEA_CONDUCTORS`` has one name and ``MUSICBRAINZ_CONDUCTORID`` has one MBID,
        the resulting :class:`~music_annotator.models.ArtistEntry` carries the MBID.

        :raises AssertionError: If the list is not populated or the MBID is absent.
        """
        tags = TrackTags()
        file_dict = {
            "CEA_CONDUCTORS": "Herbert von Karajan",
            "MUSICBRAINZ_CONDUCTORID": "hvk-1",
            "MUSICBRAINZ_ALBUMARTISTID": "",
        }
        _hydrate_performer_lists(tags, file_dict)

        assert len(tags.cea_conductors_list) == 1
        assert tags.cea_conductors_list[0].mbid == "hvk-1"

    def test_hydrate_sets_per_track_ensembles_list(self) -> None:
        """_hydrate_performer_lists populates cea_ensembles_list from CEA_ENSEMBLES.

        When ``CEA_ENSEMBLES`` has one name and ``MUSICBRAINZ_ALBUMARTISTID`` has one MBID
        (with no conductor MBIDs to subtract), the resulting
        :class:`~music_annotator.models.ArtistEntry` carries the MBID.

        :raises AssertionError: If the list is not populated or the MBID is absent.
        """
        tags = TrackTags()
        file_dict = {
            "CEA_ENSEMBLES": "Vienna Philharmonic",
            "CEA_ENSEMBLES_SORT": "Vienna Philharmonic",
            "MUSICBRAINZ_CONDUCTORID": "",
            "MUSICBRAINZ_ALBUMARTISTID": "vp-1",
        }
        _hydrate_performer_lists(tags, file_dict)

        assert len(tags.cea_ensembles_list) == 1
        assert tags.cea_ensembles_list[0].mbid == "vp-1"

    def test_hydrate_no_mbid_when_counts_mismatch(self) -> None:
        """_hydrate_performer_lists creates entries without MBIDs when name and MBID counts differ.

        When the count of album ensemble names does not match the count of ensemble MBIDs (e.g.
        two ensembles but only one MBID), entries are created without MBIDs and the canonical-form
        resolver falls back to the as-credited name.

        :raises AssertionError: If entries carry MBIDs despite the count mismatch.
        """
        tags = TrackTags()
        file_dict = {
            "CEA_ALBUM_ENSEMBLES": "Ensemble A; Ensemble B",
            "CEA_ALBUM_ENSEMBLES_SORT": "Ensemble A; Ensemble B",
            "MUSICBRAINZ_ALBUMARTISTID": "mbid-only-one",
            "MUSICBRAINZ_CONDUCTORID": "",
        }
        _hydrate_performer_lists(tags, file_dict)

        assert len(tags.cea_album_ensembles_list) == 2  # noqa: PLR2004
        # Counts mismatch (2 names, 1 MBID) → no MBIDs assigned.
        assert tags.cea_album_ensembles_list[0].mbid == ""
        assert tags.cea_album_ensembles_list[1].mbid == ""


# ---------------------------------------------------------------------------
# repath() / regroup() — work-group modal depth threading
# ---------------------------------------------------------------------------


def _make_classical_maint_tags(
    cwp_part_levels: str,
    cwp_movt_num: str,
    title: str,
    *,
    extra_parts: dict[str, str] | None = None,
) -> TrackTags:
    """Build a minimal classical TrackTags for maintenance-mode modal-depth tests.

    Sets the fields required for build_dest_path to produce a Classical path with the given
    hierarchy depth.  Dynamic per-level extras (cwp_part_N, cwp_ordering_key_N) are passed via
    ``extra_parts``.

    :param cwp_part_levels: String value for CWP_PART_LEVELS (e.g. ``"2"`` or ``"3"``).
    :param cwp_movt_num: String value for CWP_MOVT_NUM (leaf movement number).
    :param title: Track title.
    :param extra_parts: Optional dict of lowercase model_extra keys to set.
    :returns: A :class:`~music_annotator.models.TrackTags` instance.
    """
    tags = TrackTags(
        cwp_work_top="Water Music",
        cwp_worktype_genres_top="Classical",
        cwp_composer_lastnames="Handel",
        recording_date="1970",
        cwp_workid_top="w-water-music",
        cwp_part_levels=cwp_part_levels,
        cwp_movt_num=cwp_movt_num,
        movementtotal="3",
        title=title,
        artist="Karajan",
    )
    if extra_parts and tags.model_extra is not None:
        tags.model_extra.update(extra_parts)
    return tags


class TestRepathWorkGroupModalDepth:
    """Tests for the work-group modal depth threading in repath().

    Verifies that repath() computes the work-group modal depth once per group from embedded tags
    and passes it to build_dest_path, so over-resolved branches clamp to the group ceiling
    (Shape C/D) while uniform-depth groups are unchanged (no-regression).
    """

    def test_repath_clamps_over_resolved_track_to_modal_depth(self, fs: FakeFilesystem) -> None:
        """repath() clamps a Shape-C/D over-resolved track to the work-group modal depth.

        A 3-track group where 2 tracks have CWP_PART_LEVELS=2 and 1 track has CWP_PART_LEVELS=3.
        The modal depth is 2.  The PL=3 track must move to a path with one intermediate directory
        (depth 2), not two (depth 3).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Tracks 1 and 2: PL=2 (one intermediate directory: Act I)
        tags1 = _make_classical_maint_tags(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags2 = _make_classical_maint_tags(
            "2",
            "2",
            "Andante",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        # Track 3: PL=3 (two intermediate directories: Act I / Scene 1) — over-resolved
        tags3 = _make_classical_maint_tags(
            "3",
            "3",
            "Presto",
            extra_parts={
                "cwp_part_1": "Scene 1",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Act I",
                "cwp_ordering_key_2": "1",
            },
        )

        # Place all three tracks at their unclamped (old) paths.
        # PL=2 tracks: Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Allegro.flac
        # PL=3 track:  Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Scene 1/03 - Presto.flac
        old_path1 = _make_library_flac(
            dest_root,
            "Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Allegro.flac",
            tags1,
        )
        old_path2 = _make_library_flac(
            dest_root,
            "Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/02 - Andante.flac",
            tags2,
        )
        # PL=3 track at its unclamped path (two intermediate dirs)
        old_path3 = _make_library_flac(
            dest_root,
            "Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Scene 1/03 - Presto.flac",
            tags3,
        )

        # Compute the expected clamped path for track 3 (modal=2 → PL=3 clamped to PL=2)
        modal = work_group_modal_depth([2, 2, 3])
        assert modal == 2  # noqa: PLR2004
        expected_path3 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags3, group_modal_depth=modal).with_suffix(".flac")

        # Journal: all three at their old paths
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/02.flac",
                    "destination": str(old_path2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/03.flac",
                    "destination": str(old_path3),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # PL=2 tracks: already at their clamped paths — no move expected.
        assert old_path1.exists(), "PL=2 track 1 should remain at its original path (already clamped)"
        assert old_path2.exists(), "PL=2 track 2 should remain at its original path (already clamped)"

        # PL=3 track: must have moved to the clamped path (one intermediate dir, not two).
        assert not old_path3.exists(), "PL=3 track should have moved away from its unclamped path"
        assert expected_path3.exists(), f"PL=3 track should be at clamped path {expected_path3.relative_to(dest_root)}"

        # Depth check: clamped path has 5 parts (Classical/top/work/act/leaf), not 6.
        assert len(expected_path3.relative_to(dest_root).parts) == 5, (  # noqa: PLR2004
            f"Expected 5 path parts after clamp, got {expected_path3.relative_to(dest_root).parts}"
        )

    def test_repath_uniform_depth_group_unchanged(self, fs: FakeFilesystem) -> None:
        """repath() leaves a uniform-depth group unchanged (no-regression for the common case).

        A 2-track group where both tracks have CWP_PART_LEVELS=2.  The modal depth is 2.
        The clamp is a no-op and repath() produces an empty plan (no moves).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags1 = _make_classical_maint_tags(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags2 = _make_classical_maint_tags(
            "2",
            "2",
            "Andante",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )

        # Place tracks at their correct (already-clamped) paths.
        canonical1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags1, group_modal_depth=2).with_suffix(".flac")
        canonical2 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags2, group_modal_depth=2).with_suffix(".flac")

        canonical1.parent.mkdir(parents=True, exist_ok=True)
        canonical1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical1, tags1)

        canonical2.parent.mkdir(parents=True, exist_ok=True)
        canonical2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical2, tags2)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(canonical1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/02.flac",
                    "destination": str(canonical2),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # Both tracks must remain at their original paths (no move).
        assert canonical1.exists(), "Uniform PL=2 track 1 must not be moved"
        assert canonical2.exists(), "Uniform PL=2 track 2 must not be moved"

        # No "repathed" entries should have been added.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert not repathed, f"Expected no repathed entries for uniform group, got {repathed}"


class TestRegroupWorkGroupModalDepth:
    """Tests for the work-group modal depth threading in regroup().

    Verifies that regroup() computes the work-group modal depth once per group from embedded tags
    and passes it to build_dest_path, so over-resolved branches clamp to the group ceiling
    (Shape C/D) while uniform-depth groups are unchanged (no-regression).
    """

    @staticmethod
    def _make_split_tags_pl(
        cwp_part_levels: str,
        cwp_movt_num: str,
        title: str,
        *,
        extra_parts: dict[str, str] | None = None,
    ) -> TrackTags:
        """Build TrackTags for a split-release regroup test with the given hierarchy depth.

        Sets MUSICBRAINZ_ALBUMID so _confirm_fragmentation confirms the candidate via tag match.

        :param cwp_part_levels: String value for CWP_PART_LEVELS.
        :param cwp_movt_num: String value for CWP_MOVT_NUM.
        :param title: Track title.
        :param extra_parts: Optional dict of lowercase model_extra keys to set.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            cwp_work_top="Water Music",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Handel",
            recording_date="1970",
            cwp_workid_top="w-water-music",
            cwp_part_levels=cwp_part_levels,
            cwp_movt_num=cwp_movt_num,
            movementtotal="3",
            title=title,
            artist="Karajan",
            musicbrainz_albumid="split-rel-1",
        )
        if extra_parts and tags.model_extra is not None:
            tags.model_extra.update(extra_parts)
        return tags

    def test_regroup_clamps_over_resolved_track_to_modal_depth(self, fs: FakeFilesystem) -> None:
        """regroup() clamps a Shape-C/D over-resolved track to the work-group modal depth.

        A 3-track group where 2 tracks have CWP_PART_LEVELS=2 and 1 track has CWP_PART_LEVELS=3.
        The modal depth is 2.  The PL=3 track must move to a path with one intermediate directory
        (depth 2), not two (depth 3).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags1 = self._make_split_tags_pl(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags2 = self._make_split_tags_pl(
            "2",
            "2",
            "Andante",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags3 = self._make_split_tags_pl(
            "3",
            "3",
            "Presto",
            extra_parts={
                "cwp_part_1": "Scene 1",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Act I",
                "cwp_ordering_key_2": "1",
            },
        )

        # Compute the expected clamped path for track 3.
        modal = work_group_modal_depth([2, 2, 3])
        assert modal == 2  # noqa: PLR2004
        expected_path3 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags3, group_modal_depth=modal).with_suffix(".flac")

        # Place tracks at their old paths (unclamped for track 3).
        old_path1 = _make_library_flac(
            dest_root,
            "Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Allegro.flac",
            tags1,
        )
        old_path2 = _make_library_flac(
            dest_root,
            "Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/02 - Andante.flac",
            tags2,
        )
        old_path3 = _make_library_flac(
            dest_root,
            "Classical/Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Scene 1/03 - Presto.flac",
            tags3,
        )

        # Build a split-release scenario: two work_dirs for "split-rel-1".
        # The phantom entry is in a DIFFERENT work_dir ("OldWater Music [rec 1970]") so that
        # _confirm_fragmentation detects case-b fragmentation (>1 work_dir for one release_id).
        # The real files are in "Water Music [rec 1970]"; the phantom is in "OldWater Music [rec 1970]".
        phantom = dest_root / "Classical" / "Handel - Karajan" / "OldWater Music [rec 1970]" / "phantom.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(old_path2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/03.flac",
                    "destination": str(old_path3),
                    "action": "tagged",
                },
                # Phantom entry in a different work_dir to trigger the split-release detection.
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/phantom.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, dry_run=False, yes=True)

        # PL=3 track must have moved to the clamped path.
        assert not old_path3.exists(), "PL=3 track should have moved away from its unclamped path"
        assert expected_path3.exists(), f"PL=3 track should be at clamped path {expected_path3.relative_to(dest_root)}"

        # Depth check: clamped path has 5 parts (Classical/top/work/act/leaf), not 6.
        assert len(expected_path3.relative_to(dest_root).parts) == 5, (  # noqa: PLR2004
            f"Expected 5 path parts after clamp, got {expected_path3.relative_to(dest_root).parts}"
        )

    def test_regroup_uniform_depth_group_unchanged(self, fs: FakeFilesystem) -> None:
        """regroup() leaves a uniform-depth group unchanged (no-regression for the common case).

        A 2-track group where both tracks have CWP_PART_LEVELS=2.  The modal depth is 2.
        The clamp is a no-op and regroup() produces an empty plan (no moves).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags1 = self._make_split_tags_pl(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags2 = self._make_split_tags_pl(
            "2",
            "2",
            "Andante",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )

        # Place tracks at their correct (already-clamped) paths.
        canonical1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags1, group_modal_depth=2).with_suffix(".flac")
        canonical2 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags2, group_modal_depth=2).with_suffix(".flac")

        canonical1.parent.mkdir(parents=True, exist_ok=True)
        canonical1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical1, tags1)

        canonical2.parent.mkdir(parents=True, exist_ok=True)
        canonical2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical2, tags2)

        # Build a split-release scenario with a phantom entry in a different work_dir.
        phantom = dest_root / "Classical" / "Handel - Karajan" / "OtherWork [rec 1970]" / "phantom.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(canonical1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(canonical2),
                    "action": "tagged",
                },
                # Phantom entry in a different work_dir to trigger the split-release detection.
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-1",
                    "source": "/src/phantom.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, dry_run=False, yes=True)

        # Both tracks must remain at their original paths (no move).
        assert canonical1.exists(), "Uniform PL=2 track 1 must not be moved"
        assert canonical2.exists(), "Uniform PL=2 track 2 must not be moved"

        # No "regrouped" entries should have been added.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert not regrouped, f"Expected no regrouped entries for uniform group, got {regrouped}"
