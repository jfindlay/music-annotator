"""Unit tests for _audit functions: audit, diff_journal, detect_fragmented_releases,
and the identity-integrity audit passes.

Migrated from test_main.py (TestAudit, TestAuditIdentityPasses, TestReadAlbumidTag,
TestAuditConfirmsViaTag) and test_pipeline.py (TestDiffJournal, TestDetectFragmentedReleases).
"""

# pylint: disable=duplicate-code  # test setup patterns are intentionally similar across test modules

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    JOURNAL_FILENAME,
    apply_tags_flac,
    apply_tags_mp3,
)
from music_annotator.__main__ import _build_parser, main
from music_annotator._audit import (
    JournalDiffResult,
    _audit_audio_anchor,
    _audit_journal_scan,
    _audit_tag_adjudication,
    _audit_tier_pass,
    _make_audit_counts,
    detect_fragmented_releases,
    diff_journal,
)
from music_annotator._pipeline_io import (
    PROVENANCE_FILENAME,
    _audio_hash,
    _read_albumid_tag,
    _write_provenance_fields,
)
from music_annotator.models import AnnotationTier, ProvenanceSidecar, TrackTags, TransactionEntry
from tests.conftest import _MINIMAL_FLAC, _MINIMAL_MP3

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _write_library_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a JSONL journal file to ``dest_root / music_annotator_journal.json``.

    Writes one JSON object per line (JSONL format) so the file is in the format that
    :func:`~music_annotator.read_journal` expects without triggering a migration.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / "music_annotator_journal.json"
    journal_path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


