"""Composite-idempotence KAT harness and inverse-move tripwire tests (C-IDEM).

Covers every observed cycle shape that was fixed by the canonical unification work:

1. **Non-classical release (Kidz-Bop / Goodman shape)** — a release fragmented across top dirs
   because ``unify``'s old W2b patch manufactured a composer from ``ALBUMARTISTSORT``.  After the
   fix, ``unify`` consolidates to the ALBUMARTIST-led top dir (C-NC-TOP).  The second ``maintain``
   run must report "no changes" and append zero journal entries.

2. **Various-Artists compilation** — a release whose tracks were scattered across per-track top
   dirs because ``"Various"`` was treated as a composer.  After the fix, all tracks consolidate to
   the ``Various Artists`` ALBUMARTIST-led top dir.  Second run: no changes.

3. **Classical work with depth disagreement (La traviata shape)** — a classical opera fragmented
   across top dirs, where ``unify`` previously omitted ``group_modal_depth`` and produced a
   depth render that disagreed with ``repath``'s.  After the fix, ``unify`` threads
   ``group_modal_depth`` and agrees with ``repath``.  Second run: no changes.

4. **Inverse-move tripwire** — a deliberately-divergent stub canonical triggers the
   ``inverse_move_detected`` warning before executing the plan.  The tripwire warns; it does not
   block (C-CONFLUENCE ergonomics register: no formal oscillation calculus).

5. **Completer chain shape (K.626 / Mozart; Süßmayr)** — a classical release whose movements carry
   two distinct embedded composer chains (``Mozart`` and ``Mozart; Süßmayr``).  The old pass-local
   in-memory chain patch unified all movements under the fullest chain, causing ``repath`` and
   ``unify`` to disagree on the destination.  After the patch is deleted, each movement renders its
   raw embedded chain; the two chains produce two distinct top dirs (the accepted fixpoint).  The
   second ``maintain`` run must report "no changes" and append zero journal entries.

6. **Empty-composer non-classical flip** — a jazz release whose tracks carry ``MUSICBRAINZ_WORKID``
   and empty composer tags.  The old chain patch applied to these tracks via the ``musicbrainz_workid``
   fallback, flipping their top dir from ALBUMARTIST-led to composer-led (a C-NC-TOP violation).
   After the patch is deleted, the top dir stays ALBUMARTIST-led.  The second ``maintain`` run must
   report "no changes" and append zero journal entries.

7. **Depth-membership shape** — a library fixture with two recordings of the same top work with
   different ``cwp_part_levels`` distributions, one of them a fragmented release.  The old ``unify``
   computed modal depth over the fragmented release's files only (release-local membership), while
   ``repath`` computed over the full library (library-wide membership).  The same statistic over
   different denominators produced a stable orbit.  After the fix, all passes use the library-wide
   modal depth map (C-GROUPSCOPE).  The second ``maintain`` run must report "no changes" and append
   zero journal entries.
"""

# pylint: disable=duplicate-code  # test setup patterns are intentionally similar across test modules

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
import structlog.testing
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

from music_annotator._pipeline_maint import _warn_inverse_moves, maintain
from music_annotator._tagger import apply_tags_flac
from music_annotator._tags import build_dest_path
from music_annotator._works import work_group_modal_depth
from music_annotator.models import (
    MBRelease,
    MBTrack,
    TrackTags,
    TransactionEntry,
    TransactionLog,
)
from tests.conftest import _MINIMAL_FLAC

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a JSONL journal file to ``dest_root / music_annotator_journal.json``.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / "music_annotator_journal.json"
    journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _make_flac(dest_root: Path, rel_path: str, tags: TrackTags) -> Path:
    """Create a tagged FLAC file at ``dest_root / rel_path``.

    :param dest_root: Library root directory.
    :param rel_path: Relative path within the library.
    :param tags: Tags to embed via :func:`~music_annotator._tagger.apply_tags_flac`.
    :returns: Full absolute path of the created file.
    """
    full_path = dest_root / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_bytes(_MINIMAL_FLAC)
    apply_tags_flac(full_path, tags)
    return full_path


def _canonical_path(dest_root: Path, tags: TrackTags, modal: int | None = None) -> Path:
    """Compute the canonical destination for ``tags`` via :func:`build_dest_path`.

    :param dest_root: Library root.
    :param tags: Tags to drive path construction.
    :param modal: Optional ``group_modal_depth`` override.
    :returns: Full absolute path with ``.flac`` suffix.
    """
    return build_dest_path(dest_root, MBRelease(), MBTrack(), tags, global_track_idx=0, group_modal_depth=modal).with_suffix(
        ".flac"
    )


def _mock_integrity_passes(mocker: MockerFixture) -> None:
    """Mock the two integrity passes (reconstruct-xrefs, dedup-library) to be no-ops.

    These passes require interactive prompts and network calls that are out of scope for
    the idempotence KATs.  Mocking them isolates the move passes (repath, regroup, unify).

    :param mocker: pytest-mock fixture.
    """
    mocker.patch("music_annotator._pipeline_maint.reconstruct_cross_references", return_value=[])
    mocker.patch("music_annotator._pipeline_maint.dedup_library", return_value=0)


def _mock_content_passes(mocker: MockerFixture) -> None:
    """Mock the two content passes (enrich, origin-time) to be no-ops.

    These passes require fpcalc and sidecar I/O that are out of scope for the idempotence KATs.
    Mocking them isolates the move passes (repath, regroup, unify).

    :param mocker: pytest-mock fixture.
    """
    mocker.patch("music_annotator._pipeline_maint.enrich", return_value=None)
    mocker.patch("music_annotator._pipeline_maint.enrich_origin_time", return_value=0)


