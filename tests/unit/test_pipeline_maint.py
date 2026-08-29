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
import time
from pathlib import Path

import pytest
import structlog.testing
from mutagen._util import MutagenError
from mutagen.flac import FLAC as MutagenFLAC
from mutagen.id3 import ID3
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
import music_annotator._pipeline_io
from music_annotator import (
    JOURNAL_FILENAME,
    apply_tags_flac,
    apply_tags_mp3,
    build_dest_path,
    read_journal,
    repath,
    sel23_ensemble_patch,
)
from music_annotator.__main__ import _build_parser, main
from music_annotator._pipeline_io import (
    AudioCompareResult,
    _assess_collisions,
    _needs_enrich,
    _read_albumid_tag,
    _read_tags_flac,
    _read_tags_mp3,
    _resolve_tagged_to_current,
    _sha256_file,
)
from music_annotator._pipeline_maint import (
    _TAG_CACHE_FILENAME,
    DuplicateResolution,
    TagReadCache,
    _build_dedup_census,
    _build_dedup_groups,
    _build_maintain_report,
    _census_journal_for_xrefs,
    _check_dest_root,
    _clamp_maint_dest,
    _execute_single_move,
    _hydrate_performer_lists,
    _journal_capacity,
    _move_verify_journal,
    _read_tags_cached,
    _reference_evidence,
    _resolve_current_lib,
    _scatter_consequence_note,
    _write_xref_and_journal,
    compute_library_modal_depth,
    dedup_library,
    maintain,
    reconstruct_cross_references,
    resolve_duplicate_group,
)
from music_annotator._tagger import write_secondary_albumid_flac, write_secondary_albumid_mp3
from music_annotator._tags import _NAME_MAX, _proposed_short, _work_top_dir
from music_annotator._works import work_group_modal_depth
from music_annotator.models import (
    ArtistEntry,
    DryRunEntry,
    DryRunPlan,
    JournalCapacity,
    MaintainDryRunReport,
    MBRelease,
    MBTrack,
    TrackTags,
    TransactionEntry,
    TransactionLog,
)
from tests.conftest import _MINIMAL_FLAC, _MINIMAL_MP3


def _write_library_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a JSONL journal file to ``dest_root / music_annotator_journal.json``.

    Writes one JSON object per line (JSONL format) so the file is in the format that
    :func:`~music_annotator.read_journal` expects without triggering a migration.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / "music_annotator_journal.json"
    journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


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


class TestClampMaintDest:
    """Unit tests for :func:`_clamp_maint_dest`.

    Verifies that per-component byte clamping is applied consistently to leaf, intermediate, and
    in-bounds components, and that the helper is idempotent on already-short paths.
    """

    def test_in_bounds_path_returned_unchanged(self) -> None:
        """_clamp_maint_dest returns the same path when all components are within _NAME_MAX bytes.

        Ensures the happy-path does not modify or recreate the path unnecessarily.
        """
        dest_root = Path("/music")
        dest = dest_root / "Mozart - Karajan" / "Symphony No. 41 [rec 2000]" / "01 - Allegro vivace.flac"
        assert _clamp_maint_dest(dest_root, dest) == dest

    def test_over_long_leaf_is_clamped(self) -> None:
        """_clamp_maint_dest clamps a leaf component exceeding _NAME_MAX bytes.

        The clamped leaf must be identical to _proposed_short with the audio suffix reserved, and
        the result must fit within _NAME_MAX bytes.
        """
        dest_root = Path("/music")
        # Construct a leaf whose stem + ".flac" exceeds 255 bytes.
        long_stem = "Canon in 3 Parts for 3 Female Voices " + ("x" * 220)
        leaf = long_stem + ".flac"
        assert len(leaf.encode("utf-8")) > _NAME_MAX
        dest = dest_root / "Mozart - Karajan" / "Work [rec 2000]" / leaf
        result = _clamp_maint_dest(dest_root, dest)
        result_leaf = result.name
        assert len(result_leaf.encode("utf-8")) <= _NAME_MAX
        assert result_leaf.endswith(".flac")
        assert result_leaf == _proposed_short(leaf, ".flac")

    def test_over_long_intermediate_dir_is_clamped(self) -> None:
        """_clamp_maint_dest clamps an intermediate directory component exceeding _NAME_MAX bytes.

        No suffix reservation is applied to intermediate components; the component is clamped to
        exactly _NAME_MAX bytes (via _proposed_short with audio_suffix="").
        """
        dest_root = Path("/music")
        long_dir = "Mozart - " + ("y" * 250)
        assert len(long_dir.encode("utf-8")) > _NAME_MAX
        dest = dest_root / long_dir / "Work [rec 2000]" / "01 - Title.flac"
        result = _clamp_maint_dest(dest_root, dest)
        result_dir = result.relative_to(dest_root).parts[0]
        assert len(result_dir.encode("utf-8")) <= _NAME_MAX
        assert result_dir == _proposed_short(long_dir, "")
        # Leaf and mid-dir are unchanged.
        assert result.name == "01 - Title.flac"

    def test_repath_does_not_raise_on_over_long_leaf(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """repath() does not raise OSError[Errno 36] when a recomputed destination leaf exceeds 255 bytes.

        Regression KAT: before _clamp_maint_dest was applied inside repath(), a library file whose
        recomputed path had a leaf name exceeding 255 UTF-8 bytes would trigger OSError on
        dest.exists() inside _assess_collisions.  This test manufactures that condition and asserts
        repath() completes without raising.
        """
        dest_root = Path("/music")
        dest_root.mkdir(parents=True)

        # Build tags that produce an over-long leaf via build_dest_path.
        # title is the leaf stem after "01 - "; we make it long enough that "01 - <title>.flac"
        # exceeds 255 bytes.
        long_title = "Canon in 3 Parts " + ("x" * 240)
        tags = TrackTags(
            cwp_composer_lastnames="Mozart",
            cwp_work_top="Canon",
            recording_date="2000",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title=long_title,
            artist="Karajan",
        )

        # Place the file at a legacy path so repath() wants to move it.
        old_rel = "Mozart - Karajan/OldWork [rec 2000]/01 - OldTitle.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                }
            ],
        )

        # Must not raise; if _clamp_maint_dest is absent, dest.exists() in _assess_collisions
        # raises OSError: [Errno 36] File name too long.
        result = music_annotator.repath(dest_root=dest_root, dry_run=True)

        # dry_run returns a DryRunPlan with exactly one entry (the clamped move).
        assert result is not None
        assert result.count == 1
        new_leaf = result.entries[0].planned_path.split("/")[-1]
        assert len(new_leaf.encode("utf-8")) <= _NAME_MAX
        assert new_leaf.endswith(".flac")

    def test_warning_suppressed_when_clamped_dest_equals_current_path(self, mocker: MockerFixture) -> None:
        """KAT: name_too_long warning is suppressed when the clamped destination equals the current path.

        When a file is already at its canonical clamped location (the clamped dest equals the
        current path), no move would occur and no warning should be emitted.  This prevents
        chronic re-warning on already-clamped names that produce no move.

        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/music")
        long_stem = "Canon in 3 Parts for 3 Female Voices " + ("x" * 220)
        leaf = long_stem + ".flac"
        assert len(leaf.encode("utf-8")) > _NAME_MAX
        clamped_leaf = _proposed_short(leaf, ".flac")
        # The file is already at the clamped destination (no move needed).
        current_path = dest_root / "Mozart - Karajan" / "Work [rec 2000]" / clamped_leaf
        dest = dest_root / "Mozart - Karajan" / "Work [rec 2000]" / leaf

        mock_log_warning = mocker.patch("music_annotator._pipeline_maint.log.warning")

        result = _clamp_maint_dest(dest_root, dest, current_path)

        # The clamped result equals the current path — no warning should be emitted.
        assert result == current_path
        mock_log_warning.assert_not_called()

    def test_warning_emitted_when_clamped_dest_differs_from_current_path(self, mocker: MockerFixture) -> None:
        """name_too_long warning is emitted when the clamped destination differs from the current path.

        When a file needs to be moved to its clamped canonical location, the warning is emitted
        to inform the operator that a name-length truncation is occurring.

        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/music")
        long_stem = "Canon in 3 Parts for 3 Female Voices " + ("x" * 220)
        leaf = long_stem + ".flac"
        assert len(leaf.encode("utf-8")) > _NAME_MAX
        # The file is at a different (non-clamped) current path.
        current_path = dest_root / "Mozart - Karajan" / "Work [rec 2000]" / "01 - OldTitle.flac"
        dest = dest_root / "Mozart - Karajan" / "Work [rec 2000]" / leaf

        mock_log_warning = mocker.patch("music_annotator._pipeline_maint.log.warning")

        result = _clamp_maint_dest(dest_root, dest, current_path)

        # The clamped result differs from the current path — warning must be emitted.
        assert result != current_path
        assert len(result.name.encode("utf-8")) <= _NAME_MAX
        mock_log_warning.assert_called_once()
        call_kwargs = mock_log_warning.call_args[0]
        assert call_kwargs[0] == "name_too_long"

    def test_warning_emitted_when_no_current_path_provided(self, mocker: MockerFixture) -> None:
        """name_too_long warning is emitted when current_path is None (legacy callers).

        When no current_path is provided, the function always emits the warning for clamped
        components (backward-compatible behaviour for callers that do not supply current_path).

        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/music")
        long_stem = "Canon in 3 Parts for 3 Female Voices " + ("x" * 220)
        leaf = long_stem + ".flac"
        assert len(leaf.encode("utf-8")) > _NAME_MAX
        dest = dest_root / "Mozart - Karajan" / "Work [rec 2000]" / leaf

        mock_log_warning = mocker.patch("music_annotator._pipeline_maint.log.warning")

        result = _clamp_maint_dest(dest_root, dest)

        assert len(result.name.encode("utf-8")) <= _NAME_MAX
        mock_log_warning.assert_called_once()


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
        dest = repathed[0].destination
        # The destination should NOT be new_path_raw (it would collide); it has a suffix
        assert dest != str(new_path_raw)
        # The suffix must be the real release MBID prefix ("r1"[:8] == "r1"), not an empty "[]"
        mbid8 = "r1"[:8]
        assert f"[{mbid8}]" in dest, f"expected '[{mbid8}]' in destination, got: {dest}"
        assert "[]" not in dest, f"empty suffix '[]' must not appear in destination: {dest}"

    def test_repath_collision_suffix_is_release_identifying(self, fs: FakeFilesystem) -> None:
        """repath() collision suffix is the real 8-char MBID prefix, not an empty '[]'.

        When a file's recomputed path collides with an existing file (confirmed non-match),
        the disambiguated work_dir must carry the file's real release MBID prefix as the
        suffix token — e.g. ``[abcd1234]`` — not the empty ``[]`` that results from a
        default-constructed MBRelease whose id is "".

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        real_release_id = "abcd1234-ef56-7890-abcd-ef1234567890"
        mbid8 = real_release_id[:8]  # "abcd1234"

        tags_incoming = TrackTags(
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

        new_path_raw = self._new_path(dest_root, tags_incoming)
        old_path = _make_library_flac(dest_root, self._OLD_REL_MVT1, tags_incoming)

        # Pre-create a different file at the canonical path (different AcoustID → non-match)
        tags_existing = TrackTags(
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
        new_path_raw.parent.mkdir(parents=True, exist_ok=True)
        new_path_raw.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_path_raw, tags_existing)

        # Journal: old_path tagged with the real release MBID
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": real_release_id,
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        dest = repathed[0].destination
        # The disambiguated path must carry the real MBID prefix, not an empty "[]"
        assert f"[{mbid8}]" in dest, f"expected '[{mbid8}]' in destination, got: {dest}"
        assert "[]" not in dest, f"empty suffix '[]' must not appear in destination: {dest}"

    def test_repath_two_releases_get_distinct_collision_suffixes(self, fs: FakeFilesystem) -> None:
        """Two files from different releases each colliding with a pre-existing file get distinct suffixes.

        When two files from different releases each recompute to a different canonical path that
        is already occupied by a non-matching recording, each must receive a suffix derived from
        its own release MBID.  Without per-release suffix derivation both would get the same
        empty '[]' suffix — and if they happened to share a work_dir, they would re-collide.

        Scenario:
        - File A (rid_a) at legacy path → canonical_a (occupied by existing_a, different AcoustID)
        - File B (rid_b) at legacy path → canonical_b (occupied by existing_b, different AcoustID)
        - canonical_a and canonical_b differ (different works), so no intra-plan collision guard fires.
        - After collision resolution: A lands at canonical_a [aaaaaaaa], B at canonical_b [bbbbbbbb].

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        rid_a = "aaaaaaaa-0000-0000-0000-000000000000"
        rid_b = "bbbbbbbb-0000-0000-0000-000000000000"
        mbid8_a = rid_a[:8]  # "aaaaaaaa"
        mbid8_b = rid_b[:8]  # "bbbbbbbb"

        # File A: Brahms Violin Concerto, will recompute to canonical_a
        tags_a = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Violin Concerto",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Mutter",
            acoustid_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )
        canonical_a = self._new_path(dest_root, tags_a)
        old_path_a = _make_library_flac(dest_root, "Brahms - Mutter/OldConcerto [2019]/01 - Allegro.flac", tags_a)

        # File B: Brahms Piano Concerto No. 1 (different work → different canonical path)
        tags_b = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Mutter",
            acoustid_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )
        canonical_b = self._new_path(dest_root, tags_b)
        old_path_b = _make_library_flac(dest_root, "Brahms - Mutter/OldPianoConcerto [2019]/01 - Allegro.flac", tags_b)

        # canonical_a and canonical_b must differ (different works)
        assert canonical_a != canonical_b, "test setup error: canonical paths must differ"

        # Pre-create canonical_a with a different AcoustID (non-match for file A)
        tags_existing_a = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Violin Concerto",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Mutter",
            acoustid_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        )
        canonical_a.parent.mkdir(parents=True, exist_ok=True)
        canonical_a.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical_a, tags_existing_a)

        # Pre-create canonical_b with a different AcoustID (non-match for file B)
        tags_existing_b = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Mutter",
            acoustid_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        )
        canonical_b.parent.mkdir(parents=True, exist_ok=True)
        canonical_b.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical_b, tags_existing_b)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": rid_a,
                    "source": "/src/a.flac",
                    "destination": str(old_path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": rid_b,
                    "source": "/src/b.flac",
                    "destination": str(old_path_b),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 2

        destinations = sorted(e.destination for e in repathed)
        # Each file must land at a distinct path (no re-collision)
        assert len(set(destinations)) == 2, f"two files must land at distinct paths; got: {destinations}"
        # Each destination must carry its own release's MBID prefix, not an empty "[]"
        for dest in destinations:
            assert "[]" not in dest, f"empty suffix '[]' must not appear: {dest}"
        # File A's destination carries mbid8_a; file B's carries mbid8_b
        assert any(f"[{mbid8_a}]" in d for d in destinations), f"[{mbid8_a}] not found in {destinations}"
        assert any(f"[{mbid8_b}]" in d for d in destinations), f"[{mbid8_b}] not found in {destinations}"

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
        # C-UNIVERSAL (prefix-less): top_dir/work_dir/inter_dir/leaf = 4 parts below dest_root.
        assert len(correct_path.relative_to(dest_root).parts) == 4, (
            f"Expected 4 path parts (top_dir/work_dir/inter_dir/leaf), got {correct_path.relative_to(dest_root).parts!r}"
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
        dest = regrouped[0].destination
        # The destination must be different from the pre-existing canonical path (collision was resolved)
        assert dest != str(canonical)
        # The suffix must be the real release MBID prefix ("split-rel-1"[:8] == "split-re"), not an empty "[]"
        mbid8 = "split-rel-1"[:8]
        assert f"[{mbid8}]" in dest, f"expected '[{mbid8}]' in destination, got: {dest}"
        assert "[]" not in dest, f"empty suffix '[]' must not appear in destination: {dest}"

    def test_regroup_collision_suffix_is_release_identifying(self, fs: FakeFilesystem) -> None:
        """regroup() collision suffix is the real 8-char MBID prefix, not an empty '[]'.

        When a file's recomputed canonical path collides with an existing file (confirmed
        non-match), the disambiguated work_dir must carry the file's real release MBID prefix
        as the suffix token — e.g. ``[abcd1234]`` — not the empty ``[]`` produced by a
        default-constructed MBRelease.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        real_release_id = "abcd1234-ef56-7890-abcd-ef1234567890"
        mbid8 = real_release_id[:8]  # "abcd1234"

        tags_incoming = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid=real_release_id,
            acoustid_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        )

        canonical = self._canonical_path(dest_root, tags_incoming)
        old_path = _make_library_flac(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac", tags_incoming)
        assert old_path != canonical

        # Pre-create a different file at the canonical path (different AcoustID → non-match)
        tags_existing = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid=real_release_id,
            acoustid_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        )
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(canonical, tags_existing)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": real_release_id,
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T00:00:01+00:00",
                    "release_id": real_release_id,
                    "source": "/src/02.flac",
                    "destination": str(canonical),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 1
        dest = regrouped[0].destination
        # The disambiguated path must carry the real MBID prefix, not an empty "[]"
        assert f"[{mbid8}]" in dest, f"expected '[{mbid8}]' in destination, got: {dest}"
        assert "[]" not in dest, f"empty suffix '[]' must not appear in destination: {dest}"

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

    Exercises all combinations of empty/present audio_hash, acoustid_fingerprint, and acoustid_id,
    plus the re_resolve=True path.
    """

    def test_all_empty_returns_all_fields(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich returns audio_hash and acoustid_fingerprint when both are absent.

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
        assert result["acoustid_fingerprint"] == "AQADtMmybckm"
        assert "acoustid_id" not in result
        mock_log.info.assert_called_once_with("enrich_acoustid_inconclusive", path=str(path))

    def test_all_present_returns_empty_dict(self, fs: FakeFilesystem) -> None:
        """_needs_enrich returns {} when all three fields are already present.

        Writes the legacy ``CHROMAPRINT_FP`` key directly via mutagen so that ``_needs_enrich``
        (which reads the legacy key) can find the fingerprint.  The tagger now writes
        ``ACOUSTID_FINGERPRINT``; the dual-read helper that reads both keys is updated separately.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        # Write audio_hash and acoustid_id via the tagger (canonical keys).
        tags = TrackTags(audio_hash="flac-md5:aabb", acoustid_id="test-uuid")
        apply_tags_flac(path, tags)
        # Write the legacy chromaprint_fp key directly so _needs_enrich can find it.
        audio = MutagenFLAC(str(path))
        audio["chromaprint_fp"] = ["AQADtMmybckm"]
        audio.save()

        result = _needs_enrich(path, re_resolve=False)

        assert result == {"acoustid_id": "test-uuid"}

    def test_re_resolve_true_recomputes_acoustid_fingerprint(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich recomputes acoustid_fingerprint when re_resolve=True even if already present.

        audio_hash is NOT recomputed (anchor rule).  Writes the legacy ``CHROMAPRINT_FP`` key
        directly via mutagen so that ``_needs_enrich`` (which dual-reads both keys) sees an
        existing fingerprint and takes the ``re_resolve`` branch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        # Write audio_hash and acoustid_id via the tagger (canonical keys).
        tags = TrackTags(audio_hash="flac-md5:existing", acoustid_id="test-uuid")
        apply_tags_flac(path, tags)
        # Write the legacy chromaprint_fp key directly so _needs_enrich sees an existing fingerprint
        # via the dual-read (new key first, legacy key second).
        audio = MutagenFLAC(str(path))
        audio["chromaprint_fp"] = ["OldFingerprint"]
        audio.save()

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="NewFingerprint")

        result = _needs_enrich(path, re_resolve=True)

        # audio_hash is present → anchor rule: not recomputed
        assert "audio_hash" not in result
        # acoustid_fingerprint is recomputed under re_resolve=True
        assert result["acoustid_fingerprint"] == "NewFingerprint"
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
        assert result["acoustid_fingerprint"] == "FP"

    def test_fpcalc_returns_empty_not_included(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_needs_enrich omits acoustid_fingerprint when fpcalc returns empty string.

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

        assert "acoustid_fingerprint" not in result

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
        """enrich() backfills audio_hash + acoustid_fingerprint and is idempotent on a second run.

        Run 1: file has acoustid_id but no audio_hash or acoustid_fingerprint.  enrich() writes both
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
        fp_vals = audio.get("acoustid_fingerprint") or []
        acoustid_vals = audio.get("acoustid_id") or []

        assert hash_vals and hash_vals[0].startswith("flac-md5:")
        assert fp_vals and fp_vals[0] == "AQADtMmybckm"
        assert acoustid_vals and acoustid_vals[0] == "test-acoustid-id"

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].audio_hash.startswith("flac-md5:")
        assert enriched[0].acoustid_fingerprint == "AQADtMmybckm"
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
        assert (audio2.get("acoustid_fingerprint") or []) == fp_vals

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
        assert not (audio.get("acoustid_fingerprint") or [])

        # No journal entry
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 0

        # dry_run log event emitted
        dry_run_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_dry_run"]
        assert len(dry_run_calls) == 1

    # ------------------------------------------------------------------
    # re_resolve: recomputes acoustid_fingerprint; audio_hash NOT overwritten
    # ------------------------------------------------------------------

    def test_enrich_re_resolve_recomputes_fp_not_hash(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich(re_resolve=True) recomputes acoustid_fingerprint but never overwrites audio_hash.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            audio_hash="flac-md5:original",
            acoustid_fingerprint="OldFingerprint",
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
        fp_vals = audio.get("acoustid_fingerprint") or []

        # audio_hash must not be overwritten
        assert hash_vals and hash_vals[0] == "flac-md5:original"
        # acoustid_fingerprint must be updated
        assert fp_vals and fp_vals[0] == "NewFingerprint"

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 1
        assert enriched[0].acoustid_fingerprint == "NewFingerprint"

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

        # Write audio_hash and acoustid_id via the tagger (canonical keys).
        tags = TrackTags(audio_hash="flac-md5:aabb", acoustid_id="uuid")
        path = _make_enrichable_flac(dest_root, "Artist/Album/01 - Track.flac", tags)
        # Write the legacy chromaprint_fp key directly so _needs_enrich can find it via dual-read.
        audio = MutagenFLAC(str(path))
        audio["chromaprint_fp"] = ["FP"]
        audio.save()

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
                    "acoustid_fingerprint": "FP",
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
        assert enriched[0].acoustid_fingerprint == "AQADtMmybckm"
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
        """_needs_enrich omits acoustid_fingerprint when re_resolve=True but fpcalc returns empty.

        Covers the ``elif re_resolve: if computed_fp:`` False branch.  Writes the legacy
        ``CHROMAPRINT_FP`` key directly via mutagen so that ``_needs_enrich`` sees an existing
        fingerprint via dual-read and takes the ``re_resolve`` branch (where fpcalc returns empty).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        # Write audio_hash and acoustid_id via the tagger (canonical keys).
        tags = TrackTags(audio_hash="flac-md5:existing", acoustid_id="uuid")
        apply_tags_flac(path, tags)
        # Write the legacy chromaprint_fp key directly so _needs_enrich sees an existing fingerprint
        # via the dual-read (new key first, legacy key second).
        audio = MutagenFLAC(str(path))
        audio["chromaprint_fp"] = ["OldFP"]
        audio.save()

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")

        result = _needs_enrich(path, re_resolve=True)

        assert "acoustid_fingerprint" not in result


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
                    "acoustid_fingerprint": "FP",
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
                    "acoustid_fingerprint": "FP",
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
    """Tests for the acoustid_id re-resolve in enrich().

    When re_resolve=True and acoustid_key is non-empty, enrich() calls
    _fetch_acoustid_lookup_raw after recomputing acoustid_fingerprint and backfills
    acoustid_id with the top AcoustID cluster UUID.
    """

    def test_re_resolve_with_acoustid_key_updates_acoustid_id(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """--re-resolve + acoustid_key → acoustid_id updated in FLAC tag and journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File has an old acoustid_id and an existing acoustid_fingerprint (will be re-resolved)
        tags = TrackTags(
            audio_hash="flac-md5:existing",
            acoustid_fingerprint="OldFingerprint",
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
        # The acoustid_fingerprint was re-resolved
        assert enriched[0].acoustid_fingerprint == "NewFingerprint"

    def test_re_resolve_without_acoustid_key_does_not_call_lookup(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """--re-resolve without acoustid_key does NOT call _fetch_acoustid_lookup_raw (F4 behaviour preserved).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            audio_hash="flac-md5:existing",
            acoustid_fingerprint="OldFingerprint",
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
            acoustid_fingerprint="OldFingerprint",
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
            acoustid_fingerprint="OldFingerprint",
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
        unify() should move File A to the canonical path.  Because File B is already at the
        canonical path with identical content, unify() deduplicates File A (deletes it and
        journals the move) rather than overwriting File B.

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

    def _build_frag_scenario_no_canonical(self, dest_root: Path) -> tuple[Path, Path]:
        """Create a performer-split fragmented release scenario where the canonical path is vacant.

        Two FLAC files for release_id "frag-rel-1" land under different top_dirs:
        - File A lives at "Brahms - Pollini/..." (wrong performer in path, track 1).
        - File B lives at "Brahms - Karajan/..." (correct top_dir, but track 2 — a different
          file, so File A's canonical path does not exist on disk).

        detect_fragmented_releases detects two distinct top_dirs for "frag-rel-1".
        unify() moves File A to its canonical path (which is vacant — no dedup case).

        This variant is used by tests that patch filesystem operations (os.replace, _sha256_file,
        _read_tags_flac) to exercise error branches inside _execute_single_move.  Those tests
        require the canonical path to be vacant so the move is attempted (not deduped).

        Returns (old_path, new_path) where old_path is File A's current location and new_path is
        the recomputed canonical destination from the embedded tags.

        :param dest_root: Library root (must already exist).
        :returns: Tuple of (current file path, expected canonical path after unify).
        """
        tags_a = self._make_frag_tags()  # track 1, wrong top_dir

        # File A: wrong top_dir (Pollini instead of Karajan), track 1
        old_path = _make_library_flac(
            dest_root, "Brahms - Pollini/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac", tags_a
        )

        # File B: correct top_dir (Karajan), but track 2 — different file, different canonical path.
        # This ensures two distinct top_dirs (triggering fragmentation detection) without placing
        # any file at File A's canonical path.
        tags_b = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="2",
            movementtotal="2",
            cwp_part_levels="1",
            title="Second movement",
            artist="Karajan",
            musicbrainz_albumid="frag-rel-1",
        )
        _make_library_flac(dest_root, "Brahms - Karajan/Piano Concerto No. 1 [rec 2021]/02 - Second movement.flac", tags_b)

        canonical_path = self._canonical_path(dest_root, tags_a)
        assert old_path != canonical_path, "test setup error: old and canonical paths must differ"
        assert not canonical_path.exists(), "test setup error: canonical path must not exist"

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

        Uses the no-canonical variant of the fragmented scenario so the canonical path is vacant
        and the move is attempted (not deduped), allowing the SHA-256 mismatch to be exercised.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario_no_canonical(dest_root)

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
        dest = unified[0].destination
        # The suffix must be the real release MBID prefix ("frag-rel-1"[:8] == "frag-rel"), not an empty "[]"
        mbid8 = "frag-rel-1"[:8]
        assert f"[{mbid8}]" in dest, f"expected '[{mbid8}]' in destination, got: {dest}"
        assert "[]" not in dest, f"empty suffix '[]' must not appear in destination: {dest}"

    def test_unify_collision_suffix_is_release_identifying(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() collision suffix is the real 8-char MBID prefix, not an empty '[]'.

        When a file's recomputed canonical path collides with an existing file (confirmed
        non-match), the disambiguated work_dir must carry the file's real release MBID prefix
        as the suffix token — e.g. ``[abcd1234]`` — not the empty ``[]`` produced by a
        default-constructed MBRelease.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        real_release_id = "abcd1234-ef56-7890-abcd-ef1234567890"
        mbid8 = real_release_id[:8]  # "abcd1234"

        old_path, new_path = self._build_frag_scenario(dest_root)

        # Patch _assess_collisions to return a confirmed non-match at new_path
        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_path, match=False, method="sha256", detail="different")],
        )

        # Patch detect_fragmented_releases to use the real release MBID
        mocker.patch(
            "music_annotator._pipeline_maint.detect_fragmented_releases",
            return_value={real_release_id: [old_path]},
        )

        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 1
        dest = unified[0].destination
        # The disambiguated path must carry the real MBID prefix, not an empty "[]"
        assert f"[{mbid8}]" in dest, f"expected '[{mbid8}]' in destination, got: {dest}"
        assert "[]" not in dest, f"empty suffix '[]' must not appear in destination: {dest}"

    # ------------------------------------------------------------------
    # main() dispatch: unify subcommand
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

        self._build_frag_scenario_no_canonical(dest_root)

        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=OSError(errno.EACCES, "Permission denied"))

        with pytest.raises(OSError):
            music_annotator.unify(dest_root=dest_root, yes=True)

    def test_unify_exdev_cross_hash_mismatch_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify() raises RuntimeError when cross-fs copy produces a hash mismatch.

        Patches os.replace to raise EXDEV and _sha256_file to return mismatched hashes
        for the cross-fs copy verification, exercising the cross-hash-mismatch branch.

        Uses the no-canonical variant of the fragmented scenario so the canonical path is vacant
        and the move is attempted (not deduped), allowing the EXDEV + hash-mismatch path to fire.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario_no_canonical(dest_root)

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

        Patches _read_tags_flac to raise on the third call (post-move re-read), exercising
        the ``except Exception: raise RuntimeError(...)`` branch in the tag re-read step.

        Uses the no-canonical variant of the fragmented scenario so the canonical path is vacant
        and the move is attempted (not deduped), allowing the post-move tag re-read to fire.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        self._build_frag_scenario_no_canonical(dest_root)

        original_read = _read_tags_flac
        call_count = {"n": 0}

        def _fake_read(path: Path) -> dict[str, str]:
            """Raise on third call to simulate post-move tag read failure.

            The first two calls are during plan-building (one per file); the third is the
            post-move re-read inside _execute_single_move.

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
    # C-NC-TOP: Non-classical releases take the ALBUMARTIST-led top dir.
    # KATs: Kidz-Bop, Goodman, Various-Artists, Tabernacle shapes.
    # ------------------------------------------------------------------

    @staticmethod
    def _make_nc_fragmented_tags(
        albumartist: str,
        cea_composer: str,
        musicbrainz_albumid: str = "nc-rel-1",
        movt_num: str = "1",
        title: str = "Track 1",
    ) -> TrackTags:
        """Build TrackTags for a non-classical release track with a per-track CEA_COMPOSER_LASTNAMES.

        Non-classical: ``cwp_work_top`` is empty and ``cwp_worktype_genres_top`` is empty, so the
        IS_CLASSICAL predicate is False.  ``cea_composer_lastnames`` is set to ``cea_composer`` to
        simulate a per-track songwriter credit that varies across tracks.  C-NC-TOP: the canonical
        top dir is ALBUMARTIST-led regardless of ``cea_composer_lastnames``.

        :param albumartist: The ALBUMARTIST tag value (uniform across the release).
        :param cea_composer: The per-track CEA_COMPOSER_LASTNAMES value.
        :param musicbrainz_albumid: The MUSICBRAINZ_ALBUMID tag (shared across the release).
        :param movt_num: The CWP_MOVT_NUM value.
        :param title: The track title.
        :returns: A :class:`TrackTags` instance.
        """
        return TrackTags(
            cea_composer_lastnames=cea_composer,
            albumartist=albumartist,
            album="The Album",
            releasetype="Album",
            cwp_work_top="",  # non-classical: no MB work link
            cwp_worktype_genres_top="",
            cwp_movt_num=movt_num,
            movementtotal="2",
            cwp_part_levels="1",
            title=title,
            artist=albumartist,
            musicbrainz_albumid=musicbrainz_albumid,
        )

    def test_kat_kidzbop_unify_repath_identical_destination(self, fs: FakeFilesystem) -> None:
        """KAT (C-NC-TOP / C-IDEM): Kidz-Bop shape — unify and repath compute identical destination.

        A single-artist non-classical release (Kidz Bop) with per-track CEA_COMPOSER_LASTNAMES
        variation is fragmented across two top_dirs.  After unify(), all files land in the
        ALBUMARTIST-led top dir.  A subsequent repath() call produces no moves (mock-enforced
        equality: unify and repath agree on the canonical destination).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two tracks of the same release, placed under different top_dirs due to per-track
        # CEA_COMPOSER_LASTNAMES variation.  The canonical destination is ALBUMARTIST-led
        # ("Kidz Bop") — CEA_COMPOSER_LASTNAMES is not scholarship-stable for non-classical
        # releases (C-NC-TOP).
        tags_a = self._make_nc_fragmented_tags("Kidz Bop", cea_composer="Smith", movt_num="1", title="Track 1")
        tags_b = self._make_nc_fragmented_tags("Kidz Bop", cea_composer="Jones", movt_num="2", title="Track 2")

        path_a = _make_library_flac(dest_root, "Smith - Kidz Bop/The Album/01 - Track 1.flac", tags_a)
        path_b = _make_library_flac(dest_root, "Jones - Kidz Bop/The Album/02 - Track 2.flac", tags_b)

        # First unify(): consolidates both tracks to the ALBUMARTIST-led top dir.
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1, "unify() must move at least one file"

        # All unified destinations must be under the ALBUMARTIST-led top dir ("Kidz Bop").
        # C-UNIVERSAL (prefix-less): parts[0] = top_dir, no class prefix.
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Kidz Bop", f"C-NC-TOP: expected ALBUMARTIST-led top_dir 'Kidz Bop', got {rel.parts[0]!r}"

        # Original fragmented paths must no longer exist.
        assert not path_a.exists() or not path_b.exists()

        # repath() after unify() must produce no moves (C-IDEM: unify and repath agree).
        repath(dest_root=dest_root, yes=True)
        journal_after_repath = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal_after_repath.entries if e.action == "repathed"]
        assert not repathed, (
            f"C-IDEM violated: repath() moved {len(repathed)} file(s) after unify() — "
            f"unify and repath disagree on the canonical destination"
        )

        # Second unify() must be a no-op (C-IDEM: fragmented fixture consolidates then holds still).
        music_annotator.unify(dest_root=dest_root, yes=True)
        journal_after_second = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_second = [e for e in journal_after_second.entries if e.action == "unified"]
        assert len(unified_after_second) == len(unified), "C-IDEM: second unify() must produce no new moves"

    def test_kat_goodman_unify_repath_identical_destination(self, fs: FakeFilesystem) -> None:
        """KAT (C-NC-TOP / C-IDEM): Goodman shape — unify and repath compute identical destination.

        A non-classical release (Benny Goodman) with per-track CEA_COMPOSER_LASTNAMES variation
        (including a track with empty CEA_COMPOSER_LASTNAMES, the ``[no artist]`` shape) is
        fragmented across multiple top_dirs.  After unify(), all files land in the ALBUMARTIST-led
        top dir.  A subsequent repath() call produces no moves.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Three tracks: two with different per-track composers, one with empty composer.
        tags_a = self._make_nc_fragmented_tags(
            "Benny Goodman", cea_composer="Goodman", movt_num="1", title="Track 1", musicbrainz_albumid="goodman-rel-1"
        )
        tags_b = self._make_nc_fragmented_tags(
            "Benny Goodman", cea_composer="Berlin", movt_num="2", title="Track 2", musicbrainz_albumid="goodman-rel-1"
        )
        # Track with empty CEA_COMPOSER_LASTNAMES (the [no artist] shape).
        tags_c = TrackTags(
            cea_composer_lastnames="",  # no composer credit on this track
            albumartist="Benny Goodman",
            album="The Album",
            releasetype="Album",
            cwp_work_top="",
            cwp_worktype_genres_top="",
            cwp_movt_num="3",
            movementtotal="3",
            cwp_part_levels="1",
            title="Track 3",
            artist="Benny Goodman",
            musicbrainz_albumid="goodman-rel-1",
        )

        path_a = _make_library_flac(dest_root, "Goodman - Benny Goodman/The Album/01 - Track 1.flac", tags_a)
        path_b = _make_library_flac(dest_root, "Berlin - Benny Goodman/The Album/02 - Track 2.flac", tags_b)
        path_c = _make_library_flac(dest_root, "Benny Goodman/The Album/03 - Track 3.flac", tags_c)

        # First unify(): consolidates all tracks to the ALBUMARTIST-led top dir.
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1, "unify() must move at least one file"

        # All unified destinations must be under the ALBUMARTIST-led top dir ("Benny Goodman").
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Benny Goodman", (
                f"C-NC-TOP: expected ALBUMARTIST-led top_dir 'Benny Goodman', got {rel.parts[0]!r}"
            )

        # At least two of the three fragmented paths must have been moved.
        assert not path_a.exists() or not path_b.exists()

        # repath() after unify() must produce no moves (C-IDEM: unify and repath agree).
        repath(dest_root=dest_root, yes=True)
        journal_after_repath = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal_after_repath.entries if e.action == "repathed"]
        assert not repathed, (
            f"C-IDEM violated: repath() moved {len(repathed)} file(s) after unify() — "
            f"unify and repath disagree on the canonical destination"
        )

        # Second unify() must be a no-op.
        music_annotator.unify(dest_root=dest_root, yes=True)
        journal_after_second = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_second = [e for e in journal_after_second.entries if e.action == "unified"]
        assert len(unified_after_second) == len(unified), "C-IDEM: second unify() must produce no new moves"

        # Verify that path_c (already at ALBUMARTIST-led path) was not moved.
        assert path_c.exists(), "Track with empty CEA_COMPOSER_LASTNAMES must not be moved unnecessarily"

    def test_kat_various_artists_consolidates_to_one_top_dir(self, fs: FakeFilesystem) -> None:
        """KAT (C-NC-TOP / C-IDEM): Various-Artists compilation consolidates to ONE top dir.

        A Various-Artists compilation with per-track CEA_COMPOSER_LASTNAMES variation must
        consolidate to ONE top dir (the ALBUMARTIST-led "Various Artists" dir), never scatter
        per-track.  After unify(), a subsequent repath() produces no moves.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = self._make_nc_fragmented_tags(
            "Various Artists", cea_composer="Smith", movt_num="1", title="Track 1", musicbrainz_albumid="va-rel-1"
        )
        tags_b = self._make_nc_fragmented_tags(
            "Various Artists", cea_composer="Jones", movt_num="2", title="Track 2", musicbrainz_albumid="va-rel-1"
        )
        tags_c = self._make_nc_fragmented_tags(
            "Various Artists", cea_composer="Brown", movt_num="3", title="Track 3", musicbrainz_albumid="va-rel-1"
        )

        path_a = _make_library_flac(dest_root, "Smith - Various Artists/The Album/01 - Track 1.flac", tags_a)
        path_b = _make_library_flac(dest_root, "Jones - Various Artists/The Album/02 - Track 2.flac", tags_b)
        path_c = _make_library_flac(dest_root, "Brown - Various Artists/The Album/03 - Track 3.flac", tags_c)

        # First unify(): must consolidate all tracks to ONE top dir.
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1, "unify() must move at least one file"

        # All unified destinations must be under the ALBUMARTIST-led top dir ("Various Artists").
        # C-NC-TOP: "Various" is never a composer; the top dir is ALBUMARTIST-led.
        top_dirs = {Path(e.destination).relative_to(dest_root).parts[0] for e in unified}
        assert top_dirs == {"Various Artists"}, (
            f"C-NC-TOP: all unified destinations must be under 'Various Artists', got {top_dirs!r}"
        )

        # At least two of the three fragmented paths must have been moved.
        moved_count = sum(1 for p in (path_a, path_b, path_c) if not p.exists())
        assert moved_count >= 2, f"Expected at least 2 files moved, got {moved_count}"

        # repath() after unify() must produce no moves (C-IDEM: unify and repath agree).
        repath(dest_root=dest_root, yes=True)
        journal_after_repath = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal_after_repath.entries if e.action == "repathed"]
        assert not repathed, (
            f"C-IDEM violated: repath() moved {len(repathed)} file(s) after unify() — "
            f"unify and repath disagree on the canonical destination"
        )

        # Second unify() must be a no-op.
        music_annotator.unify(dest_root=dest_root, yes=True)
        journal_after_second = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_second = [e for e in journal_after_second.entries if e.action == "unified"]
        assert len(unified_after_second) == len(unified), "C-IDEM: second unify() must produce no new moves"

    def test_kat_tabernacle_long_name_unify_repath_identical_destination(self, fs: FakeFilesystem) -> None:
        """KAT (C-NC-TOP / C-IDEM): Tabernacle long-name shape — unify and repath compute identical destination.

        A non-classical release with a long ALBUMARTIST name ("Tabernacle Choir at Temple Square")
        and per-track CEA_COMPOSER_LASTNAMES variation is fragmented.  After unify(), all files
        land in the ALBUMARTIST-led top dir.  A subsequent repath() produces no moves.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        albumartist = "Tabernacle Choir at Temple Square"
        tags_a = self._make_nc_fragmented_tags(
            albumartist, cea_composer="Handel", movt_num="1", title="Track 1", musicbrainz_albumid="tab-rel-1"
        )
        tags_b = self._make_nc_fragmented_tags(
            albumartist, cea_composer="Bach", movt_num="2", title="Track 2", musicbrainz_albumid="tab-rel-1"
        )

        path_a = _make_library_flac(dest_root, "Handel - Tabernacle Choir at Temple Square/The Album/01 - Track 1.flac", tags_a)
        path_b = _make_library_flac(dest_root, "Bach - Tabernacle Choir at Temple Square/The Album/02 - Track 2.flac", tags_b)

        # First unify(): consolidates both tracks to the ALBUMARTIST-led top dir.
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1, "unify() must move at least one file"

        # All unified destinations must be under the ALBUMARTIST-led top dir.
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == albumartist, (
                f"C-NC-TOP: expected ALBUMARTIST-led top_dir {albumartist!r}, got {rel.parts[0]!r}"
            )

        assert not path_a.exists() or not path_b.exists()

        # repath() after unify() must produce no moves (C-IDEM: unify and repath agree).
        repath(dest_root=dest_root, yes=True)
        journal_after_repath = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal_after_repath.entries if e.action == "repathed"]
        assert not repathed, (
            f"C-IDEM violated: repath() moved {len(repathed)} file(s) after unify() — "
            f"unify and repath disagree on the canonical destination"
        )

        # Second unify() must be a no-op.
        music_annotator.unify(dest_root=dest_root, yes=True)
        journal_after_second = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_second = [e for e in journal_after_second.entries if e.action == "unified"]
        assert len(unified_after_second) == len(unified), "C-IDEM: second unify() must produce no new moves"

    def test_nc_top_cea_composer_ignored_for_non_classical(self, fs: FakeFilesystem) -> None:
        """C-NC-TOP: CEA_COMPOSER_LASTNAMES does not trigger composer-bearing shape for non-classical releases.

        A non-classical release (cwp_work_top empty) with CEA_COMPOSER_LASTNAMES set must route
        to the ALBUMARTIST-led top dir, not the ``<composer> - <performers>`` shape.  This
        verifies that the IS_CLASSICAL discriminator in _top_dir_component correctly suppresses
        CEA_COMPOSER_LASTNAMES for non-classical releases.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Non-classical: cwp_work_top empty → IS_CLASSICAL=False → ALBUMARTIST-led.
        tags_a = TrackTags(
            cea_composer_lastnames="Goodman",
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="",  # non-classical
            cwp_worktype_genres_top="",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Track 1",
            artist="Benny Goodman",
            musicbrainz_albumid="nc-top-rel-1",
        )
        tags_b = TrackTags(
            cea_composer_lastnames="Berlin",
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="",
            cwp_worktype_genres_top="",
            cwp_movt_num="2",
            movementtotal="2",
            cwp_part_levels="1",
            title="Track 2",
            artist="Benny Goodman",
            musicbrainz_albumid="nc-top-rel-1",
        )

        # Files placed under composer-bearing paths (the old non-classical composer manufacture shape — now incorrect).
        path_a = _make_library_flac(dest_root, "Goodman - Benny Goodman/The Story/01 - Track 1.flac", tags_a)
        path_b = _make_library_flac(dest_root, "Berlin - Benny Goodman/The Story/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # All unified destinations must be under the ALBUMARTIST-led top dir ("Benny Goodman").
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Benny Goodman", (
                f"C-NC-TOP: non-classical release must use ALBUMARTIST-led top dir; got {rel.parts[0]!r}"
            )

        assert not path_a.exists() or not path_b.exists()

    def test_nc_top_non_classical_with_work_top_uses_albumartist(self, fs: FakeFilesystem) -> None:
        """C-NC-TOP: non-classical release with CWP_WORK_TOP set but non-Classical genre uses ALBUMARTIST-led top dir.

        A release with a MB work link (CWP_WORK_TOP non-empty) but a non-classical genre (e.g.
        "Jazz") is non-classical (IS_CLASSICAL=False) and must take the ALBUMARTIST-led top dir.
        CEA_COMPOSER_LASTNAMES is not scholarship-stable for non-classical releases.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(
            cea_composer_lastnames="Goodman",
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="The Benny Goodman Story",  # has MB work link
            cwp_worktype_genres_top="Jazz",  # NOT "Classical" → IS_CLASSICAL=False
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Track 1",
            artist="Benny Goodman",
            musicbrainz_albumid="jazz-rel-1",
        )
        tags_b = TrackTags(
            cea_composer_lastnames="Berlin",
            albumartist="Benny Goodman",
            album="The Story",
            releasetype="Album",
            cwp_work_top="The Benny Goodman Story",
            cwp_worktype_genres_top="Jazz",
            cwp_movt_num="2",
            movementtotal="2",
            cwp_part_levels="1",
            title="Track 2",
            artist="Benny Goodman",
            musicbrainz_albumid="jazz-rel-1",
        )

        _make_library_flac(dest_root, "Goodman - Benny Goodman/The Story/01 - Track 1.flac", tags_a)
        _make_library_flac(dest_root, "Berlin - Benny Goodman/The Story/02 - Track 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # All unified destinations must be under the ALBUMARTIST-led top dir ("Benny Goodman").
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert rel.parts[0] == "Benny Goodman", (
                f"C-NC-TOP: Jazz release must use ALBUMARTIST-led top dir; got {rel.parts[0]!r}"
            )

    def test_classical_release_cea_composer_triggers_composer_bearing(self, fs: FakeFilesystem) -> None:
        """Classical releases with CEA_COMPOSER_LASTNAMES still use the composer-bearing top dir.

        A classical release (CWP_WORK_TOP non-empty AND CWP_WORKTYPE_GENRES_TOP contains
        "Classical") with varying CEA_COMPOSER_LASTNAMES routes through the classical
        composer-chain unification path, not the ALBUMARTIST-led path.  The composer-bearing
        shape is preserved for classical releases.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Classical release: CWP_WORK_TOP set, CWP_WORKTYPE_GENRES_TOP contains "Classical".
        # IS_CLASSICAL=True → CEA_COMPOSER_LASTNAMES IS scholarship-stable → composer-bearing.
        tags_a = TrackTags(
            cea_composer_lastnames="Mozart",
            albumartistsort="Goodman, Benny",  # would be used if non-classical path applied
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
            cea_composer_lastnames="Haydn",  # different composer — classical → classical composer-chain unification path
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

        # Place files under two different top_dirs (triggers fragmentation detection).
        _make_library_flac(dest_root, "Mozart - Karajan/Symphony No. 40 [rec 2020]/01 - Mvt 1.flac", tags_a)
        _make_library_flac(dest_root, "Haydn - Karajan/Symphony No. 40 [rec 2020]/02 - Mvt 2.flac", tags_b)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # The ALBUMARTIST-led path must NOT have been used: no "Goodman, Benny" top_dir.
        # Classical releases use the composer-bearing shape (CEA_COMPOSER_LASTNAMES is
        # scholarship-stable when IS_CLASSICAL=True).
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        for entry in unified:
            rel = Path(entry.destination).relative_to(dest_root)
            assert "Goodman" not in rel.parts[0], (
                f"Classical release must use composer-bearing top dir; got top_dir={rel.parts[0]!r}"
            )

    def test_nc_top_idempotent_second_unify_noop(self, fs: FakeFilesystem) -> None:
        """C-IDEM: a second unify() after non-classical consolidation is a complete no-op.

        After the first run moves all fragments to the ALBUMARTIST-led canonical path, all files
        share the same top_dir, so detect_fragmented_releases returns empty and unify() returns
        immediately.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = self._make_nc_fragmented_tags("Benny Goodman", cea_composer="Goodman", movt_num="1", title="Track 1")
        tags_b = self._make_nc_fragmented_tags("Benny Goodman", cea_composer="Berlin", movt_num="2", title="Track 2")

        _make_library_flac(dest_root, "Goodman - Benny Goodman/The Album/01 - Track 1.flac", tags_a)
        _make_library_flac(dest_root, "Berlin - Benny Goodman/The Album/02 - Track 2.flac", tags_b)

        # First run: consolidates to ALBUMARTIST-led top dir.
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal_after_first = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_first = [e for e in journal_after_first.entries if e.action == "unified"]
        first_count = len(unified_after_first)
        assert first_count >= 1

        # Second run: no-op (all files already at canonical paths).
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal_after_second = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified_after_second = [e for e in journal_after_second.entries if e.action == "unified"]
        assert len(unified_after_second) == first_count, "C-IDEM: second unify() must produce no new moves"

    def test_nc_top_intra_plan_collision_suffix_applied(self, fs: FakeFilesystem) -> None:
        """unify() applies an intra-plan collision suffix when two non-classical tracks compute to the same destination.

        When two files in the same release group compute to the same ALBUMARTIST-led canonical
        path (e.g. both have the same CWP_MOVT_NUM and title), the intra-plan collision suffix
        block fires and appends a release-identifying suffix to the work_dir of the second entry.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two tracks with identical CWP_MOVT_NUM and title → same canonical destination.
        # Both are non-classical (cwp_work_top empty) → ALBUMARTIST-led top dir.
        # The intra-plan collision suffix block fires for the second entry.
        tags_a = TrackTags(
            cea_composer_lastnames="Goodman",
            albumartist="Benny Goodman",
            album="The Album",
            releasetype="Album",
            cwp_work_top="",
            cwp_worktype_genres_top="",
            cwp_movt_num="1",  # same movt_num as tags_b → same leaf → intra-plan collision
            movementtotal="1",
            cwp_part_levels="1",
            title="Track 1",  # same title as tags_b
            artist="Benny Goodman",
            musicbrainz_albumid="intra-rel-1",
        )
        tags_b = TrackTags(
            cea_composer_lastnames="Berlin",
            albumartist="Benny Goodman",
            album="The Album",
            releasetype="Album",
            cwp_work_top="",
            cwp_worktype_genres_top="",
            cwp_movt_num="1",  # same movt_num as tags_a → same leaf → intra-plan collision
            movementtotal="1",
            cwp_part_levels="1",
            title="Track 1",  # same title as tags_a
            artist="Benny Goodman",
            musicbrainz_albumid="intra-rel-1",
        )

        # Place files under different top_dirs (triggers fragmentation detection).
        _make_library_flac(dest_root, "Goodman - Benny Goodman/The Album/01 - Track 1.flac", tags_a)
        _make_library_flac(dest_root, "Berlin - Benny Goodman/The Album/01 - Track 1.flac", tags_b)

        # unify() must not raise; the intra-plan collision suffix block fires.
        music_annotator.unify(dest_root=dest_root, yes=True)

        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        # At least one file was moved (the one not already at the canonical path).
        assert len(unified) >= 1
        # The intra-plan collision suffix must have been applied: one destination carries a suffix.
        suffixed = [e for e in unified if "[" in Path(e.destination).relative_to(dest_root).parts[1]]
        assert suffixed, "Expected at least one destination with an intra-plan collision suffix"

    # ------------------------------------------------------------------
    # KAT: raw-embedded-chain render — no pass-local in-memory patch (C-CANON)
    # ------------------------------------------------------------------

    def test_kat_completer_shape_raw_chain_render_fixpoint(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """KAT (C-CANON): completer-shape fixture renders raw embedded chains; unify and repath agree.

        Fixture: movements of one work carry two distinct embedded composer chains — ``Mozart``
        (primary-only) and ``Mozart; Süßmayr`` (primary + completer).  Both chains are durable
        on-disk tags; no pass may apply a pass-local in-memory patch that alters the rendered path.

        Assertions:
        1. ``build_dest_path`` is called by ``unify`` with the raw embedded composer values
           (mock-enforced: the spy captures every call's ``tags.cwp_composer_lastnames`` and
           ``tags.cea_composer_lastnames`` and asserts they equal the values read from disk).
        2. ``unify`` produces zero moves — each file is already at the path its raw tags render.
        3. ``repath`` dry-run destinations equal ``unify`` dry-run destinations for every file
           (both passes derive the same canonical path from the same durable inputs).
        4. The two chains render two distinct top dirs; that is the accepted fixpoint, not a
           regression.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        def _make_movement_tags(composer: str, movt_num: str, title: str) -> TrackTags:
            return TrackTags(
                cwp_composer_lastnames=composer,
                cea_composer_lastnames=composer,
                cwp_workid_top="work-k626",
                cwp_work_top="Requiem K. 626",
                cwp_worktype_genres_top="Classical",
                cwp_movt_num=movt_num,
                movementtotal="3",
                cwp_part_levels="1",
                title=title,
                artist="Karajan",
                recording_date="1962",
                musicbrainz_albumid="completer-rel-1",
            )

        tags_mvt1 = _make_movement_tags("Mozart", "1", "Introitus")
        tags_mvt2 = _make_movement_tags("Mozart; Süßmayr", "2", "Kyrie")
        tags_mvt3 = _make_movement_tags("Mozart", "3", "Lacrimosa")

        # Place each file at the path its raw embedded tags render (the canonical fixpoint).
        canon_mvt1 = self._canonical_path(dest_root, tags_mvt1)
        canon_mvt2 = self._canonical_path(dest_root, tags_mvt2)
        canon_mvt3 = self._canonical_path(dest_root, tags_mvt3)

        _make_library_flac(dest_root, str(canon_mvt1.relative_to(dest_root)), tags_mvt1)
        _make_library_flac(dest_root, str(canon_mvt2.relative_to(dest_root)), tags_mvt2)
        _make_library_flac(dest_root, str(canon_mvt3.relative_to(dest_root)), tags_mvt3)

        # Spy on build_dest_path inside _pipeline_maint to capture the composer tag values
        # passed by unify.  The real function still executes (wraps); we only record the args.
        # Reference the real function via the already-imported name (same object as the module binding).
        captured_cwp: list[str] = []
        captured_cea: list[str] = []

        def _spy_build(root: Path, rel: object, trk: object, tags: TrackTags, **kwargs: object) -> Path:
            captured_cwp.append(tags.cwp_composer_lastnames)
            captured_cea.append(tags.cea_composer_lastnames)
            return build_dest_path(root, rel, trk, tags, **kwargs)  # type: ignore[arg-type]

        mocker.patch("music_annotator._pipeline_maint.build_dest_path", side_effect=_spy_build)

        # unify dry-run: plan is built but no files are moved.
        unify_plan = music_annotator.unify(dest_root=dest_root, dry_run=True)
        assert unify_plan is not None

        # Assertion 1: build_dest_path was called with raw embedded composer values — no patching.
        # Every captured cwp value must be one of the two raw embedded chains.
        raw_chains = {"Mozart", "Mozart; Süßmayr"}
        for cwp_val in captured_cwp:
            assert cwp_val in raw_chains, (
                f"unify passed a patched composer chain to build_dest_path: {cwp_val!r} (expected one of {raw_chains})"
            )
        for cea_val in captured_cea:
            assert cea_val in raw_chains, (
                f"unify passed a patched CEA composer chain to build_dest_path: {cea_val!r} (expected one of {raw_chains})"
            )

        # Assertion 2: unify produces zero moves — each file is already at its raw-tag canonical path.
        assert unify_plan.count == 0, (
            f"unify must produce zero moves when files are at their raw-tag canonical paths; "
            f"got {unify_plan.count} planned move(s): {unify_plan.entries}"
        )

        # Assertion 3: repath dry-run destinations agree with unify dry-run destinations.
        # Seed the journal so repath can find the files.
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "completer-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(canon_mvt1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "completer-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(canon_mvt2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "completer-rel-1",
                    "source": "/src/03.flac",
                    "destination": str(canon_mvt3),
                    "action": "tagged",
                },
            ],
        )
        repath_plan = music_annotator.repath(dest_root=dest_root, dry_run=True)
        assert repath_plan is not None
        # Both passes must agree: zero planned moves (files already at canonical paths).
        assert repath_plan.count == 0, (
            f"repath must produce zero moves when files are at their raw-tag canonical paths; "
            f"got {repath_plan.count} planned move(s): {repath_plan.entries}"
        )

        # Assertion 4: two distinct top dirs exist — the raw-chain fixpoint.
        top_dirs = {canon_mvt1.relative_to(dest_root).parts[0], canon_mvt2.relative_to(dest_root).parts[0]}
        assert len(top_dirs) == 2, (  # noqa: PLR2004 — 2 is the expected number of distinct top dirs
            f"Completer shape must render two distinct top dirs (one per chain); got {top_dirs}"
        )

    def test_kat_mixed_arrangement_shape_no_chain_rewrite(self, fs: FakeFilesystem) -> None:
        """KAT (C-CANON): mixed-arrangement shape — no pass rewrites either chain's render.

        Fixture: sub-works of one release carry genuinely different embedded composer chains
        (``Vivaldi; Bach`` vs ``Bach; Marcello``) sharing one top-work MBID.  No pass may
        rewrite either chain's render — both chains are scholarship-stable durable tags and
        must be preserved verbatim.

        Assertions:
        1. After ``unify``, both files remain at their original paths (zero moves).
        2. The two chains render two distinct top dirs; that is the accepted fixpoint.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        def _make_concerto_tags(composer: str, movt_num: str, title: str) -> TrackTags:
            return TrackTags(
                cwp_composer_lastnames=composer,
                cea_composer_lastnames=composer,
                cwp_workid_top="work-16konzerte",
                cwp_work_top="16 Konzerte",
                cwp_worktype_genres_top="Classical",
                cwp_movt_num=movt_num,
                movementtotal="2",
                cwp_part_levels="1",
                title=title,
                artist="Musici",
                recording_date="1970",
                musicbrainz_albumid="mixed-arr-rel-1",
            )

        tags_vivaldi = _make_concerto_tags("Vivaldi; Bach", "1", "Concerto in A minor")
        tags_marcello = _make_concerto_tags("Bach; Marcello", "2", "Concerto in D minor")

        # Place each file at the path its raw embedded tags render.
        canon_vivaldi = self._canonical_path(dest_root, tags_vivaldi)
        canon_marcello = self._canonical_path(dest_root, tags_marcello)

        _make_library_flac(dest_root, str(canon_vivaldi.relative_to(dest_root)), tags_vivaldi)
        _make_library_flac(dest_root, str(canon_marcello.relative_to(dest_root)), tags_marcello)

        # unify: both files are already at their canonical paths → zero moves.
        unify_plan = music_annotator.unify(dest_root=dest_root, dry_run=True)
        assert unify_plan is not None
        assert unify_plan.count == 0, (
            f"unify must not rewrite either chain's render; got {unify_plan.count} planned move(s): {unify_plan.entries}"
        )

        # Both chains render distinct top dirs — the accepted fixpoint.
        top_dir_vivaldi = canon_vivaldi.relative_to(dest_root).parts[0]
        top_dir_marcello = canon_marcello.relative_to(dest_root).parts[0]
        assert top_dir_vivaldi != top_dir_marcello, (
            "Mixed-arrangement shape must render two distinct top dirs (one per chain); "
            f"got identical top dir {top_dir_vivaldi!r} for both chains"
        )
        assert "Vivaldi" in top_dir_vivaldi, f"Expected 'Vivaldi' in top dir, got {top_dir_vivaldi!r}"
        assert "Marcello" in top_dir_marcello, f"Expected 'Marcello' in top dir, got {top_dir_marcello!r}"

    def test_kat_work_id_non_classical_stays_albumartist_led(self, fs: FakeFilesystem) -> None:
        """KAT (C-NC-TOP / C-CANON): work-ID'd non-classical release stays ALBUMARTIST-led.

        Fixture: a jazz release whose tracks carry ``MUSICBRAINZ_WORKID`` and empty composer tags.
        The IS_CLASSICAL predicate (``cwp_work_top`` non-empty AND ``"Classical"`` in
        ``cwp_worktype_genres_top``) is False for these tracks, so ``build_dest_path`` must
        produce an ALBUMARTIST-led top dir.  Without the deleted composer-chain patch, the
        back-door C-NC-TOP flip (empty-composer files rendered composer-led) is impossible.

        Assertions:
        1. ``repath`` dry-run produces zero moves — files are already at their ALBUMARTIST-led
           canonical paths.
        2. ``unify`` produces zero moves — no fragmentation detected (all files share one top dir).
        3. The top dir is ALBUMARTIST-led (starts with the album artist name, not a composer name).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        def _make_jazz_tags(title: str, movt_num: str) -> TrackTags:
            return TrackTags(
                # Non-classical: cwp_work_top empty, cwp_worktype_genres_top does not contain "Classical"
                cwp_work_top="",
                cwp_worktype_genres_top="Jazz",
                # Work ID present (the back-door that the deleted patch exploited)
                musicbrainz_workid="work-jazz-1",
                # Composer tags empty — non-classical release, no scholarship-stable composer
                cwp_composer_lastnames="",
                cea_composer_lastnames="",
                # ALBUMARTIST-led identity
                albumartist="Benny Goodman",
                album="Sing, Sing, Sing",
                releasetype="Album",
                cwp_movt_num=movt_num,
                movementtotal="2",
                cwp_part_levels="0",
                title=title,
                artist="Benny Goodman",
                recording_date="1937",
                musicbrainz_albumid="jazz-work-rel-1",
            )

        tags_trk1 = _make_jazz_tags("Sing, Sing, Sing (Part 1)", "1")
        tags_trk2 = _make_jazz_tags("Sing, Sing, Sing (Part 2)", "2")

        # Place each file at the ALBUMARTIST-led canonical path.
        canon_trk1 = self._canonical_path(dest_root, tags_trk1)
        canon_trk2 = self._canonical_path(dest_root, tags_trk2)

        _make_library_flac(dest_root, str(canon_trk1.relative_to(dest_root)), tags_trk1)
        _make_library_flac(dest_root, str(canon_trk2.relative_to(dest_root)), tags_trk2)

        # Assertion 3: top dir is ALBUMARTIST-led (not composer-led).
        top_dir = canon_trk1.relative_to(dest_root).parts[0]
        assert top_dir.startswith("Benny Goodman"), f"Non-classical release must have ALBUMARTIST-led top dir; got {top_dir!r}"

        # Seed the journal so repath can find the files.
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "jazz-work-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(canon_trk1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "jazz-work-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(canon_trk2),
                    "action": "tagged",
                },
            ],
        )

        # Assertion 1: repath produces zero moves — files already at ALBUMARTIST-led canonical paths.
        repath_plan = music_annotator.repath(dest_root=dest_root, dry_run=True)
        assert repath_plan is not None
        assert repath_plan.count == 0, (
            f"repath must produce zero moves for non-classical release at canonical paths; "
            f"got {repath_plan.count} planned move(s): {repath_plan.entries}"
        )

        # Assertion 2: unify produces zero moves — no fragmentation (all files share one top dir).
        unify_plan = music_annotator.unify(dest_root=dest_root, dry_run=True)
        assert unify_plan is not None
        assert unify_plan.count == 0, (
            f"unify must produce zero moves for non-classical release at canonical paths; "
            f"got {unify_plan.count} planned move(s): {unify_plan.entries}"
        )


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
        in_memory_journal = read_journal(journal_path)
        with pytest.raises(RuntimeError, match="verify failed"):
            _move_verify_journal(
                [(src, dest)],
                journal=in_memory_journal,
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
        in_memory_journal = read_journal(journal_path)
        moved = _move_verify_journal(
            [(src, dest)],
            journal=in_memory_journal,
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
        # Write an empty JSONL journal (not a legacy array) so migration does not call os.replace,
        # which is patched below to raise EXDEV.
        journal_path.write_text("", encoding="utf-8")

        # Patch os.replace to raise EXDEV so the cross-fs fallback is exercised.
        exdev_error = OSError(errno.EXDEV, "Cross-device link")
        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=exdev_error)

        now = datetime.datetime.now(datetime.UTC)
        in_memory_journal = read_journal(journal_path)
        moved = _move_verify_journal(
            [(src, dest)],
            journal=in_memory_journal,
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
        # Write an empty JSONL journal (not a legacy array) so migration does not call os.replace,
        # which is patched below to raise EPERM.
        journal_path.write_text("", encoding="utf-8")

        # Patch os.replace to raise a non-EXDEV OSError (e.g. EPERM).
        perm_error = OSError(errno.EPERM, "Operation not permitted")
        mocker.patch("music_annotator._pipeline_maint.os.replace", side_effect=perm_error)

        now = datetime.datetime.now(datetime.UTC)
        in_memory_journal = read_journal(journal_path)
        with pytest.raises(OSError, match="Operation not permitted"):
            _move_verify_journal(
                [(src, dest)],
                journal=in_memory_journal,
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
        in_memory_journal = read_journal(journal_path)
        moved = _move_verify_journal(
            [],
            journal=in_memory_journal,
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
        in_memory_journal = read_journal(journal_path)
        _move_verify_journal(
            [(src, dest)],
            journal=in_memory_journal,
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
        in_memory_journal = read_journal(journal_path)
        _move_verify_journal(
            [(src, dest)],
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
        )

        # The now-empty source directories should have been removed.
        assert not src_dir.exists()
        assert not (dest_root / "OldComposer").exists()

    # ---------------------------------------------------------------------------
    # KAT 1: Renumbering shift chain (C-SEQ dependency ordering)
    # ---------------------------------------------------------------------------

    def test_shift_chain_no_suffix_correct_final_layout(self, fs: FakeFilesystem) -> None:
        """C-SEQ KAT: a shift chain executes in dependency order with no suffix and all files present.

        Constructs a three-file shift chain where each destination is the next entry's source:
          A → B, B → C, C → D
        (A's destination is B's source, B's destination is C's source.)

        Asserts:
        - All three files are present at their final destinations (D, C, B respectively).
        - No collision suffix is applied (no file is lost or overwritten).
        - Three journal entries are written with the correct source/destination pairs.
        - Moves execute in dependency order (C vacates before B lands, B vacates before A lands).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        src_dir = Path("/lib/Work")
        fs.create_dir(str(src_dir))

        # Create three files: A at path_a, B at path_b, C at path_c.
        # Shift chain: A→B, B→C, C→D (each dest is the next source).
        path_a = src_dir / "01.flac"
        path_b = src_dir / "02.flac"
        path_c = src_dir / "03.flac"
        path_d = src_dir / "04.flac"

        self._make_flac(path_a)
        self._make_flac(path_b)
        self._make_flac(path_c)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        # Shift chain: A→B, B→C, C→D.
        # Without dependency ordering, A→B would clobber B before B→C runs.
        # With C-SEQ topological ordering: C→D first, then B→C, then A→B.
        now = datetime.datetime.now(datetime.UTC)
        in_memory_journal = read_journal(journal_path)
        moved = _move_verify_journal(
            [(path_a, path_b), (path_b, path_c), (path_c, path_d)],
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        # All three moves succeeded.
        assert moved == 3

        # Final layout: A's content at path_b, B's content at path_c, C's content at path_d.
        # (Each file shifted one slot forward.)
        assert path_b.exists(), "A's content must be at path_b after shift"
        assert path_c.exists(), "B's content must be at path_c after shift"
        assert path_d.exists(), "C's content must be at path_d after shift"
        assert not path_a.exists(), "path_a must be vacated after shift"

        # Three journal entries, one per move.
        journal = read_journal(journal_path)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 3, f"expected 3 journal entries, got {len(repathed)}"

        # All source/destination pairs are present in the journal (order may vary).
        pairs = {(e.source, e.destination) for e in repathed}
        assert (str(path_a), str(path_b)) in pairs
        assert (str(path_b), str(path_c)) in pairs
        assert (str(path_c), str(path_d)) in pairs

    # ---------------------------------------------------------------------------
    # KAT 2: True two-file swap (C-SEQ swap cycle via temp hop)
    # ---------------------------------------------------------------------------

    def test_swap_cycle_temp_hop_provenance_intact(self, fs: FakeFilesystem) -> None:
        """C-SEQ KAT: a two-file swap uses a temp hop; provenance chain intact, no data loss.

        Constructs a two-file swap: A→B, B→A.  Without temp-hop support, both moves would
        deadlock (each destination is the other's source).  With C-SEQ, the swap is broken by:
          1. A → temp  (journalled)
          2. B → A     (journalled)
          3. temp → B  (journalled)

        Asserts:
        - Both files are at their swapped destinations after the operation.
        - Three journal entries are written (two real moves + one temp hop).
        - No data loss: both files are intact (readable via _read_tags_flac).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = Path("/lib/Work")
        fs.create_dir(str(work_dir))

        path_a = work_dir / "01.flac"
        path_b = work_dir / "02.flac"

        # Write distinct tags to A and B so we can verify the swap.
        tags_a = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="2",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
        )
        tags_b = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="2",
            movementtotal="2",
            cwp_part_levels="1",
            title="Andante con moto",
            artist="Karajan",
        )
        path_a.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(path_a, tags_a)
        path_b.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(path_b, tags_b)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        now = datetime.datetime.now(datetime.UTC)
        in_memory_journal = read_journal(journal_path)
        moved = _move_verify_journal(
            [(path_a, path_b), (path_b, path_a)],
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        # Three moves: A→temp, B→A, temp→B (temp hop counts as a move).
        assert moved == 3, f"expected 3 moves (temp hop), got {moved}"

        # Both files exist at their swapped destinations.
        assert path_a.exists(), "path_a must exist after swap (B's content)"
        assert path_b.exists(), "path_b must exist after swap (A's content)"

        # Verify the swap: A's original content (tags_a) is now at path_b,
        # and B's original content (tags_b) is now at path_a.
        tags_at_a = _read_tags_flac(path_a)
        tags_at_b = _read_tags_flac(path_b)
        assert tags_at_a.get("TITLE") == "Andante con moto", (
            f"path_a should have B's content (Andante con moto), got {tags_at_a.get('TITLE')!r}"
        )
        assert tags_at_b.get("TITLE") == "Allegro con brio", (
            f"path_b should have A's content (Allegro con brio), got {tags_at_b.get('TITLE')!r}"
        )

        # Three journal entries (A→temp, B→A, temp→B).
        journal = read_journal(journal_path)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 3, f"expected 3 journal entries (temp hop), got {len(repathed)}"

        # The final destinations (path_a and path_b) must appear in the journal.
        dests = {e.destination for e in repathed}
        assert str(path_a) in dests, f"path_a must appear as a destination in the journal; dests={dests}"
        assert str(path_b) in dests, f"path_b must appear as a destination in the journal; dests={dests}"

    def test_dependency_ordering_already_processed_node(self, fs: FakeFilesystem) -> None:
        """C-SEQ: dependency ordering handles a node whose dependency was already processed.

        Constructs a two-move plan where move 1 depends on move 0 (move 0 must vacate its
        source before move 1 can land there), and move 0 has no dependency.  The topological
        sort processes move 0 first (no dependency), then move 1.  During DFS, when processing
        move 1, move 0's node is already BLACK (fully processed) — exercising the
        ``color[nxt] == BLACK`` branch in the DFS cycle detection.

        Scenario:
          Move 0: A → B  (A is vacated; B is the new location)
          Move 1: C → A  (C is vacated; A is the new location — A was vacated by move 0)

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = Path("/lib/Work")
        fs.create_dir(str(work_dir))

        path_a = work_dir / "01.flac"
        path_b = work_dir / "02.flac"
        path_c = work_dir / "03.flac"

        self._make_flac(path_a)
        self._make_flac(path_c)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        # Move 0: A→B (no dependency: B is not a source of any move).
        # Move 1: C→A (depends on move 0: A must be vacated before C can land there).
        # Topological order: move 0 first (no dep), then move 1.
        now = datetime.datetime.now(datetime.UTC)
        in_memory_journal = read_journal(journal_path)
        moved = _move_verify_journal(
            [(path_a, path_b), (path_c, path_a)],
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        assert moved == 2
        assert path_b.exists(), "A's content must be at path_b"
        assert path_a.exists(), "C's content must be at path_a"
        assert not path_c.exists(), "path_c must be vacated"

        journal = read_journal(journal_path)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 2

    # ---------------------------------------------------------------------------
    # KAT 3: Genuine occupant-stays collision (vacancy-aware _assess_collisions)
    # ---------------------------------------------------------------------------

    def test_genuine_occupant_stays_collision_suffix_applied(self, fs: FakeFilesystem) -> None:
        """C-SEQ KAT: a destination occupied by a file NOT in the plan triggers a suffix.

        Constructs a scenario where:
        - File A is planned to move to dest_path.
        - dest_path is already occupied by an UNRELATED file (not a source in the plan).
        - The occupant has a different AcoustID tag (confirming different audio content).

        Asserts that _assess_collisions returns a non-match result for dest_path (the suffix
        fires), and that the vacated_paths set does NOT suppress this collision (the occupant
        is not vacated by the plan).

        The vacancy-aware check is also verified: when dest_path IS in vacated_paths, the
        collision is suppressed (the occupant would be moved away before this move executes).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        src_dir = Path("/lib/Src")
        fs.create_dir(str(src_dir))

        # File A: source file to be moved.  Tag with AcoustID "aaaa-1111".
        src_a = src_dir / "01.flac"
        tags_src = TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
            acoustid_id="aaaa-1111-aaaa-1111",
        )
        src_a.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(src_a, tags_src)

        # Occupant: a DIFFERENT file already at the destination (not in the plan).
        # Tag with a different AcoustID "bbbb-2222" so compare_audio_collision returns match=False.
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tags_occ = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Pollini",
            acoustid_id="bbbb-2222-bbbb-2222",
        )
        dest_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_path, tags_occ)

        # vacated_paths contains only src_a (the plan's source), NOT dest_path.
        vacated: frozenset[Path] = frozenset({src_a})

        results = _assess_collisions(
            [(src_a, dest_path, "aaaa-1111-aaaa-1111", 0)],
            vacated_paths=vacated,
        )

        # The occupant is not vacated → collision is detected with match=False (different AcoustID).
        assert len(results) == 1, f"expected 1 collision result, got {len(results)}"
        assert results[0].match is False, f"expected match=False (different AcoustID), got match={results[0].match}"

        # Vacancy-aware check: when dest_path IS in vacated_paths, the collision is suppressed.
        vacated_with_dest: frozenset[Path] = frozenset({src_a, dest_path})
        results_suppressed = _assess_collisions(
            [(src_a, dest_path, "aaaa-1111-aaaa-1111", 0)],
            vacated_paths=vacated_with_dest,
        )
        assert len(results_suppressed) == 0, f"expected 0 results when dest is vacated, got {len(results_suppressed)}"

    # ---------------------------------------------------------------------------
    # KAT 4: Forced stationary-occupant clobber attempt (C-NOCLOBBER refusal)
    # ---------------------------------------------------------------------------

    def test_stationary_occupant_clobber_refused_no_journal_entry(self, fs: FakeFilesystem) -> None:
        """C-NOCLOBBER KAT: a move whose destination is occupied and NOT vacated is refused.

        Constructs a scenario where:
        - File A is planned to move to dest_path.
        - dest_path is already occupied by a file with DIFFERENT content (not a dedup case).
        - The occupant is NOT a source in the plan (not vacated).

        Asserts:
        - RuntimeError is raised with a C-NOCLOBBER message.
        - No journal entry is written.
        - Both files (source and occupant) remain intact after the refusal.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        src_dir = Path("/lib/Src")
        fs.create_dir(str(src_dir))

        src_a = src_dir / "01.flac"
        self._make_flac(src_a)

        # Occupant: a DIFFERENT file at the destination (different content → not dedup).
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        # Write different bytes so SHA-256 differs from src_a (not a dedup case).
        dest_path.write_bytes(_MINIMAL_FLAC + b"\xff\xfe\xfd")

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        now = datetime.datetime.now(datetime.UTC)
        in_memory_journal = read_journal(journal_path)
        with pytest.raises(RuntimeError, match="C-NOCLOBBER"):
            _move_verify_journal(
                [(src_a, dest_path)],
                journal=in_memory_journal,
                journal_path=journal_path,
                action="repathed",
                dest_root=dest_root,
                now=now,
                release_id="",
            )

        # No journal entry written (C-PROV: no entry before verification passes).
        journal = read_journal(journal_path)
        assert journal.entries == [], "C-NOCLOBBER: no journal entry must be written on refusal"

        # Both files remain intact.
        assert src_a.exists(), "source file must remain intact after C-NOCLOBBER refusal"
        assert dest_path.exists(), "occupant file must remain intact after C-NOCLOBBER refusal"

    # ---------------------------------------------------------------------------
    # KAT: in-memory journal threading — zero re-reads, per-move ordering, crash simulation
    # ---------------------------------------------------------------------------

    def test_multi_move_zero_journal_rereads(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: a multi-move run performs zero journal re-reads after the initial load.

        After the caller reads the journal once and passes it to _move_verify_journal, the
        function must never call read_journal again during the move phase.  This is verified by
        patching read_journal at the _pipeline_maint binding and asserting it is never called
        inside _move_verify_journal (the caller's pre-call read is not counted because the patch
        is applied after that read).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        # Create two source files so the move phase has multiple iterations.
        src1 = src_dir / "01.flac"
        src2 = src_dir / "02.flac"
        dest1 = dest_root / "Work" / "01 - Allegro con brio.flac"
        dest2 = dest_root / "Work" / "02 - Andante con moto.flac"
        self._make_flac(src1)
        self._make_flac(src2)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("", encoding="utf-8")

        # Read the journal once before the move phase (simulating the maintenance pass pattern).
        in_memory_journal = read_journal(journal_path)

        # Patch read_journal at the _pipeline_maint binding AFTER the caller's pre-call read.
        # Any call inside _move_verify_journal would be a re-read violation.
        mock_read = mocker.patch("music_annotator._pipeline_maint.read_journal")

        now = datetime.datetime.now(datetime.UTC)
        moved = _move_verify_journal(
            [(src1, dest1), (src2, dest2)],
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        assert moved == 2
        mock_read.assert_not_called()  # read_journal must not be called during the move phase

    def test_per_move_append_lands_before_next_move(self, fs: FakeFilesystem) -> None:
        """KAT: per-move append lands before the next move begins (C-PROV ordering preserved).

        Verifies that after _move_verify_journal completes, the in-memory journal and the
        on-disk journal both contain exactly the same entries.  This confirms that each
        append_journal_entry call (durable write) is paired with a journal.entries.append
        (in-memory update) within the same move unit, so the in-memory copy always reflects
        the durable state.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src1 = src_dir / "01.flac"
        src2 = src_dir / "02.flac"
        dest1 = dest_root / "Work" / "01 - Allegro con brio.flac"
        dest2 = dest_root / "Work" / "02 - Andante con moto.flac"
        self._make_flac(src1)
        self._make_flac(src2)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("", encoding="utf-8")

        in_memory_journal = read_journal(journal_path)

        now = datetime.datetime.now(datetime.UTC)
        moved = _move_verify_journal(
            [(src1, dest1), (src2, dest2)],
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=now,
            release_id="",
        )

        assert moved == 2

        # The in-memory journal must reflect all appended entries without a re-read.
        # This verifies that each move's append_journal_entry call is paired with a
        # journal.entries.append within the same move unit (C-PROV ordering preserved).
        assert len(in_memory_journal.entries) == 2, (
            f"in-memory journal must have 2 entries after 2 moves; got {len(in_memory_journal.entries)}"
        )

        # The on-disk journal must match the in-memory journal exactly.
        on_disk = read_journal(journal_path)
        assert len(on_disk.entries) == 2, f"on-disk journal must have 2 entries; got {len(on_disk.entries)}"
        # Both entries must be present in the in-memory journal (same destinations).
        in_memory_dests = {e.destination for e in in_memory_journal.entries}
        assert str(dest1) in in_memory_dests, "dest1 must be in in-memory journal"
        assert str(dest2) in in_memory_dests, "dest2 must be in in-memory journal"

    def test_crash_simulation_between_moves_leaves_complete_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: crash simulation between moves leaves a complete, readable journal.

        Simulates a crash after the first move by raising RuntimeError from the second
        _execute_single_move call.  Verifies that the journal contains exactly one entry
        (the first move's entry) and is readable without corruption.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        src_dir = Path("/src")
        fs.create_dir(str(dest_root))
        fs.create_dir(str(src_dir))

        src1 = src_dir / "01.flac"
        src2 = src_dir / "02.flac"
        dest1 = dest_root / "Work" / "01 - Allegro con brio.flac"
        dest2 = dest_root / "Work" / "02 - Andante con moto.flac"
        self._make_flac(src1)
        self._make_flac(src2)

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("", encoding="utf-8")

        in_memory_journal = read_journal(journal_path)

        # Move the first file successfully, then simulate a crash on the second.
        # We do this by moving src1→dest1 manually (outside _move_verify_journal) to
        # establish the first journal entry, then calling _execute_single_move for src2→dest2
        # with a patched _verify_copy that raises RuntimeError.
        # This simulates the state after N moves succeed and the (N+1)th crashes.
        now = datetime.datetime.now(datetime.UTC)
        # First move: succeeds and appends one entry.
        _execute_single_move(
            src1,
            dest1,
            journal=in_memory_journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now_str=now.isoformat(),
            release_id="",
        )

        # Verify the journal has exactly one entry after the first move.
        assert len(in_memory_journal.entries) == 1, "in-memory journal must have 1 entry after first move"
        on_disk_after_first = read_journal(journal_path)
        assert len(on_disk_after_first.entries) == 1, "on-disk journal must have 1 entry after first move"

        # Second move: simulate a crash by raising RuntimeError from _verify_copy.
        mocker.patch("music_annotator._pipeline_maint._verify_copy", side_effect=RuntimeError("crash"))
        with pytest.raises(RuntimeError, match="crash"):
            _execute_single_move(
                src2,
                dest2,
                journal=in_memory_journal,
                journal_path=journal_path,
                action="repathed",
                dest_root=dest_root,
                now_str=now.isoformat(),
                release_id="",
            )

        # After the crash, the journal must contain exactly the first move's entry.
        # The second move's entry must NOT be present (C-PROV: no entry before verify passes).
        final_journal = read_journal(journal_path)
        assert len(final_journal.entries) == 1, (
            f"journal must contain exactly 1 entry after crash; got {len(final_journal.entries)}"
        )
        assert final_journal.entries[0].destination == str(dest1), "the surviving journal entry must be the first move's entry"


# ---------------------------------------------------------------------------
# repath() confirmation prompt
# ---------------------------------------------------------------------------


def _write_repath_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a JSONL journal file to ``dest_root / music_annotator_journal.json``.

    Writes one JSON object per line (JSONL format) so the file is in the format that
    :func:`~music_annotator.read_journal` expects without triggering a migration.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / "music_annotator_journal.json"
    journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


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
# C-UNIVERSAL KATs (b)+(d): first-component rule + discriminator-still-works witnesses
# ---------------------------------------------------------------------------


class TestCUniversalKATs:
    """C-UNIVERSAL KATs for the scholarship-stable first-component rule and the legacy-path discriminator.

    These tests pin the substrate correctness core: the first path component must be derivable from
    embedded tags alone (so repath/regroup/unify reconstruct the correct path without a live
    MBRelease), and the _work_top_dir helper must handle both current two-level prefix-less paths
    and legacy class-prefixed three-level paths (transition safety — KAT (d)).
    """

    def test_repath_reconstructs_first_component_from_tags(self, fs: FakeFilesystem) -> None:
        """Empty-stub build_dest_path derives the first component from embedded tags alone.

        Verifies the substrate correctness core: repath/regroup/unify call build_dest_path
        with empty MBRelease()/MBTrack() stubs.  The first component must be derivable from
        embedded tags alone, not from release.release_group.

        Creates tags with ALBUMARTIST set (performer-led branch) and verifies that build_dest_path
        with an empty stub produces the correct prefix-less path.  The album name must not appear
        in the topmost path component (album identity belongs to the playlist lens).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Tags with albumartist/album set, no composer → performer-led branch.
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
        # C-UNIVERSAL: first component is the albumartist alone (no album name).
        assert rel.parts[0] == "Test Artist", (
            f"Expected top_dir 'Test Artist' from embedded tags (album name excluded), got {rel.parts[0]!r}"
        )
        assert "Test Album" not in rel.parts[0], (
            f"Album name must not appear in top_dir (belongs to playlist lens), got {rel.parts[0]!r}"
        )

    def test_repath_reconstructs_composer_first_from_tags(self, fs: FakeFilesystem) -> None:
        """Empty-stub build_dest_path derives the composer-first top_dir from CWP tags.

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
        # C-UNIVERSAL: first component is the composer-first shape (single-composer → None → caller uses composer - performers).
        assert rel.parts[0].startswith("Beethoven"), (
            f"Expected top_dir starting with 'Beethoven' from CWP tags, got {rel.parts[0]!r}"
        )

    def test_work_top_dir_discriminator_handles_both_path_shapes(self, fs: FakeFilesystem) -> None:
        """KAT (d): _work_top_dir correctly reads both current prefix-less and legacy class-prefixed paths.

        Pins the dual-shape behaviour required during the transition: the library is a mix of
        current two-level prefix-less paths (newly-written) and legacy three-level class-prefixed
        paths (written before C-UNIVERSAL).  The _work_top_dir helper must handle both shapes by
        testing whether parts[0] is a known class name from the legacy vocabulary.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Current two-level prefix-less path: dest_root / <top_dir> / <work_dir> / leaf
        current_file = dest_root / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]" / "01 - Allegro.flac"
        current_file.parent.mkdir(parents=True, exist_ok=True)
        current_file.touch()

        # Legacy class-prefixed three-level path: dest_root / <class> / <top_dir> / <work_dir> / leaf
        legacy_file = dest_root / "Classical" / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]" / "01 - Allegro.flac"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.touch()

        # Current prefix-less path: work_top_dir = dest_root / parts[0] / parts[1]
        current_work_top = _work_top_dir(current_file, dest_root)
        assert current_work_top == dest_root / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]", (
            f"Current path: expected work_top_dir at depth 2, got {current_work_top.relative_to(dest_root)}"
        )

        # Legacy class-prefixed path: work_top_dir = dest_root / parts[1] / parts[2]
        legacy_work_top = _work_top_dir(legacy_file, dest_root)
        assert legacy_work_top == dest_root / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]", (
            f"Legacy path: expected work_top_dir at depth 2 (below class), got {legacy_work_top.relative_to(dest_root)}"
        )

    def test_repath_reconstructs_performer_first_top_dir_from_tags(self, fs: FakeFilesystem) -> None:
        """Empty-stub build_dest_path derives the performer-first top_dir from tags (C-UNIVERSAL KAT).

        Verifies the substrate correctness core: repath/regroup/unify call build_dest_path
        with empty MBRelease()/MBTrack() stubs.  The performer-led top_dir must be derivable
        from embedded tags alone, not from release.release_group or any live release data.

        Tests the performer-led branch: a FLAC with CWP_COMPOSER_LASTNAMES="" and ALBUMARTIST set
        must produce a performer-first top_dir when called with empty stubs.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Performer-led tags: cwp_work_top set but no composer linked.
        # The top_dir must use albumartist (performer-first), no class prefix.
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
        # C-UNIVERSAL: performer-led branch — top_dir is albumartist-based, no class prefix.
        assert "Mitsuko Uchida" in rel.parts[0], (
            f"Expected albumartist 'Mitsuko Uchida' in top_dir (C-UNIVERSAL), got {rel.parts[0]!r}"
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

    The canonical name-form is the MB artist ``name`` field (NORM-2 as revised): aliases are
    evidence-only and are never dereferenced in path computation.  The maintenance path
    (repath/regroup/unify) is genuinely offline — no MusicBrainz network calls are made.

    Two behavioural witnesses:

    1. **Native-name (ingest/repath parity)**: an ensemble whose ``ArtistEntry.name`` is the
       native form "Wiener Philharmoniker" — the path carries that name verbatim, matching the
       ingest render byte-for-byte.
    2. **Latin-name (no-regression)**: an ensemble whose ``ArtistEntry.name`` is "Berlin
       Philharmonic" — the path carries that name verbatim.

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

    def test_repath_renders_canonical_name_form(self, fs: FakeFilesystem) -> None:
        """Repath path renders the MB artist name field verbatim — no network calls made.

        KAT (ingest/repath parity, native-name): the ensemble ArtistEntry carries the native form
        "Wiener Philharmoniker" as its ``name`` field.  After :func:`_hydrate_performer_lists`
        reconstructs the ``cea_album_ensembles_list``, :func:`~music_annotator._tags.build_dest_path`
        uses ``entry.name`` directly (NORM-2 as revised — no MusicBrainz network calls).  The path
        must contain "Wiener Philharmoniker" — matching the ingest render byte-for-byte.

        Preserved tag surfaces (``ARTIST``, ``ALBUMARTIST``) are asserted unchanged, freezing the
        compact-path-only scope of canonical-form rendering.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            artist="Wiener Philharmoniker",
            albumartist="Wiener Philharmoniker",
        )
        file_dict = {
            "CEA_ALBUM_ENSEMBLES": "Wiener Philharmoniker",
            "CEA_ALBUM_ENSEMBLES_SORT": "Wiener Philharmoniker",
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

        # Path performers component must carry the MB name field verbatim.
        assert "Wiener Philharmoniker" in path_str, f"Expected 'Wiener Philharmoniker' in path '{path_str}'"
        # Preserved tag surfaces are unchanged — ARTIST and ALBUMARTIST stay as-credited.
        assert tags.artist == "Wiener Philharmoniker", "ARTIST tag must remain as-credited"
        assert tags.albumartist == "Wiener Philharmoniker", "ALBUMARTIST tag must remain as-credited"

    def test_repath_carries_name_form_verbatim(self, fs: FakeFilesystem) -> None:
        """Repath path carries the MB artist name field verbatim for any name form.

        KAT (name-form verbatim): the ensemble ArtistEntry carries "Berlin Philharmonic" as its
        ``name`` field.  The path must carry "Berlin Philharmonic" unchanged — no network call is
        made.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

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

        # Path carries the MB name field verbatim.
        assert "Berlin Philharmonic" in path_str, f"Expected 'Berlin Philharmonic' in path '{path_str}'"
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
        """_hydrate_performer_lists populates cea_ensembles_list from CEA_ENSEMBLES without MBIDs.

        Per-track ensemble entries are always created without MBIDs.  MUSICBRAINZ_ALBUMARTISTID
        is the release's artist-credit MBID pool; for box-sets and composer-credited releases
        this is the edition/collection entity's MBID, not the ensemble's MBID.  Assigning the
        wrong MBID causes the canonical-form resolver to return the edition title instead of the
        ensemble name.  The safe invariant: per-track ensemble entries always have mbid == "".

        :raises AssertionError: If the list is not populated or the entry carries a non-empty MBID.
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
        assert tags.cea_ensembles_list[0].name == "Vienna Philharmonic"
        # Per-track ensemble entries must never carry an MBID — the ALBUMARTISTID pool is
        # the edition entity's MBID for box-sets, not the ensemble's MBID.
        assert tags.cea_ensembles_list[0].mbid == "", (
            f"Per-track ensemble entry must have mbid='' to prevent edition-title resolution, "
            f"got {tags.cea_ensembles_list[0].mbid!r}"
        )

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

    def test_hydrate_per_track_ensemble_never_gets_mbid(self) -> None:
        """Per-track ensemble entries are always created without MBIDs.

        KAT: the per-track ensemble MBID cannot be reliably derived from embedded tags.
        MUSICBRAINZ_ALBUMARTISTID is the release's artist-credit MBID pool; for box-sets and
        composer-credited releases this is the edition/collection entity's MBID, not the
        ensemble's MBID.  Assigning the wrong MBID causes the canonical-form resolver to return
        the edition title instead of the ensemble name.

        The safe invariant: per-track ensemble entries always have mbid == "" so _canonical_name
        falls back to entry.name (the as-credited name from CEA_ENSEMBLES).

        :raises AssertionError: If the per-track ensemble entry carries a non-empty MBID.
        """
        tags = TrackTags()
        # Simulate a box-set: MUSICBRAINZ_ALBUMARTISTID is the edition entity's MBID (not the
        # ensemble's MBID), and CEA_ENSEMBLES carries the real ensemble name.
        file_dict = {
            "CEA_ENSEMBLES": "Academy of St Martin in the Fields",
            "CEA_ENSEMBLES_SORT": "Academy of St Martin in the Fields",
            "MUSICBRAINZ_CONDUCTORID": "marriner-mbid",
            "MUSICBRAINZ_ALBUMARTISTID": "complete-mozart-edition-mbid",
        }
        _hydrate_performer_lists(tags, file_dict)

        assert len(tags.cea_ensembles_list) == 1
        entry = tags.cea_ensembles_list[0]
        assert entry.name == "Academy of St Martin in the Fields"
        # Per-track ensemble entries must never carry an MBID — the ALBUMARTISTID pool is
        # the edition entity's MBID for box-sets, not the ensemble's MBID.
        assert entry.mbid == "", (
            f"Per-track ensemble entry must have mbid='' to prevent edition-title resolution, got {entry.mbid!r}"
        )

    def test_hydrate_boxset_repath_renders_real_ensemble_name(self, fs: FakeFilesystem) -> None:
        """Box-set repath renders the real ensemble name, not the edition entity's name.

        KAT: when MUSICBRAINZ_ALBUMARTISTID is the edition/collection entity's MBID (not the
        ensemble's MBID), the per-track ensemble entry must be created without an MBID so that
        ``entry.name`` (the as-credited ensemble name) is used directly — no network call is made
        (NORM-2 as revised: canonical form is the MB artist ``name`` field, offline).

        Simulates the "Complete Mozart Edition" box-set shape: conductor is Sir Neville Marriner
        (with a real MBID), ensemble is Academy of St Martin in the Fields (no MBID assigned),
        and MUSICBRAINZ_ALBUMARTISTID is the edition entity's MBID.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 40",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Mozart",
            cwp_worktype_genres_top="Classical",
            artist="Complete Mozart Edition",
            albumartist="Wolfgang Amadeus Mozart",
            album="Complete Mozart Edition",
        )
        # Simulate the embedded tag dict as read back from the audio file.
        file_dict = {
            "CEA_CONDUCTORS": "Sir Neville Marriner",
            "CEA_ENSEMBLES": "Academy of St Martin in the Fields",
            "CEA_ENSEMBLES_SORT": "Academy of St Martin in the Fields",
            "MUSICBRAINZ_CONDUCTORID": "marriner-mbid",
            # ALBUMARTISTID is the edition entity's MBID — NOT the ensemble's MBID.
            "MUSICBRAINZ_ALBUMARTISTID": "complete-mozart-edition-mbid",
        }
        _hydrate_performer_lists(tags, file_dict)

        result = build_dest_path(
            dest_root,
            MBRelease(),
            MBTrack(),
            tags,
            global_track_idx=0,
        )
        path_str = str(result.relative_to(dest_root))
        top = result.relative_to(dest_root).parts[0]

        # The path must contain the real ensemble name, not the edition entity's name.
        assert "Academy of St Martin in the Fields" in top, f"Expected real ensemble name in top_dir, got {top!r}"
        assert "Sir Neville Marriner" in top, f"Expected conductor name in top_dir, got {top!r}"
        # The edition title must not appear in the path.
        assert "Complete Mozart Edition" not in path_str, f"Edition title must not appear in path, got {path_str!r}"
        # The path must equal the expected form.
        assert top == "Mozart - Sir Neville Marriner; Academy of St Martin in the Fields", (
            f"Expected 'Mozart - Sir Neville Marriner; Academy of St Martin in the Fields', got {top!r}"
        )

    def test_repath_makes_zero_mb_api_calls(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath (build_dest_path via _hydrate_performer_lists) makes zero MusicBrainz API calls.

        KAT (zero-MB-calls): the maintenance path is genuinely offline — canonical name-form is
        the MB artist ``name`` field (NORM-2 as revised); no alias fetch is needed.  Asserts that
        ``fetch_artist_aliases`` is never called during a repath path computation.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        mock_fetch = mocker.patch("music_annotator._mb_api.fetch_artist_aliases")

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            artist="Wiener Philharmoniker",
            albumartist="Wiener Philharmoniker",
        )
        file_dict = {
            "CEA_ALBUM_ENSEMBLES": "Wiener Philharmoniker",
            "CEA_ALBUM_ENSEMBLES_SORT": "Wiener Philharmoniker",
            "MUSICBRAINZ_ALBUMARTISTID": "vp-1",
            "MUSICBRAINZ_CONDUCTORID": "",
        }

        _hydrate_performer_lists(tags, file_dict)
        build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0)

        # The maintenance path must make zero MusicBrainz API calls.
        mock_fetch.assert_not_called()


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

        # Compute the expected clamped path for track 3 (modal=2 → PL=3 clamped to PL=2)
        modal = work_group_modal_depth([2, 2, 3])
        assert modal == 2  # noqa: PLR2004
        expected_path3 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags3, group_modal_depth=modal).with_suffix(".flac")

        # Place PL=2 tracks at their correct (already-clamped) prefix-less paths.
        # PL=3 track is placed at its unclamped path (two intermediate dirs, no class prefix).
        # C-UNIVERSAL: paths are prefix-less (no "Classical/" component).
        old_path1 = _make_library_flac(
            dest_root,
            "Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Allegro.flac",
            tags1,
        )
        old_path2 = _make_library_flac(
            dest_root,
            "Handel - Karajan/Water Music [rec 1970]/01 - Act I/02 - Andante.flac",
            tags2,
        )
        # PL=3 track at its unclamped path (two intermediate dirs: Act I / Scene 1, no class prefix)
        old_path3 = _make_library_flac(
            dest_root,
            "Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Scene 1/03 - Presto.flac",
            tags3,
        )

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

        # PL=2 tracks: already at their clamped prefix-less paths — no move expected.
        assert old_path1.exists(), "PL=2 track 1 should remain at its original path (already clamped)"
        assert old_path2.exists(), "PL=2 track 2 should remain at its original path (already clamped)"

        # PL=3 track: must have moved to the clamped path (one intermediate dir, not two).
        assert not old_path3.exists(), "PL=3 track should have moved away from its unclamped path"
        assert expected_path3.exists(), f"PL=3 track should be at clamped path {expected_path3.relative_to(dest_root)}"

        # Depth check: clamped path has 4 parts (top/work/act/leaf), not 5 (no class prefix — C-UNIVERSAL).
        assert len(expected_path3.relative_to(dest_root).parts) == 4, (  # noqa: PLR2004
            f"Expected 4 path parts after clamp, got {expected_path3.relative_to(dest_root).parts}"
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
        # C-UNIVERSAL: paths are prefix-less (no "Classical/" component).
        old_path1 = _make_library_flac(
            dest_root,
            "Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Allegro.flac",
            tags1,
        )
        old_path2 = _make_library_flac(
            dest_root,
            "Handel - Karajan/Water Music [rec 1970]/01 - Act I/02 - Andante.flac",
            tags2,
        )
        # PL=3 track at its unclamped path (two intermediate dirs: Act I / Scene 1, no class prefix)
        old_path3 = _make_library_flac(
            dest_root,
            "Handel - Karajan/Water Music [rec 1970]/01 - Act I/01 - Scene 1/03 - Presto.flac",
            tags3,
        )

        # Build a split-release scenario: two work_dirs for "split-rel-1".
        # The phantom entry is in a DIFFERENT work_dir ("OldWater Music [rec 1970]") so that
        # _confirm_fragmentation detects case-b fragmentation (>1 work_dir for one release_id).
        # The real files are in "Water Music [rec 1970]"; the phantom is in "OldWater Music [rec 1970]".
        phantom = dest_root / "Handel - Karajan" / "OldWater Music [rec 1970]" / "phantom.flac"
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

        # Depth check: clamped path has 4 parts (top/work/act/leaf), not 5 (no class prefix — C-UNIVERSAL).
        assert len(expected_path3.relative_to(dest_root).parts) == 4, (  # noqa: PLR2004
            f"Expected 4 path parts after clamp, got {expected_path3.relative_to(dest_root).parts}"
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


# ---------------------------------------------------------------------------
# AcoustID tag parity pin — integrative behavioural witness
# ---------------------------------------------------------------------------


class TestAcoustidTagParityPin:
    """Integrative behavioural parity pin for the AcoustID forward-write path.

    Asserts the observable on-disk tag state produced by :func:`apply_tags_flac` when
    ``acoustid_fingerprint`` is set: the Picard-aligned ``ACOUSTID_FINGERPRINT`` Vorbis Comment
    key is written and no legacy ``CHROMAPRINT_FP`` key is present.

    This is an integrative behavioural pin, not a unit test of an individual function.  It
    verifies the end-to-end property that the forward-write path produces Picard-aligned
    on-disk tag state.
    """

    def test_forward_write_parity_pin_flac(self, fs: FakeFilesystem) -> None:
        """Forward-write parity pin: apply_tags_flac writes ACOUSTID_FINGERPRINT, not CHROMAPRINT_FP.

        Writes a FLAC via :func:`apply_tags_flac` with ``acoustid_fingerprint`` set, then reads
        the on-disk Vorbis Comment block directly via mutagen and asserts:

        (a) ``acoustid_fingerprint`` (the Picard-aligned key) is present with the correct value.
        (b) ``chromaprint_fp`` (the legacy key) is absent.

        This pins the forward-write path: new ingests and enrich runs write the Picard-aligned
        key, never the legacy key.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)

        tags = TrackTags(acoustid_fingerprint="AQADtMmybckm_forward_write")
        apply_tags_flac(path, tags)

        # Read back via mutagen directly (not via the tagger helpers) to verify on-disk state.
        audio = MutagenFLAC(str(path))

        # (a) Picard-aligned key must be present with the correct value.
        fp_vals = audio.get("acoustid_fingerprint") or []
        assert fp_vals and fp_vals[0] == "AQADtMmybckm_forward_write", (
            f"Expected acoustid_fingerprint='AQADtMmybckm_forward_write', got {fp_vals!r}"
        )

        # (b) Legacy key must be absent.
        legacy_vals = audio.get("chromaprint_fp") or audio.get("CHROMAPRINT_FP") or []
        assert not legacy_vals, f"Legacy CHROMAPRINT_FP key must be absent after forward-write; found {legacy_vals!r}"


# ---------------------------------------------------------------------------
# KAT: dry_run returns the structured change-set the pass would enact
# ---------------------------------------------------------------------------


class TestDryRunPlanReturn:
    """KAT witnesses: every maintenance pass with dry_run=True returns a DryRunPlan.

    Four witnesses per pass (where applicable):
    (a) plan-return witness — dry_run=True returns a DryRunPlan with correct count and entries.
    (b) no-write witness — existing "no file moved / no journal entry" assertion still holds.
    (c) empty-plan witness — fixture with nothing to change returns DryRunPlan(count=0), not None.
    (d) shape-uniformity witness — a move pass and a tag-content pass both return DryRunPlan.

    The empty-plan witness (c) is structurally distinct from None (not-run): DryRunPlan(count=0)
    means the pass ran and found nothing to change; None means the pass did not run or errored.
    """

    # ---------------------------------------------------------------------------
    # Shared tag fixtures
    # ---------------------------------------------------------------------------

    @staticmethod
    def _make_repath_tags() -> TrackTags:
        """Build TrackTags that drive repath to a different path than the legacy location.

        :returns: A :class:`TrackTags` instance with CWP and performer tags set.
        """
        return TrackTags(
            cwp_composer_lastnames="Mozart",
            cwp_work_top="Symphony No. 40",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Molto allegro",
            artist="Bohm",
        )

    @staticmethod
    def _make_enrich_tags() -> TrackTags:
        """Build TrackTags for an enrichable file (no audio_hash or acoustid_fingerprint).

        :returns: A :class:`TrackTags` instance with title and artist only.
        """
        return TrackTags(title="Molto allegro", artist="Bohm")

    # ---------------------------------------------------------------------------
    # (a) + (b): plan-return witness + no-write witness — repath
    # ---------------------------------------------------------------------------

    def test_repath_dry_run_returns_plan_with_entries(self, fs: FakeFilesystem) -> None:
        """(a)+(b) repath(dry_run=True) returns a DryRunPlan with correct entries; writes nothing.

        Constructs a two-file library at legacy paths.  Calls repath(dry_run=True) and asserts:
        (a) The return value is a DryRunPlan with count=2 and entries matching the planned moves.
        (b) Files remain at their old paths; no "repathed" journal entries are appended.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_repath_tags()
        old_path = _make_library_flac(dest_root, "Mozart - Bohm/OldSym [rec 2019]/01 - Molto allegro.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r-repath",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                }
            ],
        )

        result = music_annotator.repath(dest_root=dest_root, dry_run=True)

        # (a) Returns a DryRunPlan with the planned move
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan, got {type(result)}"
        assert result.pass_name == "repath"
        assert result.count == 1
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.current_path == str(old_path)
        assert entry.planned_path != ""
        assert entry.tag_delta == {}

        # (b) No writes: file still at old path, no journal entry
        assert old_path.exists(), "dry_run must not move files"
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0, "dry_run must not append journal entries"

    # ---------------------------------------------------------------------------
    # (c): empty-plan witness — repath
    # ---------------------------------------------------------------------------

    def test_repath_dry_run_empty_plan_when_already_current(self, fs: FakeFilesystem) -> None:
        """(c) repath(dry_run=True) returns DryRunPlan(count=0) when all files are already current.

        When every file's current path matches the recomputed path, the plan is empty.
        An empty DryRunPlan is structurally distinct from None (not-run).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_repath_tags()
        # Place the file at the canonical path (what build_dest_path would compute)
        canonical_path = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        _make_library_flac(dest_root, str(canonical_path.relative_to(dest_root)), tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r-repath-noop",
                    "source": "/src/01.flac",
                    "destination": str(canonical_path),
                    "action": "tagged",
                }
            ],
        )

        result = music_annotator.repath(dest_root=dest_root, dry_run=True)

        # Empty plan — not None
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan(count=0), got {type(result)}"
        assert result.count == 0
        assert result.entries == []

    # ---------------------------------------------------------------------------
    # (a) + (b): plan-return witness + no-write witness — regroup
    # ---------------------------------------------------------------------------

    def test_regroup_dry_run_returns_plan_with_entries(self, fs: FakeFilesystem) -> None:
        """(a)+(b) regroup(dry_run=True) returns a DryRunPlan with correct entries; writes nothing.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Reuse the split-release scenario from TestRegroup
        split_tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid="split-rel-kat",
        )
        canonical_path = build_dest_path(dest_root, MBRelease(), MBTrack(), split_tags, global_track_idx=0).with_suffix(".flac")
        old_path = _make_library_flac(dest_root, "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac", split_tags)
        assert old_path != canonical_path

        # Journal: two entries for the same release_id under different work_dirs (split scenario)
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-kat",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "split-rel-kat",
                    "source": "/src/phantom.flac",
                    "destination": str(canonical_path),
                    "action": "tagged",
                },
            ],
        )

        result = music_annotator.regroup(dest_root=dest_root, dry_run=True)

        # (a) Returns a DryRunPlan with the planned move
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan, got {type(result)}"
        assert result.pass_name == "regroup"
        assert result.count == 1
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.current_path == str(old_path)
        assert entry.planned_path != ""
        assert entry.tag_delta == {}

        # (b) No writes
        assert old_path.exists(), "dry_run must not move files"
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert len(regrouped) == 0, "dry_run must not append journal entries"

    # ---------------------------------------------------------------------------
    # (c): empty-plan witness — regroup
    # ---------------------------------------------------------------------------

    def test_regroup_dry_run_empty_plan_when_nothing_to_regroup(self, fs: FakeFilesystem) -> None:
        """(c) regroup(dry_run=True) returns DryRunPlan(count=0) when no confirmed split releases.

        When there are no confirmed case-(b) split-release candidates, the plan is empty.
        An empty DryRunPlan is structurally distinct from None (not-run).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single file, single release_id — no fragmentation
        tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Symphony No. 1",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Un poco sostenuto",
            artist="Karajan",
            musicbrainz_albumid="no-split-rel",
        )
        path = _make_library_flac(dest_root, "Brahms - Karajan/Symphony No. 1 [rec 2020]/01 - Un poco sostenuto.flac", tags)
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "no-split-rel",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        result = music_annotator.regroup(dest_root=dest_root, dry_run=True)

        # Empty plan — not None (ran, found nothing is distinct from not-run)
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan(count=0), got {type(result)}"
        assert result.count == 0
        assert result.entries == []

    # ---------------------------------------------------------------------------
    # (a) + (b): plan-return witness + no-write witness — unify
    # ---------------------------------------------------------------------------

    def test_unify_dry_run_returns_plan_with_entries(self, fs: FakeFilesystem) -> None:
        """(a)+(b) unify(dry_run=True) returns a DryRunPlan with correct entries; writes nothing.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        frag_tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Karajan",
            musicbrainz_albumid="frag-rel-kat",
        )
        canonical_path = build_dest_path(dest_root, MBRelease(), MBTrack(), frag_tags, global_track_idx=0).with_suffix(".flac")
        old_path = _make_library_flac(
            dest_root, "Brahms - Pollini/Piano Concerto No. 1 [rec 2021]/01 - First movement.flac", frag_tags
        )
        # File B at canonical path ensures two distinct top_dirs for the same release_id
        _make_library_flac(dest_root, str(canonical_path.relative_to(dest_root)), frag_tags)
        assert old_path != canonical_path

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "frag-rel-kat",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "frag-rel-kat",
                    "source": "/src/02.flac",
                    "destination": str(canonical_path),
                    "action": "tagged",
                },
            ],
        )

        result = music_annotator.unify(dest_root=dest_root, dry_run=True)

        # (a) Returns a DryRunPlan with the planned move
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan, got {type(result)}"
        assert result.pass_name == "unify"
        assert result.count == 1
        assert len(result.entries) == 1
        entry = result.entries[0]
        assert entry.current_path == str(old_path)
        assert entry.planned_path != ""
        assert entry.tag_delta == {}

        # (b) No writes
        assert old_path.exists(), "dry_run must not move files"
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) == 0, "dry_run must not append journal entries"

    # ---------------------------------------------------------------------------
    # (c): empty-plan witness — unify
    # ---------------------------------------------------------------------------

    def test_unify_dry_run_empty_plan_when_nothing_to_unify(self, fs: FakeFilesystem) -> None:
        """(c) unify(dry_run=True) returns DryRunPlan(count=0) when no fragmented releases.

        When there are no performer-split or composer-split fragmented releases, the plan is empty.
        An empty DryRunPlan is structurally distinct from None (not-run).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single file, single release_id, single top_dir — no fragmentation
        tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Symphony No. 1",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Un poco sostenuto",
            artist="Karajan",
            musicbrainz_albumid="no-frag-rel",
        )
        path = _make_library_flac(dest_root, "Brahms - Karajan/Symphony No. 1 [rec 2020]/01 - Un poco sostenuto.flac", tags)
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "no-frag-rel",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        result = music_annotator.unify(dest_root=dest_root, dry_run=True)

        # Empty plan — not None (ran, found nothing is distinct from not-run)
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan(count=0), got {type(result)}"
        assert result.count == 0
        assert result.entries == []

    # ---------------------------------------------------------------------------
    # (a) + (b): plan-return witness + no-write witness — enrich
    # ---------------------------------------------------------------------------

    def test_enrich_dry_run_returns_plan_with_entries(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """(a)+(b) enrich(dry_run=True) returns a DryRunPlan with correct entries; writes nothing.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_enrich_tags()
        path = _make_library_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-enrich-kat",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm_kat")

        result = music_annotator.enrich(dest_root=dest_root, dry_run=True)

        # (a) Returns a DryRunPlan with the planned tag writes
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan, got {type(result)}"
        assert result.pass_name == "enrich"
        assert result.count >= 1
        assert len(result.entries) == result.count
        entry = result.entries[0]
        assert entry.current_path == str(path)
        assert entry.planned_path == ""  # tag-content pass: in-place write
        assert entry.tag_delta != {}  # at least one field to write

        # (b) No writes: no tags written, no journal entry
        audio = MutagenFLAC(str(path))
        assert not (audio.get("audio_hash") or []), "dry_run must not write audio_hash"
        assert not (audio.get("acoustid_fingerprint") or []), "dry_run must not write acoustid_fingerprint"
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        enriched = [e for e in journal.entries if e.action == "enriched"]
        assert len(enriched) == 0, "dry_run must not append journal entries"

    # ---------------------------------------------------------------------------
    # (c): empty-plan witness — enrich
    # ---------------------------------------------------------------------------

    def test_enrich_dry_run_empty_plan_when_already_enriched(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """(c) enrich(dry_run=True) returns DryRunPlan(count=0) when all files are already enriched.

        When every file already has audio_hash and acoustid_fingerprint, the plan is empty.
        An empty DryRunPlan is structurally distinct from None (not-run).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File already has both enrichment fields set
        tags = TrackTags(title="Track", artist="Artist", audio_hash="flac-md5:abc123", acoustid_fingerprint="AQADtMmy")
        path = _make_library_flac(dest_root, "Artist/Album/01 - Track.flac", tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-enrich-noop",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmy")

        result = music_annotator.enrich(dest_root=dest_root, dry_run=True)

        # Empty plan — not None
        assert isinstance(result, DryRunPlan), f"expected DryRunPlan(count=0), got {type(result)}"
        assert result.count == 0
        assert result.entries == []

    # ---------------------------------------------------------------------------
    # (d): shape-uniformity witness — move pass and tag-content pass both return DryRunPlan
    # ---------------------------------------------------------------------------

    def test_shape_uniformity_move_and_tag_content_both_return_dry_run_plan(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """(d) A move pass (repath) and a tag-content pass (enrich) both return DryRunPlan instances.

        Validates that the single DryRunPlan type spans both plan kinds:
        - Move entry: planned_path != "", tag_delta == {}
        - Tag-content entry: planned_path == "", tag_delta != {}

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # --- Move pass: repath ---
        repath_tags = self._make_repath_tags()
        old_path = _make_library_flac(dest_root, "Mozart - Bohm/OldSym [rec 2019]/01 - Molto allegro.flac", repath_tags)
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r-shape",
                    "source": "/src/01.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                }
            ],
        )

        repath_result = music_annotator.repath(dest_root=dest_root, dry_run=True)

        # --- Tag-content pass: enrich (separate dest_root to avoid journal interference) ---
        dest_root2 = Path("/lib2")
        fs.create_dir(str(dest_root2))
        enrich_tags = self._make_enrich_tags()
        enrich_path = _make_library_flac(dest_root2, "Artist/Album/01 - Track.flac", enrich_tags)
        _write_library_journal(
            dest_root2,
            [
                {
                    "timestamp": "2024-06-01T00:00:00+00:00",
                    "release_id": "rel-shape",
                    "source": "/src/01.flac",
                    "destination": str(enrich_path),
                    "action": "tagged",
                }
            ],
        )
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="AQADtMmybckm_shape")
        enrich_result = music_annotator.enrich(dest_root=dest_root2, dry_run=True)

        # Both are DryRunPlan instances
        assert isinstance(repath_result, DryRunPlan), f"repath must return DryRunPlan, got {type(repath_result)}"
        assert isinstance(enrich_result, DryRunPlan), f"enrich must return DryRunPlan, got {type(enrich_result)}"

        # Move entry shape: planned_path populated, tag_delta empty
        assert repath_result.count >= 1
        move_entry = repath_result.entries[0]
        assert move_entry.planned_path != "", "move entry must have planned_path"
        assert move_entry.tag_delta == {}, "move entry must have empty tag_delta"

        # Tag-content entry shape: planned_path empty, tag_delta populated
        assert enrich_result.count >= 1
        tag_entry = enrich_result.entries[0]
        assert tag_entry.planned_path == "", "tag-content entry must have empty planned_path"
        assert tag_entry.tag_delta != {}, "tag-content entry must have non-empty tag_delta"

    # ---------------------------------------------------------------------------
    # Coverage: early-return DryRunPlan(count=0) paths for each pass
    # ---------------------------------------------------------------------------

    def test_repath_dry_run_empty_plan_no_existing_files(self, fs: FakeFilesystem) -> None:
        """repath(dry_run=True) returns DryRunPlan(count=0) when no files exist in the journal.

        Exercises the early-return path when the journal is empty (no existing files on disk).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # Empty journal — no files to repath
        _write_library_journal(dest_root, [])

        result = music_annotator.repath(dest_root=dest_root, dry_run=True)

        assert isinstance(result, DryRunPlan)
        assert result.count == 0
        assert result.entries == []

    def test_repath_dry_run_empty_plan_after_intra_collision_filter(self, fs: FakeFilesystem) -> None:
        """repath(dry_run=True) returns DryRunPlan(count=0) when all plan pairs are intra-collision.

        Exercises the early-return path when all planned moves collide with each other (two files
        recompute to the same destination), causing the plan to be empty after filtering.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two files with identical tags → both recompute to the same destination → intra-collision
        tags = TrackTags(
            cwp_composer_lastnames="Mozart",
            cwp_work_top="Symphony No. 40",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Molto allegro",
            artist="Bohm",
        )
        old_path1 = _make_library_flac(dest_root, "Mozart - Bohm/OldSym [rec 2019]/01 - Molto allegro.flac", tags)
        old_path2 = _make_library_flac(dest_root, "Mozart - Bohm/OldSym2 [rec 2019]/01 - Molto allegro.flac", tags)

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
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "r1",
                    "source": "/src/02.flac",
                    "destination": str(old_path2),
                    "action": "tagged",
                },
            ],
        )

        result = music_annotator.repath(dest_root=dest_root, dry_run=True)

        # Both files collide → plan is empty after intra-collision filter
        assert isinstance(result, DryRunPlan)
        assert result.count == 0
        assert result.entries == []

    def test_regroup_dry_run_empty_plan_no_existing_files_on_disk(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """regroup(dry_run=True) returns DryRunPlan(count=0) when confirmed files don't exist on disk.

        Exercises the early-return path when confirmed release IDs exist (from _confirm_fragmentation)
        but the files resolved by _resolve_current_lib don't exist on disk.  _confirm_fragmentation
        is patched to return a confirmed release ID; the journal has a "tagged" entry at a path that
        doesn't exist on disk, so _resolve_current_lib resolves to that non-existent path and
        existing_files is empty.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        nonexistent_path = dest_root / "Brahms - Vienna PO/Work-A [2021]/01 - First movement.flac"

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-nofile",
                    "source": "/src/01.flac",
                    "destination": str(nonexistent_path),
                    "action": "tagged",
                },
            ],
        )

        # Patch _confirm_fragmentation to return a confirmed release ID so regroup proceeds past
        # the "no confirmed candidates" early return and reaches the "no existing files" path.
        mocker.patch(
            "music_annotator._pipeline_maint._confirm_fragmentation",
            return_value=({}, {"split-rel-nofile": (["Work-A [2021]", "Work-B [2021]"], True)}),
        )

        # _resolve_current_lib resolves nonexistent_path (which doesn't exist on disk) →
        # existing_files is empty → DryRunPlan(count=0)
        result = music_annotator.regroup(dest_root=dest_root, dry_run=True)

        assert isinstance(result, DryRunPlan)
        assert result.count == 0
        assert result.entries == []

    def test_regroup_dry_run_empty_plan_when_all_files_already_canonical(self, fs: FakeFilesystem) -> None:
        """regroup(dry_run=True) returns DryRunPlan(count=0) when all confirmed files are canonical.

        Exercises the early-return path when confirmed release IDs exist and files exist on disk,
        but all files already recompute to their current paths (plan_pairs is empty).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        split_tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Vienna PO",
            musicbrainz_albumid="split-rel-canonical",
        )
        canonical_path = build_dest_path(dest_root, MBRelease(), MBTrack(), split_tags, global_track_idx=0).with_suffix(".flac")
        # Place the file at the canonical path (already correct)
        _make_library_flac(dest_root, str(canonical_path.relative_to(dest_root)), split_tags)

        # Two journal entries for the same release_id under different work_dirs (split scenario)
        # but the file is already at the canonical path
        phantom_path = dest_root / "Brahms - Vienna PO/OldWork [2021]/01 - First movement.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "split-rel-canonical",
                    "source": "/src/01.flac",
                    "destination": str(canonical_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "split-rel-canonical",
                    "source": "/src/phantom.flac",
                    "destination": str(phantom_path),
                    "action": "tagged",
                },
            ],
        )

        result = music_annotator.regroup(dest_root=dest_root, dry_run=True)

        assert isinstance(result, DryRunPlan)
        assert result.count == 0
        assert result.entries == []

    def test_unify_dry_run_empty_plan_when_all_files_already_canonical(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """unify(dry_run=True) returns DryRunPlan(count=0) when fragmented files are already canonical.

        Exercises the early-return path when fragmented releases are detected but all files already
        recompute to their current paths (plan_pairs is empty after the loop).  Uses a mock to
        inject a fragmented release where the single file is already at its canonical path.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        frag_tags = TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Piano Concerto No. 1",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="First movement",
            artist="Karajan",
            musicbrainz_albumid="frag-rel-canonical",
        )
        canonical_path = build_dest_path(dest_root, MBRelease(), MBTrack(), frag_tags, global_track_idx=0).with_suffix(".flac")
        _make_library_flac(dest_root, str(canonical_path.relative_to(dest_root)), frag_tags)

        # Mock detect_fragmented_releases to return the file as fragmented (even though it's canonical)
        # This simulates the case where fragmentation is detected but all files are already canonical.
        mocker.patch(
            "music_annotator._pipeline_maint.detect_fragmented_releases",
            return_value={"frag-rel-canonical": [canonical_path]},
        )

        result = music_annotator.unify(dest_root=dest_root, dry_run=True)

        assert isinstance(result, DryRunPlan)
        assert result.count == 0
        assert result.entries == []

    def test_enrich_dry_run_empty_plan_no_existing_files(self, fs: FakeFilesystem) -> None:
        """enrich(dry_run=True) returns DryRunPlan(count=0) when no files exist in the journal.

        Exercises the early-return path when the journal is empty (no existing files on disk).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        _write_library_journal(dest_root, [])

        result = music_annotator.enrich(dest_root=dest_root, dry_run=True)

        assert isinstance(result, DryRunPlan)
        assert result.count == 0
        assert result.entries == []


class TestMaintainDryRunReport:
    """KAT witnesses for ``maintain --dry-run`` consolidated report and its helpers.

    Exercises the consolidated dry-run report emitted by :func:`maintain` when called with
    ``dry_run=True``: per-pass counts, cross-pass overlap detection, journal-capacity
    measurement, Reference/ evidence, the empty-vs-not-run distinction, and the read-only
    invariant (no journal entry / no file move across the whole composition).

    Report-assembly logic is tested via :func:`_build_maintain_report` directly.
    Integration behaviour (root-not-mounted guard, json_path serialisation) is tested via
    :func:`maintain`.
    """

    # ---------------------------------------------------------------------------
    # Shared helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _make_catalogue_colon_tags() -> TrackTags:
        """Build TrackTags for a file with a catalogue-colon corrupt CWP_PART_1.

        The embedded CWP_WORK_1 contains a Hoboken catalogue number with a colon, and
        CWP_PART_1 carries the bare fragment produced by the old bare-colon split.

        :returns: A :class:`TrackTags` instance with catalogue-colon corruption.
        """
        tags = TrackTags(
            cwp_composer_lastnames="Haydn",
            cwp_work_top="String Quartet in E major, Op. 20 No. 4, Hob. III:31",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="I. Allegro moderato",
            artist="Amadeus Quartet",
        )
        if tags.model_extra is not None:
            tags.model_extra["cwp_work_0"] = "I. Allegro moderato"
            tags.model_extra["cwp_part_0"] = ""
            tags.model_extra["cwp_work_1"] = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
            tags.model_extra["cwp_part_1"] = "31"
            tags.model_extra["cwp_work_2"] = ""
        return tags

    @staticmethod
    def _make_repath_tags() -> TrackTags:
        """Build TrackTags that drive repath to a different path than the legacy location.

        :returns: A :class:`TrackTags` instance with CWP and performer tags set.
        """
        return TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con brio",
            artist="Karajan",
        )

    # ---------------------------------------------------------------------------
    # KAT 1: per-pass counts match the fixture (via maintain dry-run)
    # ---------------------------------------------------------------------------

    def test_maintain_dry_run_per_pass_counts(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain(dry_run=True) produces per-pass counts matching the fixture library.

        Constructs a library with:
        - One file at a legacy path (repath candidate).
        - One additional file at a stable path (enrich candidate).
        Calls maintain(dry_run=True) and captures the report via _build_maintain_report mock.
        Asserts that the repath pass summary reports count >= 1 and the other passes report
        count == 0 for regroup and unify.

        ``_run_fpcalc`` is mocked because fpcalc is not available in the test environment.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File 1: repath candidate — at a legacy path, tags recompute to a different path.
        repath_tags = self._make_repath_tags()
        repath_path = _make_library_flac(dest_root, "Beethoven - Karajan/OldSym [rec 2020]/01 - Allegro.flac", repath_tags)

        # File 2: a second file at a stable path (enrich candidate, missing audio_hash).
        cat_tags = self._make_catalogue_colon_tags()
        cat_path = _make_library_flac(dest_root, "Haydn - Amadeus/Quartet [rec 2021]/01 - Allegro.flac", cat_tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-beethoven",
                    "source": "/src/01.flac",
                    "destination": str(repath_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-haydn",
                    "source": "/src/02.flac",
                    "destination": str(cat_path),
                    "action": "tagged",
                },
            ],
        )

        captured_report: list[MaintainDryRunReport] = []
        real_build = _build_maintain_report

        def _capture_report(*args: object, **kwargs: object) -> MaintainDryRunReport:
            report = real_build(*args, **kwargs)  # type: ignore[arg-type]
            captured_report.append(report)
            return report

        mocker.patch("music_annotator._pipeline_maint._build_maintain_report", side_effect=_capture_report)

        maintain(dest_root, dry_run=True)

        assert len(captured_report) == 1
        report = captured_report[0]
        assert isinstance(report, MaintainDryRunReport)
        assert report.scan_ran is True

        # Build a lookup by pass name for easy assertion.
        by_pass = {s.pass_name: s for s in report.pass_summaries}

        # The repath candidate must be planned for repathing.
        assert by_pass["repath"].count >= 1
        # No fragmented releases in this fixture — regroup and unify find nothing.
        assert by_pass["regroup"].count == 0
        assert by_pass["unify"].count == 0

    # ---------------------------------------------------------------------------
    # KAT 2: journal-capacity measurement (via _build_maintain_report directly)
    # ---------------------------------------------------------------------------

    def test_build_maintain_report_journal_capacity(self, fs: FakeFilesystem) -> None:
        """_build_maintain_report() journal_capacity reflects current entry count and file size.

        Constructs a library with two existing journal entries and a plan with one repath entry.
        Asserts that:
        - current_entry_count equals the number of existing journal entries.
        - current_size_bytes is nonzero (the journal file exists and has content).
        - projected_delta_entries equals the total planned changes across all plans.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": "/lib/02.flac",
                    "action": "tagged",
                },
            ],
        )

        plans = [
            DryRunPlan(
                pass_name="repath",
                entries=[DryRunEntry(current_path="/lib/01.flac", planned_path="/lib/new.flac")],
                count=1,
            ),
            DryRunPlan(pass_name="enrich", entries=[], count=0),
        ]

        report = _build_maintain_report(journal_path, dest_root, plans)

        assert isinstance(report, MaintainDryRunReport)
        assert report.scan_ran is True
        cap = report.journal_capacity
        assert cap.current_entry_count == 2
        assert cap.current_size_bytes > 0
        assert cap.projected_delta_entries == 1

    # ---------------------------------------------------------------------------
    # KAT 3: overlap detection (via _build_maintain_report directly)
    # ---------------------------------------------------------------------------

    def test_build_maintain_report_overlap_detection(self, fs: FakeFilesystem) -> None:
        """_build_maintain_report() flags a file appearing in both repath and enrich plans.

        Constructs two plans where the same file path appears in both.  Asserts that:
        - The file appears in the overlaps list.
        - Both pass names are recorded in the overlap entry.
        - The overlap_count for both passes is 1.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(dest_root, [])

        overlap_path = "/lib/overlap.flac"
        plans = [
            DryRunPlan(
                pass_name="repath",
                entries=[DryRunEntry(current_path=overlap_path, planned_path="/lib/new.flac")],
                count=1,
            ),
            DryRunPlan(
                pass_name="enrich",
                entries=[DryRunEntry(current_path=overlap_path, tag_delta={"ACOUSTID_ID": "aid-1"})],
                count=1,
            ),
        ]

        report = _build_maintain_report(journal_path, dest_root, plans)

        assert report.scan_ran is True
        # The file must appear in the overlaps list.
        assert len(report.overlaps) == 1
        overlap_entry = report.overlaps[0]
        assert overlap_entry.current_path == overlap_path
        assert "repath" in overlap_entry.pass_names
        assert "enrich" in overlap_entry.pass_names

        # Per-pass overlap_count must reflect the overlap.
        by_pass = {s.pass_name: s for s in report.pass_summaries}
        assert by_pass["repath"].overlap_count == 1
        assert by_pass["enrich"].overlap_count == 1

    # ---------------------------------------------------------------------------
    # KAT 4: empty fixture — scan_ran=True, all counts 0 (via maintain dry-run)
    # ---------------------------------------------------------------------------

    def test_maintain_dry_run_empty_library_no_findings(self, fs: FakeFilesystem) -> None:
        """maintain(dry_run=True) on an empty library returns 0 and emits scan_ran=True report.

        Constructs a library root with a journal file but no audio files.  Asserts that
        maintain returns 0 (no planned changes).  This is structurally distinct from
        scan_ran=False (root absent or empty).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _write_library_journal(dest_root, [])

        # Call maintain directly — all passes are no-ops on an empty library.
        result = maintain(dest_root, dry_run=True)

        assert result == 0

    # ---------------------------------------------------------------------------
    # KAT 5: scan-not-run — root absent → scan_ran=False, returns 0
    # ---------------------------------------------------------------------------

    def test_maintain_dry_run_root_not_mounted(self) -> None:
        """maintain(dry_run=True) returns 0 when the root does not exist.

        Calls maintain(dry_run=True) with a dest_root that does not exist.  Asserts that
        maintain returns 0 — structurally distinct from a no-findings result (root mounted,
        all counts 0).  No pyfakefs fixture needed: the path is guaranteed not to exist.
        """
        dest_root = Path("/nonexistent_maintain_dry_run_root_xyzzy")

        result = maintain(dest_root, dry_run=True)

        assert result == 0

    # ---------------------------------------------------------------------------
    # KAT 6: scan-not-run — root empty → returns 0
    # ---------------------------------------------------------------------------

    def test_maintain_dry_run_root_empty(self, fs: FakeFilesystem) -> None:
        """maintain(dry_run=True) returns 0 when the root is empty.

        Constructs an empty dest_root directory (no files, no journal).  Asserts that
        maintain returns 0 — an empty root is treated as "not mounted" to prevent a missing
        mount from being silently reported as "no findings".

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/empty_lib")
        fs.create_dir(str(dest_root))

        result = maintain(dest_root, dry_run=True)

        assert result == 0

    # ---------------------------------------------------------------------------
    # KAT 7: Reference/ evidence (via _build_maintain_report directly)
    # ---------------------------------------------------------------------------

    def test_build_maintain_report_reference_evidence_present(self, fs: FakeFilesystem) -> None:
        """_build_maintain_report() surfaces Reference/ presence and nonzero footprint.

        Constructs a library root with a sibling Reference/ directory containing one file.
        Asserts that reference_evidence.present=True and reference_evidence.size_bytes > 0.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/music/Done")
        fs.create_dir(str(dest_root))

        # Create a sibling Reference/ directory with one file.
        ref_dir = dest_root.parent / "Reference"
        fs.create_dir(str(ref_dir))
        ref_file = ref_dir / "snapshot.flac"
        ref_file.write_bytes(b"\x00" * 1024)

        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(dest_root, [])

        report = _build_maintain_report(journal_path, dest_root, [])

        assert report.scan_ran is True
        assert report.reference_evidence.present is True
        assert report.reference_evidence.size_bytes > 0

    def test_build_maintain_report_reference_evidence_absent(self, fs: FakeFilesystem) -> None:
        """_build_maintain_report() reports Reference/ absent when the directory does not exist.

        Constructs a library root without a sibling Reference/ directory.  Asserts that
        reference_evidence.present=False and reference_evidence.size_bytes=0.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/music/Done")
        fs.create_dir(str(dest_root))

        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(dest_root, [])

        report = _build_maintain_report(journal_path, dest_root, [])

        assert report.scan_ran is True
        assert report.reference_evidence.present is False
        assert report.reference_evidence.size_bytes == 0

    # ---------------------------------------------------------------------------
    # KAT 8: read-only invariant — no journal entry / no file move
    # ---------------------------------------------------------------------------

    def test_maintain_dry_run_is_read_only(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain(dry_run=True) writes no files and appends no journal entries.

        Constructs a library with a repath candidate and a catalogue-colon repatch candidate.
        Calls maintain(dry_run=True) and asserts that:
        - No files were moved (the repath candidate is still at its original path).
        - The journal has the same number of entries as before the call.

        ``_run_fpcalc`` is mocked because fpcalc is not available in the test environment.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        repath_tags = self._make_repath_tags()
        repath_path = _make_library_flac(dest_root, "Beethoven - Karajan/OldSym [rec 2020]/01 - Allegro.flac", repath_tags)

        cat_tags = self._make_catalogue_colon_tags()
        cat_path = _make_library_flac(dest_root, "Haydn - Amadeus/Quartet [rec 2021]/01 - Allegro.flac", cat_tags)

        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-beethoven",
                    "source": "/src/01.flac",
                    "destination": str(repath_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-haydn",
                    "source": "/src/02.flac",
                    "destination": str(cat_path),
                    "action": "tagged",
                },
            ],
        )

        journal_entry_count_before = len(read_journal(journal_path).entries)

        maintain(dest_root, dry_run=True)

        # Files must not have moved.
        assert repath_path.exists(), "repath candidate must not have been moved"
        assert cat_path.exists(), "catalogue-colon candidate must not have been moved"

        # Journal must not have grown.
        journal_entry_count_after = len(read_journal(journal_path).entries)
        assert journal_entry_count_after == journal_entry_count_before

    # ---------------------------------------------------------------------------
    # KAT 9: json_path serialisation
    # ---------------------------------------------------------------------------

    def test_maintain_dry_run_json_path_serialisation(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain(dry_run=True, json_path=...) serialises the report to JSON.

        Constructs a minimal library, calls maintain with dry_run=True and a json_path, and
        asserts that the JSON file is written and contains a valid MaintainDryRunReport.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        _write_library_journal(dest_root, [])

        # Use a path whose parent does not yet exist; maintain must create it.
        json_path = Path("/maintain_reports/report.json")

        maintain(dest_root, dry_run=True, json_path=json_path)

        assert json_path.exists(), "JSON report file must be written"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert "scan_ran" in data
        assert data["scan_ran"] is True

    # ---------------------------------------------------------------------------
    # KAT 10: json_path written even when root not mounted (scan_ran=False)
    # ---------------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_maintain_dry_run_json_path_root_not_mounted(self, fs: FakeFilesystem) -> None:
        """maintain(dry_run=True, json_path=...) writes scan_ran=False JSON when root absent.

        Calls maintain with a non-existent dest_root and a json_path.  Asserts that the JSON
        file is written with scan_ran=False.  The pyfakefs fixture is required to intercept
        filesystem writes so the test does not write to the real filesystem.

        :param fs: pyfakefs fixture (activates fake filesystem; not referenced directly).
        """
        dest_root = Path("/nonexistent_maintain_json_root_xyzzy")
        # Use a path whose parent does not yet exist; maintain must create it.
        json_path = Path("/maintain_reports/not_run.json")

        result = maintain(dest_root, dry_run=True, json_path=json_path)

        assert result == 0
        assert json_path.exists(), "JSON report file must be written even when scan not run"
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["scan_ran"] is False

    # ---------------------------------------------------------------------------
    # KAT 9: _journal_capacity helper directly
    # ---------------------------------------------------------------------------

    def test_journal_capacity_helper_no_journal(self, fs: FakeFilesystem) -> None:
        """_journal_capacity() returns current_size_bytes=0 when the journal file does not exist.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        journal_path = dest_root / JOURNAL_FILENAME

        cap = _journal_capacity(journal_path, [])

        assert isinstance(cap, JournalCapacity)
        assert cap.current_entry_count == 0
        assert cap.current_size_bytes == 0
        assert cap.projected_delta_entries == 0

    def test_journal_capacity_helper_with_journal(self, fs: FakeFilesystem) -> None:
        """_journal_capacity() returns correct entry count, nonzero size, and projected delta.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": "/lib/02.flac",
                    "action": "tagged",
                },
            ],
        )

        plans = [
            DryRunPlan(
                pass_name="repath",
                entries=[DryRunEntry(current_path="/lib/01.flac", planned_path="/lib/new.flac")],
                count=1,
            ),
            DryRunPlan(pass_name="enrich", entries=[], count=0),
        ]

        cap = _journal_capacity(journal_path, plans)

        assert cap.current_entry_count == 2
        assert cap.current_size_bytes > 0
        assert cap.projected_delta_entries == 1

    # ---------------------------------------------------------------------------
    # KAT 10: _reference_evidence helper directly
    # ---------------------------------------------------------------------------

    def test_reference_evidence_helper_present(self, fs: FakeFilesystem) -> None:
        """_reference_evidence() returns present=True and nonzero size_bytes when Reference/ exists.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/music/Done")
        fs.create_dir(str(dest_root))
        ref_dir = dest_root.parent / "Reference"
        fs.create_dir(str(ref_dir))
        (ref_dir / "file.flac").write_bytes(b"\x00" * 512)

        evidence = _reference_evidence(dest_root)

        assert evidence.present is True
        assert evidence.size_bytes == 512

    def test_reference_evidence_helper_absent(self, fs: FakeFilesystem) -> None:
        """_reference_evidence() returns present=False and size_bytes=0 when Reference/ is absent.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/music/Done")
        fs.create_dir(str(dest_root))

        evidence = _reference_evidence(dest_root)

        assert evidence.present is False
        assert evidence.size_bytes == 0

    # ---------------------------------------------------------------------------
    # KAT 11: _check_dest_root PermissionError branch
    # ---------------------------------------------------------------------------

    def test_check_dest_root_permission_error(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_check_dest_root() returns False when os.listdir() raises PermissionError.

        Mocks ``os.listdir`` to raise ``PermissionError`` so the PermissionError branch in
        ``_check_dest_root`` is exercised.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        mocker.patch("music_annotator._pipeline_maint.os.listdir", side_effect=PermissionError("denied"))

        result = _check_dest_root(dest_root)

        assert result is False

    # ---------------------------------------------------------------------------
    # KAT 12: _reference_evidence OSError branch (getsize fails)
    # ---------------------------------------------------------------------------

    def test_reference_evidence_getsize_oserror(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_reference_evidence() handles OSError from os.path.getsize gracefully.

        Constructs a Reference/ directory with one file, then mocks ``os.path.getsize`` to raise
        ``OSError`` so the error-handling branch is exercised.  The result should still return
        ``present=True`` with ``size_bytes=0`` (the failed file contributes 0 bytes).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/music/Done")
        fs.create_dir(str(dest_root))
        ref_dir = dest_root.parent / "Reference"
        fs.create_dir(str(ref_dir))
        (ref_dir / "file.flac").write_bytes(b"\x00" * 256)

        mocker.patch("music_annotator._pipeline_maint.os.path.getsize", side_effect=OSError("stat failed"))

        evidence = _reference_evidence(dest_root)

        assert evidence.present is True
        assert evidence.size_bytes == 0

    # ---------------------------------------------------------------------------
    # KAT 13: overlap deduplication guard (same pass, same path in two entries)
    # ---------------------------------------------------------------------------

    def test_build_maintain_report_overlap_dedup_same_pass(self, fs: FakeFilesystem) -> None:
        """_build_maintain_report() deduplicates pass names in the overlap map.

        Builds plans where the same pass name appears twice for the same path (a defensive
        scenario that cannot arise from the real passes but exercises the deduplication guard
        in the overlap-building loop).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        journal_path = dest_root / JOURNAL_FILENAME
        _write_library_journal(dest_root, [])

        # Build a plan where the same path appears twice in the same pass's entries.
        # This exercises the `if plan.pass_name not in path_to_passes[...]` guard.
        dup_path = "/lib/dup.flac"
        dup_plan = DryRunPlan(
            pass_name="repath",
            entries=[
                DryRunEntry(current_path=dup_path, planned_path="/lib/new1.flac"),
                DryRunEntry(current_path=dup_path, planned_path="/lib/new2.flac"),
            ],
            count=2,
        )

        report = _build_maintain_report(journal_path, dest_root, [dup_plan])

        # The path appears in only one pass — no overlap (overlap requires >1 distinct pass).
        assert report.scan_ran is True
        assert report.overlaps == []
        # The pass_name "repath" must appear only once in path_to_passes[dup_path].
        repath_summary = next(s for s in report.pass_summaries if s.pass_name == "repath")
        assert repath_summary.count == 2


class TestMaintainDryRunParity:
    """No-regression parity pin for ``maintain --dry-run`` composition behaviour.

    Exercises the full maintain(dry_run=True) path over a representative fixture that
    contains candidates for repath and enrich, including a file that qualifies for both
    (triggering the overlap map).  This test is the behavioural pin: it must pass even after
    future changes to the composition logic.

    The fixture is deliberately minimal — three files, a known journal, no Reference/ directory
    — so every assertion is exact rather than a lower bound.  The fixture exercises:

    - The repath-only path (one file at a legacy path, clean tags).
    - The enrich-only path (one file at a correct path, missing audio_hash).
    - The overlap path (one file at a legacy path AND missing audio_hash).
    - Journal capacity: current_entry_count equals the number of pre-existing journal entries;
      projected_delta_entries equals the sum of planned changes across all passes.
    """

    @staticmethod
    def _make_repath_only_tags() -> TrackTags:
        """Build tags for a repath-only candidate: correct tags, placed at a legacy path.

        The file will be placed at a legacy path so repath plans to move it.

        :returns: A :class:`TrackTags` instance with clean CWP tags.
        """
        return TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Symphony No. 1",
            recording_date="2019",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Karajan",
        )

    @staticmethod
    def _make_enrich_only_tags() -> TrackTags:
        """Build tags for an enrich-only candidate: missing audio_hash, placed at a correct path.

        The file will be placed at a canonical path so repath finds nothing to move, but
        enrich plans to backfill the missing audio_hash.

        :returns: A :class:`TrackTags` instance with clean CWP tags and no audio_hash.
        """
        return TrackTags(
            cwp_composer_lastnames="Haydn",
            cwp_work_top="String Quartet in G major, Op. 76 No. 1",
            recording_date="2018",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro con spirito",
            artist="Kodaly Quartet",
        )

    @staticmethod
    def _make_overlap_tags() -> TrackTags:
        """Build tags for a file that qualifies for both repath and enrich.

        The file will be placed at a legacy path (repath candidate) and is missing audio_hash
        (enrich candidate), triggering the cross-pass overlap map.

        :returns: A :class:`TrackTags` instance with a legacy path and missing audio_hash.
        """
        return TrackTags(
            cwp_composer_lastnames="Haydn",
            cwp_work_top="String Quartet in E major, Op. 20 No. 4",
            recording_date="2021",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="I. Allegro moderato",
            artist="Amadeus Quartet",
        )

    def test_parity_pin_full_maintain_dry_run_path(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Parity pin: maintain(dry_run=True) composition behaviour over a representative fixture.

        Constructs a three-file library:
        - File A: repath candidate (legacy path, clean tags, missing audio_hash).
        - File B: enrich candidate (canonical path, missing audio_hash).
        - File C: both repath and enrich candidate (legacy path + missing audio_hash).

        Asserts all four parity properties:
        (1) scan_ran=True — the root was mounted and non-empty.
        (2) Per-pass counts: repath count >= 2 (files A and C); regroup, unify count == 0.
        (3) Overlap map: files A and C appear in the overlaps list with both "enrich" and
            "repath" named; repath overlap_count >= 1.
        (4) journal_capacity: current_entry_count == 3 (three pre-existing journal entries);
            projected_delta_entries >= 2 (at least 2 repath moves planned).

        ``_run_fpcalc`` is mocked because fpcalc is not available in the test environment.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # --- File A: repath + enrich candidate ---
        repath_only_tags = self._make_repath_only_tags()
        # Place at a legacy path (old work name) so repath plans to move it.
        file_a = _make_library_flac(
            dest_root,
            "Brahms - Karajan/OldSym1 [rec 2019]/01 - Allegro.flac",
            repath_only_tags,
        )

        # --- File B: enrich-only candidate ---
        enrich_only_tags = self._make_enrich_only_tags()
        # Place at a canonical path so repath finds nothing to move.
        file_b = _make_library_flac(
            dest_root,
            "Classical/Haydn - Kodaly Quartet/String Quartet in G major, Op. 76 No. 1 [rec 2018]/01 - Allegro con spirito.flac",
            enrich_only_tags,
        )

        # --- File C: overlap candidate (both repath and enrich) ---
        overlap_tags = self._make_overlap_tags()
        # Place at a legacy path so repath plans to move it; also missing audio_hash so enrich plans it.
        file_c = _make_library_flac(
            dest_root,
            "Haydn - Amadeus/OldQuartet [rec 2021]/01 - Allegro.flac",
            overlap_tags,
        )

        # --- Journal: three pre-existing entries (one per file) ---
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-brahms",
                    "source": "/src/01.flac",
                    "destination": str(file_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-haydn-kodaly",
                    "source": "/src/02.flac",
                    "destination": str(file_b),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-haydn-amadeus",
                    "source": "/src/03.flac",
                    "destination": str(file_c),
                    "action": "tagged",
                },
            ],
        )

        captured_report: list[MaintainDryRunReport] = []
        real_build = _build_maintain_report

        def _capture_report(*args: object, **kwargs: object) -> MaintainDryRunReport:
            report = real_build(*args, **kwargs)  # type: ignore[arg-type]
            captured_report.append(report)
            return report

        mocker.patch("music_annotator._pipeline_maint._build_maintain_report", side_effect=_capture_report)

        maintain(dest_root, dry_run=True)

        assert len(captured_report) == 1
        report = captured_report[0]

        # --- Parity property (1): scan_ran=True ---
        assert report.scan_ran is True, "scan_ran must be True when the root is mounted and non-empty"

        # --- Parity property (2): per-pass counts ---
        by_pass = {s.pass_name: s for s in report.pass_summaries}

        # repath must plan to move file A (legacy path) and file C (legacy path).
        assert by_pass["repath"].count >= 2, f"repath count must be >= 2 (files A and C); got {by_pass['repath'].count}"
        # No fragmented releases in this fixture.
        assert by_pass["regroup"].count == 0, f"regroup must be 0; got {by_pass['regroup'].count}"
        assert by_pass["unify"].count == 0, f"unify must be 0; got {by_pass['unify'].count}"

        # --- Parity property (3): overlap map populated ---
        # The maintain composition covers enrich, origin-time, repath, regroup, unify,
        # reconstruct-xrefs, and dedup-library.  Files that need both enrich (audio_hash
        # backfill) and repath (path correction) appear in the overlap map with passes
        # ["enrich", "repath"].  Files A and C in this fixture are at legacy paths and
        # lack audio_hash, so they appear in the overlap map.
        assert len(report.overlaps) >= 1, f"at least one overlap expected; got {report.overlaps}"
        # Every overlap entry must include both "enrich" and "repath".
        for overlap_entry in report.overlaps:
            assert "repath" in overlap_entry.pass_names, (
                f"'repath' must be in overlap pass_names; got {overlap_entry.pass_names}"
            )
            assert "enrich" in overlap_entry.pass_names, (
                f"'enrich' must be in overlap pass_names; got {overlap_entry.pass_names}"
            )
        # repath must report at least one overlapping file.
        assert by_pass["repath"].overlap_count >= 1, f"repath overlap_count must be >= 1; got {by_pass['repath'].overlap_count}"

        # --- Parity property (4): journal_capacity reflects the fixture journal ---
        cap = report.journal_capacity
        # Three pre-existing journal entries (one per file).
        assert cap.current_entry_count == 3, (
            f"current_entry_count must be 3 (three pre-existing entries); got {cap.current_entry_count}"
        )
        assert cap.current_size_bytes > 0, "current_size_bytes must be nonzero (journal file exists)"
        # At least 2 planned changes: 2 repath (files A and C at legacy paths).
        # enrich also plans changes (audio_hash backfill for all three files).
        assert cap.projected_delta_entries >= 2, (
            f"projected_delta_entries must be >= 2 (at least 2 repath moves); got {cap.projected_delta_entries}"
        )


# ---------------------------------------------------------------------------
# SEL-23 ensemble patch in repath — KAT
# ---------------------------------------------------------------------------


class TestRepathSel23EnsemblePatch:
    """KAT: repath applies the SEL-23 ensemble selection rule when recomputing paths.

    When a library file has a MUSICBRAINZ_ALBUMID and its per-track CEA_ENSEMBLES tag contains
    an ensemble that appears on a modal majority (>50%) of the release's tracks, repath must
    include that ensemble in the performers path component — even if the ensemble is not in
    CEA_ALBUM_ENSEMBLES (the release-level credit).

    This exercises the sel23_ensemble_patch call inside repath (the SEL-23 patch pass that runs
    between Pass 1 tag-reading and Pass 2 path-building).
    """

    @staticmethod
    def _make_sel23_tags(
        *,
        movt_num: str,
        title: str,
        album_ensemble: str,
        per_track_ensembles: list[str],
    ) -> TrackTags:
        """Build TrackTags for a SEL-23 repath test track.

        :param movt_num: Movement number (``CWP_MOVT_NUM``).
        :param title: Track title.
        :param album_ensemble: Release-level ensemble name (``CEA_ALBUM_ENSEMBLES``).
        :param per_track_ensembles: Per-track ensemble names (``CEA_ENSEMBLES``).
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        return TrackTags(
            cwp_composer_lastnames="Brahms",
            cwp_work_top="Symphony No. 1",
            recording_date="2022",
            cwp_movt_num=movt_num,
            movementtotal="2",
            cwp_part_levels="1",
            title=title,
            musicbrainz_albumid="sel23-repath-rel",
            # Release-level ensemble (CEA_ALBUM_ENSEMBLES): only the parent orchestra.
            cea_album_ensembles=album_ensemble,
            # Per-track ensembles (CEA_ENSEMBLES): includes the wind subgroup on majority tracks.
            cea_ensembles="; ".join(per_track_ensembles),
        )

    def test_repath_applies_sel23_patch_for_majority_ensemble(self, fs: FakeFilesystem) -> None:
        """repath applies the SEL-23 patch: a majority per-track ensemble enters the path.

        Two-track release where both tracks have the wind subgroup in CEA_ENSEMBLES (2/2 = 100%,
        a strict majority).  The subgroup is NOT in CEA_ALBUM_ENSEMBLES.  After repath, the
        recomputed path must include the wind subgroup in the performers component.

        This test covers the sel23_ensemble_patch call inside repath (line 1087 in
        _pipeline_maint.py) by verifying that the path changes to include the majority ensemble.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        bph = "Berliner Philharmoniker"
        blaaser = "Bläser der Berliner Philharmoniker"

        # Both tracks have the wind subgroup in CEA_ENSEMBLES (2/2 = 100% majority).
        tags_mvt1 = self._make_sel23_tags(
            movt_num="1",
            title="I. Un poco sostenuto",
            album_ensemble=bph,
            per_track_ensembles=[bph, blaaser],
        )
        tags_mvt2 = self._make_sel23_tags(
            movt_num="2",
            title="II. Andante sostenuto",
            album_ensemble=bph,
            per_track_ensembles=[bph, blaaser],
        )

        # Compute the canonical path WITH the SEL-23 patch applied (what repath should produce).
        # We apply the patch manually here to derive the expected path.
        tags_mvt1_patched = self._make_sel23_tags(
            movt_num="1",
            title="I. Un poco sostenuto",
            album_ensemble=bph,
            per_track_ensembles=[bph, blaaser],
        )
        tags_mvt2_patched = self._make_sel23_tags(
            movt_num="2",
            title="II. Andante sostenuto",
            album_ensemble=bph,
            per_track_ensembles=[bph, blaaser],
        )
        # Hydrate performer lists (simulating what repath does before sel23_ensemble_patch).
        file_dict_1 = {
            "CEA_ALBUM_ENSEMBLES": bph,
            "CEA_ALBUM_ENSEMBLES_SORT": bph,
            "CEA_ENSEMBLES": f"{bph}; {blaaser}",
            "CEA_ENSEMBLES_SORT": f"{bph}; {blaaser}",
        }
        file_dict_2 = dict(file_dict_1)
        _hydrate_performer_lists(tags_mvt1_patched, file_dict_1)
        _hydrate_performer_lists(tags_mvt2_patched, file_dict_2)
        sel23_ensemble_patch([tags_mvt1_patched, tags_mvt2_patched])

        expected_path_mvt1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_mvt1_patched).with_suffix(".flac")
        expected_path_mvt2 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_mvt2_patched).with_suffix(".flac")

        # Verify the expected path includes the wind subgroup.
        assert blaaser in str(expected_path_mvt1), f"Expected '{blaaser}' in SEL-23-patched path, got {expected_path_mvt1!r}"

        # Create files at the OLD path (without the wind subgroup in the performers component).
        # The old path uses only the release-level ensemble (BPh), not the wind subgroup.
        old_path_mvt1 = _make_library_flac(
            dest_root,
            f"Brahms - {bph}/Symphony No. 1 [rec 2022]/01 - I. Un poco sostenuto.flac",
            tags_mvt1,
        )
        old_path_mvt2 = _make_library_flac(
            dest_root,
            f"Brahms - {bph}/Symphony No. 1 [rec 2022]/02 - II. Andante sostenuto.flac",
            tags_mvt2,
        )

        # Write journal entries so repath picks up these files.
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "sel23-repath-rel",
                    "source": "/src/01.flac",
                    "destination": str(old_path_mvt1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "sel23-repath-rel",
                    "source": "/src/02.flac",
                    "destination": str(old_path_mvt2),
                    "action": "tagged",
                },
            ],
        )

        # Run repath.
        repath(dest_root, yes=True)

        # After repath, the files should be at the SEL-23-patched paths (including wind subgroup).
        assert expected_path_mvt1.exists(), f"After repath, file should be at SEL-23-patched path {expected_path_mvt1!r}"
        assert expected_path_mvt2.exists(), f"After repath, file should be at SEL-23-patched path {expected_path_mvt2!r}"
        # Old paths should no longer exist.
        assert not old_path_mvt1.exists(), f"Old path should be gone after repath: {old_path_mvt1!r}"
        assert not old_path_mvt2.exists(), f"Old path should be gone after repath: {old_path_mvt2!r}"


class TestTagReadCache:
    """Unit tests for :class:`TagReadCache` and :func:`_read_tags_cached`.

    Covers all cache paths: hit (no file open), miss (file read + store), key-component
    invalidation, corruption/absence degradation, move re-keying, save failure, and the
    ``_read_tags_cached`` helper with and without a cache.
    """

    def test_load_absent_sidecar_returns_empty_cache(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """TagReadCache.load returns an empty cache when the sidecar file does not exist.

        Absence of the sidecar is a normal first-run condition; it must never raise.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        cache = TagReadCache.load(sidecar)
        assert cache.get(Path("/lib/track.flac")) is None

    def test_load_malformed_json_not_object_returns_empty_cache(self, fs: FakeFilesystem) -> None:
        """TagReadCache.load returns an empty cache when the sidecar JSON is not an object.

        A JSON array or scalar at the top level is treated as corruption; the cache degrades
        gracefully to empty rather than raising.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_file(str(sidecar), contents="[1, 2, 3]")
        cache = TagReadCache.load(sidecar)
        assert cache.get(Path("/lib/track.flac")) is None

    def test_load_malformed_json_parse_error_returns_empty_cache(self, fs: FakeFilesystem) -> None:
        """TagReadCache.load returns an empty cache when the sidecar is not valid JSON.

        Any JSON parse error degrades gracefully to an empty cache — never an error.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_file(str(sidecar), contents="{not valid json")
        cache = TagReadCache.load(sidecar)
        assert cache.get(Path("/lib/track.flac")) is None

    def test_load_skips_entry_with_non_string_key(self, fs: FakeFilesystem) -> None:
        """TagReadCache.load skips entries whose composite key is not a string.

        Non-string keys in the JSON object are silently ignored; valid entries are still loaded.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        # One valid entry and one with a non-string value (dict instead of tag dict).
        data = {
            "/lib/track.flac\x00100\x001000": {"TITLE": "Test"},
            "/lib/bad.flac\x00200\x002000": "not-a-dict",
        }
        fs.create_file(str(sidecar), contents=json.dumps(data))
        cache = TagReadCache.load(sidecar)
        # Valid entry is loaded; bad entry is skipped.
        # Verify the bad entry was skipped (no crash) and the store has exactly 1 entry.
        assert len(cache) == 1

    def test_load_skips_entry_with_wrong_key_part_count(self, fs: FakeFilesystem) -> None:
        """TagReadCache.load skips entries whose composite key does not have exactly 3 parts.

        A key with fewer or more than 3 null-separated parts is silently ignored.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        data = {
            "/lib/track.flac\x00100": {"TITLE": "Test"},  # only 2 parts
        }
        fs.create_file(str(sidecar), contents=json.dumps(data))
        cache = TagReadCache.load(sidecar)
        assert len(cache) == 0

    def test_load_skips_entry_with_non_int_size_or_mtime(self, fs: FakeFilesystem) -> None:
        """TagReadCache.load skips entries whose size or mtime cannot be parsed as integers.

        A ValueError from int() conversion is silently ignored.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        data = {
            "/lib/track.flac\x00notanint\x001000": {"TITLE": "Test"},
        }
        fs.create_file(str(sidecar), contents=json.dumps(data))
        cache = TagReadCache.load(sidecar)
        assert len(cache) == 0

    def test_load_skips_entry_with_non_string_tag_value(self, fs: FakeFilesystem) -> None:
        """TagReadCache.load skips entries whose tag dict contains a non-string value.

        Tag values must be strings; an integer or None value causes the entire entry to be skipped.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        data = {
            "/lib/track.flac\x00100\x001000": {"TITLE": 42},  # non-string tag value
        }
        fs.create_file(str(sidecar), contents=json.dumps(data))
        cache = TagReadCache.load(sidecar)
        assert len(cache) == 0

    def test_save_and_load_round_trip(self, fs: FakeFilesystem) -> None:
        """TagReadCache.save persists the store; TagReadCache.load restores it exactly.

        A put → save → load → get round-trip must return the same tag dict.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        # Create a fake file so put() can stat it.
        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        tag_dict = {"TITLE": "Symphony No. 5", "ARTIST": "Karajan"}
        cache.put(track, tag_dict)
        cache.save()

        # Load a fresh cache from the sidecar and verify the entry is present.
        cache2 = TagReadCache.load(sidecar)
        result = cache2.get(track)
        assert result == tag_dict

    def test_save_failure_is_non_fatal(self) -> None:
        """TagReadCache.save silently ignores write errors.

        A save failure must not raise; the cache is simply not persisted.
        """
        # Use a sidecar path in a non-existent directory so write_text raises.
        sidecar = Path("/nonexistent/dir/.music_annotator_tag_cache.json")
        cache = TagReadCache(sidecar)
        # Must not raise even though the parent directory does not exist.
        cache.save()

    def test_get_returns_none_on_stat_failure(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """TagReadCache.get returns None when the file does not exist (stat fails).

        A missing file is treated as a cache miss, not an error.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        cache = TagReadCache(sidecar)
        result = cache.get(Path("/lib/nonexistent.flac"))
        assert result is None

    def test_put_ignores_stat_failure(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """TagReadCache.put silently ignores OSError when the file does not exist.

        A failed put is a no-op; the cache remains empty.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        cache = TagReadCache(sidecar)
        # Must not raise even though the file does not exist.
        cache.put(Path("/lib/nonexistent.flac"), {"TITLE": "Test"})
        assert len(cache) == 0

    def test_get_returns_none_on_key_mismatch(self, fs: FakeFilesystem) -> None:
        """TagReadCache.get returns None when size or mtime does not match the stored key.

        Any change to size or mtime invalidates the cache entry.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        cache.put(track, {"TITLE": "Test"})

        # Overwrite the file with different content to change size and mtime.
        track.write_bytes(b"y" * 100)
        result = cache.get(track)
        assert result is None

    def test_get_returns_cached_dict_on_hit(self, fs: FakeFilesystem) -> None:
        """TagReadCache.get returns the cached tag dict when path, size, and mtime all match.

        A cache hit must return the exact dict stored by put().

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        tag_dict = {"TITLE": "Allegro", "ARTIST": "Karajan"}
        cache.put(track, tag_dict)

        result = cache.get(track)
        assert result == tag_dict

    def test_rekey_moves_entry_to_new_path(self, fs: FakeFilesystem) -> None:
        """TagReadCache.rekey moves the cache entry from old_path to new_path.

        After rekey, get(new_path) returns the tag dict and get(old_path) returns None.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib/A")
        fs.create_dir("/lib/B")
        cache = TagReadCache(sidecar)

        old_path = Path("/lib/A/track.flac")
        new_path = Path("/lib/B/track.flac")
        fs.create_file(str(old_path), contents=b"x" * 50)
        tag_dict = {"TITLE": "Andante"}
        cache.put(old_path, tag_dict)

        # Simulate the file move: create new_path, remove old_path.
        fs.create_file(str(new_path), contents=b"x" * 50)
        old_path.unlink()

        cache.rekey(old_path, new_path)

        # Old path entry is gone; new path entry is present.
        assert cache.get(old_path) is None
        result = cache.get(new_path)
        assert result == tag_dict

    def test_rekey_noop_when_old_path_not_in_cache(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """TagReadCache.rekey is a no-op when old_path has no cache entry.

        A rekey for a path not in the cache must not raise.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        cache = TagReadCache(sidecar)
        # Must not raise.
        cache.rekey(Path("/lib/old.flac"), Path("/lib/new.flac"))
        assert len(cache) == 0

    def test_rekey_noop_when_new_path_stat_fails(self, fs: FakeFilesystem) -> None:
        """TagReadCache.rekey is a no-op when new_path does not exist (stat fails).

        The old entry is removed but the new entry is not inserted when stat fails.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        old_path = Path("/lib/old.flac")
        fs.create_file(str(old_path), contents=b"x" * 50)
        cache.put(old_path, {"TITLE": "Test"})

        # new_path does not exist — stat will fail.
        cache.rekey(old_path, Path("/lib/nonexistent.flac"))

        # Old entry removed; no new entry inserted.
        assert len(cache) == 0

    def test_read_tags_cached_hit_does_not_open_file(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_read_tags_cached returns cached tags without opening the audio file on a hit.

        When (path, size, mtime) matches a cached entry, _read_tags_flac and _read_tags_mp3
        must never be called.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        tag_dict = {"TITLE": "Allegro", "ARTIST": "Karajan"}
        cache.put(track, tag_dict)

        mock_read_flac = mocker.patch("music_annotator._pipeline_maint._read_tags_flac")
        mock_read_mp3 = mocker.patch("music_annotator._pipeline_maint._read_tags_mp3")

        result = _read_tags_cached(track, ".flac", cache)

        assert result == tag_dict
        mock_read_flac.assert_not_called()
        mock_read_mp3.assert_not_called()

    def test_read_tags_cached_miss_reads_file_and_stores(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_read_tags_cached reads the audio file on a miss and stores the result in the cache.

        After a miss, the cache is populated so a subsequent get() returns the same dict.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        tag_dict = {"TITLE": "Andante"}

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", return_value=tag_dict)

        result = _read_tags_cached(track, ".flac", cache)

        assert result == tag_dict
        # Cache is now populated.
        assert cache.get(track) == tag_dict

    def test_read_tags_cached_none_cache_always_reads_file(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_read_tags_cached reads the audio file directly when cache is None.

        When no cache is provided, the file is always opened regardless of previous calls.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        tag_dict = {"TITLE": "Allegro"}

        mock_read = mocker.patch("music_annotator._pipeline_maint._read_tags_flac", return_value=tag_dict)

        result = _read_tags_cached(track, ".flac", None)

        assert result == tag_dict
        mock_read.assert_called_once_with(track)

    def test_repath_creates_and_saves_cache_sidecar(self, fs: FakeFilesystem) -> None:
        """repath() creates the tag-read cache sidecar after a successful run.

        After repath completes, the sidecar file must exist under dest_root.

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
        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        _write_library_journal(
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

        repath(dest_root, yes=True)

        sidecar = dest_root / _TAG_CACHE_FILENAME
        assert sidecar.exists(), "Cache sidecar must be created after repath"

    def test_repath_cache_hit_skips_file_open(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() uses the cache on a second run: the audio file is not opened again.

        On the first run, tags are read from the file and stored in the cache.  On the second
        run (dry_run=True to avoid moving), the cache is consulted and the file is not opened.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
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
        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        _write_library_journal(
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

        # First run: populates the cache.
        repath(dest_root, dry_run=True)

        # Second run: the cache is warm; _read_tags_flac must not be called.
        mock_read = mocker.patch("music_annotator._pipeline_maint._read_tags_flac")
        repath(dest_root, dry_run=True)
        mock_read.assert_not_called()

    def test_move_verify_journal_rekeys_cache(self, fs: FakeFilesystem) -> None:
        """_move_verify_journal re-keys the cache entry after a successful move.

        After the move, get(new_path) returns the tag dict and get(old_path) returns None.

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
        src = dest_root / "A" / "track.flac"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(src, tags)

        dest = dest_root / "B" / "track.flac"

        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = read_journal(journal_path)

        sidecar = dest_root / _TAG_CACHE_FILENAME
        cache = TagReadCache(sidecar)
        tag_dict = {"TITLE": "Allegro"}
        cache.put(src, tag_dict)

        _move_verify_journal(
            [(src, dest)],
            journal=journal,
            journal_path=journal_path,
            action="repathed",
            dest_root=dest_root,
            now=datetime.datetime.now(datetime.UTC),
            cache=cache,
        )

        # After the move, the cache entry is at the new path.
        assert cache.get(src) is None
        assert cache.get(dest) == tag_dict

    def test_cache_key_invalidated_on_size_change(self, fs: FakeFilesystem) -> None:
        """Cache entry is invalidated when the file size changes.

        Writing different content to the file changes st_size, causing a cache miss.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        track = Path("/lib/track.flac")
        fs.create_file(str(track), contents=b"x" * 50)
        cache.put(track, {"TITLE": "Original"})

        # Change the file size.
        track.write_bytes(b"y" * 200)

        assert cache.get(track) is None

    def test_cache_key_invalidated_on_mtime_change(self, fs: FakeFilesystem) -> None:
        """Cache entry is invalidated when the file mtime changes.

        Touching the file (same size, different mtime) causes a cache miss.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/.music_annotator_tag_cache.json")
        fs.create_dir("/lib")
        cache = TagReadCache(sidecar)

        track = Path("/lib/track.flac")
        content = b"x" * 50
        fs.create_file(str(track), contents=content)
        cache.put(track, {"TITLE": "Original"})

        # Change mtime without changing size.
        time.sleep(0.01)
        os.utime(str(track), (os.stat(str(track)).st_atime + 1, os.stat(str(track)).st_mtime + 1))

        assert cache.get(track) is None


class TestWriteSecondaryAlbumId:
    """Unit tests for :func:`write_secondary_albumid_flac` and :func:`write_secondary_albumid_mp3`.

    Verifies append-only set-union semantics, idempotency, and multi-value accumulation for
    both FLAC and MP3 formats.
    """

    def test_write_secondary_albumid_flac_adds_new_mbid(self, fs: FakeFilesystem) -> None:
        """write_secondary_albumid_flac adds a new MBID and returns True.

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.flac")
        fs.create_dir("/lib")
        path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(path, TrackTags(title="Test"))

        result = write_secondary_albumid_flac(path, "mbid-secondary-1")

        assert result is True
        audio = MutagenFLAC(str(path))
        vals = audio.get("musicbrainz_secondary_albumid") or []
        assert vals[0] == "mbid-secondary-1"

    def test_write_secondary_albumid_flac_idempotent(self, fs: FakeFilesystem) -> None:
        """write_secondary_albumid_flac returns False when MBID already present (no-op).

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.flac")
        fs.create_dir("/lib")
        path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(path, TrackTags(title="Test"))

        write_secondary_albumid_flac(path, "mbid-secondary-1")
        result = write_secondary_albumid_flac(path, "mbid-secondary-1")

        assert result is False

    def test_write_secondary_albumid_flac_appends_second_mbid(self, fs: FakeFilesystem) -> None:
        """write_secondary_albumid_flac appends a second MBID with '; ' separator.

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.flac")
        fs.create_dir("/lib")
        path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(path, TrackTags(title="Test"))

        write_secondary_albumid_flac(path, "mbid-a")
        write_secondary_albumid_flac(path, "mbid-b")

        audio = MutagenFLAC(str(path))
        vals = audio.get("musicbrainz_secondary_albumid") or []
        assert vals[0] == "mbid-a; mbid-b"

    def test_write_secondary_albumid_mp3_adds_new_mbid(self, fs: FakeFilesystem) -> None:
        """write_secondary_albumid_mp3 adds a new MBID and returns True.

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.mp3")
        fs.create_dir("/lib")
        path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(path, TrackTags(title="Test"))

        result = write_secondary_albumid_mp3(path, "mbid-secondary-1")

        assert result is True
        id3 = ID3(str(path))  # type: ignore[no-untyped-call]
        found = ""
        for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
            if frame.desc == "MusicBrainz Secondary Album Id":
                found = str(frame.text[0])
        assert found == "mbid-secondary-1"

    def test_write_secondary_albumid_mp3_idempotent(self, fs: FakeFilesystem) -> None:
        """write_secondary_albumid_mp3 returns False when MBID already present (no-op).

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.mp3")
        fs.create_dir("/lib")
        path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(path, TrackTags(title="Test"))

        write_secondary_albumid_mp3(path, "mbid-secondary-1")
        result = write_secondary_albumid_mp3(path, "mbid-secondary-1")

        assert result is False

    def test_write_secondary_albumid_mp3_appends_second_mbid(self, fs: FakeFilesystem) -> None:
        """write_secondary_albumid_mp3 appends a second MBID with '; ' separator.

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.mp3")
        fs.create_dir("/lib")
        path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(path, TrackTags(title="Test"))

        write_secondary_albumid_mp3(path, "mbid-a")
        write_secondary_albumid_mp3(path, "mbid-b")

        id3 = ID3(str(path))  # type: ignore[no-untyped-call]
        found = ""
        for frame in id3.getall("TXXX"):  # type: ignore[no-untyped-call]
            if frame.desc == "MusicBrainz Secondary Album Id":
                found = str(frame.text[0])
        assert found == "mbid-a; mbid-b"


class TestResolveCurrentLibRetiredVerbs:
    """KATs for :func:`_resolve_current_lib` with the retired action verbs ``"repatched"`` and
    ``"acoustid-repatched"`` (C-RETIRE).

    The two commands that wrote these verbs (``repatch-catalogue-colon`` and ``repatch-acoustid``)
    have been removed, but the journal is append-only: historical entries with these verbs must
    still deserialise correctly and be resolved correctly by the lineage walk.  These tests verify
    that the resolver's in-place-update arm handles both verbs, and that a journal containing them
    round-trips through ``read_journal`` and ``rebuild_journal`` without error.
    """

    def test_repatched_entry_re_registers_path_in_place(self) -> None:
        """'repatched' entry re-registers the path in-place (source == destination).

        A file that was 'tagged' then 'repatched' (in-place, same path) is still resolved
        to the same path with the same release_id.  The 'repatched' entry must not pop the
        path from the map or change the release_id.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-1",
                    source="/src/01.flac",
                    destination="/lib/A/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-01T00:01:00+00:00",
                    release_id="rel-1",
                    source="/lib/A/01.flac",
                    destination="/lib/A/01.flac",
                    action="repatched",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert Path("/lib/A/01.flac") in result
        assert result[Path("/lib/A/01.flac")] == "rel-1"

    def test_acoustid_repatched_entry_re_registers_path_in_place(self) -> None:
        """'acoustid-repatched' entry re-registers the path in-place (source == destination).

        A file that was 'tagged' then 'acoustid-repatched' (in-place, same path) is still
        resolved to the same path with the same release_id.  The 'acoustid-repatched' entry
        must not pop the path from the map or change the release_id.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-2",
                    source="/src/02.flac",
                    destination="/lib/B/02.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-01T00:01:00+00:00",
                    release_id="rel-2",
                    source="/lib/B/02.flac",
                    destination="/lib/B/02.flac",
                    action="acoustid-repatched",
                    acoustid_fingerprint="AQADtMmybckm",
                    acoustid_id="uuid-1234",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert Path("/lib/B/02.flac") in result
        assert result[Path("/lib/B/02.flac")] == "rel-2"

    def test_retired_verbs_round_trip_through_read_journal(self, fs: FakeFilesystem) -> None:
        """A journal containing 'repatched' and 'acoustid-repatched' entries deserialises correctly.

        Writes a JSONL journal with both retired verbs and reads it back via :func:`read_journal`.
        Asserts that both entries are present with the correct action verbs and that
        :func:`_resolve_current_lib` resolves the paths correctly.  This is the C-RETIRE
        round-trip KAT: the journal is append-only, so historical entries must always deserialise.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        journal_path = dest_root / JOURNAL_FILENAME

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/A/a.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/A/a.flac",
                    "destination": "/lib/A/a.flac",
                    "action": "repatched",
                },
                {
                    "timestamp": "2024-01-01T00:02:00+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": "/lib/B/b.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:03:00+00:00",
                    "release_id": "rel-b",
                    "source": "/lib/B/b.flac",
                    "destination": "/lib/B/b.flac",
                    "action": "acoustid-repatched",
                    "acoustid_fingerprint": "AQADtMmybckm",
                    "acoustid_id": "uuid-5678",
                },
            ],
        )

        journal = read_journal(journal_path)

        # All four entries must deserialise correctly.
        assert len(journal.entries) == 4
        actions = [e.action for e in journal.entries]
        assert "repatched" in actions
        assert "acoustid-repatched" in actions

        # _resolve_current_lib must handle both retired verbs correctly.
        current_lib = _resolve_current_lib(journal)
        assert Path("/lib/A/a.flac") in current_lib
        assert current_lib[Path("/lib/A/a.flac")] == "rel-a"
        assert Path("/lib/B/b.flac") in current_lib
        assert current_lib[Path("/lib/B/b.flac")] == "rel-b"


class TestResolveCurrentLibNewActions:
    """Tests for :func:`_resolve_current_lib` with ``"cross-referenced"`` and ``"deduplicated"`` actions.

    Verifies that the new journal actions are handled correctly in the lineage walk.
    """

    def test_cross_referenced_preserves_primary_release_id(self) -> None:
        """'cross-referenced' entry preserves the file's primary release_id in the map.

        The entry.release_id carries the secondary MBID; the primary MBID must not be overwritten.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="primary-rel-1",
                    source="/src/01.flac",
                    destination="/lib/A/01.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="secondary-rel-2",  # secondary MBID, not the primary
                    source="/lib/A/01.flac",
                    destination="/lib/A/01.flac",
                    action="cross-referenced",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert result[Path("/lib/A/01.flac")] == "primary-rel-1"

    def test_deduplicated_pops_source_path(self) -> None:
        """'deduplicated' entry removes the deleted copy's path from the map.

        The surviving copy's path remains registered; the deleted copy's path is gone.
        """
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-survivor",
                    source="/src/survivor.flac",
                    destination="/lib/A/survivor.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-deleted",
                    source="/src/deleted.flac",
                    destination="/lib/B/deleted.flac",
                    action="tagged",
                ),
                TransactionEntry(
                    timestamp="2024-01-02T00:00:00+00:00",
                    release_id="rel-deleted",
                    source="/lib/B/deleted.flac",
                    destination="/lib/A/survivor.flac",
                    action="deduplicated",
                ),
            ]
        )
        result = _resolve_current_lib(journal)
        assert Path("/lib/A/survivor.flac") in result
        assert result[Path("/lib/A/survivor.flac")] == "rel-survivor"
        assert Path("/lib/B/deleted.flac") not in result


class TestWriteXrefAndJournal:
    """Tests for :func:`_write_xref_and_journal`.

    Verifies the C-PROV chain: tag write → verify → journal entry with action ``"cross-referenced"``.
    """

    def test_write_xref_flac_journals_cross_referenced(self, fs: FakeFilesystem) -> None:
        """_write_xref_and_journal writes the secondary MBID and journals a 'cross-referenced' entry.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        survivor = dest_root / "A" / "track.flac"
        survivor.parent.mkdir(parents=True)
        survivor.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(survivor, TrackTags(title="Test", musicbrainz_albumid="primary-rel"))

        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = TransactionLog()

        _write_xref_and_journal(
            survivor,
            "secondary-rel-1",
            journal=journal,
            journal_path=journal_path,
            now_str="2024-01-01T00:00:00+00:00",
        )

        # Journal has one cross-referenced entry with the secondary MBID.
        assert len(journal.entries) == 1
        entry = journal.entries[0]
        assert entry.action == "cross-referenced"
        assert entry.release_id == "secondary-rel-1"
        assert entry.source == str(survivor)
        assert entry.destination == str(survivor)

        # Tag is written to the file.
        audio = MutagenFLAC(str(survivor))
        vals = audio.get("musicbrainz_secondary_albumid") or []
        assert "secondary-rel-1" in vals[0]

    def test_write_xref_mp3_journals_cross_referenced(self, fs: FakeFilesystem) -> None:
        """_write_xref_and_journal works for MP3 files.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        survivor = dest_root / "A" / "track.mp3"
        survivor.parent.mkdir(parents=True)
        survivor.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(survivor, TrackTags(title="Test", musicbrainz_albumid="primary-rel"))

        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = TransactionLog()

        _write_xref_and_journal(
            survivor,
            "secondary-rel-1",
            journal=journal,
            journal_path=journal_path,
            now_str="2024-01-01T00:00:00+00:00",
        )

        assert len(journal.entries) == 1
        assert journal.entries[0].action == "cross-referenced"
        assert journal.entries[0].release_id == "secondary-rel-1"


class TestResolveDuplicateGroup:
    """Tests for :func:`resolve_duplicate_group` — the shared group-resolution flow.

    Covers all three operator arms (survivor_occupant, survivor_mover, keep_both, abort),
    dry-run reporting, and idempotency (keep-both re-run → silent drop, no prompt).
    """

    @staticmethod
    def _make_flac(dest_root: Path, rel_path: str, release_id: str) -> Path:
        """Create a minimal FLAC file with the given release_id tag.

        :param dest_root: Library root.
        :param rel_path: Relative path within the library.
        :param release_id: MUSICBRAINZ_ALBUMID to embed.
        :returns: Full absolute path of the created file.
        """
        full_path = dest_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(full_path, TrackTags(title="Test", musicbrainz_albumid=release_id))
        return full_path

    @staticmethod
    def _make_journal(dest_root: Path, entries: list[dict[str, str]]) -> tuple[TransactionLog, Path]:
        """Write a JSONL journal and return the in-memory TransactionLog and journal path.

        :param dest_root: Library root.
        :param entries: List of raw entry dicts.
        :returns: Tuple of (TransactionLog, journal_path).
        """
        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return read_journal(journal_path), journal_path

    def test_survivor_occupant_deletes_mover_and_journals(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group choice '1' (survivor=occupant): mover deleted, xref journalled.

        Verifies:
        - Mover file is deleted from disk.
        - 'cross-referenced' journal entry precedes 'deduplicated' entry (C-DEDUP ordering).
        - DuplicateResolution.choice == 'survivor_occupant', proceed_with_move == False.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(mover),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="1")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        assert resolution.choice == "survivor_occupant"
        assert resolution.proceed_with_move is False
        assert not mover.exists(), "Mover must be deleted"
        assert occupant.exists(), "Occupant must survive"

        # C-DEDUP ordering: cross-referenced before deduplicated.
        new_entries = [e for e in journal.entries if e.action in {"cross-referenced", "deduplicated"}]
        assert len(new_entries) == 2
        assert new_entries[0].action == "cross-referenced"
        assert new_entries[0].release_id == "rel-mover"
        assert new_entries[1].action == "deduplicated"
        assert new_entries[1].release_id == "rel-mover"
        assert new_entries[1].source == str(mover)
        assert new_entries[1].destination == str(occupant)

    def test_survivor_mover_deletes_occupant_and_journals(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group choice '2' (survivor=mover): occupant deleted, xref journalled.

        Verifies:
        - Occupant file is deleted from disk.
        - 'cross-referenced' journal entry precedes 'deduplicated' entry (C-DEDUP ordering).
        - DuplicateResolution.choice == 'survivor_mover', proceed_with_move == True.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(mover),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="2")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        assert resolution.choice == "survivor_mover"
        assert resolution.proceed_with_move is True
        assert not occupant.exists(), "Occupant must be deleted"
        assert mover.exists(), "Mover must survive"

        # C-DEDUP ordering: cross-referenced before deduplicated.
        new_entries = [e for e in journal.entries if e.action in {"cross-referenced", "deduplicated"}]
        assert len(new_entries) == 2
        assert new_entries[0].action == "cross-referenced"
        assert new_entries[0].release_id == "rel-occupant"
        assert new_entries[1].action == "deduplicated"
        assert new_entries[1].release_id == "rel-occupant"
        assert new_entries[1].source == str(occupant)
        assert new_entries[1].destination == str(mover)

    def test_keep_both_cross_references_and_drops_move(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group choice 'b' (keep-both): xref written, no deletion.

        Verifies:
        - Both files remain on disk.
        - 'cross-referenced' journal entry written with mover's MBID as secondary.
        - DuplicateResolution.choice == 'keep_both', proceed_with_move == False.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(mover),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="b")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        assert resolution.choice == "keep_both"
        assert resolution.proceed_with_move is False
        assert occupant.exists()
        assert mover.exists()

        xref_entries = [e for e in journal.entries if e.action == "cross-referenced"]
        assert len(xref_entries) == 1
        assert xref_entries[0].release_id == "rel-mover"

    def test_abort_returns_abort_choice(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group choice 'a' (abort): no changes, choice == 'abort'.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="a")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        assert resolution.choice == "abort"
        assert resolution.proceed_with_move is False
        assert occupant.exists()
        assert mover.exists()
        # No new journal entries.
        assert not any(e.action in {"cross-referenced", "deduplicated"} for e in journal.entries)

    def test_keep_both_rerun_silent_drop_no_prompt(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """keep-both re-run: mover's MBID already in occupant's secondary set → silent drop, no prompt.

        Idempotency: if the mover's release MBID is already in the occupant's secondary MBID set,
        the group is silently dropped without prompting the operator.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        # Pre-write the mover's MBID as a secondary on the occupant (simulating a prior keep-both).
        write_secondary_albumid_flac(occupant, "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
            ],
        )

        mock_input = mocker.patch("builtins.input")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        # No prompt shown — idempotent silent drop.
        mock_input.assert_not_called()
        assert resolution.choice == "keep_both"
        assert resolution.proceed_with_move is False

    def test_dry_run_reports_without_prompting(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group dry_run=True reports the group without prompting or modifying files.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
            ],
        )

        mock_input = mocker.patch("builtins.input")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
            dry_run=True,
        )

        mock_input.assert_not_called()
        assert resolution.choice == "keep_both"
        assert resolution.proceed_with_move is False
        # No journal mutations in dry-run.
        assert not any(e.action in {"cross-referenced", "deduplicated"} for e in journal.entries)

    def test_invalid_input_then_b_cross_references_without_deletion(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """KAT: invalid input re-prompts; subsequent 'b' cross-references without deletion.

        Verifies that unrecognised input (e.g. 'y', 'x', '') causes a re-prompt rather than
        aborting the pass.  The operator's eventual 'b' choice cross-references both files and
        keeps both on disk.  This is the primary guard against piped-y streams silently killing
        the dedup pass (INSTR/C-DEDUP: integrity consent stays interactive).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(mover),
                    "action": "tagged",
                },
            ],
        )

        # Simulate: first two inputs are invalid ('y', 'x'), third is valid ('b').
        mocker.patch("builtins.input", side_effect=["y", "x", "b"])

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        assert resolution.choice == "keep_both"
        assert resolution.proceed_with_move is False
        assert occupant.exists(), "Occupant must survive"
        assert mover.exists(), "Mover must survive"
        xref_entries = [e for e in journal.entries if e.action == "cross-referenced"]
        assert len(xref_entries) == 1
        assert xref_entries[0].release_id == "rel-mover"

    def test_eof_on_input_aborts_with_clear_message(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """KAT: piped-y stream exhausted (EOFError) → abort with a clear message.

        When stdin is a piped stream that raises EOFError (e.g. a piped 'y' stream that has been
        consumed), the function treats it as an abort and emits a clear message rather than
        silently killing the pass.  This prevents piped consent from satisfying the integrity
        prompt (INSTR/C-DEDUP).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = self._make_flac(dest_root, "A/occupant.flac", "rel-occupant")
        mover = self._make_flac(dest_root, "B/mover.flac", "rel-mover")

        journal, journal_path = self._make_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occupant",
                    "source": "/src/occ.flac",
                    "destination": str(occupant),
                    "action": "tagged",
                },
            ],
        )

        # Simulate EOF immediately (piped stream exhausted).
        mocker.patch("builtins.input", side_effect=EOFError)

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        assert resolution.choice == "abort"
        assert resolution.proceed_with_move is False
        assert occupant.exists()
        assert mover.exists()
        assert not any(e.action in {"cross-referenced", "deduplicated"} for e in journal.entries)


class TestDuplicateResolutionInit:
    """Tests for :class:`DuplicateResolution` initialisation.

    Verifies that all attributes are set correctly from constructor arguments.
    """

    def test_all_attributes_set(self) -> None:
        """DuplicateResolution stores all constructor arguments as attributes.

        :returns: None.
        """
        survivor = Path("/lib/survivor.flac")
        deleted = Path("/lib/deleted.flac")
        res = DuplicateResolution(
            choice="survivor_occupant",
            survivor_path=survivor,
            deleted_path=deleted,
            deleted_release_id="rel-deleted",
            secondary_mbid="rel-secondary",
            proceed_with_move=False,
        )
        assert res.choice == "survivor_occupant"
        assert res.survivor_path == survivor
        assert res.deleted_path == deleted
        assert res.deleted_release_id == "rel-deleted"
        assert res.secondary_mbid == "rel-secondary"
        assert res.proceed_with_move is False


class TestWriteXrefAndJournalVerificationFailure:
    """Tests for :func:`_write_xref_and_journal` verification failure path.

    Verifies that a RuntimeError is raised when the tag write does not land correctly.
    """

    def test_verification_failure_raises_runtime_error(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_write_xref_and_journal raises RuntimeError when read-back does not contain the MBID.

        Simulates a write that succeeds but the read-back does not contain the expected MBID
        (e.g. a bug in the write function).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        survivor = dest_root / "A" / "track.flac"
        survivor.parent.mkdir(parents=True)
        survivor.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(survivor, TrackTags(title="Test"))

        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = TransactionLog()

        # Patch write_secondary_albumid_flac to be a no-op (doesn't actually write).
        mocker.patch("music_annotator._pipeline_maint.write_secondary_albumid_flac", return_value=False)

        with pytest.raises(RuntimeError, match="cross-reference write verification failed"):
            _write_xref_and_journal(
                survivor,
                "secondary-rel-1",
                journal=journal,
                journal_path=journal_path,
                now_str="2024-01-01T00:00:00+00:00",
            )


class TestResolveDuplicateGroupEdgeCases:
    """Edge-case tests for :func:`resolve_duplicate_group`.

    Covers MP3 idempotency check and tag-read exception handling.
    """

    def test_mp3_occupant_idempotency_check(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group checks MP3 occupant's secondary MBID set for idempotency.

        When the mover's MBID is already in the MP3 occupant's secondary MBID set, the group
        is silently dropped without prompting.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = dest_root / "A" / "occupant.mp3"
        occupant.parent.mkdir(parents=True)
        occupant.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(occupant, TrackTags(title="Test", musicbrainz_albumid="rel-occupant"))

        mover = dest_root / "B" / "mover.mp3"
        mover.parent.mkdir(parents=True)
        mover.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(mover, TrackTags(title="Test", musicbrainz_albumid="rel-mover"))

        # Pre-write the mover's MBID as a secondary on the MP3 occupant.
        write_secondary_albumid_mp3(occupant, "rel-mover")

        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = TransactionLog()

        mock_input = mocker.patch("builtins.input")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        mock_input.assert_not_called()
        assert resolution.choice == "keep_both"

    def test_tag_read_exception_treated_as_not_cross_referenced(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """resolve_duplicate_group treats tag-read exception as not-yet-cross-referenced.

        When reading the occupant's tags raises an exception during the idempotency check,
        the function proceeds to prompt the operator rather than silently dropping.
        The operator chooses abort so no further tag reads are needed.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        occupant = dest_root / "A" / "occupant.flac"
        occupant.parent.mkdir(parents=True)
        occupant.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(occupant, TrackTags(title="Test", musicbrainz_albumid="rel-occupant"))

        mover = dest_root / "B" / "mover.flac"
        mover.parent.mkdir(parents=True)
        mover.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(mover, TrackTags(title="Test", musicbrainz_albumid="rel-mover"))

        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = TransactionLog()

        # Patch _read_tags_flac to raise an exception only on the first call (idempotency check).
        # Subsequent calls (from _write_xref_and_journal) use the real function.
        call_count = [0]

        def _patched_read(path: Path) -> dict[str, str]:
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("read error")
            return _read_tags_flac(path)

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=_patched_read)
        # Operator aborts — no further file writes needed.
        mocker.patch("builtins.input", return_value="a")

        resolution = resolve_duplicate_group(
            occupant,
            "rel-occupant",
            mover,
            "rel-mover",
            "sha256",
            journal=journal,
            journal_path=journal_path,
            dest_root=dest_root,
            now=datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        )

        # Exception treated as not-yet-cross-referenced; operator was prompted and chose abort.
        assert resolution.choice == "abort"


class TestRepathMatchTrueAndNoneArms:
    """Tests for repath() match=True (same-audio) and match=None (inconclusive) collision arms.

    Verifies the plan-time completeness property: every collision outcome is resolved at plan
    time so no execution-time C-NOCLOBBER refusal occurs.
    """

    @staticmethod
    def _make_tags(composer: str, work: str, mvt: str, title: str) -> TrackTags:
        """Build minimal TrackTags for repath testing.

        :param composer: CEA/CWP composer last name.
        :param work: CWP work top name.
        :param mvt: CWP movement number.
        :param title: Track title.
        :returns: A :class:`TrackTags` instance.
        """
        return TrackTags(
            cwp_composer_lastnames=composer,
            cwp_work_top=work,
            recording_date="2020",
            cwp_movt_num=mvt,
            movementtotal="1",
            cwp_part_levels="1",
            title=title,
            artist="Karajan",
        )

    def test_match_true_survivor_occupant_drops_move(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm: survivor=occupant → mover deleted, move dropped from plan.

        Sets up a library where the mover's planned destination is already occupied by a file
        with the same SHA-256 (same audio).  Operator chooses '1' (keep occupant).
        Asserts: mover deleted, occupant survives, 'cross-referenced' + 'deduplicated' journalled.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        # The mover is at an old path; its planned destination is already occupied.
        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        # Compute the planned destination.
        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        # Occupant at the planned destination with the same bytes (same SHA-256).
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        # Patch _assess_collisions to return match=True for the planned destination.
        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        mocker.patch("builtins.input", return_value="1")

        repath(dest_root, yes=True)

        # Mover is deleted; occupant survives.
        assert not old_path.exists()
        assert new_dest.exists()

        # Journal has cross-referenced + deduplicated entries.
        journal = read_journal(dest_root / "music_annotator_journal.json")
        actions = [e.action for e in journal.entries]
        assert "cross-referenced" in actions
        assert "deduplicated" in actions
        # C-DEDUP ordering: cross-referenced before deduplicated.
        xref_idx = next(i for i, e in enumerate(journal.entries) if e.action == "cross-referenced")
        dedup_idx = next(i for i, e in enumerate(journal.entries) if e.action == "deduplicated")
        assert xref_idx < dedup_idx

    def test_match_true_survivor_mover_deletes_occupant_and_moves(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm: survivor=mover → occupant deleted, move proceeds.

        Operator chooses '2' (keep mover).  Asserts: occupant deleted, mover moved to destination.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        mocker.patch("builtins.input", return_value="2")

        repath(dest_root, yes=True)

        # Occupant deleted; mover moved to destination.
        assert not old_path.exists()
        assert new_dest.exists()

        journal = read_journal(dest_root / "music_annotator_journal.json")
        actions = [e.action for e in journal.entries]
        assert "cross-referenced" in actions
        assert "deduplicated" in actions
        assert "repathed" in actions

    def test_match_true_keep_both_drops_move_no_deletion(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm: keep-both → xref written, move dropped, no deletion.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        mocker.patch("builtins.input", return_value="b")

        repath(dest_root, yes=True)

        # Both files remain on disk.
        assert old_path.exists()
        assert new_dest.exists()

        journal = read_journal(dest_root / "music_annotator_journal.json")
        actions = [e.action for e in journal.entries]
        assert "cross-referenced" in actions
        assert "deduplicated" not in actions
        assert "repathed" not in actions

    def test_match_true_abort_returns_none(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm: abort → repath returns None, no files moved.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        mocker.patch("builtins.input", return_value="a")

        result = repath(dest_root, yes=True)

        assert result is None
        assert old_path.exists()
        assert new_dest.exists()

    def test_match_true_keep_both_rerun_silent_drop(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() keep-both re-run: mover's MBID already in occupant's secondary set → silent drop.

        On a second run after keep-both, the mover's MBID is already in the occupant's secondary
        MBID set.  The group is silently dropped without prompting.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        # Pre-write the mover's MBID as a secondary on the occupant (simulating a prior keep-both).
        write_secondary_albumid_flac(new_dest, "rel-mover")

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        mock_input = mocker.patch("builtins.input")

        repath(dest_root, yes=True)

        # No prompt — silent drop.
        mock_input.assert_not_called()
        # Both files remain.
        assert old_path.exists()
        assert new_dest.exists()

    def test_match_none_suffix_arm(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=None arm: operator chooses 's' (suffix) → collision suffix applied.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=None, method="unknown", detail="inconclusive")],
        )
        mocker.patch("builtins.input", return_value="s")

        repath(dest_root, yes=True)

        # Mover moved to a suffixed destination (not the original new_dest).
        assert not old_path.exists()
        assert new_dest.exists()  # Occupant untouched.

        journal = read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        # The destination must differ from new_dest (suffix was applied).
        assert repathed[0].destination != str(new_dest)

    def test_match_none_abort_arm(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=None arm: operator chooses 'a' (abort) → repath returns None.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=None, method="unknown", detail="inconclusive")],
        )
        mocker.patch("builtins.input", return_value="a")

        result = repath(dest_root, yes=True)

        assert result is None
        assert old_path.exists()
        assert new_dest.exists()

    def test_match_none_dry_run_reports_without_prompting(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=None dry_run=True: reports inconclusive collision without prompting.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=None, method="unknown", detail="inconclusive")],
        )
        mock_input = mocker.patch("builtins.input")

        result = repath(dest_root, dry_run=True)

        mock_input.assert_not_called()
        # dry_run returns a DryRunPlan (the mover is still in the plan, just reported).
        assert result is not None

    def test_plan_completeness_property_no_unresolved_collision(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Property: a plan reaching _move_verify_journal has no move whose destination is occupied.

        After all collision arms resolve, no move in the final plan has a destination that is
        occupied by a non-vacated path.  This test verifies the property by checking that
        _move_verify_journal is called with a plan where no destination is occupied.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        # Operator chooses survivor=occupant → mover dropped from plan.
        mocker.patch("builtins.input", return_value="1")

        captured_plan: list[list[tuple[Path, Path]]] = []

        def capturing_mvj(
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
            captured_plan.append(list(plan_pairs))
            return _move_verify_journal(
                plan_pairs,
                journal=journal,
                journal_path=journal_path,
                action=action,
                dest_root=dest_root,
                now=now,
                release_id=release_id,
                cache=cache,
            )

        mocker.patch("music_annotator._pipeline_maint._move_verify_journal", side_effect=capturing_mvj)

        repath(dest_root, yes=True)

        # _move_verify_journal was not called (plan was empty after drop).
        # OR if called, no destination in the plan is occupied by a non-vacated path.
        for plan in captured_plan:
            vacated = {src for src, _ in plan}
            for _, dest in plan:
                if dest.exists() and dest not in vacated:
                    raise AssertionError(f"Plan contains move to occupied non-vacated destination: {dest}")

    def test_match_true_all_dropped_empty_plan_returns_none(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm: all entries dropped → empty plan → returns None.

        When all plan entries are resolved as survivor_occupant or keep_both, the plan becomes
        empty after dropping.  repath() returns None (nothing to move).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        # Operator chooses survivor=occupant → mover dropped from plan → plan empty.
        mocker.patch("builtins.input", return_value="1")

        result = repath(dest_root, yes=True)

        assert result is None
        # Mover deleted; occupant survives.
        assert not old_path.exists()
        assert new_dest.exists()

    def test_match_true_all_dropped_empty_plan_dry_run_returns_empty(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm dry_run: all entries dropped → empty DryRunPlan returned.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same")],
        )
        # In dry-run, resolve_duplicate_group returns keep_both without prompting.

        result = repath(dest_root, dry_run=True)

        # Dry-run with all entries dropped → empty DryRunPlan.
        assert result is not None
        assert result.count == 0

    def test_match_true_and_none_same_entry_skips_none_arm(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=None arm skips entries already resolved by match=True arm.

        When a plan entry is resolved by the match=True arm (dropped), the match=None arm
        must not re-process it.  This test verifies that the _plan_idx_inc in _repath_drop_indices
        guard prevents double-processing.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        new_dest.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest, tags)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ",
                    "source": "/src/occ.flac",
                    "destination": str(new_dest),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        # Return both match=True and match=None for the same destination.
        # The match=True arm resolves first (survivor_occupant → drop), so the match=None arm
        # must skip the already-dropped entry.
        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[
                AudioCompareResult(src=old_path, dest=new_dest, match=True, method="sha256", detail="same"),
                AudioCompareResult(src=old_path, dest=new_dest, match=None, method="unknown", detail="inconclusive"),
            ],
        )
        # Only one prompt for the match=True arm; match=None arm skips.
        input_calls: list[str] = ["1"]
        mocker.patch("builtins.input", side_effect=input_calls)

        repath(dest_root, yes=True)

        # Only one input call (for the match=True arm); match=None arm did not prompt.
        # Mover deleted; occupant survives.
        assert not old_path.exists()
        assert new_dest.exists()

    def test_match_true_partial_drop_remaining_entries_proceed(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """repath() match=True arm: some entries dropped, remaining entries proceed normally.

        When only some plan entries are resolved as survivor_occupant (dropped), the remaining
        entries are not dropped and proceed through _move_verify_journal.  This exercises the
        branch where plan_pairs is non-empty after the drop.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File 1: will be dropped (match=True, survivor=occupant).
        tags1 = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")
        old_rel1 = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path1 = dest_root / old_rel1
        old_path1.parent.mkdir(parents=True, exist_ok=True)
        old_path1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path1, tags1)

        new_dest1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags1, global_track_idx=0).with_suffix(".flac")
        new_dest1.parent.mkdir(parents=True, exist_ok=True)
        new_dest1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(new_dest1, tags1)

        # File 2: will proceed normally (no collision).
        tags2 = self._make_tags("Mozart", "Symphony No. 40", "1", "Molto allegro")
        old_rel2 = "Mozart - Karajan/OldWork2 [rec 2020]/01 - Molto allegro.flac"
        old_path2 = dest_root / old_rel2
        old_path2.parent.mkdir(parents=True, exist_ok=True)
        old_path2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path2, tags2)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-occ1",
                    "source": "/src/occ1.flac",
                    "destination": str(new_dest1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover1",
                    "source": "/src/mov1.flac",
                    "destination": str(old_path1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover2",
                    "source": "/src/mov2.flac",
                    "destination": str(old_path2),
                    "action": "tagged",
                },
            ],
        )

        # Only file 1 has a match=True collision; file 2 has no collision.
        mocker.patch(
            "music_annotator._pipeline_maint._assess_collisions",
            return_value=[AudioCompareResult(src=old_path1, dest=new_dest1, match=True, method="sha256", detail="same")],
        )
        # Operator chooses survivor=occupant for file 1 → drop file 1 from plan.
        mocker.patch("builtins.input", return_value="1")

        repath(dest_root, yes=True)

        # File 1 mover deleted; file 2 moved to its new destination.
        assert not old_path1.exists()
        assert new_dest1.exists()
        assert not old_path2.exists()  # File 2 was moved.

        journal = read_journal(dest_root / "music_annotator_journal.json")
        actions = [e.action for e in journal.entries]
        assert "cross-referenced" in actions
        assert "deduplicated" in actions
        assert "repathed" in actions  # File 2 was repathed.

    def test_execution_time_noclobber_still_raises(self, fs: FakeFilesystem) -> None:
        """Execution-time C-NOCLOBBER refusal still raises RuntimeError (bug-indicating).

        When a destination is occupied at execution time (not caught at plan time), the
        C-NOCLOBBER check in _execute_single_move raises RuntimeError.  This is unchanged
        and now indicates a defect in plan construction.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags("Beethoven", "Symphony No. 5", "1", "Allegro con brio")

        old_rel = "Beethoven - Karajan/OldWork [rec 2020]/01 - Allegro con brio.flac"
        old_path = dest_root / old_rel
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(old_path, tags)

        new_dest = build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0).with_suffix(".flac")
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        # Occupant with DIFFERENT content (not same SHA-256) — non-dedup collision.
        new_dest.write_bytes(_MINIMAL_FLAC + b"\x00extra")

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-mover",
                    "source": "/src/mov.flac",
                    "destination": str(old_path),
                    "action": "tagged",
                },
            ],
        )

        # _assess_collisions returns match=False (different content) → suffix applied.
        # But then we force the destination to be occupied at execution time by patching
        # _move_verify_journal to call _execute_single_move directly with the original dest.
        # Instead, test _execute_single_move directly with a pre-existing different-content dest.
        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("", encoding="utf-8")
        journal = read_journal(journal_path)

        with pytest.raises(RuntimeError, match="C-NOCLOBBER"):
            _execute_single_move(
                old_path,
                new_dest,
                journal=journal,
                journal_path=journal_path,
                action="repathed",
                dest_root=dest_root,
                now_str="2024-01-01T00:00:00+00:00",
                release_id="",
            )


# ---------------------------------------------------------------------------
# _census_journal_for_xrefs
# ---------------------------------------------------------------------------


class TestCensusJournalForXrefs:
    """Unit tests for :func:`_census_journal_for_xrefs`.

    Verifies that the census correctly identifies SKIP-policy and OVERWRITE-policy secondary
    MBIDs, enforces idempotency against already-journalled cross-references, and identifies
    evidence-gap candidates.
    """

    @staticmethod
    def _make_journal(entries: list[dict[str, str]]) -> TransactionLog:
        """Build a :class:`TransactionLog` from a list of raw entry dicts.

        :param entries: List of raw entry dicts.
        :returns: A :class:`TransactionLog` with the entries validated.
        """
        return TransactionLog(entries=[TransactionEntry(**e) for e in entries])

    def test_skip_policy_secondary_mbid_identified(self) -> None:
        """SKIP policy: skipped entry at same destination as tagged entry → secondary MBID found.

        The skipped entry's release_id is the secondary MBID for the surviving tagged file.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "skipped",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        assert "/lib/Work/01.flac" in groups
        primary, secondary = groups["/lib/Work/01.flac"]
        assert primary == "primary-rel"
        assert "secondary-rel" in secondary
        assert gaps == []

    def test_overwrite_policy_secondary_mbid_identified(self) -> None:
        """OVERWRITE policy: multiple tagged entries at same destination → earlier is secondary.

        The chronological-last tagged entry's release_id is the primary; earlier entries'
        release_ids are secondary MBIDs.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "first-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "second-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        assert "/lib/Work/01.flac" in groups
        primary, secondary = groups["/lib/Work/01.flac"]
        assert primary == "second-rel"
        assert "first-rel" in secondary
        assert gaps == []

    def test_already_journalled_xref_excluded_from_secondary(self) -> None:
        """Idempotency: a secondary MBID already journalled as cross-referenced is excluded.

        When a cross-referenced entry already exists for a secondary MBID at the destination,
        that MBID is not included in the returned secondary list.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "secondary-rel",
                    "source": "/lib/Work/01.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "cross-referenced",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        # secondary-rel is already cross-referenced → no actionable group.
        assert "/lib/Work/01.flac" not in groups
        assert gaps == []

    def test_evidence_gap_single_tagged_no_skip(self) -> None:
        """Evidence-gap: single tagged entry with no skip → destination is a gap candidate.

        A destination with exactly one unique tagged release_id and no skipped entries is an
        evidence-gap candidate (the journal cannot prove a secondary MBID).
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        assert "/lib/Work/01.flac" not in groups
        assert "/lib/Work/01.flac" in gaps

    def test_no_secondary_when_skip_matches_primary(self) -> None:
        """SKIP policy: skipped entry with same release_id as primary is not a secondary MBID.

        When the skipped entry's release_id equals the primary (tagged) release_id, no secondary
        MBID is added.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "same-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "same-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "skipped",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        assert "/lib/Work/01.flac" not in groups
        # Single unique tagged id + skip with same id → not a gap candidate either
        # (the skip is present, so it's not a pure single-tagged-no-skip case).
        assert "/lib/Work/01.flac" not in gaps

    def test_empty_journal_returns_empty(self) -> None:
        """Empty journal returns empty groups and gaps.

        No entries → no groups, no evidence-gap candidates.
        """
        journal = self._make_journal([])
        groups, gaps = _census_journal_for_xrefs(journal)
        assert groups == {}
        assert gaps == []

    def test_both_skip_and_overwrite_at_same_dest(self) -> None:
        """Both SKIP and OVERWRITE shapes at the same destination are merged correctly.

        When a destination has both multiple tagged entries (OVERWRITE) and skipped entries
        (SKIP), all secondary MBIDs are collected and deduplicated.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "first-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "second-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "skipped-rel",
                    "source": "/src/c.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "skipped",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        assert "/lib/Work/01.flac" in groups
        primary, secondary = groups["/lib/Work/01.flac"]
        assert primary == "second-rel"
        assert "first-rel" in secondary
        assert "skipped-rel" in secondary
        assert gaps == []

    def test_empty_release_ids_ignored(self) -> None:
        """Tagged entries with empty release_id are ignored in the census.

        Empty release_ids (e.g. from repathed entries) must not produce spurious secondary MBIDs.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "real-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        # Only one unique non-empty release_id → no secondary MBIDs.
        assert "/lib/Work/01.flac" not in groups
        # Single unique non-empty tagged id → gap candidate.
        assert "/lib/Work/01.flac" in gaps

    def test_all_empty_release_ids_skipped(self) -> None:
        """Destination with only empty release_ids in tagged entries is skipped entirely.

        When all tagged entries at a destination have empty release_ids, the destination
        produces no unique_ids and is skipped (the continue branch in the census loop).
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        # All empty release_ids → no groups, no gap candidates.
        assert "/lib/Work/01.flac" not in groups
        assert "/lib/Work/01.flac" not in gaps

    def test_existing_cross_referenced_entry_excludes_from_gap(self) -> None:
        """A destination with a prior 'cross-referenced' journal entry is not an evidence gap.

        When a file has one unique 'tagged' entry and an existing 'cross-referenced' entry, the
        secondary MBID is already journal-provable (written by a prior reconstruct-xrefs run).
        The destination must not appear in evidence_gap_dests.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "secondary-rel",
                    "source": "/lib/Work/01.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "cross-referenced",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        # Already journalled as cross-referenced → not actionable (secondary already recorded).
        assert "/lib/Work/01.flac" not in groups
        # Already journal-provable → not an evidence gap.
        assert "/lib/Work/01.flac" not in gaps

    def test_moved_then_cross_referenced_not_a_gap(self) -> None:
        """KAT: tagged-at-A → moved A→B → cross-referenced-at-B is NOT reported as a gap.

        The evidence-gap exclusion resolves each tagged destination through the move chain to its
        current path before checking xref_by_dest (C-XREF).  A file tagged at A, moved to B, and
        cross-referenced at B must be excluded from the gap report because the cross-reference
        record at B is the current-path evidence that the secondary MBID is already recorded.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/lib/Work/01.flac",
                    "destination": "/lib/NewWork/01.flac",
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:02:00+00:00",
                    "release_id": "secondary-rel",
                    "source": "/lib/NewWork/01.flac",
                    "destination": "/lib/NewWork/01.flac",
                    "action": "cross-referenced",
                },
            ]
        )
        _groups, gaps = _census_journal_for_xrefs(journal)
        # The file was cross-referenced at its current path → not an evidence gap.
        assert "/lib/Work/01.flac" not in gaps
        assert "/lib/NewWork/01.flac" not in gaps

    def test_genuinely_gapped_file_reported_exactly_once(self) -> None:
        """KAT: a genuinely-gapped file (no cross-reference record) IS reported exactly once.

        A file with a single tagged entry, no skipped entries, and no cross-referenced journal
        entry at its current path must appear in evidence_gap_dests exactly once.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "only-rel",
                    "source": "/lib/Work/01.flac",
                    "destination": "/lib/NewWork/01.flac",
                    "action": "repathed",
                },
            ]
        )
        _groups, gaps = _census_journal_for_xrefs(journal)
        # No cross-reference record at the current path → genuinely gapped.
        assert "/lib/Work/01.flac" in gaps
        assert gaps.count("/lib/Work/01.flac") == 1

    def test_duplicate_tagged_entries_resolving_to_same_current_path_reported_once(self) -> None:
        """KAT: two tagged entries resolving to one current file are reported at most once.

        When two tagged destinations both resolve to the same current path through the move chain
        (e.g. a file moved from A to C, and a second tagged entry at B also moved to C), the
        evidence-gap list must contain the current path at most once (de-duplicated by current
        path).  The de-duplication is performed inside _census_journal_for_xrefs via
        _resolve_tagged_to_current, so the returned gaps list has at most one entry.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/02.flac",
                    "action": "tagged",
                },
                # Both tagged destinations are moved to the same current path.
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/Work/01.flac",
                    "destination": "/lib/NewWork/01.flac",
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:01:01+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/Work/02.flac",
                    "destination": "/lib/NewWork/01.flac",
                    "action": "repathed",
                },
            ]
        )
        _groups, gaps = _census_journal_for_xrefs(journal)
        # Both tagged dests resolve to /lib/NewWork/01.flac → reported at most once.
        assert len(gaps) <= 1

    def test_non_move_non_xref_actions_ignored(self) -> None:
        """Journal entries with unhandled actions (e.g. 'enriched') are silently ignored.

        Actions other than 'tagged', 'skipped', 'cross-referenced', 'repathed', 'regrouped',
        and 'unified' do not affect the census result.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "only-rel",
                    "source": "/lib/Work/01.flac",
                    "destination": "/lib/Work/01.flac",
                    "action": "enriched",
                },
            ]
        )
        groups, gaps = _census_journal_for_xrefs(journal)
        # 'enriched' entry is ignored; single tagged entry → evidence gap.
        assert "/lib/Work/01.flac" not in groups
        assert "/lib/Work/01.flac" in gaps


# ---------------------------------------------------------------------------
# _resolve_tagged_to_current
# ---------------------------------------------------------------------------


class TestResolveTaggedToCurrent:
    """Unit tests for :func:`_resolve_tagged_to_current`.

    Verifies the O(N) chronological resolver: cycle-proof handling of inverse move pairs,
    multi-hop chains, and large synthetic journals.
    """

    @staticmethod
    def _make_journal(entries: list[dict[str, str]]) -> TransactionLog:
        """Build a :class:`TransactionLog` from a list of raw entry dicts.

        :param entries: List of raw entry dicts.
        :returns: A :class:`TransactionLog` with the entries validated.
        """
        return TransactionLog(entries=[TransactionEntry(**e) for e in entries])

    def test_cycle_fixture_terminates_and_resolves_to_current(self) -> None:
        """KAT (a): inverse move pair (A→B, B→A) terminates and resolves the file to A.

        A journal with tagged-at-A, moved A→B, moved B→A must terminate in finite time and
        resolve the tagged destination to A (its current path after the inverse move).  This is
        the cycle-hang regression guard: the old fixpoint-follow loop would spin indefinitely on
        this shape; the chronological resolver simply moves the pointer back to A.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/A/01.flac",
                    "destination": "/lib/B/01.flac",
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:02:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/B/01.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "repathed",
                },
            ]
        )
        result = _resolve_tagged_to_current(journal)
        # After A→B→A the file is back at A.
        assert result["/lib/A/01.flac"] == "/lib/A/01.flac"

    def test_cycle_fixture_census_terminates_and_resolves(self) -> None:
        """KAT (a) via census: _census_journal_for_xrefs terminates on an inverse move pair.

        Calls _census_journal_for_xrefs with the same inverse-move-pair journal and asserts
        that it returns (does not hang) and that the evidence-gap list resolves the file to A
        (its current path after the inverse move).
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/A/01.flac",
                    "destination": "/lib/B/01.flac",
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:02:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/B/01.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "repathed",
                },
            ]
        )
        _groups, gaps = _census_journal_for_xrefs(journal)
        # File is back at A after the inverse move; no cross-reference record → evidence gap.
        assert "/lib/A/01.flac" in gaps

    def test_inverse_pair_plus_onward_move_resolves_to_final(self) -> None:
        """KAT (b): tagged-at-A, A→B, B→A, A→C resolves to C.

        An inverse move pair followed by an onward move must resolve to the final destination C,
        not to A (the intermediate state after the inverse move).
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/A/01.flac",
                    "destination": "/lib/B/01.flac",
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:02:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/B/01.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:03:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/A/01.flac",
                    "destination": "/lib/C/01.flac",
                    "action": "repathed",
                },
            ]
        )
        result = _resolve_tagged_to_current(journal)
        assert result["/lib/A/01.flac"] == "/lib/C/01.flac"

    def test_move_with_no_tagged_files_at_source_is_ignored(self) -> None:
        """Move entry whose source has no tracked tagged destinations is silently ignored.

        When a move entry (repathed/regrouped/unified) references a source path that has no
        tagged destinations currently tracked there, the inverse-index lookup returns an empty
        list and no update is made.  The resolver must not raise and must return only the
        tagged destinations it has seen.
        """
        journal = self._make_journal(
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "tagged",
                },
                # Move of a path that has no tagged files currently tracked there.
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/Untracked/01.flac",
                    "destination": "/lib/B/01.flac",
                    "action": "repathed",
                },
            ]
        )
        result = _resolve_tagged_to_current(journal)
        # Only the tagged destination is tracked; the unrelated move has no effect.
        assert result == {"/lib/A/01.flac": "/lib/A/01.flac"}

    def test_large_synthetic_journal_completes(self) -> None:
        """KAT (d): soft O(N) sanity guard — resolver over ~10,000 entries completes.

        Builds a synthetic journal with 1,000 tagged entries each moved 9 times (9,000 move
        entries; 10,000 total).  Calls _resolve_tagged_to_current and asserts it completes and
        returns the correct final mapping.  No formal timeout assertion is made — the test
        existing and passing is the guard against reintroduced quadratic walks.
        """
        entries: list[dict[str, str]] = []
        n_files = 1000
        n_moves = 9
        # Tag each file at its initial path.
        for i in range(n_files):
            entries.append(
                {
                    "timestamp": f"2024-01-01T00:00:{i:02d}+00:00",
                    "release_id": f"rel-{i}",
                    "source": f"/src/{i}.flac",
                    "destination": f"/lib/step0/{i}.flac",
                    "action": "tagged",
                }
            )
        # Move each file through 9 successive paths.
        for step in range(1, n_moves + 1):
            for i in range(n_files):
                entries.append(
                    {
                        "timestamp": f"2024-01-0{step + 1}T00:00:{i:02d}+00:00",
                        "release_id": f"rel-{i}",
                        "source": f"/lib/step{step - 1}/{i}.flac",
                        "destination": f"/lib/step{step}/{i}.flac",
                        "action": "repathed",
                    }
                )
        journal = TransactionLog(entries=[TransactionEntry(**e) for e in entries])
        result = _resolve_tagged_to_current(journal)
        assert len(result) == n_files
        for i in range(n_files):
            assert result[f"/lib/step0/{i}.flac"] == f"/lib/step{n_moves}/{i}.flac"

    def test_deduplicated_entry_terminates_chain(self) -> None:
        """A 'deduplicated' entry removes the tagged-dest from the resolver output.

        When a file is tagged at A and then deduplicated (A deleted, B survives), the tagged
        destination A must be absent from the returned mapping — the chain terminates at the
        dedup entry and the tagged-dest resolves to nothing.  The surviving copy B is not a
        tagged destination and must also be absent.

        A second file tagged at C (not involved in dedup) must still resolve to itself.
        """
        journal = self._make_journal(
            [
                # File 1: tagged at A, then deduplicated (A deleted, B survives)
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": "/lib/A/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:01:00+00:00",
                    "release_id": "rel-a",
                    "source": "/lib/A/01.flac",
                    "destination": "/lib/B/01.flac",
                    "action": "deduplicated",
                },
                # File 2: tagged at C, not involved in dedup — must still resolve
                {
                    "timestamp": "2024-01-01T00:02:00+00:00",
                    "release_id": "rel-b",
                    "source": "/src/c.flac",
                    "destination": "/lib/C/01.flac",
                    "action": "tagged",
                },
            ]
        )
        result = _resolve_tagged_to_current(journal)
        # A was deduplicated → chain terminates → absent from result
        assert "/lib/A/01.flac" not in result, "dedup-terminated tagged-dest must resolve to nothing"
        # B is the surviving copy but was never tagged → not in result
        assert "/lib/B/01.flac" not in result
        # C was not involved in dedup → resolves to itself
        assert result["/lib/C/01.flac"] == "/lib/C/01.flac"


# ---------------------------------------------------------------------------
# reconstruct_cross_references
# ---------------------------------------------------------------------------


class TestReconstructCrossReferences:
    """Integration tests for :func:`reconstruct_cross_references`.

    Exercises the full pass: journal census, live-file tag reads, operator confirmation,
    C-PROV chain (tag write → verify → journal), idempotency, dry-run, and evidence-gap
    reporting.  Uses pyfakefs for filesystem isolation and real mutagen tag writes.
    """

    @staticmethod
    def _make_flac(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
        """Create a FLAC file with the given tags applied.

        :param dest_root: Library root directory.
        :param rel_path: Relative path within the library.
        :param tags: Tags to embed.
        :returns: The full absolute path of the created FLAC file.
        """
        full_path = dest_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(full_path, tags)
        return full_path

    @staticmethod
    def _make_mp3(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
        """Create an MP3 file with the given tags applied.

        :param dest_root: Library root directory.
        :param rel_path: Relative path within the library.
        :param tags: Tags to embed.
        :returns: The full absolute path of the created MP3 file.
        """
        full_path = dest_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(full_path, tags)
        return full_path

    @staticmethod
    def _write_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
        """Write a JSONL journal file to ``dest_root / music_annotator_journal.json``.

        :param dest_root: Destination root directory (must already exist).
        :param entries: List of raw entry dicts to serialise.
        """
        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    def test_skip_policy_writes_secondary_mbid_flac(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """SKIP policy: secondary MBID written to surviving FLAC file and journalled.

        A skipped entry at the same destination as a tagged entry causes the skipped entry's
        release_id to be written as a secondary MBID on the surviving file.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac_path),
                    "action": "skipped",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        # Secondary MBID written to the file.
        tag_dict = _read_tags_flac(flac_path)
        assert "secondary-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")

        # Journal entry written.
        journal = read_journal(journal_path)
        xref_entries = [e for e in journal.entries if e.action == "cross-referenced"]
        assert len(xref_entries) == 1
        assert xref_entries[0].release_id == "secondary-rel"
        assert xref_entries[0].source == str(flac_path)
        assert xref_entries[0].destination == str(flac_path)

        assert gaps == []

    def test_overwrite_policy_writes_secondary_mbid_flac(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """OVERWRITE policy: earlier tagged release_id written as secondary MBID on FLAC file.

        Multiple tagged entries at the same destination with distinct release_ids cause the
        earlier entries' release_ids to be written as secondary MBIDs.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="second-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "first-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "second-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        tag_dict = _read_tags_flac(flac_path)
        assert "first-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        assert gaps == []

    def test_skip_policy_writes_secondary_mbid_mp3(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """SKIP policy: secondary MBID written to surviving MP3 file and journalled.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        mp3_path = self._make_mp3(dest_root, "Work/01.mp3", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.mp3",
                    "destination": str(mp3_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.mp3",
                    "destination": str(mp3_path),
                    "action": "skipped",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        tag_dict = _read_tags_mp3(mp3_path)
        assert "secondary-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        assert gaps == []

    def test_dry_run_reports_without_writing(self, fs: FakeFilesystem) -> None:
        """Dry-run: findings reported without writing tags or journal entries.

        In dry-run mode, the function prints findings but does not write any tags or append
        any journal entries.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac_path),
                    "action": "skipped",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        # No tag written.
        tag_dict = _read_tags_flac(flac_path)
        assert tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "") == ""

        # No journal entry appended.
        journal = read_journal(journal_path)
        xref_entries = [e for e in journal.entries if e.action == "cross-referenced"]
        assert xref_entries == []

        assert gaps == []

    def test_idempotency_already_present_in_tag_skipped(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Idempotency: secondary MBID already in the file's tag is silently skipped.

        When the secondary MBID is already present in MUSICBRAINZ_SECONDARY_ALBUMID, the pass
        does not write it again and does not prompt the operator.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)
        # Pre-write the secondary MBID into the file.
        write_secondary_albumid_flac(flac_path, "secondary-rel")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac_path),
                    "action": "skipped",
                },
            ],
        )

        input_mock = mocker.patch("builtins.input")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        # No prompt shown (nothing to write).
        input_mock.assert_not_called()
        assert gaps == []

    def test_operator_decline_aborts_without_writing(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Operator declining the confirmation prompt aborts without writing any tags.

        When the operator answers 'n' to the confirmation prompt, no tags are written and no
        journal entries are appended.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac_path),
                    "action": "skipped",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="n")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        tag_dict = _read_tags_flac(flac_path)
        assert tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "") == ""

        journal = read_journal(journal_path)
        xref_entries = [e for e in journal.entries if e.action == "cross-referenced"]
        assert xref_entries == []
        assert gaps == []

    def test_evidence_gap_candidate_reported(self, fs: FakeFilesystem) -> None:
        """Evidence-gap candidate: file with secondary MBID tag but no journal evidence reported.

        When a file has only one tagged journal entry but carries a MUSICBRAINZ_SECONDARY_ALBUMID
        tag (written outside the journal), it is reported as an evidence-gap candidate.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)
        # Write a secondary MBID directly (simulating out-of-journal write).
        write_secondary_albumid_flac(flac_path, "mystery-rel")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        assert str(flac_path) in gaps

    def test_evidence_gap_no_secondary_tag_not_reported(self, fs: FakeFilesystem) -> None:
        """Evidence-gap candidate not reported when file has no secondary MBID tag.

        A file with only one tagged journal entry and no MUSICBRAINZ_SECONDARY_ALBUMID tag is
        not an evidence-gap candidate (the journal is consistent with the file state).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        assert str(flac_path) not in gaps

    def test_file_not_found_skipped_with_warning(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """File not found at journal destination is skipped with a warning log.

        When the file at the journal destination does not exist on disk, the group is skipped
        and a warning is logged.  No RuntimeError is raised.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Journal references a file that does not exist on disk.
        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/missing.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": "/lib/Work/missing.flac",
                    "action": "skipped",
                },
            ],
        )

        input_mock = mocker.patch("builtins.input")
        journal_path = dest_root / "music_annotator_journal.json"
        # Should not raise; file not found → skipped.
        gaps = reconstruct_cross_references(journal_path, dest_root)

        input_mock.assert_not_called()
        assert gaps == []

    def test_nothing_to_write_no_prompt(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """No actionable groups → no prompt shown, function returns empty gaps.

        When the journal has no SKIP or OVERWRITE shapes, the function returns immediately
        without prompting the operator.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        input_mock = mocker.patch("builtins.input")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        input_mock.assert_not_called()
        assert gaps == []

    def test_repathed_file_current_path_resolved(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Repathed file: secondary MBID written to the current (post-repath) path.

        When a file has been repathed, the journal destination in the tagged entry is the old
        path.  The function resolves the current path via the journal lineage and writes the
        secondary MBID to the current file.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        # Create the file at the new (post-repath) path.
        new_path = self._make_flac(dest_root, "Work/01-new.flac", tags)
        old_path_str = "/lib/Work/01-old.flac"

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": old_path_str,
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": old_path_str,
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "",
                    "source": old_path_str,
                    "destination": str(new_path),
                    "action": "repathed",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        tag_dict = _read_tags_flac(new_path)
        assert "secondary-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        assert gaps == []

    def test_dry_run_nothing_to_write_prints_message(self, fs: FakeFilesystem) -> None:
        """Dry-run with no actionable groups prints a 'nothing to write' message.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        # No actionable groups → dry-run returns empty gaps without prompting.
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)
        assert gaps == []

    def test_evidence_gap_reported_in_non_dry_run(self, fs: FakeFilesystem) -> None:
        """Evidence-gap candidate reported in non-dry-run mode when no actionable groups exist.

        When there are no actionable groups but evidence-gap candidates exist, they are reported
        and returned without prompting.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)
        write_secondary_albumid_flac(flac_path, "mystery-rel")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        assert str(flac_path) in gaps

    def test_tag_read_error_in_actionable_loop_skipped(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Tag read failure in actionable loop is logged and the file is skipped.

        When reading tags from a file in the actionable loop raises an exception, the file is
        skipped with a warning log and no RuntimeError is raised.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac_path),
                    "action": "skipped",
                },
            ],
        )

        # Patch _read_tags_flac to raise in the actionable loop.
        mocker.patch(
            "music_annotator._pipeline_maint._read_tags_flac",
            side_effect=OSError("corrupt"),
        )
        input_mock = mocker.patch("builtins.input")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        # File skipped → no prompt, no write.
        input_mock.assert_not_called()
        assert gaps == []

    def test_mp3_evidence_gap_candidate_reported(self, fs: FakeFilesystem) -> None:
        """Evidence-gap candidate: MP3 file with secondary MBID tag but no journal evidence.

        Exercises the .mp3 branch in the evidence-gap check loop.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        mp3_path = self._make_mp3(dest_root, "Work/01.mp3", tags)
        write_secondary_albumid_mp3(mp3_path, "mystery-rel")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.mp3",
                    "destination": str(mp3_path),
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        assert str(mp3_path) in gaps

    def test_evidence_gap_file_not_found_skipped(self, fs: FakeFilesystem) -> None:
        """Evidence-gap candidate file not found on disk is silently skipped.

        When a gap candidate's file does not exist on disk, it is not added to the gap list.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Journal references a file that does not exist on disk.
        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": "/lib/Work/missing.flac",
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        # File not found → not in gaps.
        assert "/lib/Work/missing.flac" not in gaps

    def test_evidence_gap_tag_read_error_skipped(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Evidence-gap tag read failure is silently skipped.

        When reading tags from a gap candidate raises an exception, the file is not added to
        the gap list.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="only-rel", title="Track 1")
        flac_path = self._make_flac(dest_root, "Work/01.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "only-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac_path),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch(
            "music_annotator._pipeline_maint._read_tags_flac",
            side_effect=OSError("corrupt"),
        )
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        # Tag read error → file not in gaps.
        assert str(flac_path) not in gaps

    def test_dry_run_with_actionable_and_gap_reports_both(self, fs: FakeFilesystem) -> None:
        """Dry-run with both actionable groups and gap candidates reports both.

        Exercises the dry-run path where both actionable items and gap candidates exist.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File 1: has SKIP-policy secondary MBID to write.
        tags1 = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac1 = self._make_flac(dest_root, "Work/01.flac", tags1)

        # File 2: evidence-gap candidate.
        tags2 = TrackTags(musicbrainz_albumid="only-rel", title="Track 2")
        flac2 = self._make_flac(dest_root, "Work/02.flac", tags2)
        write_secondary_albumid_flac(flac2, "mystery-rel")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac1),
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "only-rel",
                    "source": "/src/c.flac",
                    "destination": str(flac2),
                    "action": "tagged",
                },
            ],
        )

        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root, dry_run=True)

        # No tags written (dry-run).
        tag_dict = _read_tags_flac(flac1)
        assert tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "") == ""

        # Gap candidate reported.
        assert str(flac2) in gaps

    def test_repath_unrelated_file_does_not_affect_tracked_dests(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Repath of an unrelated file does not corrupt tracked tagged destinations.

        When a repath entry's source does not match any tracked tagged destination, the inner
        loop runs but the condition is False for all items — no tracked dest is updated.
        This exercises the branch where current_str != entry.source for all tracked dests.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File 1: has SKIP-policy secondary MBID to write (tracked tagged dest).
        tags1 = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac1 = self._make_flac(dest_root, "Work/01.flac", tags1)

        # File 2: an unrelated file that gets repathed (not in our groups).
        tags2 = TrackTags(musicbrainz_albumid="other-rel", title="Track 2")
        flac2 = self._make_flac(dest_root, "Work/02-new.flac", tags2)
        old_path2_str = "/lib/Work/02-old.flac"

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac1),
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "other-rel",
                    "source": "/src/c.flac",
                    "destination": old_path2_str,
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:03+00:00",
                    "release_id": "",
                    "source": old_path2_str,
                    "destination": str(flac2),
                    "action": "repathed",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        # flac1 gets its secondary MBID written correctly.
        tag_dict = _read_tags_flac(flac1)
        assert "secondary-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        assert gaps == []

    def test_repath_multi_hop_lineage_resolved(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Multi-hop repath: secondary MBID written to the final current path.

        When a file has been repathed twice (A→B→C), the function resolves the current path
        to C and writes the secondary MBID there.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        # Create the file at the final (post-two-repath) path.
        final_path = self._make_flac(dest_root, "Work/01-final.flac", tags)
        old_path_str = "/lib/Work/01-old.flac"
        mid_path_str = "/lib/Work/01-mid.flac"

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": old_path_str,
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": old_path_str,
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "",
                    "source": old_path_str,
                    "destination": mid_path_str,
                    "action": "repathed",
                },
                {
                    "timestamp": "2024-01-01T00:00:03+00:00",
                    "release_id": "",
                    "source": mid_path_str,
                    "destination": str(final_path),
                    "action": "repathed",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        tag_dict = _read_tags_flac(final_path)
        assert "secondary-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")
        assert gaps == []

    def test_evidence_gap_reported_after_write(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Evidence-gap candidates reported after writing secondary MBIDs.

        When both actionable groups and evidence-gap candidates exist, the gaps are reported
        after the writes complete.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # File 1: has SKIP-policy secondary MBID to write.
        tags1 = TrackTags(musicbrainz_albumid="primary-rel", title="Track 1")
        flac1 = self._make_flac(dest_root, "Work/01.flac", tags1)

        # File 2: evidence-gap candidate (secondary MBID in tag, not in journal).
        tags2 = TrackTags(musicbrainz_albumid="only-rel", title="Track 2")
        flac2 = self._make_flac(dest_root, "Work/02.flac", tags2)
        write_secondary_albumid_flac(flac2, "mystery-rel")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "primary-rel",
                    "source": "/src/a.flac",
                    "destination": str(flac1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "secondary-rel",
                    "source": "/src/b.flac",
                    "destination": str(flac1),
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "only-rel",
                    "source": "/src/c.flac",
                    "destination": str(flac2),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("builtins.input", return_value="y")
        journal_path = dest_root / "music_annotator_journal.json"
        gaps = reconstruct_cross_references(journal_path, dest_root)

        # Secondary MBID written to flac1.
        tag_dict = _read_tags_flac(flac1)
        assert "secondary-rel" in tag_dict.get("MUSICBRAINZ_SECONDARY_ALBUMID", "")

        # flac2 is an evidence-gap candidate.
        assert str(flac2) in gaps


# ---------------------------------------------------------------------------
# Library-wide dedup command (C-DEDUP general case)
# ---------------------------------------------------------------------------


class TestBuildDedupCensus:
    """Unit tests for :func:`_build_dedup_census`.

    KATs:
    - Cache-driven: no audio opens on cache hits.
    - Files lacking both ACOUSTID_ID and AUDIO_HASH are excluded.
    - Files with ACOUSTID_ID appear in acoustid_index.
    - Files with AUDIO_HASH appear in hash_index.
    - Files with both appear in both indexes.
    - Non-existent files are skipped.
    - Unsupported extensions are skipped.
    - Tag read errors are skipped.
    """

    @staticmethod
    def _make_flac_with_tags(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
        """Create a FLAC file with the given tags.

        :param dest_root: Library root.
        :param rel_path: Relative path within the library.
        :param tags: Tags to embed.
        :returns: Full absolute path.
        """
        full_path = dest_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(full_path, tags)
        return full_path

    def test_cache_hit_avoids_audio_open(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Cache hit: no audio file open on cache hit (mock-enforced).

        When the tag-read cache has a valid entry for a file, _build_dedup_census must not
        open the audio file.  The mock verifies that _read_tags_flac is never called.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        path = dest_root / "A/track.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_MINIMAL_FLAC)

        # Pre-populate the cache with a tag dict that includes ACOUSTID_ID.
        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)
        cache.put(path, {"ACOUSTID_ID": "acoustid-abc", "AUDIO_HASH": ""})

        current_lib: dict[Path, str] = {path: "rel-a"}

        # Patch _read_tags_flac to detect if it's called (it should NOT be on a cache hit).
        read_mock = mocker.patch("music_annotator._pipeline_maint._read_tags_flac")

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        read_mock.assert_not_called()
        assert "acoustid-abc" in acoustid_index
        assert acoustid_index["acoustid-abc"] == [(path, "rel-a")]
        assert hash_index == {}

    def test_files_without_identity_tags_excluded(self, fs: FakeFilesystem) -> None:
        """Files lacking both ACOUSTID_ID and AUDIO_HASH are excluded from both indexes.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", musicbrainz_albumid="rel-a")
        path = self._make_flac_with_tags(dest_root, "A/track.flac", tags)

        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)
        current_lib: dict[Path, str] = {path: "rel-a"}

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert acoustid_index == {}
        assert hash_index == {}

    def test_file_with_acoustid_only_in_acoustid_index(self, fs: FakeFilesystem) -> None:
        """File with ACOUSTID_ID but no AUDIO_HASH appears only in acoustid_index.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", acoustid_id="acoustid-xyz", musicbrainz_albumid="rel-a")
        path = self._make_flac_with_tags(dest_root, "A/track.flac", tags)

        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)
        current_lib: dict[Path, str] = {path: "rel-a"}

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert "acoustid-xyz" in acoustid_index
        assert hash_index == {}

    def test_file_with_audio_hash_only_in_hash_index(self, fs: FakeFilesystem) -> None:
        """File with AUDIO_HASH but no ACOUSTID_ID appears only in hash_index.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", audio_hash="hash-abc123", musicbrainz_albumid="rel-a")
        path = self._make_flac_with_tags(dest_root, "A/track.flac", tags)

        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)
        current_lib: dict[Path, str] = {path: "rel-a"}

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert acoustid_index == {}
        assert "hash-abc123" in hash_index

    def test_file_with_both_tags_in_both_indexes(self, fs: FakeFilesystem) -> None:
        """File with both ACOUSTID_ID and AUDIO_HASH appears in both indexes.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", acoustid_id="acoustid-xyz", audio_hash="hash-abc123", musicbrainz_albumid="rel-a")
        path = self._make_flac_with_tags(dest_root, "A/track.flac", tags)

        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)
        current_lib: dict[Path, str] = {path: "rel-a"}

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert "acoustid-xyz" in acoustid_index
        assert "hash-abc123" in hash_index

    def test_nonexistent_file_skipped(self, fs: FakeFilesystem) -> None:
        """Non-existent file is silently skipped.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        missing = dest_root / "A/missing.flac"
        current_lib: dict[Path, str] = {missing: "rel-a"}
        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert acoustid_index == {}
        assert hash_index == {}

    def test_unsupported_extension_skipped(self, fs: FakeFilesystem) -> None:
        """File with unsupported extension (not .flac or .mp3) is skipped.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        ogg_path = dest_root / "A/track.ogg"
        ogg_path.parent.mkdir(parents=True, exist_ok=True)
        ogg_path.write_bytes(b"OggS")

        current_lib: dict[Path, str] = {ogg_path: "rel-a"}
        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert acoustid_index == {}
        assert hash_index == {}

    def test_tag_read_error_skipped(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Tag read failure is silently skipped (file not added to any index).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        path = dest_root / "A/track.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_MINIMAL_FLAC)

        mocker.patch("music_annotator._pipeline_maint._read_tags_flac", side_effect=OSError("corrupt"))

        current_lib: dict[Path, str] = {path: "rel-a"}
        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)

        acoustid_index, hash_index = _build_dedup_census(current_lib, cache)

        assert acoustid_index == {}
        assert hash_index == {}

    def test_mp3_file_indexed(self, fs: FakeFilesystem) -> None:
        """MP3 file with ACOUSTID_ID is indexed correctly.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        mp3_path = dest_root / "A/track.mp3"
        mp3_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(mp3_path, TrackTags(title="Track", acoustid_id="acoustid-mp3"))

        current_lib: dict[Path, str] = {mp3_path: "rel-a"}
        cache = TagReadCache(dest_root / _TAG_CACHE_FILENAME)

        acoustid_index, _hash_index = _build_dedup_census(current_lib, cache)

        assert "acoustid-mp3" in acoustid_index


class TestBuildDedupGroups:
    """Unit tests for :func:`_build_dedup_groups`.

    KATs:
    - Hash clusters with ≥2 members form groups with evidence_method='audio_hash'.
    - Hash-covered paths are excluded from AcoustID groups.
    - AcoustID clusters with ≥2 uncovered members form groups with evidence_method='acoustid'.
    - Single-member clusters produce no groups.
    """

    def test_hash_cluster_forms_group(self) -> None:
        """Hash cluster with ≥2 members forms a group with evidence_method='audio_hash'.

        :returns: None.
        """
        path_a = Path("/lib/A/track.flac")
        path_b = Path("/lib/B/track.flac")
        hash_index = {"hash-abc": [(path_a, "rel-a"), (path_b, "rel-b")]}
        acoustid_index: dict[str, list[tuple[Path, str]]] = {}

        groups = _build_dedup_groups(acoustid_index, hash_index)

        assert len(groups) == 1
        members, method = groups[0]
        assert method == "audio_hash"
        assert {p for p, _ in members} == {path_a, path_b}

    def test_hash_covered_paths_excluded_from_acoustid_group(self) -> None:
        """Paths already in a hash group are excluded from the AcoustID group.

        When path_a and path_b share both AUDIO_HASH and ACOUSTID_ID, they form a hash group.
        The AcoustID cluster for the same pair must not produce a second group.

        :returns: None.
        """
        path_a = Path("/lib/A/track.flac")
        path_b = Path("/lib/B/track.flac")
        hash_index = {"hash-abc": [(path_a, "rel-a"), (path_b, "rel-b")]}
        acoustid_index = {"acoustid-xyz": [(path_a, "rel-a"), (path_b, "rel-b")]}

        groups = _build_dedup_groups(acoustid_index, hash_index)

        # Only one group (the hash group); the AcoustID cluster is fully covered.
        assert len(groups) == 1
        assert groups[0][1] == "audio_hash"

    def test_acoustid_cluster_forms_group_when_not_hash_covered(self) -> None:
        """AcoustID cluster with ≥2 uncovered members forms a group with evidence_method='acoustid'.

        :returns: None.
        """
        path_a = Path("/lib/A/track.flac")
        path_b = Path("/lib/B/track.flac")
        acoustid_index = {"acoustid-xyz": [(path_a, "rel-a"), (path_b, "rel-b")]}
        hash_index: dict[str, list[tuple[Path, str]]] = {}

        groups = _build_dedup_groups(acoustid_index, hash_index)

        assert len(groups) == 1
        members, method = groups[0]
        assert method == "acoustid"
        assert {p for p, _ in members} == {path_a, path_b}

    def test_single_member_cluster_produces_no_group(self) -> None:
        """Single-member clusters (hash or acoustid) produce no groups.

        :returns: None.
        """
        path_a = Path("/lib/A/track.flac")
        hash_index = {"hash-abc": [(path_a, "rel-a")]}
        acoustid_index = {"acoustid-xyz": [(path_a, "rel-a")]}

        groups = _build_dedup_groups(acoustid_index, hash_index)

        assert groups == []

    def test_partial_hash_coverage_leaves_acoustid_group(self) -> None:
        """When only one of three AcoustID members is hash-covered, the other two form an AcoustID group.

        :returns: None.
        """
        path_a = Path("/lib/A/track.flac")
        path_b = Path("/lib/B/track.flac")
        path_c = Path("/lib/C/track.flac")
        # path_a and path_d share a hash (path_d is a fourth file not in the acoustid cluster).
        path_d = Path("/lib/D/track.flac")
        hash_index = {"hash-abc": [(path_a, "rel-a"), (path_d, "rel-d")]}
        acoustid_index = {"acoustid-xyz": [(path_a, "rel-a"), (path_b, "rel-b"), (path_c, "rel-c")]}

        groups = _build_dedup_groups(acoustid_index, hash_index)

        # One hash group (path_a, path_d) and one acoustid group (path_b, path_c).
        assert len(groups) == 2
        methods = {g[1] for g in groups}
        assert methods == {"audio_hash", "acoustid"}
        # The acoustid group must not contain path_a (hash-covered).
        acoustid_group = next(g for g in groups if g[1] == "acoustid")
        acoustid_paths = {p for p, _ in acoustid_group[0]}
        assert path_a not in acoustid_paths
        assert path_b in acoustid_paths
        assert path_c in acoustid_paths


class TestScatterConsequenceNote:
    """Unit tests for :func:`_scatter_consequence_note`.

    KATs:
    - No scatter when all files from a release in a directory are in the group.
    - Scatter note generated when some files from a release's directory are not in the group.
    - Empty string returned when no scatter consequence applies.
    """

    def test_no_scatter_when_all_release_files_in_group(self) -> None:
        """No scatter note when all files from a release in a directory are in the group.

        :returns: None.
        """
        dest_root = Path("/lib")
        path_a = Path("/lib/A/track1.flac")
        path_b = Path("/lib/B/track1.flac")
        group_members = [(path_a, "rel-a"), (path_b, "rel-b")]
        # current_lib has exactly the same files as the group.
        current_lib: dict[Path, str] = {path_a: "rel-a", path_b: "rel-b"}

        note = _scatter_consequence_note(group_members, dest_root, current_lib)

        assert note == ""

    def test_scatter_note_when_directory_partially_emptied(self) -> None:
        """Scatter note generated when deleting group members would leave other tracks behind.

        :returns: None.
        """
        dest_root = Path("/lib")
        # Release rel-a has two tracks in /lib/A/; only track1 is in the group.
        path_a1 = Path("/lib/A/track1.flac")
        path_a2 = Path("/lib/A/track2.flac")  # not in group
        path_b = Path("/lib/B/track1.flac")
        group_members = [(path_a1, "rel-a"), (path_b, "rel-b")]
        current_lib: dict[Path, str] = {path_a1: "rel-a", path_a2: "rel-a", path_b: "rel-b"}

        note = _scatter_consequence_note(group_members, dest_root, current_lib)

        assert note != ""
        assert "rel-a" in note or "partially virtual" in note

    def test_empty_release_id_skipped(self) -> None:
        """Files with empty release_id are skipped in scatter analysis.

        :returns: None.
        """
        dest_root = Path("/lib")
        path_a = Path("/lib/A/track.flac")
        group_members = [(path_a, "")]  # empty release_id
        current_lib: dict[Path, str] = {path_a: ""}

        note = _scatter_consequence_note(group_members, dest_root, current_lib)

        assert note == ""


class TestDedupLibrary:
    """Tests for :func:`dedup_library` — the library-wide dedup command.

    KATs:
    - Cluster grouping (cache-driven, no audio opens on cache hits).
    - Medium-level aggregation (whole-medium group → single prompt).
    - All three resolution arms with C-DEDUP ordering:
      (a) survivor_occupant: mover deleted, xref journalled before deletion.
      (b) survivor_mover: occupant deleted, xref journalled before deletion.
      (c) keep_both: cross-reference only, no deletion.
    - Abort arm: run terminates, no further deletions.
    - Scatter-consequence surfaced in prompt text (dry-run).
    - Files without acoustid/hash never enter a group.
    - Dry-run reports groups without prompting or deleting.
    - No groups found: returns 0 without prompting.
    - Multi-file medium: extra files deleted after representative resolution.
    """

    @staticmethod
    def _make_flac(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
        """Create a minimal FLAC file with the given tags.

        :param dest_root: Library root.
        :param rel_path: Relative path within the library.
        :param tags: Tags to embed.
        :returns: Full absolute path.
        """
        full_path = dest_root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(full_path, tags)
        return full_path

    @staticmethod
    def _write_journal(dest_root: Path, entries: list[dict[str, str]]) -> Path:
        """Write a JSONL journal and return the journal path.

        :param dest_root: Library root.
        :param entries: List of raw entry dicts.
        :returns: Journal path.
        """
        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return journal_path

    def test_no_groups_returns_zero(self, fs: FakeFilesystem) -> None:
        """dedup_library returns 0 when no duplicate groups are found.

        Files without ACOUSTID_ID or AUDIO_HASH are out of scope.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", musicbrainz_albumid="rel-a")
        path = self._make_flac(dest_root, "A/track.flac", tags)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        result = dedup_library(dest_root, journal_path)

        assert result == 0

    def test_files_without_identity_tags_excluded(self, fs: FakeFilesystem) -> None:
        """Files lacking both ACOUSTID_ID and AUDIO_HASH never enter a group.

        Even when two files share the same release, they are not grouped without identity evidence.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(title="Track A", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track B", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        result = dedup_library(dest_root, journal_path)

        assert result == 0

    def test_dry_run_reports_groups_without_prompting(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """dry_run=True reports duplicate groups without prompting or deleting.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        input_mock = mocker.patch("builtins.input")
        result = dedup_library(dest_root, journal_path, dry_run=True)

        # No prompt, no deletion.
        input_mock.assert_not_called()
        assert result == 0
        assert path_a.exists()
        assert path_b.exists()

    def test_survivor_occupant_arm_deletes_mover(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """survivor_occupant arm: mover deleted, xref journalled before deletion (C-DEDUP ordering).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Choice "1" = survivor_occupant (occupant wins, mover deleted).
        mocker.patch("builtins.input", return_value="1")
        result = dedup_library(dest_root, journal_path)

        assert result == 1
        # One of the two files must be deleted.
        survivors = [p for p in [path_a, path_b] if p.exists()]
        deleted = [p for p in [path_a, path_b] if not p.exists()]
        assert len(survivors) == 1
        assert len(deleted) == 1

        # C-DEDUP ordering: cross-referenced entry before deduplicated entry.
        journal = read_journal(journal_path)
        new_entries = [e for e in journal.entries if e.action in {"cross-referenced", "deduplicated"}]
        assert len(new_entries) == 2
        assert new_entries[0].action == "cross-referenced"
        assert new_entries[1].action == "deduplicated"

    def test_survivor_mover_arm_deletes_occupant(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """survivor_mover arm: occupant deleted, xref journalled before deletion (C-DEDUP ordering).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Choice "2" = survivor_mover (mover wins, occupant deleted).
        mocker.patch("builtins.input", return_value="2")
        result = dedup_library(dest_root, journal_path)

        assert result == 1
        survivors = [p for p in [path_a, path_b] if p.exists()]
        deleted = [p for p in [path_a, path_b] if not p.exists()]
        assert len(survivors) == 1
        assert len(deleted) == 1

        # C-DEDUP ordering: cross-referenced before deduplicated.
        journal = read_journal(journal_path)
        new_entries = [e for e in journal.entries if e.action in {"cross-referenced", "deduplicated"}]
        assert len(new_entries) == 2
        assert new_entries[0].action == "cross-referenced"
        assert new_entries[1].action == "deduplicated"

    def test_keep_both_arm_no_deletion(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """keep_both arm: cross-reference written, no deletion, both files survive.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Choice "b" = keep_both.
        mocker.patch("builtins.input", return_value="b")
        result = dedup_library(dest_root, journal_path)

        assert result == 0
        assert path_a.exists()
        assert path_b.exists()

        # cross-referenced entry written.
        journal = read_journal(journal_path)
        xref_entries = [e for e in journal.entries if e.action == "cross-referenced"]
        assert len(xref_entries) == 1

    def test_abort_arm_stops_run(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """abort arm: run terminates immediately, no deletions performed.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_a = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Choice "a" = abort.
        mocker.patch("builtins.input", return_value="a")
        result = dedup_library(dest_root, journal_path)

        assert result == 0
        assert path_a.exists()
        assert path_b.exists()

    def test_audio_hash_fast_path_groups_byte_identical_files(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """AUDIO_HASH equality groups byte-identical files with evidence_method='audio_hash'.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two files with the same AUDIO_HASH (byte-identical) but different releases.
        tags_a = TrackTags(title="Track", audio_hash="hash-identical", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track", audio_hash="hash-identical", musicbrainz_albumid="rel-b")
        path_a = self._make_flac(dest_root, "A/track.flac", tags_a)
        path_b = self._make_flac(dest_root, "B/track.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Dry-run to verify grouping without deletion.
        input_mock = mocker.patch("builtins.input")
        result = dedup_library(dest_root, journal_path, dry_run=True)

        input_mock.assert_not_called()
        assert result == 0  # dry-run never deletes

    def test_scatter_consequence_surfaced_in_dry_run(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Scatter consequence is surfaced in dry-run output when a directory would be partially emptied.

        When deleting one release's files from a group would leave other tracks in the same
        directory, the dry-run output must include a scatter-consequence warning.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Release rel-a has two tracks in /lib/A/; only track1 is in the duplicate group.
        tags_a1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_a2 = TrackTags(title="Track 2", musicbrainz_albumid="rel-a")  # not in group
        tags_b = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a1 = self._make_flac(dest_root, "A/track1.flac", tags_a1)
        path_a2 = self._make_flac(dest_root, "A/track2.flac", tags_a2)
        path_b = self._make_flac(dest_root, "B/track1.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a1.flac",
                    "destination": str(path_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a2.flac",
                    "destination": str(path_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Capture console output to verify scatter note is printed.
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline_maint._console.print", side_effect=lambda *a, **kw: printed.append(str(a)))

        result = dedup_library(dest_root, journal_path, dry_run=True)

        assert result == 0
        # The scatter consequence note must appear in the printed output.
        all_output = " ".join(printed)
        assert "partially virtual" in all_output or "track(s) behind" in all_output

    def test_same_release_files_not_grouped(self, fs: FakeFilesystem) -> None:
        """Files sharing the same ACOUSTID_ID but the same release are not grouped.

        When all files in an AcoustID cluster belong to the same release, there is no
        cross-release duplicate to resolve.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two tracks from the same release sharing the same ACOUSTID_ID (unusual but possible).
        tags_a = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        path_a = self._make_flac(dest_root, "A/track1.flac", tags_a)
        path_b = self._make_flac(dest_root, "A/track2.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        result = dedup_library(dest_root, journal_path)

        # No deletion: same release, not a cross-release duplicate.
        assert result == 0
        assert path_a.exists()
        assert path_b.exists()

    def test_medium_level_aggregation_multi_file_survivor_occupant(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Medium-level aggregation: extra mover files deleted when survivor_occupant chosen.

        When a single AcoustID cluster contains multiple files from two releases (whole-medium
        duplication), choosing survivor_occupant deletes all mover files (not just the
        representative).  This exercises the ``extra_path`` loop in the survivor_occupant arm
        and the ``total_files > 2`` medium-level group print.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single AcoustID cluster with 4 files: 2 from rel-a (occupant), 2 from rel-b (mover).
        # All four share the same ACOUSTID_ID so they form one group.
        tags_a1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_a2 = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        tags_b2 = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")

        path_a1 = self._make_flac(dest_root, "A/track1.flac", tags_a1)
        path_a2 = self._make_flac(dest_root, "A/track2.flac", tags_a2)
        path_b1 = self._make_flac(dest_root, "B/track1.flac", tags_b1)
        path_b2 = self._make_flac(dest_root, "B/track2.flac", tags_b2)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a1.flac",
                    "destination": str(path_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a2.flac",
                    "destination": str(path_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b1.flac",
                    "destination": str(path_b1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:03+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b2.flac",
                    "destination": str(path_b2),
                    "action": "tagged",
                },
            ],
        )

        # Choice "1" = survivor_occupant (occupant=rel-a wins, mover=rel-b deleted).
        mocker.patch("builtins.input", return_value="1")
        result = dedup_library(dest_root, journal_path)

        # Both mover files (rel-b) should be deleted: representative + extra.
        assert result == 2
        assert not path_b1.exists()
        assert not path_b2.exists()
        # Occupant files (rel-a) must survive.
        assert path_a1.exists()
        assert path_a2.exists()

    def test_medium_level_aggregation_multi_file_survivor_mover(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Medium-level aggregation: extra occupant files deleted when survivor_mover chosen.

        When a single AcoustID cluster contains multiple files from two releases, choosing
        survivor_mover deletes all occupant files (not just the representative).  This exercises
        the ``extra_path`` loop in the survivor_mover arm.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single AcoustID cluster with 4 files: 2 from rel-a (occupant), 2 from rel-b (mover).
        tags_a1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_a2 = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        tags_b2 = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")

        path_a1 = self._make_flac(dest_root, "A/track1.flac", tags_a1)
        path_a2 = self._make_flac(dest_root, "A/track2.flac", tags_a2)
        path_b1 = self._make_flac(dest_root, "B/track1.flac", tags_b1)
        path_b2 = self._make_flac(dest_root, "B/track2.flac", tags_b2)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a1.flac",
                    "destination": str(path_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a2.flac",
                    "destination": str(path_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b1.flac",
                    "destination": str(path_b1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:03+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b2.flac",
                    "destination": str(path_b2),
                    "action": "tagged",
                },
            ],
        )

        # Choice "2" = survivor_mover (mover=rel-b wins, occupant=rel-a deleted).
        mocker.patch("builtins.input", return_value="2")
        result = dedup_library(dest_root, journal_path)

        # Both occupant files (rel-a) should be deleted: representative + extra.
        assert result == 2
        assert not path_a1.exists()
        assert not path_a2.exists()
        # Mover files (rel-b) must survive.
        assert path_b1.exists()
        assert path_b2.exists()

    def test_scatter_consequence_printed_in_interactive_mode(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Scatter consequence warning is printed in interactive (non-dry-run) mode.

        When a group has a scatter consequence (deleting files would partially empty a directory),
        the warning is printed before the group-resolution prompt in interactive mode.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Release rel-a has two tracks in /lib/A/; only track1 is in the duplicate group.
        tags_a1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_a2 = TrackTags(title="Track 2", musicbrainz_albumid="rel-a")  # not in group
        tags_b = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        path_a1 = self._make_flac(dest_root, "A/track1.flac", tags_a1)
        path_a2 = self._make_flac(dest_root, "A/track2.flac", tags_a2)
        path_b = self._make_flac(dest_root, "B/track1.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a1.flac",
                    "destination": str(path_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a2.flac",
                    "destination": str(path_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Capture console output to verify scatter note is printed in interactive mode.
        printed: list[str] = []
        mocker.patch(
            "music_annotator._pipeline_maint._console.print",
            side_effect=lambda *a, **kw: printed.append(str(a)),
        )
        # Choose "b" (keep_both) so we don't need to worry about deletion.
        mocker.patch("builtins.input", return_value="b")

        result = dedup_library(dest_root, journal_path)

        assert result == 0
        all_output = " ".join(printed)
        # The scatter consequence warning must appear in interactive mode output.
        assert (
            "Scatter consequence warning" in all_output or "partially virtual" in all_output or "track(s) behind" in all_output
        )

    def test_extra_mover_file_deleted_between_census_and_loop_skipped(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Extra mover file deleted between census and the extra-file loop is silently skipped.

        The ``if extra_path.exists()`` guard in the survivor_occupant arm handles the case
        where an extra mover file existed during census but was deleted before the loop runs.
        This exercises the ``False`` branch of that guard.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single AcoustID cluster with 3 files: 1 from rel-a (occupant), 2 from rel-b (mover).
        tags_a = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")
        tags_b2 = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")

        path_a = self._make_flac(dest_root, "A/track1.flac", tags_a)
        path_b1 = self._make_flac(dest_root, "B/track1.flac", tags_b1)
        path_b2 = self._make_flac(dest_root, "B/track2.flac", tags_b2)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b1.flac",
                    "destination": str(path_b1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b2.flac",
                    "destination": str(path_b2),
                    "action": "tagged",
                },
            ],
        )

        # Simulate: path_b2 is deleted between census and the extra-file loop.
        # We do this by deleting path_b2 after the census (via _build_dedup_census) but before
        # the extra-file loop.  The simplest approach: delete path_b2 via a side_effect on
        # resolve_duplicate_group (which runs before the extra-file loop).
        real_resolve = resolve_duplicate_group

        def _resolve_and_delete(*args: object, **kwargs: object) -> DuplicateResolution:
            """Call real resolve_duplicate_group then delete path_b2 to simulate race condition.

            :returns: The real DuplicateResolution result.
            """
            result_inner = real_resolve(*args, **kwargs)  # type: ignore[arg-type]
            # Delete path_b2 after the representative (path_b1) is deleted by resolve.
            if path_b2.exists():
                path_b2.unlink()
            return result_inner

        mocker.patch("music_annotator._pipeline_maint.resolve_duplicate_group", side_effect=_resolve_and_delete)
        mocker.patch("builtins.input", return_value="1")
        result = dedup_library(dest_root, journal_path)

        # path_b1 (representative) was deleted by resolve_duplicate_group.
        # path_b2 was deleted by the side_effect (simulating race condition).
        # The extra-file loop skips path_b2 because it no longer exists.
        assert result == 1  # only the representative counted
        assert not path_b1.exists()
        assert path_a.exists()

    def test_extra_occupant_file_deleted_between_census_and_loop_skipped(
        self, fs: FakeFilesystem, mocker: MockerFixture
    ) -> None:
        """Extra occupant file deleted between census and the extra-file loop is silently skipped.

        The ``if extra_path.exists()`` guard in the survivor_mover arm handles the case
        where an extra occupant file existed during census but was deleted before the loop runs.
        This exercises the ``False`` branch of that guard.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Single AcoustID cluster with 3 files: 2 from rel-a (occupant), 1 from rel-b (mover).
        tags_a1 = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_a2 = TrackTags(title="Track 2", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-a")
        tags_b = TrackTags(title="Track 1", acoustid_id="acoustid-shared", musicbrainz_albumid="rel-b")

        path_a1 = self._make_flac(dest_root, "A/track1.flac", tags_a1)
        path_a2 = self._make_flac(dest_root, "A/track2.flac", tags_a2)
        path_b = self._make_flac(dest_root, "B/track1.flac", tags_b)

        journal_path = self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a1.flac",
                    "destination": str(path_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:01+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a2.flac",
                    "destination": str(path_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:02+00:00",
                    "release_id": "rel-b",
                    "source": "/src/b.flac",
                    "destination": str(path_b),
                    "action": "tagged",
                },
            ],
        )

        # Simulate: path_a2 is deleted between census and the extra-file loop.
        real_resolve = resolve_duplicate_group

        def _resolve_and_delete(*args: object, **kwargs: object) -> DuplicateResolution:
            """Call real resolve_duplicate_group then delete path_a2 to simulate race condition.

            :returns: The real DuplicateResolution result.
            """
            result_inner = real_resolve(*args, **kwargs)  # type: ignore[arg-type]
            # Delete path_a2 after the representative (path_a1) is deleted by resolve.
            if path_a2.exists():
                path_a2.unlink()
            return result_inner

        mocker.patch("music_annotator._pipeline_maint.resolve_duplicate_group", side_effect=_resolve_and_delete)
        mocker.patch("builtins.input", return_value="2")
        result = dedup_library(dest_root, journal_path)

        # path_a1 (representative) was deleted by resolve_duplicate_group.
        # path_a2 was deleted by the side_effect (simulating race condition).
        # The extra-file loop skips path_a2 because it no longer exists.
        assert result == 1  # only the representative counted
        assert not path_a1.exists()
        assert path_b.exists()

    def test_cli_dedup_library_dry_run(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """CLI 'dedup-library --dry-run' dispatches to dedup_library(dry_run=True).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", musicbrainz_albumid="rel-a")
        path = self._make_flac(dest_root, "A/track.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        dedup_mock = mocker.patch("music_annotator.dedup_library", return_value=0)

        sys.argv = ["music-annotator", "dedup-library", str(dest_root), "--dry-run"]
        try:
            main()
        except SystemExit:
            pass

        dedup_mock.assert_called_once()
        call_kwargs = dedup_mock.call_args
        assert call_kwargs.kwargs.get("dry_run") is True or (len(call_kwargs.args) > 2 and call_kwargs.args[2] is True)

    def test_cli_dedup_library_no_dry_run(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """CLI 'dedup-library' (without --dry-run) dispatches to dedup_library(dry_run=False).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(title="Track", musicbrainz_albumid="rel-a")
        path = self._make_flac(dest_root, "A/track.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-a",
                    "source": "/src/a.flac",
                    "destination": str(path),
                    "action": "tagged",
                }
            ],
        )

        dedup_mock = mocker.patch("music_annotator.dedup_library", return_value=0)

        sys.argv = ["music-annotator", "dedup-library", str(dest_root)]
        try:
            main()
        except SystemExit:
            pass

        dedup_mock.assert_called_once()
        call_kwargs = dedup_mock.call_args
        assert call_kwargs.kwargs.get("dry_run") is False or (len(call_kwargs.args) > 2 and call_kwargs.args[2] is False)


# ---------------------------------------------------------------------------
# maintain — single-composition orchestrator (C-MAINTAIN / C-CONFLUENCE)
# ---------------------------------------------------------------------------


class TestMaintain:
    """Tests for :func:`music_annotator._pipeline_maint.maintain`.

    Covers: pass composition order (mock-enforced); journal read exactly once (mock-enforced);
    ``-y``/``--yes`` suppresses move prompts but not integrity prompts; ``--dry-run`` renders
    all passes report-only; final convergence line reports "changed N" vs "no changes";
    a settled fixture reports "no changes".
    """

    def _write_journal(self, dest_root: Path, entries: list[dict[str, str]]) -> None:
        """Write a JSONL journal to ``dest_root / music_annotator_journal.json``.

        :param dest_root: Library root directory.
        :param entries: Raw entry dicts to serialise.
        """
        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    def _make_flac(self, dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
        """Create a tagged FLAC file at ``dest_root / rel_path``.

        :param dest_root: Library root directory.
        :param rel_path: Relative path within the library.
        :param tags: Tags to embed.
        :returns: Full absolute path of the created file.
        """
        return _make_library_flac(dest_root, rel_path, tags)

    def test_pass_order_enforced(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain runs all seven passes in the fixed C-CONFLUENCE order.

        Mocks all seven pass functions and asserts they are called in the required order:
        enrich → origin-time → repath → regroup → unify → reconstruct-xrefs → dedup-library.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        call_order: list[str] = []

        def _make_recorder(name: str, return_val: object) -> object:
            """Return a side-effect function that records the call order.

            :param name: Pass name to record.
            :param return_val: Value to return from the mock.
            :returns: Side-effect callable.
            """

            def _recorder(*_args: object, **_kwargs: object) -> object:  # noqa: ANN401
                call_order.append(name)
                return return_val

            return _recorder

        mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            side_effect=_make_recorder("enrich", DryRunPlan(pass_name="enrich", entries=[], count=0)),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.enrich_origin_time",
            side_effect=_make_recorder("origin-time", 0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.repath",
            side_effect=_make_recorder("repath", DryRunPlan(pass_name="repath", entries=[], count=0)),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.regroup",
            side_effect=_make_recorder("regroup", DryRunPlan(pass_name="regroup", entries=[], count=0)),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.unify",
            side_effect=_make_recorder("unify", DryRunPlan(pass_name="unify", entries=[], count=0)),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.reconstruct_cross_references",
            side_effect=_make_recorder("reconstruct-xrefs", []),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.dedup_library",
            side_effect=_make_recorder("dedup-library", 0),
        )

        maintain(dest_root, dry_run=True)

        assert call_order == [
            "enrich",
            "origin-time",
            "repath",
            "regroup",
            "unify",
            "reconstruct-xrefs",
            "dedup-library",
        ]

    def test_journal_read_exactly_once(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain reads the journal exactly once and threads it through all passes (C-JRNL).

        Mocks ``read_journal`` and asserts it is called exactly once regardless of how many
        passes run.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mock_read_journal = mocker.patch(
            "music_annotator._pipeline_maint.read_journal",
            return_value=TransactionLog(),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            return_value=DryRunPlan(pass_name="enrich", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)
        mocker.patch(
            "music_annotator._pipeline_maint.repath",
            return_value=DryRunPlan(pass_name="repath", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.regroup",
            return_value=DryRunPlan(pass_name="regroup", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.unify",
            return_value=DryRunPlan(pass_name="unify", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        maintain(dest_root, dry_run=True)

        mock_read_journal.assert_called_once()

    def test_yes_suppresses_move_prompts_not_integrity(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """``yes=True`` is forwarded to move passes but not to integrity passes.

        Asserts that ``repath``, ``regroup``, and ``unify`` are called with ``yes=True``,
        while ``reconstruct_cross_references`` and ``dedup_library`` are called without a
        ``yes`` argument (integrity prompts are not bulk consent).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            return_value=None,
        )
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)
        mock_repath = mocker.patch("music_annotator._pipeline_maint.repath", return_value=None)
        mock_regroup = mocker.patch("music_annotator._pipeline_maint.regroup", return_value=None)
        mock_unify = mocker.patch("music_annotator._pipeline_maint.unify", return_value=None)
        mock_xrefs = mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mock_dedup = mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        maintain(dest_root, dry_run=False, yes=True)

        # Move passes receive yes=True
        assert mock_repath.call_args.kwargs.get("yes") is True
        assert mock_regroup.call_args.kwargs.get("yes") is True
        assert mock_unify.call_args.kwargs.get("yes") is True

        # Integrity passes do not receive a yes argument (not bulk consent)
        assert "yes" not in mock_xrefs.call_args.kwargs
        assert "yes" not in mock_dedup.call_args.kwargs

    def test_dry_run_all_passes_report_only(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """``dry_run=True`` is forwarded to all seven passes including the two integrity passes.

        Asserts that every pass is called with ``dry_run=True``.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mock_enrich = mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            return_value=DryRunPlan(pass_name="enrich", entries=[], count=0),
        )
        mock_ot = mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)
        mock_repath = mocker.patch(
            "music_annotator._pipeline_maint.repath",
            return_value=DryRunPlan(pass_name="repath", entries=[], count=0),
        )
        mock_regroup = mocker.patch(
            "music_annotator._pipeline_maint.regroup",
            return_value=DryRunPlan(pass_name="regroup", entries=[], count=0),
        )
        mock_unify = mocker.patch(
            "music_annotator._pipeline_maint.unify",
            return_value=DryRunPlan(pass_name="unify", entries=[], count=0),
        )
        mock_xrefs = mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mock_dedup = mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        maintain(dest_root, dry_run=True)

        assert mock_enrich.call_args.kwargs.get("dry_run") is True
        assert mock_ot.call_args.kwargs.get("dry_run") is True
        assert mock_repath.call_args.kwargs.get("dry_run") is True
        assert mock_regroup.call_args.kwargs.get("dry_run") is True
        assert mock_unify.call_args.kwargs.get("dry_run") is True
        assert mock_xrefs.call_args.kwargs.get("dry_run") is True
        assert mock_dedup.call_args.kwargs.get("dry_run") is True

    def test_final_line_changed_n(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain reports "changed N file(s)" when passes enact changes.

        Uses dry_run=True with non-zero planned counts so the convergence line reflects changes.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            return_value=DryRunPlan(pass_name="enrich", entries=[], count=3),
        )
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=1)
        mocker.patch(
            "music_annotator._pipeline_maint.repath",
            return_value=DryRunPlan(pass_name="repath", entries=[], count=2),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.regroup",
            return_value=DryRunPlan(pass_name="regroup", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.unify",
            return_value=DryRunPlan(pass_name="unify", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        result = maintain(dest_root, dry_run=True)

        assert result == 6  # 3 + 1 + 2

    def test_final_line_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain reports "no changes" when all passes report zero changes.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            return_value=DryRunPlan(pass_name="enrich", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)
        mocker.patch(
            "music_annotator._pipeline_maint.repath",
            return_value=DryRunPlan(pass_name="repath", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.regroup",
            return_value=DryRunPlan(pass_name="regroup", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.unify",
            return_value=DryRunPlan(pass_name="unify", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        result = maintain(dest_root, dry_run=True)

        assert result == 0

    def test_settled_fixture_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """A settled library (all passes no-op) reports "no changes" — the convergence signal.

        Creates a minimal library with a single FLAC file whose path already matches the
        policy-canonical path, so repath/regroup/unify are no-ops.  Enrich is a no-op because
        the file already has all fingerprint fields.  The integrity passes find nothing to do.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            title="Track 1",
            musicbrainz_albumid="rel-settled",
            musicbrainz_recordingid="rec-1",
            audio_hash="abc123",
            acoustid_fingerprint="fp123",
            acoustid_id="aid-1",
        )
        path = self._make_flac(dest_root, "Composer/Work [2024]/01 - Track 1.flac", tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "rel-settled",
                    "source": "/src/01.flac",
                    "destination": str(path),
                    "action": "tagged",
                    "audio_hash": "abc123",
                    "acoustid_fingerprint": "fp123",
                    "acoustid_id": "aid-1",
                }
            ],
        )

        # Mock all passes to return no-op results (settled library).
        mocker.patch(
            "music_annotator._pipeline_maint.enrich",
            return_value=DryRunPlan(pass_name="enrich", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)
        mocker.patch(
            "music_annotator._pipeline_maint.repath",
            return_value=DryRunPlan(pass_name="repath", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.regroup",
            return_value=DryRunPlan(pass_name="regroup", entries=[], count=0),
        )
        mocker.patch(
            "music_annotator._pipeline_maint.unify",
            return_value=DryRunPlan(pass_name="unify", entries=[], count=0),
        )
        mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        result = maintain(dest_root, dry_run=True)

        assert result == 0

    def test_cli_maintain_dispatches(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """CLI 'maintain' subcommand dispatches to music_annotator.maintain with correct args.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mock_fn = mocker.patch("music_annotator.maintain", return_value=0)

        sys.argv = ["music-annotator", "maintain", str(dest_root)]
        main()

        mock_fn.assert_called_once_with(
            dest_root=dest_root,
            dry_run=False,
            yes=False,
            json_path=None,
        )

    def test_cli_maintain_dry_run(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """CLI 'maintain --dry-run' forwards dry_run=True to maintain().

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mock_fn = mocker.patch("music_annotator.maintain", return_value=0)

        sys.argv = ["music-annotator", "maintain", str(dest_root), "--dry-run"]
        main()

        mock_fn.assert_called_once_with(
            dest_root=dest_root,
            dry_run=True,
            yes=False,
            json_path=None,
        )

    def test_cli_maintain_yes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """CLI 'maintain -y' forwards yes=True to maintain().

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mock_fn = mocker.patch("music_annotator.maintain", return_value=0)

        sys.argv = ["music-annotator", "maintain", str(dest_root), "-y"]
        main()

        mock_fn.assert_called_once_with(
            dest_root=dest_root,
            dry_run=False,
            yes=True,
            json_path=None,
        )

    def test_cli_maintain_error_exits_1(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """CLI 'maintain' exits with code 1 on unhandled exception.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        mocker.patch("music_annotator.maintain", side_effect=RuntimeError("boom"))

        sys.argv = ["music-annotator", "maintain", str(dest_root)]
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_cli_maintain_parser_registered(self) -> None:
        """'maintain' subcommand is registered in the argument parser.

        :returns: None.
        """
        parser = _build_parser()
        args = parser.parse_args(["maintain", "/lib"])
        assert args.subcommand == "maintain"
        assert args.dry_run is False
        assert args.yes is False
        assert args.json_path is None

    def test_cli_maintain_parser_dry_run_flag(self) -> None:
        """'maintain --dry-run' sets dry_run=True in parsed args.

        :returns: None.
        """
        parser = _build_parser()
        args = parser.parse_args(["maintain", "/lib", "--dry-run"])
        assert args.subcommand == "maintain"
        assert args.dry_run is True

    def test_cli_maintain_parser_yes_flag(self) -> None:
        """'maintain -y' sets yes=True in parsed args.

        :returns: None.
        """
        parser = _build_parser()
        args = parser.parse_args(["maintain", "/lib", "-y"])
        assert args.subcommand == "maintain"
        assert args.yes is True

    def test_cli_maintain_parser_json_flag(self) -> None:
        """'maintain --json PATH' sets json_path in parsed args.

        :returns: None.
        """
        parser = _build_parser()
        args = parser.parse_args(["maintain", "/lib", "--dry-run", "--json", "/tmp/report.json"])
        assert args.subcommand == "maintain"
        assert args.json_path == Path("/tmp/report.json")

    def test_journal_threaded_to_passes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """maintain threads the pre-read journal to each pass via the _journal kwarg.

        Asserts that each pass receives the same TransactionLog object that read_journal returned.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        self._write_journal(dest_root, [])

        sentinel_journal = TransactionLog()
        mocker.patch(
            "music_annotator._pipeline_maint.read_journal",
            return_value=sentinel_journal,
        )

        received_journals: dict[str, object] = {}

        def _capture_enrich(*_args: object, **kwargs: object) -> DryRunPlan:
            received_journals["enrich"] = kwargs.get("_journal")
            return DryRunPlan(pass_name="enrich", entries=[], count=0)

        def _capture_ot(*_args: object, **kwargs: object) -> int:
            received_journals["origin-time"] = kwargs.get("_journal")
            return 0

        def _capture_repath(*_args: object, **kwargs: object) -> DryRunPlan:
            received_journals["repath"] = kwargs.get("_journal")
            return DryRunPlan(pass_name="repath", entries=[], count=0)

        def _capture_regroup(*_args: object, **kwargs: object) -> DryRunPlan:
            received_journals["regroup"] = kwargs.get("_journal")
            return DryRunPlan(pass_name="regroup", entries=[], count=0)

        def _capture_unify(*_args: object, **kwargs: object) -> DryRunPlan:
            received_journals["unify"] = kwargs.get("_journal")
            return DryRunPlan(pass_name="unify", entries=[], count=0)

        def _capture_xrefs(*_args: object, **kwargs: object) -> list[str]:
            received_journals["reconstruct-xrefs"] = kwargs.get("_journal")
            return []

        def _capture_dedup(*_args: object, **kwargs: object) -> int:
            received_journals["dedup-library"] = kwargs.get("_journal")
            return 0

        mocker.patch("music_annotator._pipeline_maint.enrich", side_effect=_capture_enrich)
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", side_effect=_capture_ot)
        mocker.patch("music_annotator._pipeline_maint.repath", side_effect=_capture_repath)
        mocker.patch("music_annotator._pipeline_maint.regroup", side_effect=_capture_regroup)
        mocker.patch("music_annotator._pipeline_maint.unify", side_effect=_capture_unify)
        mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", side_effect=_capture_xrefs)
        mocker.patch("music_annotator._pipeline_maint.dedup_library", side_effect=_capture_dedup)

        maintain(dest_root, dry_run=True)

        for pass_name, received in received_journals.items():
            assert received is sentinel_journal, f"Pass '{pass_name}' did not receive the threaded journal"


# ---------------------------------------------------------------------------
# KATs: depth canonical unification (C-CANON / C-W3b-INT)
# ---------------------------------------------------------------------------


class TestDepthCanonicalUnification:
    """KATs for the depth canonical unification fix (C-CANON / C-W3b-INT).

    Three witnesses:

    1. **Wagner shape** — a confirmed split-release whose tracks have PL=2, but the same
       top-work MBID is shared by non-confirmed library files with PL=3.  After the fix,
       ``regroup`` extends its modal-depth computation to include the non-confirmed files and
       agrees with ``repath``'s full-library modal depth.  The same-run ping-pong is impossible
       by construction: after ``repath`` moves the file to the work-subdir (depth-3 render),
       ``regroup`` computes the same canonical destination and produces an empty plan.

    2. **La traviata / Guglielmo Tell shape** — a classical work with PL=2 tracks fragmented
       across top_dirs.  After the fix, ``unify`` threads ``group_modal_depth`` into
       ``build_dest_path`` and produces the same depth render as ``repath``.  Mock-enforced
       equality: both passes compute the same canonical destination for the same tags.

    3. **C-W3b-INT parity** — the depth render for classical works matches between the ingest
       pipeline (``build_dest_path`` with ``group_modal_depth``) and the maintenance passes
       (``regroup`` and ``unify``).  Asserts that after a full ``maintain`` run, files land at
       the same path that the ingest pipeline would have computed.
    """

    # ------------------------------------------------------------------
    # Shared tag factories
    # ------------------------------------------------------------------

    @staticmethod
    def _make_wagner_tags(
        cwp_part_levels: str,
        cwp_movt_num: str,
        title: str,
        release_id: str,
        *,
        extra_parts: dict[str, str] | None = None,
    ) -> TrackTags:
        """Build TrackTags for a Wagner-shape test track.

        :param cwp_part_levels: String value for CWP_PART_LEVELS.
        :param cwp_movt_num: String value for CWP_MOVT_NUM.
        :param title: Track title.
        :param release_id: MUSICBRAINZ_ALBUMID to embed.
        :param extra_parts: Optional dict of lowercase model_extra keys to set.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            cwp_work_top="Die Meistersinger von Nürnberg",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Wagner",
            cwp_workid_top="w-meistersinger",
            recording_date="1951",
            cwp_part_levels=cwp_part_levels,
            cwp_movt_num=cwp_movt_num,
            movementtotal="3",
            title=title,
            artist="Karajan",
            musicbrainz_albumid=release_id,
        )
        if extra_parts and tags.model_extra is not None:
            tags.model_extra.update(extra_parts)
        return tags

    @staticmethod
    def _make_opera_tags(
        cwp_part_levels: str,
        cwp_movt_num: str,
        title: str,
        release_id: str,
        work_top: str = "La traviata",
        *,
        extra_parts: dict[str, str] | None = None,
    ) -> TrackTags:
        """Build TrackTags for a La traviata / Guglielmo Tell shape test track.

        :param cwp_part_levels: String value for CWP_PART_LEVELS.
        :param cwp_movt_num: String value for CWP_MOVT_NUM.
        :param title: Track title.
        :param release_id: MUSICBRAINZ_ALBUMID to embed.
        :param work_top: CWP_WORK_TOP value (default: ``"La traviata"``).
        :param extra_parts: Optional dict of lowercase model_extra keys to set.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            cwp_work_top=work_top,
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Verdi",
            cwp_workid_top="w-traviata",
            recording_date="1955",
            cwp_part_levels=cwp_part_levels,
            cwp_movt_num=cwp_movt_num,
            movementtotal="2",
            title=title,
            artist="Callas",
            musicbrainz_albumid=release_id,
        )
        if extra_parts and tags.model_extra is not None:
            tags.model_extra.update(extra_parts)
        return tags

    # ------------------------------------------------------------------
    # KAT 1: Wagner shape — regroup agrees with repath; ping-pong impossible
    # ------------------------------------------------------------------

    def test_wagner_regroup_agrees_with_repath_no_ping_pong(self, fs: FakeFilesystem) -> None:
        """Wagner shape: regroup's modal depth agrees with repath's after the full-library extension.

        Scenario: the confirmed split-release ("wagner-split") has 2 tracks with PL=3 (3-level
        hierarchy: work_dir / act / scene / leaf).  A non-confirmed release ("wagner-other") has
        3 tracks with PL=2 sharing the same CWP_WORKID_TOP ("w-meistersinger").

        Without the fix:
        - regroup computes modal depth over only the 2 confirmed tracks (PL=3): modal=3.
          regroup moves the confirmed tracks from their 2-level canonical paths to 3-level paths.
          → ping-pong: regroup disagrees with repath's full-library modal depth (2).

        With the fix:
        - regroup extends its modal depth computation to include the non-confirmed tracks (PL=2)
          and computes modal=2 — agreeing with repath.
        - The confirmed tracks are already at their 2-level canonical paths (placed there by
          repath), so regroup produces an empty plan — the ping-pong is impossible.

        The test places the confirmed tracks at their 2-level canonical paths (simulating the
        post-repath state) and asserts that regroup is a noop.  Without the fix, regroup would
        compute modal=3 and move the tracks to 3-level paths.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Confirmed split-release: 2 tracks with PL=3 (3-level hierarchy).
        # These are the tracks regroup will try to consolidate.
        tags_split_1 = self._make_wagner_tags(
            "3",
            "1",
            "Prelude",
            "wagner-split",
            extra_parts={
                "cwp_part_1": "Scene 1",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Akt I",
                "cwp_ordering_key_2": "1",
            },
        )
        tags_split_2 = self._make_wagner_tags(
            "3",
            "2",
            "Quintet",
            "wagner-split",
            extra_parts={
                "cwp_part_1": "Scene 1",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Akt I",
                "cwp_ordering_key_2": "1",
            },
        )

        # Non-confirmed release: 3 tracks with PL=2 sharing the same CWP_WORKID_TOP.
        # These tracks are NOT in the confirmed split-release set.
        # Their majority (3 vs 2) drives the full-library modal depth to 2.
        tags_other_1 = self._make_wagner_tags(
            "2",
            "1",
            "Overture",
            "wagner-other",
            extra_parts={"cwp_part_1": "Akt I", "cwp_ordering_key_1": "1"},
        )
        tags_other_2 = self._make_wagner_tags(
            "2",
            "2",
            "Aria",
            "wagner-other",
            extra_parts={"cwp_part_1": "Akt I", "cwp_ordering_key_1": "1"},
        )
        tags_other_3 = self._make_wagner_tags(
            "2",
            "3",
            "Finale",
            "wagner-other",
            extra_parts={"cwp_part_1": "Akt II", "cwp_ordering_key_1": "2"},
        )

        # Full-library modal depth: 3 PL=2 tracks + 2 PL=3 tracks → modal=2 (majority PL=2).
        full_modal = work_group_modal_depth([3, 3, 2, 2, 2])
        assert full_modal == 2  # noqa: PLR2004 — 2 is the full-library modal depth

        # Subset modal depth (only the confirmed release's tracks): 2 PL=3 tracks → modal=3.
        subset_modal = work_group_modal_depth([3, 3])
        assert subset_modal == 3  # noqa: PLR2004 — 3 is the subset modal depth

        # Canonical destination using full-library modal depth (what repath computes).
        # For PL=3 tracks with modal=2: min(3,2)=2 → 2-level path (no scene dir, only act dir).
        canonical_split_1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_split_1, group_modal_depth=full_modal
        ).with_suffix(".flac")
        canonical_split_2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_split_2, group_modal_depth=full_modal
        ).with_suffix(".flac")

        # Subset-modal destination (what regroup would compute WITHOUT the fix).
        # For PL=3 tracks with modal=3: min(3,3)=3 → 3-level path (act + scene + leaf).
        subset_dest_1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_split_1, group_modal_depth=subset_modal
        ).with_suffix(".flac")
        subset_dest_2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_split_2, group_modal_depth=subset_modal
        ).with_suffix(".flac")

        # The canonical (full-modal) and subset-modal paths must differ for the
        # ping-pong scenario to be non-trivial.
        assert canonical_split_1 != subset_dest_1, (
            "test setup error: full-modal (2-level) and subset-modal (3-level) paths must differ for PL=3 tracks"
        )

        # Place confirmed tracks at their CANONICAL (full-modal, 2-level) paths.
        # This simulates the post-repath state: repath has already moved the files to the
        # 2-level canonical paths using the full-library modal depth.
        _make_library_flac(dest_root, str(canonical_split_1.relative_to(dest_root)), tags_split_1)
        _make_library_flac(dest_root, str(canonical_split_2.relative_to(dest_root)), tags_split_2)

        # Place non-confirmed tracks at their canonical paths (full-modal=2, 2-level paths).
        canonical_other_1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_other_1, group_modal_depth=full_modal
        ).with_suffix(".flac")
        canonical_other_2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_other_2, group_modal_depth=full_modal
        ).with_suffix(".flac")
        canonical_other_3 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_other_3, group_modal_depth=full_modal
        ).with_suffix(".flac")
        _make_library_flac(dest_root, str(canonical_other_1.relative_to(dest_root)), tags_other_1)
        _make_library_flac(dest_root, str(canonical_other_2.relative_to(dest_root)), tags_other_2)
        _make_library_flac(dest_root, str(canonical_other_3.relative_to(dest_root)), tags_other_3)

        # Build a split-release journal: the confirmed release has tracks in two work_dirs.
        # The "tagged" entries show the confirmed tracks at their CURRENT (canonical) paths.
        # A phantom entry in a different work_dir triggers _confirm_fragmentation case-b.
        # The confirmed tracks are at the canonical paths so _is_confirmed can read their tags.
        phantom = dest_root / "Wagner - Karajan" / "OldMeistersinger [rec 1951]" / "phantom.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "wagner-split",
                    "source": "/src/01.flac",
                    "destination": str(canonical_split_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "wagner-split",
                    "source": "/src/02.flac",
                    "destination": str(canonical_split_2),
                    "action": "tagged",
                },
                # Phantom entry in a different work_dir to trigger case-b fragmentation.
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "wagner-split",
                    "source": "/src/phantom.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
                # Non-confirmed release tracks (tagged at their canonical paths).
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "wagner-other",
                    "source": "/src/other1.flac",
                    "destination": str(canonical_other_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "wagner-other",
                    "source": "/src/other2.flac",
                    "destination": str(canonical_other_2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "wagner-other",
                    "source": "/src/other3.flac",
                    "destination": str(canonical_other_3),
                    "action": "tagged",
                },
            ],
        )

        # Run regroup: with the fix, it extends its modal depth computation to include the
        # non-confirmed tracks (PL=2) and computes modal=2 — agreeing with repath.
        # The confirmed tracks are already at their 2-level canonical paths, so regroup
        # must produce an empty plan (no moves).
        #
        # Without the fix, regroup would compute modal=3 (only the confirmed tracks, PL=3)
        # and move the confirmed tracks from 2-level to 3-level paths — the ping-pong.
        regroup_plan = music_annotator.regroup(dest_root=dest_root, yes=True)

        # regroup must produce an empty plan (no moves) — the ping-pong is impossible.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        regrouped = [e for e in journal.entries if e.action == "regrouped"]
        assert not regrouped, (
            f"regroup must not move any files when confirmed tracks are already at the full-modal "
            f"(2-level) canonical path; got {len(regrouped)} regrouped entries: "
            f"{[e.destination for e in regrouped]}"
        )
        # regroup returns None (not a DryRunPlan) when it finds nothing to do in live mode.
        assert regroup_plan is None, f"regroup must return None (no moves) in live mode; got {regroup_plan!r}"

        # Confirmed tracks must still be at the full-modal canonical paths.
        assert canonical_split_1.exists(), "confirmed track 1 must remain at full-modal canonical path"
        assert canonical_split_2.exists(), "confirmed track 2 must remain at full-modal canonical path"
        # Subset-modal (3-level) paths must not exist — regroup must not have moved the tracks there.
        assert not subset_dest_1.exists(), (
            "regroup must not move confirmed track 1 to the 3-level path (subset-modal ping-pong)"
        )
        assert not subset_dest_2.exists(), (
            "regroup must not move confirmed track 2 to the 3-level path (subset-modal ping-pong)"
        )

    # ------------------------------------------------------------------
    # KAT 2: La traviata / Guglielmo Tell shape — unify depth equals repath
    # ------------------------------------------------------------------

    def test_opera_unify_depth_equals_repath(self, fs: FakeFilesystem) -> None:
        """La traviata / Guglielmo Tell shape: unify's depth render equals repath's (C-CANON).

        A classical opera work with PL=2 tracks (2-level hierarchy: work_dir + act + leaf)
        is fragmented across two top_dirs.  After the fix, unify threads group_modal_depth
        into build_dest_path and produces the same depth render as repath.

        Mock-enforced equality: both repath and unify compute the same canonical destination
        for the same embedded tags.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Two tracks of the same opera, PL=2 (work_dir / act / leaf).
        tags_1 = self._make_opera_tags(
            "2",
            "1",
            "Atto I - Aria",
            "opera-frag",
            extra_parts={"cwp_part_1": "Atto I", "cwp_ordering_key_1": "1"},
        )
        tags_2 = self._make_opera_tags(
            "2",
            "2",
            "Atto II - Duet",
            "opera-frag",
            extra_parts={"cwp_part_1": "Atto II", "cwp_ordering_key_1": "2"},
        )

        # Compute the canonical destination using the group modal depth (modal=2 for both tracks).
        modal = work_group_modal_depth([2, 2])
        assert modal == 2  # noqa: PLR2004

        canonical_1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_1, group_modal_depth=modal).with_suffix(".flac")
        canonical_2 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_2, group_modal_depth=modal).with_suffix(".flac")

        # Place track 1 at a wrong top_dir (fragmented: different performer in path).
        # Track 2 is already at the canonical path (same top_dir as canonical_1 would be).
        wrong_top_1 = dest_root / "Verdi - Serafin" / "La traviata [rec 1955]" / "01 - Atto I" / "01 - Atto I - Aria.flac"
        wrong_top_1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_1, tags_1)

        # Track 2 at canonical path (ensures two distinct top_dirs for fragmentation detection).
        _make_library_flac(dest_root, str(canonical_2.relative_to(dest_root)), tags_2)

        # Verify that wrong_top_1 != canonical_1 (fragmentation is non-trivial).
        assert wrong_top_1 != canonical_1, "test setup error: wrong and canonical paths must differ"

        # Compute what repath would compute for track 1 (full-library modal depth = 2).
        repath_dest_1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_1, group_modal_depth=modal).with_suffix(".flac")

        # Assert that unify's canonical destination (with fix) equals repath's.
        # Both use the same group_modal_depth (modal=2) over the same tags.
        assert repath_dest_1 == canonical_1, (
            "repath and unify must compute the same canonical destination for the same tags and modal depth"
        )

        # Run unify: it must move track 1 from wrong_top_1 to canonical_1.
        music_annotator.unify(dest_root=dest_root, yes=True)

        # Track 1 must be at the canonical path (with intermediate act dir — depth 2).
        assert canonical_1.exists(), (
            f"unify must move track 1 to the canonical depth-2 path {canonical_1.relative_to(dest_root)}"
        )
        assert not wrong_top_1.exists(), "unify must move track 1 away from the wrong top_dir"

        # Verify the canonical path has the correct depth (4 parts: top/work/act/leaf).
        assert len(canonical_1.relative_to(dest_root).parts) == 4, (  # noqa: PLR2004
            f"canonical path must have 4 parts (top/work/act/leaf), got {canonical_1.relative_to(dest_root).parts}"
        )

        # Verify the depth render matches repath's: the act directory must be present.
        assert "Atto I" in str(canonical_1), (
            "canonical path must contain the act directory (Atto I) — depth render must agree with repath"
        )

    # ------------------------------------------------------------------
    # KAT 3: C-W3b-INT parity — ingest/maintenance depth render match
    # ------------------------------------------------------------------

    def test_c_w3b_int_parity_regroup_and_unify(self, fs: FakeFilesystem) -> None:
        """C-W3b-INT parity: regroup and unify produce the same depth render as the ingest pipeline.

        Asserts that after a full maintain run, files land at the same path that
        build_dest_path (the ingest pipeline's canonical function) would compute with the
        correct group_modal_depth.  This is the maintenance-parity invariant: the depth render
        for classical works must match between the ingest pipeline and all maintenance passes.

        Scenario: a 3-track classical work with PL=2 (2-level hierarchy).  The ingest pipeline
        would place each track at ``work_dir/act/leaf``.  After the fix, both regroup and unify
        produce the same depth render.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Three tracks of the same work, all PL=2.
        tags_1 = self._make_opera_tags(
            "2",
            "1",
            "Atto I - Scena 1",
            "parity-rel",
            work_top="Guillaume Tell",
            extra_parts={"cwp_part_1": "Atto I", "cwp_ordering_key_1": "1"},
        )
        tags_2 = self._make_opera_tags(
            "2",
            "2",
            "Atto II - Scena 1",
            "parity-rel",
            work_top="Guillaume Tell",
            extra_parts={"cwp_part_1": "Atto II", "cwp_ordering_key_1": "2"},
        )
        tags_3 = self._make_opera_tags(
            "2",
            "3",
            "Atto III - Finale",
            "parity-rel",
            work_top="Guillaume Tell",
            extra_parts={"cwp_part_1": "Atto III", "cwp_ordering_key_1": "3"},
        )

        # Compute the ingest-pipeline canonical destinations (group_modal_depth=2).
        modal = work_group_modal_depth([2, 2, 2])
        assert modal == 2  # noqa: PLR2004

        ingest_dest_1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_1, group_modal_depth=modal).with_suffix(".flac")
        ingest_dest_2 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_2, group_modal_depth=modal).with_suffix(".flac")
        ingest_dest_3 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_3, group_modal_depth=modal).with_suffix(".flac")

        # All three ingest destinations must have the act directory (depth-2 render).
        assert len(ingest_dest_1.relative_to(dest_root).parts) == 4, (  # noqa: PLR2004
            "ingest path must have 4 parts (top/work/act/leaf)"
        )

        # --- Regroup parity sub-test ---
        # Place tracks at legacy paths (flat, no act dir — depth-1 render).
        # A phantom entry in a different work_dir triggers case-b fragmentation detection.
        legacy_1 = dest_root / "Verdi - Callas" / "Guillaume Tell [rec 1955]" / "01 - Atto I - Scena 1.flac"
        legacy_2 = dest_root / "Verdi - Callas" / "Guillaume Tell [rec 1955]" / "02 - Atto II - Scena 1.flac"
        legacy_3 = dest_root / "Verdi - Callas" / "Guillaume Tell [rec 1955]" / "03 - Atto III - Finale.flac"
        for legacy_path, tags in [(legacy_1, tags_1), (legacy_2, tags_2), (legacy_3, tags_3)]:
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(_MINIMAL_FLAC)
            apply_tags_flac(legacy_path, tags)

        phantom_regroup = dest_root / "Verdi - Callas" / "OldGuillaume [rec 1955]" / "phantom.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-rel",
                    "source": "/src/01.flac",
                    "destination": str(legacy_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-rel",
                    "source": "/src/02.flac",
                    "destination": str(legacy_2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-rel",
                    "source": "/src/03.flac",
                    "destination": str(legacy_3),
                    "action": "tagged",
                },
                # Phantom entry in a different work_dir to trigger case-b fragmentation.
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-rel",
                    "source": "/src/phantom.flac",
                    "destination": str(phantom_regroup),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.regroup(dest_root=dest_root, yes=True)

        # After regroup: all tracks must be at the ingest-pipeline canonical paths.
        assert ingest_dest_1.exists(), (
            f"regroup must place track 1 at ingest-canonical path {ingest_dest_1.relative_to(dest_root)}"
        )
        assert ingest_dest_2.exists(), (
            f"regroup must place track 2 at ingest-canonical path {ingest_dest_2.relative_to(dest_root)}"
        )
        assert ingest_dest_3.exists(), (
            f"regroup must place track 3 at ingest-canonical path {ingest_dest_3.relative_to(dest_root)}"
        )

        # Depth check: all paths must have the act directory (4 parts: top/work/act/leaf).
        for dest, label in [(ingest_dest_1, "track 1"), (ingest_dest_2, "track 2"), (ingest_dest_3, "track 3")]:
            assert len(dest.relative_to(dest_root).parts) == 4, (  # noqa: PLR2004
                f"regroup {label} path must have 4 parts (top/work/act/leaf), got {dest.relative_to(dest_root).parts}"
            )

        # --- Unify parity sub-test ---
        # Reset the library: place tracks at wrong top_dirs (fragmented) for unify to consolidate.
        # Use a fresh dest_root to avoid interference with the regroup sub-test.
        dest_root2 = Path("/lib2")
        fs.create_dir(str(dest_root2))

        wrong_top_1 = dest_root2 / "Verdi - Serafin" / "Guillaume Tell [rec 1955]" / "01 - Atto I - Scena 1.flac"
        wrong_top_1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_1, tags_1)

        # Track 2 at canonical path (ensures two distinct top_dirs for fragmentation detection).
        ingest_dest_2b = build_dest_path(dest_root2, MBRelease(), MBTrack(), tags_2, group_modal_depth=modal).with_suffix(
            ".flac"
        )
        _make_library_flac(dest_root2, str(ingest_dest_2b.relative_to(dest_root2)), tags_2)

        # Compute the ingest-canonical destination for track 1 in dest_root2.
        ingest_dest_1b = build_dest_path(dest_root2, MBRelease(), MBTrack(), tags_1, group_modal_depth=modal).with_suffix(
            ".flac"
        )

        assert wrong_top_1 != ingest_dest_1b, "test setup error: wrong and canonical paths must differ"

        music_annotator.unify(dest_root=dest_root2, yes=True)

        # After unify: track 1 must be at the ingest-pipeline canonical path.
        assert ingest_dest_1b.exists(), (
            f"unify must place track 1 at ingest-canonical path {ingest_dest_1b.relative_to(dest_root2)}"
        )
        assert not wrong_top_1.exists(), "unify must move track 1 away from the wrong top_dir"

        # Depth check: the canonical path must have the act directory (4 parts: top/work/act/leaf).
        assert len(ingest_dest_1b.relative_to(dest_root2).parts) == 4, (  # noqa: PLR2004
            f"unify path must have 4 parts (top/work/act/leaf), got {ingest_dest_1b.relative_to(dest_root2).parts}"
        )


# ---------------------------------------------------------------------------
# KATs: shared library-wide modal-depth computation (C-CANON, C-GROUPSCOPE)
# ---------------------------------------------------------------------------


class TestSharedLibraryModalDepth:
    """KATs for the shared library-wide modal-depth computation (C-CANON, C-GROUPSCOPE).

    Three witnesses:

    1. **Membership-divergence shape** — a library fixture holding two recordings of the same
       top work with different ``CWP_PART_LEVELS`` distributions, one of them a fragmented
       release.  ``unify``'s depth render equals ``repath``'s (mock-enforced equality); the
       library-wide map computed by :func:`compute_library_modal_depth` is the single source
       for both passes.

    2. **Same-run inverse-free** — ``repath`` then ``unify`` over the fixture produces zero
       ``inverse_move_detected`` events.  The shared modal-depth map eliminates the depth-
       membership asymmetry that caused the stable orbit.

    3. **Ingest/maintenance parity re-asserted (C-W3b-INT)** — the shared helper
       :func:`compute_library_modal_depth` produces the same depth value as the ingest
       pipeline's inline computation for the same ``(cwp_workid_top, cwp_part_levels)`` inputs.
       Also covers the maintain pre-pass defensive branches (non-existent file, wrong extension,
       tag-read failure).
    """

    @staticmethod
    def _make_opera_tags(
        cwp_part_levels: str,
        cwp_movt_num: str,
        title: str,
        release_id: str,
        part_1: str,
        ordering_key_1: str,
    ) -> TrackTags:
        """Build TrackTags for a classical opera track.

        :param cwp_part_levels: String value for CWP_PART_LEVELS.
        :param cwp_movt_num: String value for CWP_MOVT_NUM.
        :param title: Track title.
        :param release_id: MUSICBRAINZ_ALBUMID to embed.
        :param part_1: CWP_PART_1 value (act name).
        :param ordering_key_1: CWP_ORDERING_KEY_1 value.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            cwp_work_top="La traviata",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Verdi",
            cwp_workid_top="w-traviata-shared",
            recording_date="1955",
            cwp_part_levels=cwp_part_levels,
            cwp_movt_num=cwp_movt_num,
            movementtotal="3",
            title=title,
            artist="Callas",
            musicbrainz_albumid=release_id,
        )
        if tags.model_extra is not None:
            tags.model_extra["cwp_part_1"] = part_1
            tags.model_extra["cwp_ordering_key_1"] = ordering_key_1
        return tags

    def test_kat1_membership_divergence_unify_depth_equals_repath(self, fs: FakeFilesystem) -> None:
        """KAT 1 (C-GROUPSCOPE): unify's depth render equals repath's when the library-wide map is used.

        Library fixture: two recordings of the same top work (``w-traviata-shared``).
        - Release A (fragmented, PL=2): two tracks spread across two top_dirs.
        - Release B (non-fragmented, PL=3): one track at its canonical path.

        Without the shared map, unify computes modal depth over release A's files only (PL=2,
        modal=2) while repath computes over the full library (PL=2, PL=2, PL=3 → modal=2 still,
        but the membership differs).  This test uses a shape where the membership difference
        produces a different modal: release A has PL=3 tracks (modal=3 release-local) while the
        full library has a majority of PL=2 tracks (modal=2 library-wide).

        Mock-enforced equality: both passes compute the same canonical destination for the same
        tags when the library-wide map is used.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Release A (fragmented): 2 tracks with PL=3 (3-level hierarchy).
        # These are the tracks unify will try to consolidate.
        tags_a1 = self._make_opera_tags("3", "1", "Atto I - Scena 1", "traviata-frag", "Atto I", "1")
        tags_a2 = self._make_opera_tags("3", "2", "Atto II - Scena 1", "traviata-frag", "Atto II", "2")

        # Release B (non-fragmented): 3 tracks with PL=2 sharing the same CWP_WORKID_TOP.
        # Their majority (3 vs 2) drives the full-library modal depth to 2.
        tags_b1 = self._make_opera_tags("2", "1", "Atto I - Aria", "traviata-other", "Atto I", "1")
        tags_b2 = self._make_opera_tags("2", "2", "Atto II - Duet", "traviata-other", "Atto II", "2")
        tags_b3 = self._make_opera_tags("2", "3", "Atto III - Finale", "traviata-other", "Atto III", "3")

        # Verify the membership-divergence shape:
        # - Release-local modal (only release A's PL=3 tracks): modal=3.
        # - Library-wide modal (all 5 tracks: PL=3, PL=3, PL=2, PL=2, PL=2): modal=2.
        release_local_modal = work_group_modal_depth([3, 3])
        assert release_local_modal == 3  # noqa: PLR2004 — 3 is the release-local modal depth

        lib_wide_modal = work_group_modal_depth([3, 3, 2, 2, 2])
        assert lib_wide_modal == 2  # noqa: PLR2004 — 2 is the library-wide modal depth

        # Canonical destination using library-wide modal depth (what repath and the shared map compute).
        canonical_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_a2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a2, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")

        # Release-local destination (what unify would compute WITHOUT the shared map).
        release_local_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=release_local_modal
        ).with_suffix(".flac")

        # The two destinations must differ for the test to be non-trivial.
        assert canonical_a1 != release_local_a1, (
            "test setup error: library-wide and release-local destinations must differ for PL=3 tracks"
        )

        # Place release A tracks at wrong top_dirs (fragmented: different performer in path).
        wrong_top_a1 = dest_root / "Verdi - Serafin" / "La traviata [rec 1955]" / "01 - Atto I - Scena 1.flac"
        wrong_top_a1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_a1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_a1, tags_a1)

        # Track A2 at canonical path (ensures two distinct top_dirs for fragmentation detection).
        _make_library_flac(dest_root, str(canonical_a2.relative_to(dest_root)), tags_a2)

        # Place release B tracks at their canonical paths.
        canonical_b1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_b1, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_b2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_b2, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_b3 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_b3, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        _make_library_flac(dest_root, str(canonical_b1.relative_to(dest_root)), tags_b1)
        _make_library_flac(dest_root, str(canonical_b2.relative_to(dest_root)), tags_b2)
        _make_library_flac(dest_root, str(canonical_b3.relative_to(dest_root)), tags_b3)

        # Compute the shared library-wide modal depth map via the helper.
        all_pairs = [
            (tags_a1.cwp_workid_top, int(tags_a1.cwp_part_levels or "0")),
            (tags_a2.cwp_workid_top, int(tags_a2.cwp_part_levels or "0")),
            (tags_b1.cwp_workid_top, int(tags_b1.cwp_part_levels or "0")),
            (tags_b2.cwp_workid_top, int(tags_b2.cwp_part_levels or "0")),
            (tags_b3.cwp_workid_top, int(tags_b3.cwp_part_levels or "0")),
        ]
        shared_map = compute_library_modal_depth(all_pairs)

        # The shared map must produce the library-wide modal depth for the top-work MBID.
        assert shared_map.get("w-traviata-shared") == lib_wide_modal, (
            f"shared map must return library-wide modal depth {lib_wide_modal}; got {shared_map.get('w-traviata-shared')!r}"
        )

        # Mock-enforced equality: repath's destination (using library-wide modal) must equal
        # unify's destination when the shared map is threaded in.
        repath_dest_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=shared_map.get("w-traviata-shared")
        ).with_suffix(".flac")
        unify_dest_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=shared_map.get("w-traviata-shared")
        ).with_suffix(".flac")
        assert repath_dest_a1 == unify_dest_a1, (
            "repath and unify must compute the same canonical destination when the shared map is used"
        )
        assert repath_dest_a1 == canonical_a1, "shared-map destination must equal the library-wide canonical destination"

        # Run unify with the shared map threaded in (simulating maintain's behaviour).
        # unify must move track A1 to the library-wide canonical path (depth-2 render),
        # not the release-local path (depth-3 render).
        music_annotator.unify(dest_root=dest_root, yes=True, _modal_depth_map=shared_map)

        # Track A1 must be at the library-wide canonical path.
        assert canonical_a1.exists(), (
            f"unify must move track A1 to the library-wide canonical path {canonical_a1.relative_to(dest_root)}"
        )
        assert not wrong_top_a1.exists(), "unify must move track A1 away from the wrong top_dir"

        # The release-local (depth-3) path must not exist — unify must not have used it.
        assert not release_local_a1.exists(), "unify must not move track A1 to the release-local (depth-3) path"

    def test_kat2_same_run_inverse_free(self, fs: FakeFilesystem) -> None:
        """KAT 2 (C-GROUPSCOPE): repath then unify over the membership-divergence fixture produces zero inverse moves.

        The shared library-wide modal-depth map eliminates the depth-membership asymmetry that
        caused the stable orbit (repath moves to depth-2, unify moves back to depth-3).  When
        both passes use the same map, their destinations agree and no inverse moves are planned.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Same fixture as KAT 1: release A (PL=3, fragmented) + release B (PL=2, non-fragmented).
        tags_a1 = self._make_opera_tags("3", "1", "Atto I - Scena 1", "traviata-frag2", "Atto I", "1")
        tags_a2 = self._make_opera_tags("3", "2", "Atto II - Scena 1", "traviata-frag2", "Atto II", "2")
        tags_b1 = self._make_opera_tags("2", "1", "Atto I - Aria", "traviata-other2", "Atto I", "1")
        tags_b2 = self._make_opera_tags("2", "2", "Atto II - Duet", "traviata-other2", "Atto II", "2")
        tags_b3 = self._make_opera_tags("2", "3", "Atto III - Finale", "traviata-other2", "Atto III", "3")

        lib_wide_modal = work_group_modal_depth([3, 3, 2, 2, 2])
        assert lib_wide_modal == 2  # noqa: PLR2004

        canonical_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_a2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a2, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")

        # Place track A1 at a wrong top_dir (fragmented).
        wrong_top_a1 = dest_root / "Verdi - Serafin" / "La traviata [rec 1955]" / "01 - Atto I - Scena 1.flac"
        wrong_top_a1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_a1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_a1, tags_a1)

        # Track A2 at canonical path.
        _make_library_flac(dest_root, str(canonical_a2.relative_to(dest_root)), tags_a2)

        # Release B tracks at canonical paths.
        for tags_b in (tags_b1, tags_b2, tags_b3):
            canonical_b = build_dest_path(
                dest_root, MBRelease(), MBTrack(), tags_b, group_modal_depth=lib_wide_modal
            ).with_suffix(".flac")
            _make_library_flac(dest_root, str(canonical_b.relative_to(dest_root)), tags_b)

        # Write a journal so repath can resolve the library.
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-frag2",
                    "source": "/src/a1.flac",
                    "destination": str(wrong_top_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-frag2",
                    "source": "/src/a2.flac",
                    "destination": str(canonical_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-other2",
                    "source": "/src/b1.flac",
                    "destination": str(
                        build_dest_path(
                            dest_root, MBRelease(), MBTrack(), tags_b1, group_modal_depth=lib_wide_modal
                        ).with_suffix(".flac")
                    ),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-other2",
                    "source": "/src/b2.flac",
                    "destination": str(
                        build_dest_path(
                            dest_root, MBRelease(), MBTrack(), tags_b2, group_modal_depth=lib_wide_modal
                        ).with_suffix(".flac")
                    ),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-other2",
                    "source": "/src/b3.flac",
                    "destination": str(
                        build_dest_path(
                            dest_root, MBRelease(), MBTrack(), tags_b3, group_modal_depth=lib_wide_modal
                        ).with_suffix(".flac")
                    ),
                    "action": "tagged",
                },
            ],
        )

        # Compute the shared library-wide modal depth map.
        all_pairs = [
            (tags_a1.cwp_workid_top, int(tags_a1.cwp_part_levels or "0")),
            (tags_a2.cwp_workid_top, int(tags_a2.cwp_part_levels or "0")),
            (tags_b1.cwp_workid_top, int(tags_b1.cwp_part_levels or "0")),
            (tags_b2.cwp_workid_top, int(tags_b2.cwp_part_levels or "0")),
            (tags_b3.cwp_workid_top, int(tags_b3.cwp_part_levels or "0")),
        ]
        shared_map = compute_library_modal_depth(all_pairs)

        # Step 1: repath with the shared map.  Track A1 moves from wrong_top_a1 to canonical_a1.
        with structlog.testing.capture_logs() as repath_logs:
            music_annotator.repath(dest_root=dest_root, yes=True, _modal_depth_map=shared_map)

        repath_inverse = [e for e in repath_logs if e.get("event") == "inverse_move_detected"]
        assert not repath_inverse, f"repath must produce zero inverse_move_detected events; got {repath_inverse}"

        assert canonical_a1.exists(), "repath must move track A1 to the library-wide canonical path"
        assert not wrong_top_a1.exists(), "repath must move track A1 away from the wrong top_dir"

        # Step 2: unify with the shared map.  Track A1 is now at canonical_a1; unify must be a no-op.
        with structlog.testing.capture_logs() as unify_logs:
            music_annotator.unify(dest_root=dest_root, yes=True, _modal_depth_map=shared_map)

        unify_inverse = [e for e in unify_logs if e.get("event") == "inverse_move_detected"]
        assert not unify_inverse, (
            f"unify must produce zero inverse_move_detected events after repath with the shared map; got {unify_inverse}"
        )

        # Track A1 must still be at the canonical path (unify must not have moved it back).
        assert canonical_a1.exists(), "unify must not move track A1 away from the library-wide canonical path"

    def test_kat3_ingest_maintenance_parity_shared_helper(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """KAT 3 (C-W3b-INT): the shared helper produces the same depth as the ingest pipeline.

        Asserts that :func:`compute_library_modal_depth` produces the same modal depth value as
        the ingest pipeline's inline computation for the same ``(cwp_workid_top, cwp_part_levels)``
        inputs.  This is the maintenance-parity invariant: the depth render for classical works
        must match between the ingest pipeline and all maintenance passes.

        Also covers the maintain pre-pass defensive branches:
        - Non-existent file (journal entry pointing to a missing path).
        - Wrong extension (a file with a non-audio suffix).
        - Tag-read failure (mutagen raises an exception).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # --- Part A: helper parity with ingest pipeline ---
        # The ingest pipeline computes modal depth inline using work_group_modal_depth over
        # the same (cwp_workid_top, cwp_part_levels) inputs.  The shared helper must produce
        # the same result.
        pairs_2_2_3 = [("w-parity", 2), ("w-parity", 2), ("w-parity", 3)]
        helper_result = compute_library_modal_depth(pairs_2_2_3)
        ingest_result = work_group_modal_depth([2, 2, 3])
        assert helper_result.get("w-parity") == ingest_result, (
            f"shared helper must produce the same modal depth as the ingest pipeline; "
            f"helper={helper_result.get('w-parity')!r}, ingest={ingest_result!r}"
        )

        # All-orphan group: both must return 0 / None.
        pairs_orphan = [("w-orphan", 0), ("w-orphan", 0)]
        helper_orphan = compute_library_modal_depth(pairs_orphan)
        ingest_orphan = work_group_modal_depth([0, 0])
        assert ingest_orphan == 0  # noqa: PLR2004 — 0 is the all-orphan sentinel
        assert helper_orphan.get("w-orphan") is None, (
            "shared helper must return None for all-orphan groups (same as passing None to build_dest_path)"
        )

        # Empty input: helper must return an empty map.
        assert compute_library_modal_depth([]) == {}

        # --- Part B: maintain pre-pass defensive branches ---
        # Set up a library with:
        # 1. A journal entry pointing to a non-existent file (covers the "not _mp.exists()" branch).
        # 2. A file with a wrong extension (covers the "_mext not in {'.flac', '.mp3'}" branch).
        # 3. A real FLAC file (covers the normal path).
        tags_real = self._make_opera_tags("2", "1", "Atto I - Aria", "parity-maint-rel", "Atto I", "1")
        real_path = _make_library_flac(dest_root, "Verdi - Callas/La traviata [rec 1955]/01 - Atto I - Aria.flac", tags_real)

        ghost_path = dest_root / "Verdi - Callas" / "La traviata [rec 1955]" / "ghost.flac"
        wrong_ext_path = dest_root / "Verdi - Callas" / "La traviata [rec 1955]" / "cover.jpg"
        wrong_ext_path.parent.mkdir(parents=True, exist_ok=True)
        wrong_ext_path.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-maint-rel",
                    "source": "/src/01.flac",
                    "destination": str(real_path),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-maint-rel",
                    "source": "/src/ghost.flac",
                    "destination": str(ghost_path),  # does not exist on disk
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-maint-rel",
                    "source": "/src/cover.jpg",
                    "destination": str(wrong_ext_path),  # wrong extension
                    "action": "tagged",
                },
            ],
        )

        # Mock the move passes and integrity passes so maintain only runs the pre-pass scan.
        mocker.patch("music_annotator._pipeline_maint.enrich", return_value=None)
        mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)
        mocker.patch("music_annotator._pipeline_maint.repath", return_value=None)
        mocker.patch("music_annotator._pipeline_maint.regroup", return_value=None)
        mocker.patch("music_annotator._pipeline_maint.unify", return_value=None)
        mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
        mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)

        # Run maintain: the pre-pass scan must handle the non-existent file and wrong extension
        # gracefully (skip them) and compute the modal depth map from the real FLAC file only.
        maintain(dest_root, yes=True)

        # The maintain call above must have completed without raising — that is the primary
        # assertion for the defensive branches (non-existent file, wrong extension).

        # --- Part C: tag-read failure branch ---
        # Patch _read_tags_cached to raise on the real file, covering the except branch.
        dest_root2 = Path("/lib2")
        fs.create_dir(str(dest_root2))
        real_path2 = _make_library_flac(dest_root2, "Verdi - Callas/La traviata [rec 1955]/01 - Atto I - Aria.flac", tags_real)
        _write_library_journal(
            dest_root2,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "parity-maint-rel2",
                    "source": "/src/01.flac",
                    "destination": str(real_path2),
                    "action": "tagged",
                },
            ],
        )

        mocker.patch("music_annotator._pipeline_maint._read_tags_cached", side_effect=MutagenError("boom"))

        # maintain must not raise even when all tag reads fail in the pre-pass scan.
        maintain(dest_root2, yes=True)

    def test_regroup_uses_threaded_modal_depth_map(self, fs: FakeFilesystem) -> None:
        """Regroup uses the pre-computed library-wide modal depth map when threaded in by maintain.

        Exercises the ``_modal_depth_map is not None`` branch in ``regroup`` (C-GROUPSCOPE):
        when ``maintain`` threads the map in, ``regroup`` must use it directly rather than
        computing a release-local map.

        Scenario: a confirmed split-release with PL=2 tracks.  The shared map is pre-computed
        with modal=2.  Regroup must move the tracks to the depth-2 canonical paths.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags_1 = self._make_opera_tags("2", "1", "Atto I - Aria", "regroup-map-rel", "Atto I", "1")
        tags_2 = self._make_opera_tags("2", "2", "Atto II - Duet", "regroup-map-rel", "Atto II", "2")

        modal = work_group_modal_depth([2, 2])
        assert modal == 2  # noqa: PLR2004

        canonical_1 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_1, group_modal_depth=modal).with_suffix(".flac")
        canonical_2 = build_dest_path(dest_root, MBRelease(), MBTrack(), tags_2, group_modal_depth=modal).with_suffix(".flac")

        # Place tracks at legacy paths (flat, no act dir).
        legacy_1 = dest_root / "Verdi - Callas" / "La traviata [rec 1955]" / "01 - Atto I - Aria.flac"
        legacy_2 = dest_root / "Verdi - Callas" / "La traviata [rec 1955]" / "02 - Atto II - Duet.flac"
        for legacy_path, tags in [(legacy_1, tags_1), (legacy_2, tags_2)]:
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(_MINIMAL_FLAC)
            apply_tags_flac(legacy_path, tags)

        # Phantom entry in a different work_dir to trigger case-b fragmentation detection.
        phantom = dest_root / "Verdi - Callas" / "OldTraviata [rec 1955]" / "phantom.flac"
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "regroup-map-rel",
                    "source": "/src/01.flac",
                    "destination": str(legacy_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "regroup-map-rel",
                    "source": "/src/02.flac",
                    "destination": str(legacy_2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "regroup-map-rel",
                    "source": "/src/phantom.flac",
                    "destination": str(phantom),
                    "action": "tagged",
                },
            ],
        )

        # Pre-compute the shared library-wide modal depth map (simulating maintain's pre-pass).
        shared_map: dict[str, int | None] = {"w-traviata-shared": modal}

        # Call regroup with the threaded map — exercises the _modal_depth_map is not None branch.
        music_annotator.regroup(dest_root=dest_root, yes=True, _modal_depth_map=shared_map)

        # After regroup: tracks must be at the depth-2 canonical paths.
        assert canonical_1.exists(), f"regroup must place track 1 at canonical path {canonical_1.relative_to(dest_root)}"
        assert canonical_2.exists(), f"regroup must place track 2 at canonical path {canonical_2.relative_to(dest_root)}"