class TestAudit:
    """Tests for :func:`music_annotator.audit` — the read-only journal fragmentation detector.

    All tests use pyfakefs to provide a fake filesystem and patch the structlog ``log`` object in
    ``music_annotator._pipeline_io`` to assert on logged events without relying on log capture
    infrastructure.  No audio files, no network, no journal writes are involved.
    """

    def test_audit_reports_mixed_mbid_and_split_release(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() reports both case (a) and case (b) fragmentation when both are present.

        Case (a): work_dir ``"Work-A [2020]"`` has entries with two distinct release_ids
        (``"rel-1"`` and ``"rel-2"``) — a regrouping candidate.

        Case (b): release_id ``"rel-3"`` has entries in two distinct work_dirs
        (``"Work-B [2020]"`` and ``"Work-C [2020]"``) — a split release.

        Asserts that ``log.warning`` is called with ``audit_multiple_release_ids`` for case (a)
        and ``audit_split_release`` for case (b).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create destination FLAC files so the new identity passes find no issues.
        dest_paths = [
            "/lib/Beethoven - Karajan/Work-A [2020]/01 - Mvt1.flac",
            "/lib/Beethoven - Karajan/Work-A [2020]/02 - Mvt2.flac",
            "/lib/Beethoven - Karajan/Work-B [2020]/01 - Mvt1.flac",
            "/lib/Beethoven - Karajan/Work-C [2020]/01 - Mvt1.flac",
        ]
        for dp in dest_paths:
            p = Path(dp)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_MINIMAL_FLAC)

        # case (a): same work_dir, two different release_ids
        # case (b): same release_id, two different work_dirs
        _write_library_journal(
            dest_root,
            [
                # case (a) entry 1: Work-A, release rel-1
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-A [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
                # case (a) entry 2: Work-A, release rel-2 — triggers case (a)
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-A [2020]/02 - Mvt2.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                },
                # case (b) entry 1: rel-3 in Work-B
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-3",
                    "source": "/src/03.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-B [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                },
                # case (b) entry 2: rel-3 in Work-C — triggers case (b)
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-3",
                    "source": "/src/04.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-C [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        warning_events = [call.args[0] for call in mock_log.warning.call_args_list]
        assert "audit_multiple_release_ids" in warning_events
        assert "audit_split_release" in warning_events

        # Verify case (a) kwargs: work_dir and release_ids present
        case_a_calls = [c for c in mock_log.warning.call_args_list if c.args[0] == "audit_multiple_release_ids"]
        assert len(case_a_calls) == 1
        assert case_a_calls[0].kwargs["work_dir"] == "Work-A [2020]"
        assert case_a_calls[0].kwargs["release_ids"] == ["rel-1", "rel-2"]

        # Verify case (b) kwargs: release_id and work_dirs present
        case_b_calls = [c for c in mock_log.warning.call_args_list if c.args[0] == "audit_split_release"]
        assert len(case_b_calls) == 1
        assert case_b_calls[0].kwargs["release_id"] == "rel-3"
        assert case_b_calls[0].kwargs["work_dirs"] == ["Work-B [2020]", "Work-C [2020]"]

        # audit() must not have called log.info for fragmentation events (fragmentation was present);
        # only audit_summary is expected as an info event.
        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_multiple_release_ids" not in info_events
        assert "audit_split_release" not in info_events
        assert "audit_summary" in info_events

    def test_audit_clean_no_fragmentation(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() logs a clean message and does not warn when no fragmentation is detected.

        All entries share the same release_id and map to the same work_dir, so neither
        case (a) nor case (b) fires.  A provenance sidecar with ``full-mb-verified`` is written
        so the tier pass does not log ``audit_tier_unset`` warnings.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create destination FLAC files so the new identity passes find no issues.
        work_top_dir = Path("/lib/Beethoven - Karajan/Symphony No 5 [2020]")
        for dp in [
            str(work_top_dir / "01 - Mvt1.flac"),
            str(work_top_dir / "02 - Mvt2.flac"),
        ]:
            p = Path(dp)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_MINIMAL_FLAC)

        # Write a provenance sidecar so the tier pass does not log audit_tier_unset.
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(work_top_dir / "01 - Mvt1.flac"),
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/02.flac",
                    "destination": str(work_top_dir / "02 - Mvt2.flac"),
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    def test_audit_skips_malformed_destination_not_under_dest_root(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() skips entries whose destination is not under dest_root without crashing.

        A destination outside ``dest_root`` (e.g. ``/other/Work-X/01.flac`` when dest_root is
        ``/lib``) raises ``ValueError`` in ``Path.relative_to``.  The entry must be skipped and
        the audit must complete as though the entry were absent.  A provenance sidecar is written
        for the valid work_dir so the tier pass does not log ``audit_tier_unset`` warnings.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create both destination FLAC files so the new identity passes find no issues.
        # The "malformed" entry is outside dest_root but Pass 2 still checks it on disk.
        foreign_dest = Path("/other/Work-X/01.flac")
        foreign_dest.parent.mkdir(parents=True, exist_ok=True)
        foreign_dest.write_bytes(_MINIMAL_FLAC)
        valid_work_top_dir = Path("/lib/Beethoven - Karajan/Work-A [2020]")
        valid_dest = valid_work_top_dir / "01 - Mvt1.flac"
        valid_dest.parent.mkdir(parents=True, exist_ok=True)
        valid_dest.write_bytes(_MINIMAL_FLAC)

        # Write a provenance sidecar for the valid work_dir so the tier pass does not warn.
        _write_provenance_fields(
            valid_work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        _write_library_journal(
            dest_root,
            [
                # Malformed: destination is not under /lib
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-foreign",
                    "source": "/src/01.flac",
                    "destination": "/other/Work-X/01.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
                # Valid entry: should still be processed
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/02.flac",
                    "destination": str(valid_dest),
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        # Only the valid entry was processed; no fragmentation → clean log
        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    def test_audit_skips_entry_with_too_few_path_parts(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() skips entries whose relative destination has fewer than two path parts.

        An entry whose destination is directly inside ``dest_root`` (e.g. ``/lib/track.flac``)
        has only one relative part and cannot yield a ``work_dir`` component.  The entry must
        be skipped silently.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create the shallow destination FLAC file so the new identity passes find no issues.
        shallow_dest = Path("/lib/track.flac")
        shallow_dest.write_bytes(_MINIMAL_FLAC)

        _write_library_journal(
            dest_root,
            [
                # Only one relative part: /lib/track.flac → relative = track.flac (parts[0] only)
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-shallow",
                    "source": "/src/01.flac",
                    "destination": "/lib/track.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        # The shallow entry was skipped; no tagged entries qualify → clean log
        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    def test_audit_ignores_non_tagged_actions(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() only considers ``action == "tagged"`` entries; other actions are ignored.

        Entries with actions ``"skipped"``, ``"dry_run"``, ``"repathed"`` etc. must not
        contribute to the fragmentation groupings.  A provenance sidecar is written for the
        tagged work_dir so the tier pass does not log ``audit_tier_unset`` warnings.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create the tagged destination FLAC file so the new identity passes find no issues.
        tagged_work_top_dir = Path("/lib/Beethoven - Karajan/Work-C [2020]")
        tagged_dest = tagged_work_top_dir / "01 - Mvt1.flac"
        tagged_dest.parent.mkdir(parents=True, exist_ok=True)
        tagged_dest.write_bytes(_MINIMAL_FLAC)

        # Write a provenance sidecar for the tagged work_dir so the tier pass does not warn.
        _write_provenance_fields(
            tagged_work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        _write_library_journal(
            dest_root,
            [
                # Non-tagged entries with different release_ids and work_dirs: must be ignored
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-A",
                    "source": "/src/01.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-A [2020]/01 - Mvt1.flac",
                    "action": "skipped",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-B",
                    "source": "/src/02.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-B [2020]/01 - Mvt1.flac",
                    "action": "dry_run",
                },
                # One tagged entry: no fragmentation
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/03.flac",
                    "destination": str(tagged_dest),
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    # pylint: disable-next=unused-argument
    def test_audit_dispatches_from_main(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() audit subcommand dispatches to music_annotator.audit with dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mock_audit = mocker.patch("music_annotator.audit")
        mocker.patch.object(sys, "argv", new=["music-annotator", "audit", "/d"])
        main()
        mock_audit.assert_called_once_with(dest_root=Path("/d"))

    # pylint: disable-next=unused-argument
    def test_audit_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() audit exits with code 1 when audit() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.audit", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=["music-annotator", "audit", "/d"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_audit_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() audit exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.audit", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=["music-annotator", "audit", "/d"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_audit_parser_parses_dest_dir(self) -> None:
        """audit parser accepts dest_dir as a positional argument.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["audit", "/dest"])
        assert ns.subcommand == "audit"
        assert ns.dest_dir == Path("/dest")

    def test_audit_is_read_only_no_extra_flags(self) -> None:
        """audit parser accepts only dest_dir; mutating flags are no longer present.

        Verifies that ``--enrich``, ``--diff``, and ``--origin-time`` are not recognised by the
        ``audit`` subcommand (they are now top-level subcommands).
        """
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["audit", "/dest", "--enrich"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# TestEnrichDispatch — enrich top-level subcommand
# ---------------------------------------------------------------------------


class TestEnrichDispatch:
    """Tests for the ``enrich`` top-level subcommand dispatch in :func:`main`."""

    def _patch_common(self, mocker: MockerFixture) -> None:
        """Patch logging and structlog so tests don't reconfigure the process logger.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")

    _ENRICH_ARGV = ["music-annotator", "enrich", "/d"]

    # pylint: disable-next=unused-argument
    def test_enrich_dispatches_to_enrich(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich subcommand dispatches to music_annotator.enrich with dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        mocker.patch.object(sys, "argv", new=self._ENRICH_ARGV)
        main()
        mock_enrich.assert_called_once_with(
            dest_root=Path("/d"),
            re_resolve=False,
            dry_run=False,
            acoustid_key="",
        )

    # pylint: disable-next=unused-argument
    def test_enrich_acoustid_key_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() enrich --acoustid-key passes acoustid_key to enrich().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        mocker.patch.object(sys, "argv", new=[*self._ENRICH_ARGV, "--acoustid-key", "MY_KEY"])
        main()
        _, kwargs = mock_enrich.call_args
        assert kwargs["acoustid_key"] == "MY_KEY"

    # pylint: disable-next=unused-argument
    def test_enrich_re_resolve_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
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
    def test_enrich_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
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
    def test_enrich_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
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
    def test_enrich_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
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

    def test_enrich_parser_parses_dest_dir(self) -> None:
        """enrich parser accepts dest_dir as a positional argument and defaults to no flags.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["enrich", "/dest"])
        assert ns.subcommand == "enrich"
        assert ns.dest_dir == Path("/dest")
        assert not ns.dry_run
        assert not ns.re_resolve
        assert ns.acoustid_key == ""

    def test_enrich_acoustid_key_accepted_by_parser(self) -> None:
        """enrich --acoustid-key is accepted and stored on the namespace.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["enrich", "/dest", "--acoustid-key", "MY_KEY"])
        assert ns.acoustid_key == "MY_KEY"


# ---------------------------------------------------------------------------
# TestDiffDispatch — diff top-level subcommand
# ---------------------------------------------------------------------------


class TestDiffDispatch:
    """Tests for the ``diff`` top-level subcommand dispatch in :func:`main`."""

    def _patch_common(self, mocker: MockerFixture) -> None:
        """Patch logging and structlog so tests don't reconfigure the process logger.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")

    _DIFF_ARGV = ["music-annotator", "diff", "/d"]

    # pylint: disable-next=unused-argument
    def test_diff_dispatches_to_diff_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() diff subcommand dispatches to music_annotator.diff_journal with dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_diff = mocker.patch("music_annotator.diff_journal")
        mocker.patch.object(sys, "argv", new=self._DIFF_ARGV)
        main()
        mock_diff.assert_called_once_with(dest_root=Path("/d"))

    # pylint: disable-next=unused-argument
    def test_diff_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() diff exits with code 1 when diff_journal() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.diff_journal", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._DIFF_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_diff_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() diff exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.diff_journal", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._DIFF_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_diff_parser_parses_dest_dir(self) -> None:
        """diff parser accepts dest_dir as a positional argument.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["diff", "/dest"])
        assert ns.subcommand == "diff"
        assert ns.dest_dir == Path("/dest")


# ---------------------------------------------------------------------------
# TestOriginTimeDispatch — origin-time top-level subcommand
# ---------------------------------------------------------------------------


class TestOriginTimeDispatch:
    """Tests for the ``origin-time`` top-level subcommand dispatch in :func:`main`."""

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
        """main() origin-time subcommand dispatches to music_annotator.enrich_origin_time with dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_ot = mocker.patch("music_annotator.enrich_origin_time")
        mocker.patch.object(sys, "argv", new=self._ORIGIN_TIME_ARGV)
        main()
        mock_ot.assert_called_once_with(dest_root=Path("/d"), dry_run=False)

    # pylint: disable-next=unused-argument
    def test_origin_time_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() origin-time --dry-run passes dry_run=True to enrich_origin_time().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_ot = mocker.patch("music_annotator.enrich_origin_time")
        mocker.patch.object(sys, "argv", new=[*self._ORIGIN_TIME_ARGV, "--dry-run"])
        main()
        _, kwargs = mock_ot.call_args
        assert kwargs["dry_run"] is True

    # pylint: disable-next=unused-argument
    def test_origin_time_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() origin-time exits with code 1 when enrich_origin_time() raises an unexpected exception.

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

    def test_origin_time_parser_parses_dest_dir(self) -> None:
        """origin-time parser accepts dest_dir as a positional argument and defaults dry_run=False.

        Pure parser test — no mocker needed.
        """
        parser = _build_parser()
        ns = parser.parse_args(["origin-time", "/dest"])
        assert ns.subcommand == "origin-time"
        assert ns.dest_dir == Path("/dest")
        assert not ns.dry_run


# ---------------------------------------------------------------------------
# TestAuditIdentityPasses — F7 identity-integrity passes
# ---------------------------------------------------------------------------


class TestAuditIdentityPasses:
    """Tests for the three identity-integrity passes added in F7.

    Covers :func:`_audit_journal_scan` (Pass 1), :func:`_audit_tag_adjudication` (Pass 2),
    :func:`_audit_audio_anchor` (Pass 3), and the full :func:`audit` integration with all
    three passes active.

    All tests use pyfakefs for filesystem isolation and patch the structlog ``log`` object in
    ``music_annotator._pipeline_io`` to assert on logged events.
    """

    # ------------------------------------------------------------------
    # KAT: test_audit_flags_wrong_acoustid_keeps_audio_anchor
    # ------------------------------------------------------------------

    def test_audit_flags_wrong_acoustid_keeps_audio_anchor(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() flags an acoustid mismatch between journal and tag but confirms the audio anchor.

        Scenario:
        - A FLAC file has ``audio_hash`` tag matching the recomputed hash (anchor is stable).
        - The file's ``ACOUSTID_ID`` tag is ``"tag-acoustid-id"``.
        - The journal entry has ``audio_hash`` = same correct value, ``acoustid_id`` =
          ``"journal-acoustid-id"`` (different from the tag — simulating a stale journal).

        Asserts:
        - ``audit_acoustid_journal_mismatch`` is logged (tag and journal acoustid_id differ).
        - ``audit_audio_drift`` is NOT logged (audio is stable — anchor matches).
        - ``audit_audio_stable`` is logged (anchor confirmed).
        - ``audit_summary`` is logged with correct counts.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create a FLAC file and embed the correct audio_hash + a tag acoustid_id.
        dest_path = dest_root / "Composer - Performer" / "Work [2020]" / "01 - Track.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        correct_hash = _audio_hash(dest_path)
        apply_tags_flac(dest_path, TrackTags(audio_hash=correct_hash, acoustid_id="tag-acoustid-id"))

        # Journal entry: audio_hash matches the file's tag, but acoustid_id differs.
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(dest_path),
                    "action": "tagged",
                    "audio_hash": correct_hash,
                    "acoustid_id": "journal-acoustid-id",
                }
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        debug_events = [c.args[0] for c in mock_log.debug.call_args_list]
        info_events = [c.args[0] for c in mock_log.info.call_args_list]

        # acoustid mismatch must be flagged
        assert "audit_acoustid_journal_mismatch" in warning_events

        # audio anchor must be confirmed (no drift)
        assert "audit_audio_drift" not in warning_events
        assert "audit_audio_stable" in debug_events

        # summary must be present
        assert "audit_summary" in info_events
        summary_call = next(c for c in mock_log.info.call_args_list if c.args[0] == "audit_summary")
        assert summary_call.kwargs["acoustid_journal_mismatch"] == 1
        assert summary_call.kwargs["audio_drift"] == 0
        assert summary_call.kwargs["audio_stable"] == 1

    # ------------------------------------------------------------------
    # Pass 1 (_audit_journal_scan) branch coverage
    # ------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_pass1_empty_audio_hash_logs_needs_enrich(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 1 logs audit_needs_enrich when audio_hash is empty in the journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/01.flac",
                action="tagged",
                audio_hash="",
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_journal_scan(entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_needs_enrich" in info_events
        assert counts["needs_enrich"] == 1
        assert counts["total"] == 1

    # pylint: disable-next=unused-argument
    def test_pass1_empty_acoustid_logs_acoustid_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 1 logs audit_acoustid_missing when acoustid_id is empty in the journal entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/01.flac",
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_journal_scan(entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_acoustid_missing" in info_events
        assert counts["acoustid_missing"] == 1
        assert counts["total"] == 1

    # pylint: disable-next=unused-argument
    def test_pass1_both_non_empty_no_findings(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 1 emits no findings when both audio_hash and acoustid_id are non-empty.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/01.flac",
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_journal_scan(entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_needs_enrich" not in info_events
        assert "audit_acoustid_missing" not in info_events
        assert counts["needs_enrich"] == 0
        assert counts["acoustid_missing"] == 0
        assert counts["total"] == 1

    # pylint: disable-next=unused-argument
    def test_pass1_only_tagged_and_enriched_scanned(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 1 only scans entries with action 'tagged' or 'enriched'; others are skipped.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/01.flac",
                action="repathed",
                audio_hash="",
                acoustid_id="",
            ),
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/02.flac",
                destination="/lib/Work/02.flac",
                action="enriched",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            ),
        ]
        mocker.patch("music_annotator._audit.log")
        _audit_journal_scan(entries, counts)

        # Only the "enriched" entry is scanned; "repathed" is ignored.
        assert counts["total"] == 1
        assert counts["needs_enrich"] == 0
        assert counts["acoustid_missing"] == 0

    # pylint: disable-next=unused-argument
    def test_pass1_duplicate_destination_counted_once(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 1 counts each unique destination only once (first occurrence wins).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/01.flac",
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
            # Same destination — must be skipped (seen set)
            TransactionEntry(
                timestamp="2024-01-01T01:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/01.flac",
                action="enriched",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            ),
        ]
        mocker.patch("music_annotator._audit.log")
        _audit_journal_scan(entries, counts)

        assert counts["total"] == 1

    # ------------------------------------------------------------------
    # Pass 2 (_audit_tag_adjudication) branch coverage
    # ------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_pass2_file_missing_logs_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 2 logs audit_file_missing when the destination file does not exist on disk.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/missing.flac",
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tag_adjudication(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_file_missing" in warning_events
        assert counts["file_missing"] == 1

    def test_pass2_acoustid_journal_mismatch_logged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 2 logs audit_acoustid_journal_mismatch when journal and tag acoustid_id differ.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_path, TrackTags(acoustid_id="tag-acoustid", audio_hash="flac-md5:aabb"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="journal-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tag_adjudication(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_acoustid_journal_mismatch" in warning_events
        assert counts["acoustid_journal_mismatch"] == 1

    def test_pass2_audio_hash_tag_mismatch_logged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 2 logs audit_audio_hash_tag_mismatch when journal and tag audio_hash differ.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_path, TrackTags(audio_hash="flac-md5:tag-hash", acoustid_id="same-acoustid"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:journal-hash",
                acoustid_id="same-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tag_adjudication(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_audio_hash_tag_mismatch" in warning_events
        assert counts["audio_hash_tag_mismatch"] == 1

    def test_pass2_empty_journal_acoustid_no_mismatch(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 2 does not log a mismatch when journal acoustid_id is empty (can't compare).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_path, TrackTags(acoustid_id="tag-acoustid", audio_hash="flac-md5:aabb"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="",  # empty journal acoustid → no comparison
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tag_adjudication(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_acoustid_journal_mismatch" not in warning_events
        assert counts["acoustid_journal_mismatch"] == 0

    def test_pass2_empty_tag_acoustid_no_mismatch(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 2 does not log a mismatch when tag acoustid_id is empty (can't compare).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        # File has no acoustid_id tag
        apply_tags_flac(dest_path, TrackTags(audio_hash="flac-md5:aabb"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="journal-acoustid",  # journal has value but tag is empty
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tag_adjudication(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_acoustid_journal_mismatch" not in warning_events
        assert counts["acoustid_journal_mismatch"] == 0

    def test_pass2_all_match_no_findings(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 2 emits no findings when journal and tag values all match.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_path, TrackTags(acoustid_id="same-acoustid", audio_hash="flac-md5:same-hash"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:same-hash",
                acoustid_id="same-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tag_adjudication(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_acoustid_journal_mismatch" not in warning_events
        assert "audit_audio_hash_tag_mismatch" not in warning_events
        assert "audit_file_missing" not in warning_events

    # ------------------------------------------------------------------
    # Pass 3 (_audit_audio_anchor) branch coverage
    # ------------------------------------------------------------------

    def test_pass3_stored_hash_empty_logs_needs_enrich_tag_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 3 logs audit_needs_enrich_tag_empty (debug) when stored audio_hash tag is empty.

        This branch fires when both the journal and the tag are empty (journal-empty case was
        already counted in pass 1; pass 3 logs at debug level only).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        # No audio_hash tag written
        apply_tags_flac(dest_path, TrackTags(title="No Hash"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="",  # journal also empty
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_audio_anchor(entries, counts)

        debug_events = [c.args[0] for c in mock_log.debug.call_args_list]
        assert "audit_needs_enrich_tag_empty" in debug_events

    def test_pass3_audio_drift_logged_when_hash_differs(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 3 logs audit_audio_drift when recomputed hash differs from the stored tag.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        # Store a deliberately wrong audio_hash in the tag
        apply_tags_flac(dest_path, TrackTags(audio_hash="flac-md5:wronghash00000000000000000000000"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:wronghash00000000000000000000000",
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_audio_anchor(entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_audio_drift" in warning_events
        assert counts["audio_drift"] == 1

    def test_pass3_audio_stable_logged_when_hash_matches(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 3 logs audit_audio_stable (debug) when recomputed hash matches the stored tag.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        correct_hash = _audio_hash(dest_path)
        apply_tags_flac(dest_path, TrackTags(audio_hash=correct_hash))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash=correct_hash,
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_audio_anchor(entries, counts)

        debug_events = [c.args[0] for c in mock_log.debug.call_args_list]
        assert "audit_audio_stable" in debug_events
        assert counts["audio_stable"] == 1

    # pylint: disable-next=unused-argument
    def test_pass3_file_missing_skipped_silently(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 3 silently skips entries whose destination file does not exist (already counted in pass 2).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination="/lib/Work/missing.flac",
                action="tagged",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            )
        ]
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_audio_anchor(entries, counts)

        # No warning logged (file_missing is pass 2's responsibility)
        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_file_missing" not in warning_events
        assert counts["audio_drift"] == 0
        assert counts["audio_stable"] == 0

    def test_pass3_unsupported_format_skipped_silently(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Pass 3 silently skips entries when _audio_hash returns empty (unsupported format).

        When the file exists but _audio_hash cannot compute a hash (e.g. unsupported format or
        read error), the entry is skipped without logging any event.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        dest_path = dest_root / "Work" / "01.flac"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_path, TrackTags(audio_hash="flac-md5:some-stored-hash"))

        counts = _make_audit_counts()
        entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="r1",
                source="/src/01.flac",
                destination=str(dest_path),
                action="tagged",
                audio_hash="flac-md5:some-stored-hash",
                acoustid_id="some-acoustid",
            )
        ]
        # Patch _audio_hash to return "" (simulating unsupported format / read error)
        mocker.patch("music_annotator._audit._audio_hash", return_value="")
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_audio_anchor(entries, counts)

        # No events logged — silently skipped
        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        debug_events = [c.args[0] for c in mock_log.debug.call_args_list]
        assert "audit_audio_drift" not in warning_events
        assert "audit_audio_stable" not in debug_events
        assert counts["audio_drift"] == 0
        assert counts["audio_stable"] == 0

    # ------------------------------------------------------------------
    # audit_summary counts
    # ------------------------------------------------------------------

    def test_audit_summary_logged_with_correct_counts(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() logs audit_summary with correct counts after all three passes.

        Scenario: one entry with empty audio_hash (needs_enrich) and empty acoustid_id
        (acoustid_missing), and the file does not exist on disk (file_missing).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Entry with empty identity fields and no file on disk
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/Composer - Performer/Work [2020]/01.flac",
                    "action": "tagged",
                    "audio_hash": "",
                    "acoustid_id": "",
                }
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_summary" in info_events
        summary_call = next(c for c in mock_log.info.call_args_list if c.args[0] == "audit_summary")
        assert summary_call.kwargs["total_scanned"] == 1
        assert summary_call.kwargs["needs_enrich"] == 1
        assert summary_call.kwargs["acoustid_missing"] == 1
        assert summary_call.kwargs["file_missing"] == 1
        assert summary_call.kwargs["audio_drift"] == 0
        assert summary_call.kwargs["audio_stable"] == 0


# ---------------------------------------------------------------------------
class TestReadAlbumidTag:
    """Unit tests for :func:`music_annotator._pipeline_io._read_albumid_tag`.

    Exercises the FLAC read path, the tag-absent path, and the read-error (exception) path.
    The unsupported-suffix ``case _:`` arm is genuinely unreachable for journal-backed calls and
    is marked ``# pragma: no cover`` in the implementation.
    """

    def test_flac_with_albumid_tag_returns_id(self, fs: FakeFilesystem) -> None:
        """_read_albumid_tag returns the embedded MUSICBRAINZ_ALBUMID for a tagged FLAC.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(musicbrainz_albumid="test-release-uuid")
        apply_tags_flac(path, tags)

        assert _read_albumid_tag(path) == "test-release-uuid"

    def test_flac_without_albumid_tag_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_albumid_tag returns "" when the file has no MUSICBRAINZ_ALBUMID tag.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        # Write a minimal FLAC with no MUSICBRAINZ_ALBUMID tag
        tags = TrackTags(title="A Track")
        apply_tags_flac(path, tags)

        assert _read_albumid_tag(path) == ""

    # pylint: disable-next=unused-argument
    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_albumid_tag returns "" and logs a warning when the tag read raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/broken.flac")
        mocker.patch("music_annotator._pipeline_io._read_tags_flac", side_effect=OSError("corrupt"))
        mock_log = mocker.patch("music_annotator._pipeline_io.log")

        result = _read_albumid_tag(path)

        assert result == ""
        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "albumid_tag_read_error" in warning_events

    def test_mp3_with_albumid_tag_returns_id(self, fs: FakeFilesystem) -> None:
        """_read_albumid_tag returns the embedded MUSICBRAINZ_ALBUMID for a tagged MP3.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.mp3"
        path.write_bytes(_MINIMAL_MP3)
        tags = TrackTags(musicbrainz_albumid="mp3-release-uuid")
        apply_tags_mp3(path, tags)

        assert _read_albumid_tag(path) == "mp3-release-uuid"


class TestAuditConfirmsViaTag:
    """Tests for audit()'s tag-confirmation layer.

    Verifies that fragmentation candidates are annotated with ``confirmed=True`` when the
    embedded ``MUSICBRAINZ_ALBUMID`` tag in a backing file matches the journal's ``release_id``,
    and ``confirmed=False`` (stale) when all backing files have absent, differing, or unreadable tags.

    Uses real FLAC bytes and :func:`apply_tags_flac` so that :func:`_read_tags_flac` executes
    the real mutagen round-trip rather than a mock.  The structlog ``log`` object is patched so
    logged events can be inspected without a logging infrastructure.
    """

    def test_audit_confirms_candidate_via_tag(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() distinguishes confirmed fragmentation from stale journal entries.

        Scenario: two case-(a) fragmentation candidates.

        * ``Work-A [2020]``: two entries with ``rel-1`` and ``rel-2``.  The ``rel-1`` entry's
          destination FLAC embeds ``MUSICBRAINZ_ALBUMID=rel-1`` — tag matches the journal's
          ``release_id`` → this candidate is reported as **confirmed**.

        * ``Work-B [2020]``: two entries with ``rel-3`` and ``rel-4``.  Both destination FLACs
          embed a mismatched ``MUSICBRAINZ_ALBUMID`` (or have no tag) — no tag matches → this
          candidate is reported as **stale** (``confirmed=False``).

        Asserts that:

        * Both candidates produce an ``audit_multiple_release_ids`` warning.
        * The ``Work-A [2020]`` warning carries ``confirmed=True``.
        * The ``Work-B [2020]`` warning carries ``confirmed=False``.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # --- Work-A [2020]: confirmed candidate ---
        # Entry for rel-1: FLAC with MUSICBRAINZ_ALBUMID=rel-1 (tag matches → confirms rel-1 entry)
        dest_a_rel1 = dest_root / "Beethoven - Karajan" / "Work-A [2020]" / "01 - Mvt1.flac"
        dest_a_rel1.parent.mkdir(parents=True, exist_ok=True)
        dest_a_rel1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_a_rel1, TrackTags(musicbrainz_albumid="rel-1"))

        # Entry for rel-2: FLAC with wrong tag (MUSICBRAINZ_ALBUMID=other-id, not rel-2)
        dest_a_rel2 = dest_root / "Beethoven - Karajan" / "Work-A [2020]" / "02 - Mvt2.flac"
        dest_a_rel2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_a_rel2, TrackTags(musicbrainz_albumid="other-id"))

        # --- Work-B [2020]: stale candidate ---
        # Entry for rel-3: FLAC with MUSICBRAINZ_ALBUMID absent (title tag only)
        dest_b_rel3 = dest_root / "Beethoven - Karajan" / "Work-B [2020]" / "01 - Mvt1.flac"
        dest_b_rel3.parent.mkdir(parents=True, exist_ok=True)
        dest_b_rel3.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_b_rel3, TrackTags(title="A Movement"))

        # Entry for rel-4: FLAC with mismatching MUSICBRAINZ_ALBUMID
        dest_b_rel4 = dest_root / "Beethoven - Karajan" / "Work-B [2020]" / "02 - Mvt2.flac"
        dest_b_rel4.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest_b_rel4, TrackTags(musicbrainz_albumid="totally-wrong"))

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(dest_a_rel1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": str(dest_a_rel2),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-3",
                    "source": "/src/03.flac",
                    "destination": str(dest_b_rel3),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-4",
                    "source": "/src/04.flac",
                    "destination": str(dest_b_rel4),
                    "action": "tagged",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        warning_calls = mock_log.warning.call_args_list
        work_a_calls = [c for c in warning_calls if c.kwargs.get("work_dir") == "Work-A [2020]"]
        work_b_calls = [c for c in warning_calls if c.kwargs.get("work_dir") == "Work-B [2020]"]

        assert len(work_a_calls) == 1, "expected one warning for Work-A [2020]"
        assert work_a_calls[0].kwargs["confirmed"] is True, "Work-A [2020] must be confirmed (tag matches)"

        assert len(work_b_calls) == 1, "expected one warning for Work-B [2020]"
        assert work_b_calls[0].kwargs["confirmed"] is False, "Work-B [2020] must be stale (no matching tag)"

    def test_audit_confirmed_requires_tag_match(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """confirmed=True requires the embedded tag value to equal the journal's release_id.

        A FLAC with a tag value that does not match its journal ``release_id`` is not sufficient
        to confirm — the whole candidate remains stale.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Both Work-A entries have mismatching tags (tag present but wrong value)
        dest1 = dest_root / "C - P" / "Work-A [2020]" / "01.flac"
        dest1.parent.mkdir(parents=True, exist_ok=True)
        dest1.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest1, TrackTags(musicbrainz_albumid="wrong-id"))

        dest2 = dest_root / "C - P" / "Work-A [2020]" / "02.flac"
        dest2.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(dest2, TrackTags(musicbrainz_albumid="also-wrong"))

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(dest1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": str(dest2),
                    "action": "tagged",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        work_a_calls = [c for c in mock_log.warning.call_args_list if c.kwargs.get("work_dir") == "Work-A [2020]"]
        assert len(work_a_calls) == 1
        assert work_a_calls[0].kwargs["confirmed"] is False

    def test_audit_stale_when_file_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """confirmed=False when the backing destination file does not exist on disk.

        A journal entry whose destination file is absent is treated as stale: _read_albumid_tag
        logs a warning and returns "", which does not match the journal's release_id.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Destination files for both entries are NOT created on disk
        dest1 = dest_root / "C - P" / "Work-A [2020]" / "01.flac"
        dest2 = dest_root / "C - P" / "Work-A [2020]" / "02.flac"
        dest1.parent.mkdir(parents=True, exist_ok=True)
        # Neither dest1 nor dest2 is written

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": str(dest1),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": str(dest2),
                    "action": "tagged",
                },
            ],
        )

        # Patch log so we can check the stale result without noise from the read-error warning
        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        work_a_calls = [c for c in mock_log.warning.call_args_list if c.kwargs.get("work_dir") == "Work-A [2020]"]
        assert len(work_a_calls) == 1
        assert work_a_calls[0].kwargs["confirmed"] is False

    def test_confirmed_existing_test_still_reports_warning_events(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Backward-compat: warning events still appear; confirmed kwarg is now present too.

        The KAT (test_audit_reports_mixed_mbid_and_split_release) asserts on
        ``audit_multiple_release_ids`` and ``audit_split_release`` event names.  This test verifies
        that those events still fire after tag-confirmation was added, and that each carries the new
        ``confirmed`` kwarg.  No audio files are created so all
        candidates are stale, but the event names remain unchanged.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Same scenario as test_audit_reports_mixed_mbid_and_split_release — no audio files
        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-A [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-2",
                    "source": "/src/02.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-A [2020]/02 - Mvt2.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-3",
                    "source": "/src/03.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-B [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-3",
                    "source": "/src/04.flac",
                    "destination": "/lib/Beethoven - Karajan/Work-C [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_multiple_release_ids" in warning_events
        assert "audit_split_release" in warning_events

        # All candidates are stale (no audio files to confirm); confirmed kwarg must be present
        for call in mock_log.warning.call_args_list:
            if call.args[0] in {"audit_multiple_release_ids", "audit_split_release"}:
                assert "confirmed" in call.kwargs


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# diff_journal
# ---------------------------------------------------------------------------


class TestDiffJournal:
    """Tests for :func:`diff_journal` — the audit --diff mode.

    Covers the three output buckets: matches (journal and rebuild agree), stale (journal path
    absent from rebuild — expected after repath/regroup), and leaked (journal has a field value
    not reproducible by rebuild — authority leak).
    """

    def test_matches_only(self, fs: FakeFilesystem) -> None:
        """diff_journal returns all entries in matches when journal and rebuild agree on all fields.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        # Write a journal that matches what rebuild would produce: same release_id, empty
        # identity fields (rebuild reads them from tags; journal entry also has empty fields).
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text(
            json.dumps(
                [
                    {
                        "timestamp": "2024-01-01T00:00:00+00:00",
                        "release_id": "rel-1",
                        "source": str(flac_path),
                        "destination": str(flac_path),
                        "action": "tagged",
                        "audio_hash": "",
                        "acoustid_fingerprint": "",
                        "acoustid_id": "",
                        "origin_time": "",
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = diff_journal(dest_root)

        assert str(flac_path) in result.matches
        assert result.stale == []
        assert result.leaked == []

    def test_stale_path_not_in_rebuild(self, fs: FakeFilesystem) -> None:
        """diff_journal puts a journal path in stale when rebuild has no entry for that path.

        This simulates the post-repath/regroup state: the journal still references the old path
        but the file has been moved, so rebuild (which walks current disk state) finds it at the
        new path only.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        # The file now lives at the new path (after repath).
        new_work_dir = dest_root / "Composer" / "Work [2025]"
        fs.create_dir(str(new_work_dir))
        new_flac_path = new_work_dir / "01 - Movement.flac"
        fs.create_file(str(new_flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(new_flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        # The journal still references the old path (pre-repath).
        old_flac_path = dest_root / "Composer" / "Work [2024]" / "01 - Movement.flac"
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text(
            json.dumps(
                [
                    {
                        "timestamp": "2024-01-01T00:00:00+00:00",
                        "release_id": "rel-1",
                        "source": "/rip/source/01.flac",
                        "destination": str(old_flac_path),
                        "action": "tagged",
                        "audio_hash": "",
                        "acoustid_fingerprint": "",
                        "acoustid_id": "",
                        "origin_time": "",
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = diff_journal(dest_root)

        assert str(old_flac_path) in result.stale
        assert result.matches == []
        assert result.leaked == []

    def test_leaked_field_mismatch(self, fs: FakeFilesystem) -> None:
        """diff_journal puts an entry in leaked when the journal has a field value rebuild cannot reproduce.

        This exercises the authority-leak bucket: the journal carries a release_id that differs
        from what rebuild reads from the embedded MUSICBRAINZ_ALBUMID tag.  This is the shape of
        a real authority leak — a journal value that cannot be reconstructed from the track alone.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        # Tag the file with release_id "rel-actual" — this is what rebuild will read.
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-actual"))

        # The journal claims a different release_id — not reproducible from the tag.
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text(
            json.dumps(
                [
                    {
                        "timestamp": "2024-01-01T00:00:00+00:00",
                        "release_id": "rel-leaked",
                        "source": str(flac_path),
                        "destination": str(flac_path),
                        "action": "tagged",
                        "audio_hash": "",
                        "acoustid_fingerprint": "",
                        "acoustid_id": "",
                        "origin_time": "",
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = diff_journal(dest_root)

        assert result.matches == []
        assert result.stale == []
        assert len(result.leaked) == 1
        leaked_dest, leaked_diffs = result.leaked[0]
        assert leaked_dest == str(flac_path)
        assert "release_id" in leaked_diffs
        journal_val, rebuild_val = leaked_diffs["release_id"]
        assert journal_val == "rel-leaked"
        assert rebuild_val == "rel-actual"

    def test_result_type(self, fs: FakeFilesystem) -> None:
        """diff_journal returns a JournalDiffResult instance.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        result = diff_journal(dest_root)

        assert isinstance(result, JournalDiffResult)
        assert result.matches == []
        assert result.stale == []
        assert result.leaked == []

    def test_non_tagged_entries_skipped(self, fs: FakeFilesystem) -> None:
        """diff_journal ignores non-tagged entries in both the journal and the rebuild.

        A sidecar entry in the journal and a sidecar file on disk (producing a sidecar entry in
        the rebuild) must not appear in any bucket.  This exercises the action-filter branches in
        both the rebuild-map construction loop and the journal-latest construction loop.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        # A sidecar file on disk — rebuild will emit action="sidecar" for it.
        cover_path = work_dir / "cover.jpg"
        fs.create_file(str(cover_path), contents=b"\xff\xd8\xff\xe0")

        # Journal has only a sidecar entry (action != "tagged") — must be ignored.
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text(
            json.dumps(
                [
                    {
                        "timestamp": "2024-01-01T00:00:00+00:00",
                        "release_id": "",
                        "source": str(cover_path),
                        "destination": str(cover_path),
                        "action": "sidecar",
                        "audio_hash": "",
                        "acoustid_fingerprint": "",
                        "acoustid_id": "",
                        "origin_time": "",
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = diff_journal(dest_root)

        assert result.matches == []
        assert result.stale == []
        assert result.leaked == []


# ---------------------------------------------------------------------------
# detect_fragmented_releases
# ---------------------------------------------------------------------------


class TestDetectFragmentedReleases:
    """Tests for :func:`detect_fragmented_releases` — tag-based performer-split detection.

    Exercises the detection logic (C-W2): a release is fragmented when ≥2 distinct top_dirs share
    the same ``MUSICBRAINZ_ALBUMID`` tag.  The join key is the embedded tag, not the journal.
    """

    # pylint: disable=unused-argument  # fs activates pyfakefs; not all tests call fs.create_* directly

    def test_fragmented_release_detected(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases returns a release whose files span ≥2 top_dirs.

        Creates two FLAC files for the same release_id under different top_dirs and asserts that
        the release is returned in the result dict.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        # File A: top_dir "Brahms - Karajan"
        path_a = dest_root / "Brahms - Karajan" / "Piano Concerto No. 1 [rec 2021]" / "01.flac"
        path_a.parent.mkdir(parents=True, exist_ok=True)
        path_a.write_bytes(_MINIMAL_FLAC)
        audio_a = FLAC(str(path_a))
        audio_a["MUSICBRAINZ_ALBUMID"] = "rel-frag"
        audio_a.save()

        # File B: top_dir "Brahms - Pollini" (different performer → different top_dir)
        path_b = dest_root / "Brahms - Pollini" / "Piano Concerto No. 1 [rec 2021]" / "01.flac"
        path_b.parent.mkdir(parents=True, exist_ok=True)
        path_b.write_bytes(_MINIMAL_FLAC)
        audio_b = FLAC(str(path_b))
        audio_b["MUSICBRAINZ_ALBUMID"] = "rel-frag"
        audio_b.save()

        result = detect_fragmented_releases(dest_root)

        assert "rel-frag" in result
        assert sorted(result["rel-frag"]) == sorted([path_a, path_b])

    def test_non_fragmented_release_excluded(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases excludes releases whose files all share one top_dir.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        path_a = dest_root / "Brahms - Karajan" / "Piano Concerto No. 1 [rec 2021]" / "01.flac"
        path_a.parent.mkdir(parents=True, exist_ok=True)
        path_a.write_bytes(_MINIMAL_FLAC)
        audio_a = FLAC(str(path_a))
        audio_a["MUSICBRAINZ_ALBUMID"] = "rel-clean"
        audio_a.save()

        path_b = dest_root / "Brahms - Karajan" / "Piano Concerto No. 1 [rec 2021]" / "02.flac"
        path_b.write_bytes(_MINIMAL_FLAC)
        audio_b = FLAC(str(path_b))
        audio_b["MUSICBRAINZ_ALBUMID"] = "rel-clean"
        audio_b.save()

        result = detect_fragmented_releases(dest_root)

        assert "rel-clean" not in result

    def test_empty_dest_root_returns_empty(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases returns an empty dict for an empty library.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        dest_root.mkdir()

        result = detect_fragmented_releases(dest_root)

        assert result == {}

    def test_missing_dest_root_returns_empty(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases returns an empty dict when dest_root does not exist.

        :param fs: pyfakefs fixture (activates fake filesystem so /nonexistent truly does not exist).
        """
        result = detect_fragmented_releases(Path("/nonexistent"))

        assert result == {}

    def test_files_with_empty_albumid_skipped(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases skips files whose MUSICBRAINZ_ALBUMID tag is empty.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        path_a = dest_root / "Brahms - Karajan" / "Work [2021]" / "01.flac"
        path_a.parent.mkdir(parents=True, exist_ok=True)
        path_a.write_bytes(_MINIMAL_FLAC)
        # No MUSICBRAINZ_ALBUMID tag written → empty tag → skipped

        path_b = dest_root / "Brahms - Pollini" / "Work [2021]" / "01.flac"
        path_b.parent.mkdir(parents=True, exist_ok=True)
        path_b.write_bytes(_MINIMAL_FLAC)
        # No MUSICBRAINZ_ALBUMID tag written → empty tag → skipped

        result = detect_fragmented_releases(dest_root)

        assert result == {}

    def test_non_audio_files_skipped(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases skips non-audio files (e.g. YAML sidecars).

        Exercises the ``file_path.suffix.lower() not in _REBUILD_AUDIO_EXTENSIONS`` branch.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        # Create a sidecar YAML file (not an audio file) under two different top_dirs
        sidecar_a = dest_root / "Brahms - Karajan" / "Work [2021]" / "freedb_disc_1.yaml"
        sidecar_a.parent.mkdir(parents=True, exist_ok=True)
        sidecar_a.write_text("release_id: rel-1\n", encoding="utf-8")

        sidecar_b = dest_root / "Brahms - Pollini" / "Work [2021]" / "freedb_disc_1.yaml"
        sidecar_b.parent.mkdir(parents=True, exist_ok=True)
        sidecar_b.write_text("release_id: rel-1\n", encoding="utf-8")

        result = detect_fragmented_releases(dest_root)

        # YAML files are not audio files → skipped → no fragmented releases detected
        assert result == {}

    def test_non_dir_work_dir_skipped(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases skips non-directory entries under top_dir.

        Exercises the ``not work_dir.is_dir()`` branch.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        top_dir = dest_root / "Brahms - Karajan"
        top_dir.mkdir(parents=True)
        # Create a file directly under top_dir (not a directory) — should be skipped
        (top_dir / "not_a_dir.txt").write_text("hello", encoding="utf-8")

        result = detect_fragmented_releases(dest_root)

        assert result == {}

    def test_non_dir_file_in_work_dir_skipped(self, fs: FakeFilesystem) -> None:
        """detect_fragmented_releases skips non-file entries when walking work_dir.

        Exercises the ``not file_path.is_file()`` branch by creating a subdirectory inside
        the work_dir (rglob returns it, but it is not a file).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Brahms - Karajan" / "Work [2021]"
        work_dir.mkdir(parents=True)
        # Create a subdirectory inside work_dir — rglob will yield it, but it is not a file
        sub_dir = work_dir / "subdir"
        sub_dir.mkdir()

        result = detect_fragmented_releases(dest_root)

        assert result == {}


# ---------------------------------------------------------------------------
# TestAuditTierCaseIds — KAT: applied contested-default case-IDs in audit pass
# ---------------------------------------------------------------------------


class TestAuditTierCaseIds:
    """KAT tests for the applied contested-default case-ID surface in the tier-enumeration pass.

    Covers the ``audit_tier_case_ids`` log event and ``applied_case_ids_total`` counter added to
    :func:`music_annotator._audit._audit_tier_pass`.  The applied case-IDs are read from the
    work-dir provenance sidecar's ``applied_case_ids`` field (C-CASE-PROV) and reported per
    destination file.

    Two branches are exercised:
    - Non-empty ``applied_case_ids``: the event is logged and the counter incremented.
    - Empty ``applied_case_ids``: no event is logged and the counter stays at zero.
    """

    def test_case_ids_logged_and_counted_when_present(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass logs audit_tier_case_ids and increments applied_case_ids_total when
        the sidecar carries applied_case_ids=["SEL-11", "REND-14"].

        Asserts:
        - ``audit_tier_case_ids`` is logged at INFO with the correct ``applied_case_ids`` value.
        - ``counts["applied_case_ids_total"]`` equals 1.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-A [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
                applied_case_ids=["SEL-11", "REND-14"],
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-1",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            )
        ]

        counts = _make_audit_counts()
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_tier_case_ids" in info_events

        case_id_calls = [c for c in mock_log.info.call_args_list if c.args[0] == "audit_tier_case_ids"]
        assert len(case_id_calls) == 1
        assert case_id_calls[0].kwargs["applied_case_ids"] == ["REND-14", "SEL-11"]

        assert counts["applied_case_ids_total"] == 1

    def test_no_case_id_event_when_applied_case_ids_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass does not log audit_tier_case_ids when applied_case_ids is empty.

        Asserts:
        - ``audit_tier_case_ids`` is NOT logged.
        - ``counts["applied_case_ids_total"]`` remains 0.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-B [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
                applied_case_ids=[],
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-2",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            )
        ]

        counts = _make_audit_counts()
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_tier_case_ids" not in info_events
        assert counts["applied_case_ids_total"] == 0