# ---------------------------------------------------------------------------
# KAT 1: Non-classical release (Kidz-Bop / Goodman shape)
# ---------------------------------------------------------------------------


class TestNonClassicalIdempotence:
    """KAT: non-classical release consolidation is idempotent (C-IDEM, C-NC-TOP).

    Covers the Kidz-Bop / Goodman cycle shape: a non-classical release whose tracks were
    fragmented across top dirs because the old W2b patch manufactured a composer from
    ``ALBUMARTISTSORT``.  After the fix, ``unify`` consolidates all tracks to the
    ALBUMARTIST-led top dir.  The second ``maintain`` run must report "no changes" and
    append zero journal entries.
    """

    @staticmethod
    def _make_nonclassical_tags(track_num: str, title: str, release_id: str, albumartist: str) -> TrackTags:
        """Build TrackTags for a non-classical track (no CWP work hierarchy).

        :param track_num: Track number string (e.g. ``"1"``).
        :param title: Track title.
        :param release_id: MUSICBRAINZ_ALBUMID to embed.
        :param albumartist: ALBUMARTIST tag value.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        return TrackTags(
            title=title,
            musicbrainz_albumid=release_id,
            albumartist=albumartist,
            artist=albumartist,
            cwp_movt_num=track_num,
            movementtotal="2",
            recording_date="2010",
        )

    def test_kidz_bop_shape_second_run_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Kidz-Bop shape: second maintain run reports no changes after consolidation.

        A non-classical release (Kidz Bop) is fragmented across two top dirs.  The first
        ``maintain`` run consolidates both tracks to the ALBUMARTIST-led top dir.  The second
        run must report "no changes" and append zero journal entries.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        release_id = "kidz-bop-rel-1"
        albumartist = "Kidz Bop Kids"

        tags_1 = self._make_nonclassical_tags("1", "Party Rock Anthem", release_id, albumartist)
        tags_2 = self._make_nonclassical_tags("2", "Call Me Maybe", release_id, albumartist)

        # Compute the canonical (ALBUMARTIST-led) destination for both tracks.
        canonical_1 = _canonical_path(dest_root, tags_1)
        canonical_2 = _canonical_path(dest_root, tags_2)

        # Place tracks at fragmented (wrong) top dirs — simulating the pre-fix state where
        # W2b manufactured a composer from ALBUMARTISTSORT and scattered the release.
        wrong_top_1 = dest_root / "Kidz Bop Kids - Kidz Bop Kids" / "Kidz Bop [2010]" / "01 - Party Rock Anthem.flac"
        wrong_top_2 = dest_root / "Kidz Bop Kids - Kidz Bop Kids" / "Kidz Bop [2010]" / "02 - Call Me Maybe.flac"
        wrong_top_1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_1.write_bytes(_MINIMAL_FLAC)
        wrong_top_2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_1, tags_1)
        apply_tags_flac(wrong_top_2, tags_2)

        # Verify the canonical paths differ from the wrong paths (fragmentation is non-trivial).
        assert canonical_1 != wrong_top_1, "test setup: canonical and wrong paths must differ"
        assert canonical_2 != wrong_top_2, "test setup: canonical and wrong paths must differ"

        # Journal: both tracks tagged at the wrong (fragmented) paths.
        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/01.flac",
                    "destination": str(wrong_top_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/02.flac",
                    "destination": str(wrong_top_2),
                    "action": "tagged",
                },
            ],
        )

        # --- Run 1: maintain consolidates the fragmented release ---
        changed_1 = maintain(dest_root, yes=True)

        # After run 1: both tracks must be at the canonical paths.
        assert canonical_1.exists(), f"run 1: track 1 must be at canonical path {canonical_1.relative_to(dest_root)}"
        assert canonical_2.exists(), f"run 1: track 2 must be at canonical path {canonical_2.relative_to(dest_root)}"
        assert not wrong_top_1.exists(), "run 1: track 1 must be moved away from the wrong top dir"
        assert not wrong_top_2.exists(), "run 1: track 2 must be moved away from the wrong top dir"
        assert changed_1 > 0, "run 1 must report changes (consolidation moves)"

        # --- Run 2: maintain must be a no-op ---
        journal_before_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        changed_2 = maintain(dest_root, yes=True)

        assert changed_2 == 0, f"run 2 must report no changes; got changed={changed_2}"
        journal_after_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        assert journal_after_run2 == journal_before_run2, "run 2 must append zero journal entries"

    def test_various_artists_shape_second_run_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Various-Artists compilation: second maintain run reports no changes after consolidation.

        A Various-Artists compilation is fragmented across two top dirs.  The first ``maintain``
        run consolidates all tracks to the ``Various Artists``-led top dir.  The second run must
        report "no changes" and append zero journal entries.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        release_id = "various-jazz-rel-1"
        albumartist = "Various Artists"

        tags_1 = self._make_nonclassical_tags("1", "Take Five", release_id, albumartist)
        tags_2 = self._make_nonclassical_tags("2", "So What", release_id, albumartist)

        canonical_1 = _canonical_path(dest_root, tags_1)
        canonical_2 = _canonical_path(dest_root, tags_2)

        # Place tracks at fragmented top dirs — simulating the old Various-scattering shape.
        wrong_top_1 = dest_root / "Various - Jazz Collection" / "Jazz Collection [2010]" / "01 - Take Five.flac"
        wrong_top_2 = dest_root / "Various Artists" / "Jazz Collection [2010]" / "02 - So What.flac"
        wrong_top_1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_2.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_1.write_bytes(_MINIMAL_FLAC)
        wrong_top_2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_1, tags_1)
        apply_tags_flac(wrong_top_2, tags_2)

        # Verify fragmentation is non-trivial: the two tracks are in different top dirs.
        assert wrong_top_1.parent.parent != wrong_top_2.parent.parent, "test setup: tracks must be in different top dirs"

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/01.flac",
                    "destination": str(wrong_top_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/02.flac",
                    "destination": str(wrong_top_2),
                    "action": "tagged",
                },
            ],
        )

        # --- Run 1: maintain consolidates the fragmented release ---
        changed_1 = maintain(dest_root, yes=True)

        assert canonical_1.exists(), f"run 1: track 1 must be at canonical path {canonical_1.relative_to(dest_root)}"
        assert canonical_2.exists(), f"run 1: track 2 must be at canonical path {canonical_2.relative_to(dest_root)}"
        assert changed_1 > 0, "run 1 must report changes (consolidation moves)"

        # --- Run 2: maintain must be a no-op ---
        journal_before_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        changed_2 = maintain(dest_root, yes=True)

        assert changed_2 == 0, f"run 2 must report no changes; got changed={changed_2}"
        journal_after_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        assert journal_after_run2 == journal_before_run2, "run 2 must append zero journal entries"


# ---------------------------------------------------------------------------
# KAT 2: Classical work with depth disagreement (La traviata shape)
# ---------------------------------------------------------------------------


class TestClassicalDepthIdempotence:
    """KAT: classical work depth consolidation is idempotent (C-IDEM, C-CANON).

    Covers the La traviata / depth-insertion cycle shape: a classical opera fragmented across
    top dirs, where ``unify`` previously omitted ``group_modal_depth`` and produced a depth
    render that disagreed with ``repath``'s.  After the fix, ``unify`` threads
    ``group_modal_depth`` and agrees with ``repath``.  The second ``maintain`` run must report
    "no changes" and append zero journal entries.
    """

    @staticmethod
    def _make_opera_tags(
        track_num: str,
        title: str,
        release_id: str,
        part_1: str,
        ordering_key_1: str,
    ) -> TrackTags:
        """Build TrackTags for a classical opera track with PL=2 (2-level hierarchy).

        :param track_num: Track number string.
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
            cwp_workid_top="w-traviata",
            recording_date="1955",
            cwp_part_levels="2",
            cwp_movt_num=track_num,
            movementtotal="2",
            title=title,
            artist="Callas",
            musicbrainz_albumid=release_id,
        )
        if tags.model_extra is not None:
            tags.model_extra["cwp_part_1"] = part_1
            tags.model_extra["cwp_ordering_key_1"] = ordering_key_1
        return tags

    def test_opera_depth_second_run_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """La traviata shape: second maintain run reports no changes after depth consolidation.

        A classical opera with PL=2 tracks is fragmented across two top dirs.  The first
        ``maintain`` run consolidates both tracks to the canonical depth-2 path (with act dir).
        The second run must report "no changes" and append zero journal entries.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        release_id = "traviata-rel-1"

        tags_1 = self._make_opera_tags("1", "Atto I - Aria", release_id, "Atto I", "1")
        tags_2 = self._make_opera_tags("2", "Atto II - Duet", release_id, "Atto II", "2")

        # Compute the canonical depth-2 destination (with act dir).
        modal = work_group_modal_depth([2, 2])
        assert modal == 2  # noqa: PLR2004 — 2 is the expected modal depth for PL=2 tracks

        canonical_1 = _canonical_path(dest_root, tags_1, modal)
        canonical_2 = _canonical_path(dest_root, tags_2, modal)

        # Verify the canonical paths have 4 parts (top/work/act/leaf) — depth-2 render.
        assert len(canonical_1.relative_to(dest_root).parts) == 4, (  # noqa: PLR2004
            "canonical path must have 4 parts (top/work/act/leaf)"
        )

        # Place track 1 at a wrong top dir (fragmented: different performer in path).
        # Track 2 is at the canonical path (ensures two distinct top_dirs for fragmentation detection).
        wrong_top_1 = dest_root / "Verdi - Serafin" / "La traviata [rec 1955]" / "01 - Atto I - Aria.flac"
        wrong_top_1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_1, tags_1)

        _make_flac(dest_root, str(canonical_2.relative_to(dest_root)), tags_2)

        assert wrong_top_1 != canonical_1, "test setup: wrong and canonical paths must differ"

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/01.flac",
                    "destination": str(wrong_top_1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/02.flac",
                    "destination": str(canonical_2),
                    "action": "tagged",
                },
            ],
        )

        # --- Run 1: maintain consolidates the fragmented release ---
        changed_1 = maintain(dest_root, yes=True)

        assert canonical_1.exists(), f"run 1: track 1 must be at canonical path {canonical_1.relative_to(dest_root)}"
        assert canonical_2.exists(), f"run 1: track 2 must remain at canonical path {canonical_2.relative_to(dest_root)}"
        assert not wrong_top_1.exists(), "run 1: track 1 must be moved away from the wrong top dir"
        assert changed_1 > 0, "run 1 must report changes (consolidation moves)"

        # Verify the canonical path has the act directory (depth-2 render).
        assert "Atto I" in str(canonical_1), "canonical path must contain the act directory (Atto I)"

        # --- Run 2: maintain must be a no-op ---
        journal_before_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        changed_2 = maintain(dest_root, yes=True)

        assert changed_2 == 0, f"run 2 must report no changes; got changed={changed_2}"
        journal_after_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        assert journal_after_run2 == journal_before_run2, "run 2 must append zero journal entries"


# ---------------------------------------------------------------------------
# KAT 3: Inverse-move tripwire (C-IDEM)
# ---------------------------------------------------------------------------


class TestInverseMoveTripwire:
    """KAT: the inverse-move tripwire warns when a planned move inverts a prior journal entry.

    The tripwire warns; it does not block (C-CONFLUENCE ergonomics register: no formal
    oscillation calculus).  These tests verify:

    1. When a pass plans a move ``(A → B)`` and the journal contains a prior move ``(B → A)``,
       the ``inverse_move_detected`` warning is emitted naming both passes.
    2. The move still executes (the tripwire does not block).
    3. When no inverse is present, no warning is emitted.
    4. The tripwire checks both the in-memory journal (current-run moves) and the on-disk
       journal (prior-run moves loaded at startup).
    """

    def test_tripwire_warns_on_inverse_move(self) -> None:
        """_warn_inverse_moves emits inverse_move_detected when a planned move inverts a journal entry.

        Sets up a journal with a prior ``"repathed"`` move ``(B → A)`` and a plan with the
        inverse move ``(A → B)``.  Asserts that ``inverse_move_detected`` is logged with the
        correct ``current_pass`` and ``prior_pass`` values.

        :returns: None.
        """
        path_a = Path("/lib/ArtistA/Work/01 - Track.flac")
        path_b = Path("/lib/ArtistB/Work/01 - Track.flac")

        # Journal: prior run moved B → A (action="repathed").
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="",
                    source=str(path_b),
                    destination=str(path_a),
                    action="repathed",
                )
            ]
        )

        # Plan: current pass wants to move A → B (the inverse of the journal entry).
        plan_pairs = [(path_a, path_b)]

        with structlog.testing.capture_logs() as cap_logs:
            _warn_inverse_moves(plan_pairs, "unify", journal)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert len(warning_events) == 1, f"expected exactly one inverse_move_detected warning; got {warning_events}"
        evt = warning_events[0]
        assert evt["current_pass"] == "unify"
        assert evt["prior_pass"] == "repathed"
        assert evt["old"] == str(path_a)
        assert evt["new"] == str(path_b)

    def test_tripwire_warns_for_regrouped_inverse(self) -> None:
        """_warn_inverse_moves detects inverse of a ``"regrouped"`` journal entry.

        :returns: None.
        """
        path_a = Path("/lib/Composer - Ensemble/Work/01 - Track.flac")
        path_b = Path("/lib/Composer - Ensemble/OtherWork/01 - Track.flac")

        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-1",
                    source=str(path_b),
                    destination=str(path_a),
                    action="regrouped",
                )
            ]
        )

        plan_pairs = [(path_a, path_b)]

        with structlog.testing.capture_logs() as cap_logs:
            _warn_inverse_moves(plan_pairs, "repath", journal)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert len(warning_events) == 1
        assert warning_events[0]["current_pass"] == "repath"
        assert warning_events[0]["prior_pass"] == "regrouped"

    def test_tripwire_warns_for_unified_inverse(self) -> None:
        """_warn_inverse_moves detects inverse of a ``"unified"`` journal entry.

        :returns: None.
        """
        path_a = Path("/lib/ArtistA/Work/01 - Track.flac")
        path_b = Path("/lib/ArtistB/Work/01 - Track.flac")

        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-1",
                    source=str(path_b),
                    destination=str(path_a),
                    action="unified",
                )
            ]
        )

        plan_pairs = [(path_a, path_b)]

        with structlog.testing.capture_logs() as cap_logs:
            _warn_inverse_moves(plan_pairs, "regroup", journal)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert len(warning_events) == 1
        assert warning_events[0]["current_pass"] == "regroup"
        assert warning_events[0]["prior_pass"] == "unified"

    def test_tripwire_no_warning_when_no_inverse(self) -> None:
        """_warn_inverse_moves emits no warning when no planned move inverts a journal entry.

        :returns: None.
        """
        path_a = Path("/lib/ArtistA/Work/01 - Track.flac")
        path_b = Path("/lib/ArtistB/Work/01 - Track.flac")
        path_c = Path("/lib/ArtistC/Work/01 - Track.flac")

        # Journal: prior run moved A → C (not the inverse of A → B).
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="",
                    source=str(path_a),
                    destination=str(path_c),
                    action="repathed",
                )
            ]
        )

        plan_pairs = [(path_a, path_b)]

        with structlog.testing.capture_logs() as cap_logs:
            _warn_inverse_moves(plan_pairs, "unify", journal)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert not warning_events, f"expected no warnings; got {warning_events}"

    def test_tripwire_no_warning_for_non_move_actions(self) -> None:
        """_warn_inverse_moves ignores non-move journal entries (tagged, enriched, etc.).

        :returns: None.
        """
        path_a = Path("/lib/ArtistA/Work/01 - Track.flac")
        path_b = Path("/lib/ArtistB/Work/01 - Track.flac")

        # Journal: a "tagged" entry at path_a (not a move-type action).
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="rel-1",
                    source="/src/01.flac",
                    destination=str(path_a),
                    action="tagged",
                )
            ]
        )

        # Plan: move A → B.  The journal has a "tagged" entry at A, not a move from B → A.
        plan_pairs = [(path_a, path_b)]

        with structlog.testing.capture_logs() as cap_logs:
            _warn_inverse_moves(plan_pairs, "unify", journal)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert not warning_events, f"expected no warnings for non-move journal entries; got {warning_events}"

    def test_tripwire_warns_multiple_inverses(self) -> None:
        """_warn_inverse_moves emits one warning per inverse move in the plan.

        :returns: None.
        """
        path_a = Path("/lib/ArtistA/Work/01 - Track.flac")
        path_b = Path("/lib/ArtistB/Work/01 - Track.flac")
        path_c = Path("/lib/ArtistC/Work/02 - Track.flac")
        path_d = Path("/lib/ArtistD/Work/02 - Track.flac")

        # Journal: two prior moves (B → A) and (D → C).
        journal = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="",
                    source=str(path_b),
                    destination=str(path_a),
                    action="repathed",
                ),
                TransactionEntry(
                    timestamp="2024-01-01T00:00:00+00:00",
                    release_id="",
                    source=str(path_d),
                    destination=str(path_c),
                    action="repathed",
                ),
            ]
        )

        # Plan: two inverse moves (A → B) and (C → D).
        plan_pairs = [(path_a, path_b), (path_c, path_d)]

        with structlog.testing.capture_logs() as cap_logs:
            _warn_inverse_moves(plan_pairs, "unify", journal)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert len(warning_events) == 2, f"expected 2 warnings; got {warning_events}"  # noqa: PLR2004

    def test_tripwire_does_not_block_move(self) -> None:
        """The tripwire warns but does not block: _warn_inverse_moves does not raise.

        Calls ``_warn_inverse_moves`` with an inverse plan and verifies that:
        (a) the warning is emitted, and
        (b) the function completes without raising (the move is not blocked).

        :returns: None.
        """
        path_a = Path("/lib/ArtistA/Work/01 - Track.flac")
        path_b = Path("/lib/ArtistB/Work/01 - Track.flac")

        # Journal: prior run moved B → A (action="unified").
        journal_obj = TransactionLog(
            entries=[
                TransactionEntry(
                    timestamp="2024-01-01T01:00:00+00:00",
                    release_id="rel-1",
                    source=str(path_b),
                    destination=str(path_a),
                    action="unified",
                )
            ]
        )

        # Plan: current pass wants to move A → B (the inverse).
        raised = False
        with structlog.testing.capture_logs() as cap_logs:
            try:
                _warn_inverse_moves([(path_a, path_b)], "repath", journal_obj)
            except Exception:  # noqa: BLE001
                raised = True

        assert not raised, "tripwire must not raise (it warns only; it does not block)"
        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert len(warning_events) == 1, "tripwire must emit exactly one warning for the inverse move"

    def test_tripwire_triggered_in_maintain_via_divergent_canonical(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """The tripwire fires during a maintain run when a pass plans an inverse of a prior move.

        Simulates a divergent canonical by patching ``build_dest_path`` to return a different
        destination on the second call for the same file.  The first ``maintain`` run moves the
        file from A to B (journalled as ``"unified"``).  The second run's ``repath`` pass plans
        to move the file from B back to A (the inverse).  The tripwire must emit
        ``inverse_move_detected`` before the move executes.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        release_id = "divergent-rel-1"
        albumartist = "Benny Goodman"

        tags = TrackTags(
            title="Sing Sing Sing",
            musicbrainz_albumid=release_id,
            albumartist=albumartist,
            artist=albumartist,
            cwp_movt_num="1",
            movementtotal="1",
            recording_date="1938",
        )

        # Canonical destination A (ALBUMARTIST-led top dir).
        canonical_a = _canonical_path(dest_root, tags)

        # Canonical destination B (a different path — simulating a divergent canonical).
        # We use a path that differs from canonical_a by the top dir.
        canonical_b = dest_root / "Goodman - Benny Goodman" / "Benny Goodman [1938]" / "01 - Sing Sing Sing.flac"

        # Place the file at canonical_a (the "correct" location after a prior run).
        _make_flac(dest_root, str(canonical_a.relative_to(dest_root)), tags)

        # Journal: a prior "unified" move from canonical_b to canonical_a.
        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": release_id,
                    "source": "/src/01.flac",
                    "destination": str(canonical_a),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T01:00:00+00:00",
                    "release_id": release_id,
                    "source": str(canonical_b),
                    "destination": str(canonical_a),
                    "action": "unified",
                },
            ],
        )

        # Patch build_dest_path (as imported in _pipeline_maint) to return canonical_b for this
        # file — simulating a divergent canonical that would move the file from A back to B.
        # This is the "deliberately-divergent stub canonical" that triggers the tripwire.
        original_build_dest_path = build_dest_path

        def _divergent_build_dest_path(
            dest_root_arg: Path,
            release: Any,
            track: Any,
            tags_arg: TrackTags,
            *,
            global_track_idx: int = 0,
            group_modal_depth: int | None = None,
        ) -> Path:
            """Return canonical_b for the test file; delegate all others to the real function.

            :param dest_root_arg: Library root.
            :param release: MBRelease stub.
            :param track: MBTrack stub.
            :param tags_arg: Tags to drive path construction.
            :param global_track_idx: Track index.
            :param group_modal_depth: Modal depth override.
            :returns: Divergent path for the test file; real path for all others.
            """
            if tags_arg.musicbrainz_albumid == release_id:
                # Return canonical_b (without suffix) — the divergent destination.
                return canonical_b.with_suffix("")
            return original_build_dest_path(
                dest_root_arg,
                release,
                track,
                tags_arg,
                global_track_idx=global_track_idx,
                group_modal_depth=group_modal_depth,
            )

        mocker.patch("music_annotator._pipeline_maint.build_dest_path", side_effect=_divergent_build_dest_path)

        # Run maintain: repath will plan to move canonical_a → canonical_b (the inverse of the
        # prior "unified" move canonical_b → canonical_a).  The tripwire must warn.
        with structlog.testing.capture_logs() as cap_logs:
            maintain(dest_root, yes=True)

        warning_events = [e for e in cap_logs if e.get("event") == "inverse_move_detected"]
        assert len(warning_events) >= 1, (
            f"tripwire must emit inverse_move_detected when a pass plans the inverse of a prior move; "
            f"got events: {[e.get('event') for e in cap_logs]}"
        )
        assert warning_events[0]["current_pass"] in {"repath", "regroup", "unify"}
        assert warning_events[0]["prior_pass"] == "unified"


# ---------------------------------------------------------------------------
# KAT 5: Completer chain shape (C-IDEM, C-CANON)
# ---------------------------------------------------------------------------


class TestCompleterChainIdempotence:
    """KAT: completer-chain shape consolidation is idempotent (C-IDEM, C-CANON).

    Covers the K.626 / Mozart; Süßmayr cycle shape: a classical release whose movements carry
    two distinct embedded composer chains (``Mozart`` and ``Mozart; Süßmayr``).  The old
    pass-local in-memory chain patch unified all movements under the fullest chain, causing
    ``repath`` and ``unify`` to disagree on the destination (a stable orbit).

    After the patch is deleted, each movement renders its raw embedded chain.  The two chains
    produce two distinct top dirs (the accepted fixpoint — not a regression).  The second
    ``maintain`` run must report "no changes" and append zero journal entries.

    Also covers the empty-composer non-classical flip: a jazz release whose tracks carry
    ``MUSICBRAINZ_WORKID`` and empty composer tags.  The old chain patch applied to these tracks
    via the ``musicbrainz_workid`` fallback, flipping their top dir from ALBUMARTIST-led to
    composer-led (a C-NC-TOP violation).  After the patch is deleted, the top dir stays
    ALBUMARTIST-led.  The second ``maintain`` run must report "no changes" and append zero
    journal entries.
    """

    @staticmethod
    def _make_requiem_tags(composer: str, movt_num: str, title: str) -> TrackTags:
        """Build TrackTags for a K.626 Requiem movement with the given embedded composer chain.

        :param composer: Embedded ``CWP_COMPOSER_LASTNAMES`` value (e.g. ``"Mozart"`` or
            ``"Mozart; Süßmayr"``).
        :param movt_num: Track number string.
        :param title: Track title.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        return TrackTags(
            cwp_composer_lastnames=composer,
            cea_composer_lastnames=composer,
            cwp_workid_top="work-k626-idem",
            cwp_work_top="Requiem K. 626",
            cwp_worktype_genres_top="Classical",
            cwp_movt_num=movt_num,
            movementtotal="2",
            cwp_part_levels="1",
            title=title,
            artist="Karajan",
            recording_date="1962",
            musicbrainz_albumid="completer-idem-rel-1",
        )

    @staticmethod
    def _make_jazz_tags(title: str, movt_num: str) -> TrackTags:
        """Build TrackTags for a jazz track with ``MUSICBRAINZ_WORKID`` and empty composer tags.

        :param title: Track title.
        :param movt_num: Track number string.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        return TrackTags(
            cwp_work_top="",
            cwp_worktype_genres_top="Jazz",
            musicbrainz_workid="work-jazz-idem-1",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            albumartist="Benny Goodman",
            album="Sing, Sing, Sing",
            releasetype="Album",
            cwp_movt_num=movt_num,
            movementtotal="2",
            cwp_part_levels="0",
            title=title,
            artist="Benny Goodman",
            recording_date="1937",
            musicbrainz_albumid="jazz-idem-rel-1",
        )

    def test_completer_chain_second_run_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Completer chain shape: second maintain run reports no changes after raw-chain consolidation.

        Movement 1 (``Mozart``) and movement 2 (``Mozart; Süßmayr``) start at the wrong location:
        both are placed under the ``Mozart`` top dir, simulating the pre-fix state where the old
        chain patch unified all movements under the fullest chain.  The first ``maintain`` run moves
        movement 2 to its raw-embedded-chain canonical path (``Mozart; Süßmayr`` top dir).  The
        second run must report "no changes" and append zero journal entries.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        tags_mvt1 = self._make_requiem_tags("Mozart", "1", "Introitus")
        tags_mvt2 = self._make_requiem_tags("Mozart; Süßmayr", "2", "Kyrie")

        # Compute the canonical (raw-embedded-chain) destinations.
        canonical_mvt1 = _canonical_path(dest_root, tags_mvt1)
        canonical_mvt2 = _canonical_path(dest_root, tags_mvt2)

        # The two chains must render distinct top dirs (the accepted fixpoint).
        top_dir_mvt1 = canonical_mvt1.relative_to(dest_root).parts[0]
        top_dir_mvt2 = canonical_mvt2.relative_to(dest_root).parts[0]
        assert top_dir_mvt1 != top_dir_mvt2, "test setup: the two embedded chains must render distinct top dirs"
        assert "Mozart" in top_dir_mvt1
        assert "Süßmayr" in top_dir_mvt2

        # Place movement 1 at its canonical path (already correct).
        _make_flac(dest_root, str(canonical_mvt1.relative_to(dest_root)), tags_mvt1)

        # Place movement 2 at the wrong path — under the Mozart top dir (simulating the pre-fix
        # state where the chain patch unified all movements under the fullest chain, then repath
        # moved the Süßmayr movement to the Mozart top dir).
        wrong_mvt2 = dest_root / top_dir_mvt1 / canonical_mvt2.parent.name / canonical_mvt2.name
        wrong_mvt2.parent.mkdir(parents=True, exist_ok=True)
        wrong_mvt2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_mvt2, tags_mvt2)

        assert wrong_mvt2 != canonical_mvt2, "test setup: wrong and canonical paths must differ"

        # Journal: both movements tagged at their initial (wrong) locations.
        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "completer-idem-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(canonical_mvt1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "completer-idem-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(wrong_mvt2),
                    "action": "tagged",
                },
            ],
        )

        # --- Run 1: maintain moves movement 2 to its raw-chain canonical path ---
        changed_1 = maintain(dest_root, yes=True)

        assert canonical_mvt1.exists(), "run 1: movement 1 must remain at its canonical path"
        assert canonical_mvt2.exists(), (
            f"run 1: movement 2 must be at its raw-chain canonical path {canonical_mvt2.relative_to(dest_root)}"
        )
        assert not wrong_mvt2.exists(), "run 1: movement 2 must be moved away from the wrong top dir"
        assert changed_1 > 0, "run 1 must report changes (raw-chain consolidation move)"

        # --- Run 2: maintain must be a no-op ---
        journal_before_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        changed_2 = maintain(dest_root, yes=True)

        assert changed_2 == 0, f"run 2 must report no changes; got changed={changed_2}"
        journal_after_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        assert journal_after_run2 == journal_before_run2, "run 2 must append zero journal entries"

    def test_empty_composer_nonclassical_flip_second_run_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Empty-composer non-classical flip: second maintain run reports no changes.

        A jazz release with ``MUSICBRAINZ_WORKID`` and empty composer tags starts at a
        composer-led path (simulating the pre-fix state where the old chain patch applied to
        empty-composer files via the ``musicbrainz_workid`` fallback, flipping their top dir
        from ALBUMARTIST-led to composer-led — a C-NC-TOP violation).  The first ``maintain``
        run moves the tracks to the ALBUMARTIST-led canonical path.  The second run must report
        "no changes" and append zero journal entries.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        tags_trk1 = self._make_jazz_tags("Sing, Sing, Sing (Part 1)", "1")
        tags_trk2 = self._make_jazz_tags("Sing, Sing, Sing (Part 2)", "2")

        # Compute the canonical (ALBUMARTIST-led) destinations.
        canonical_trk1 = _canonical_path(dest_root, tags_trk1)
        canonical_trk2 = _canonical_path(dest_root, tags_trk2)

        # Verify the canonical paths are ALBUMARTIST-led.
        top_dir = canonical_trk1.relative_to(dest_root).parts[0]
        assert top_dir.startswith("Benny Goodman"), (
            f"test setup: canonical path must be ALBUMARTIST-led; got top dir {top_dir!r}"
        )

        # Place tracks at a composer-led wrong path — simulating the pre-fix state where the
        # old chain patch applied to empty-composer files via the musicbrainz_workid fallback
        # and flipped the top dir from ALBUMARTIST-led to composer-led.
        wrong_top = dest_root / "Waller; Kander - Goodman" / "Sing, Sing, Sing [rec 1937]"
        wrong_trk1 = wrong_top / "01 - Sing, Sing, Sing (Part 1).flac"
        wrong_trk2 = wrong_top / "02 - Sing, Sing, Sing (Part 2).flac"
        wrong_top.mkdir(parents=True, exist_ok=True)
        wrong_trk1.write_bytes(_MINIMAL_FLAC)
        wrong_trk2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_trk1, tags_trk1)
        apply_tags_flac(wrong_trk2, tags_trk2)

        assert wrong_trk1 != canonical_trk1, "test setup: wrong and canonical paths must differ"
        assert wrong_trk2 != canonical_trk2, "test setup: wrong and canonical paths must differ"

        # Journal: both tracks tagged at the wrong (composer-led) paths.
        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "jazz-idem-rel-1",
                    "source": "/src/01.flac",
                    "destination": str(wrong_trk1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "jazz-idem-rel-1",
                    "source": "/src/02.flac",
                    "destination": str(wrong_trk2),
                    "action": "tagged",
                },
            ],
        )

        # --- Run 1: maintain moves tracks to ALBUMARTIST-led canonical paths ---
        changed_1 = maintain(dest_root, yes=True)

        assert canonical_trk1.exists(), (
            f"run 1: track 1 must be at ALBUMARTIST-led canonical path {canonical_trk1.relative_to(dest_root)}"
        )
        assert canonical_trk2.exists(), (
            f"run 1: track 2 must be at ALBUMARTIST-led canonical path {canonical_trk2.relative_to(dest_root)}"
        )
        assert not wrong_trk1.exists(), "run 1: track 1 must be moved away from the composer-led wrong path"
        assert not wrong_trk2.exists(), "run 1: track 2 must be moved away from the composer-led wrong path"
        assert changed_1 > 0, "run 1 must report changes (ALBUMARTIST-led consolidation moves)"

        # --- Run 2: maintain must be a no-op ---
        journal_before_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        changed_2 = maintain(dest_root, yes=True)

        assert changed_2 == 0, f"run 2 must report no changes; got changed={changed_2}"
        journal_after_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        assert journal_after_run2 == journal_before_run2, "run 2 must append zero journal entries"


# ---------------------------------------------------------------------------
# KAT 6: Depth-membership shape (C-IDEM, C-GROUPSCOPE)
# ---------------------------------------------------------------------------


class TestDepthMembershipIdempotence:
    """KAT: depth-membership shape consolidation is idempotent (C-IDEM, C-GROUPSCOPE).

    Covers the Saint-Saëns op. 78 / La traviata / Guglielmo Tell / Walküre depth-membership
    cycle shape: a library fixture with two recordings of the same top work with different
    ``cwp_part_levels`` distributions, one of them a fragmented release.

    The old ``unify`` computed modal depth over the fragmented release's files only
    (release-local membership), while ``repath`` computed over the full library (library-wide
    membership).  The same statistic over different denominators produced a stable orbit: repath
    collapsed a work subdir, unify re-inserted it, every run.

    After the fix, all passes use the library-wide modal depth map (C-GROUPSCOPE).  The second
    ``maintain`` run must report "no changes" and append zero journal entries.
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
            cwp_workid_top="w-traviata-idem",
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

    def test_depth_membership_second_run_no_changes(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Depth-membership shape: second maintain run reports no changes after depth consolidation.

        Library fixture:
        - Release A (fragmented, PL=3): two tracks spread across two top_dirs.
        - Release B (non-fragmented, PL=2): three tracks at canonical paths.

        Both releases share the same ``CWP_WORKID_TOP``.  The library-wide modal depth is 2
        (three PL=2 tracks vs two PL=3 tracks).  Release A's tracks start at the release-local
        depth-3 paths (simulating the pre-fix state where ``unify`` computed release-local modal
        depth = 3 and moved them there).  The first ``maintain`` run moves release A's tracks to
        the library-wide depth-2 canonical paths.  The second run must report "no changes" and
        append zero journal entries.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        _mock_content_passes(mocker)
        _mock_integrity_passes(mocker)

        # Release A (fragmented): 2 tracks with PL=3.
        tags_a1 = self._make_opera_tags("3", "1", "Atto I - Scena 1", "traviata-idem-frag", "Atto I", "1")
        tags_a2 = self._make_opera_tags("3", "2", "Atto II - Scena 1", "traviata-idem-frag", "Atto II", "2")

        # Release B (non-fragmented): 3 tracks with PL=2 sharing the same CWP_WORKID_TOP.
        # Their majority (3 vs 2) drives the full-library modal depth to 2.
        tags_b1 = self._make_opera_tags("2", "1", "Atto I - Aria", "traviata-idem-other", "Atto I", "1")
        tags_b2 = self._make_opera_tags("2", "2", "Atto II - Duet", "traviata-idem-other", "Atto II", "2")
        tags_b3 = self._make_opera_tags("2", "3", "Atto III - Finale", "traviata-idem-other", "Atto III", "3")

        # Verify the membership-divergence shape:
        # - Release-local modal (only release A's PL=3 tracks): modal=3.
        # - Library-wide modal (all 5 tracks: PL=3, PL=3, PL=2, PL=2, PL=2): modal=2.
        release_local_modal = work_group_modal_depth([3, 3])
        assert release_local_modal == 3  # noqa: PLR2004 — 3 is the release-local modal depth

        lib_wide_modal = work_group_modal_depth([3, 3, 2, 2, 2])
        assert lib_wide_modal == 2  # noqa: PLR2004 — 2 is the library-wide modal depth

        # Canonical destinations using library-wide modal depth (what maintain now computes).
        canonical_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_a2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a2, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")

        # Release-local destinations (what unify would compute WITHOUT the shared map — depth-3).
        release_local_a1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a1, group_modal_depth=release_local_modal
        ).with_suffix(".flac")
        release_local_a2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_a2, group_modal_depth=release_local_modal
        ).with_suffix(".flac")

        # The library-wide and release-local destinations must differ (non-trivial test).
        assert canonical_a1 != release_local_a1, (
            "test setup: library-wide and release-local destinations must differ for PL=3 tracks"
        )

        # Place release A track 1 at a wrong top_dir (fragmented: different performer in path).
        # This simulates the pre-fix state where unify moved it to the release-local depth-3 path
        # under a different top_dir (fragmentation detected because tracks are in different top_dirs).
        wrong_top_a1 = dest_root / "Verdi - Serafin" / "La traviata [rec 1955]" / "01 - Atto I - Scena 1.flac"
        wrong_top_a1.parent.mkdir(parents=True, exist_ok=True)
        wrong_top_a1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(wrong_top_a1, tags_a1)

        # Place release A track 2 at the release-local depth-3 canonical path.
        # This ensures two distinct top_dirs for fragmentation detection in unify.
        _make_flac(dest_root, str(release_local_a2.relative_to(dest_root)), tags_a2)

        # Place release B tracks at their library-wide canonical paths.
        canonical_b1 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_b1, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_b2 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_b2, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        canonical_b3 = build_dest_path(
            dest_root, MBRelease(), MBTrack(), tags_b3, group_modal_depth=lib_wide_modal
        ).with_suffix(".flac")
        _make_flac(dest_root, str(canonical_b1.relative_to(dest_root)), tags_b1)
        _make_flac(dest_root, str(canonical_b2.relative_to(dest_root)), tags_b2)
        _make_flac(dest_root, str(canonical_b3.relative_to(dest_root)), tags_b3)

        # Journal: all tracks at their initial locations.
        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-idem-frag",
                    "source": "/src/a1.flac",
                    "destination": str(wrong_top_a1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-idem-frag",
                    "source": "/src/a2.flac",
                    "destination": str(release_local_a2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-idem-other",
                    "source": "/src/b1.flac",
                    "destination": str(canonical_b1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-idem-other",
                    "source": "/src/b2.flac",
                    "destination": str(canonical_b2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "traviata-idem-other",
                    "source": "/src/b3.flac",
                    "destination": str(canonical_b3),
                    "action": "tagged",
                },
            ],
        )

        # --- Run 1: maintain moves release A tracks to library-wide depth-2 canonical paths ---
        changed_1 = maintain(dest_root, yes=True)

        assert canonical_a1.exists(), (
            f"run 1: track A1 must be at library-wide canonical path {canonical_a1.relative_to(dest_root)}"
        )
        assert canonical_a2.exists(), (
            f"run 1: track A2 must be at library-wide canonical path {canonical_a2.relative_to(dest_root)}"
        )
        assert not wrong_top_a1.exists(), "run 1: track A1 must be moved away from the wrong top_dir"
        assert changed_1 > 0, "run 1 must report changes (depth-membership consolidation moves)"

        # --- Run 2: maintain must be a no-op ---
        journal_before_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        changed_2 = maintain(dest_root, yes=True)

        assert changed_2 == 0, f"run 2 must report no changes; got changed={changed_2}"
        journal_after_run2 = (dest_root / "music_annotator_journal.json").read_text(encoding="utf-8")
        assert journal_after_run2 == journal_before_run2, "run 2 must append zero journal entries"
