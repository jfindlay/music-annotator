"""Unit tests for music_annotator.__main__."""
# pylint: disable=duplicate-code  # _MINIMAL_FLAC/_MINIMAL_MP3 are intentionally replicated across
# test modules so each module is self-contained (no cross-test imports).

from __future__ import annotations

import errno
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen._util import MutagenError
from mutagen.flac import FLAC as MutagenFLAC
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator.__main__ import (
    _DEFAULT_USER_AGENT_APP,
    _VERSION,
    _build_parser,
    _configure_logging,
    _resolve_path,
    main,
)
from music_annotator._pipeline import _unify_classical_composer_groups
from music_annotator._pipeline_io import (
    AudioCompareResult,
    _audio_hash,
    _audit_audio_anchor,
    _audit_journal_scan,
    _audit_tag_adjudication,
    _make_audit_counts,
    _needs_enrich,
    _read_albumid_tag,
    _read_audio_hash_tag,
    _read_chromaprint_fp_tag,
    _read_tags_flac,
    _sha256_file,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import build_dest_path
from music_annotator.models import MBRelease, MBTrack, TrackTags, TransactionEntry

# ---------------------------------------------------------------------------
# Minimal FLAC byte sequence (same constant as test_pipeline.py)
# Valid minimal FLAC: magic + STREAMINFO block (last-metadata, 44100 Hz, 2 ch, 16-bit, 0 samples)
# ---------------------------------------------------------------------------

_MINIMAL_FLAC = (
    b"fLaC"
    b"\x80\x00\x00\x22"  # block header: last=1, type=0, length=34
    b"\x10\x00\x10\x00"  # min_blocksize=4096, max_blocksize=4096
    b"\x00\x00\x00"  # min_framesize=0
    b"\x00\x00\x00"  # max_framesize=0
    b"\x0a\xc4\x42\xf0\x00\x00\x00\x00"  # 44100 Hz, 2ch, 16-bit, 0 samples
    b"\x00" * 16  # MD5
)

# Minimal valid MP3: ID3v2.3 header + one null MP3 frame (same as test_pipeline.py)
_ID3_HEADER = b"ID3\x03\x00\x00" + b"\x00\x00\x00\x00"
_MINIMAL_MP3 = _ID3_HEADER + b"\xff\xfb\x90\x00" + b"\x00" * 413

# ---------------------------------------------------------------------------
# _resolve_path
# ---------------------------------------------------------------------------


class TestResolvePath:
    """Tests for _resolve_path."""

    def test_absolute_path_unchanged(self, fs: FakeFilesystem) -> None:
        """An already-absolute path with no symlinks is returned as-is.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/music/album")
        assert _resolve_path("/music/album") == Path("/music/album")

    def test_relative_path_made_absolute(self, fs: FakeFilesystem) -> None:
        """A relative path is resolved against the current working directory.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cwd/sub")
        os.chdir("/cwd")
        assert _resolve_path("sub") == Path("/cwd/sub")

    def test_tilde_expanded(self, fs: FakeFilesystem) -> None:
        """A leading ``~`` is expanded to the user's home directory.

        :param fs: pyfakefs fixture.
        """
        home = Path.home()
        fs.create_dir(str(home / "music"))
        result = _resolve_path("~/music")
        assert result == home / "music"

    def test_symlink_resolved(self, fs: FakeFilesystem) -> None:
        """A symlink target is resolved to the real path.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/real/dir")
        fs.create_symlink("/link/dir", "/real/dir")
        assert _resolve_path("/link/dir") == Path("/real/dir")


# ---------------------------------------------------------------------------
# _configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    """Tests for _configure_logging."""

    def test_verbose_sets_debug(self, mocker: MockerFixture) -> None:
        """When verbose=True the root log level is DEBUG.

        :param mocker: pytest-mock fixture.
        """
        mock_basic = mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        mocker.patch("music_annotator.__main__.structlog.dev.ConsoleRenderer")
        _configure_logging(verbose=True)
        mock_basic.assert_called_once()
        _, kwargs = mock_basic.call_args
        assert kwargs["level"] == logging.DEBUG

    def test_non_verbose_sets_info(self, mocker: MockerFixture) -> None:
        """When verbose=False the root log level is INFO.

        :param mocker: pytest-mock fixture.
        """
        mock_basic = mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        mocker.patch("music_annotator.__main__.structlog.dev.ConsoleRenderer")
        _configure_logging(verbose=False)
        _, kwargs = mock_basic.call_args
        assert kwargs["level"] == logging.INFO

    def test_no_color_disables_renderer_colors(self, mocker: MockerFixture) -> None:
        """When no_color=True the ConsoleRenderer is instantiated with colors=False.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        mock_renderer = mocker.patch("music_annotator.__main__.structlog.dev.ConsoleRenderer")
        _configure_logging(verbose=False, no_color=True)
        mock_renderer.assert_called_once_with(colors=False)

    def test_color_enabled_by_default(self, mocker: MockerFixture) -> None:
        """When no_color=False the ConsoleRenderer is instantiated with colors=True.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        mock_renderer = mocker.patch("music_annotator.__main__.structlog.dev.ConsoleRenderer")
        _configure_logging(verbose=False)
        mock_renderer.assert_called_once_with(colors=True)


# ---------------------------------------------------------------------------
# _build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    """Tests for _build_parser."""

    # Minimal positional + required named args for each subcommand.
    _APPLY_BASE = [
        "apply",
        "/src",
        "/dest",
        "--release-id",
        "abc-123",
        "--user-agent-email",
        "t@x.com",
    ]
    _SEARCH_BASE = ["search", "/src", "/dest", "--user-agent-email", "t@x.com"]
    _PRUNE_BASE = ["prune", "/src", "/dest"]

    def test_no_subcommand_exits(self) -> None:
        """Parser exits with code 2 when no subcommand is provided."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([])
        assert exc.value.code == 2

    def test_version_flag_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """-V/--version prints the version string and exits with code 0.

        :param capsys: pytest stdout/stderr capture fixture.
        """
        parser = _build_parser()
        for flag in ("-V", "--version"):
            with pytest.raises(SystemExit) as exc:
                parser.parse_args([flag])
            assert exc.value.code == 0
            assert _VERSION in capsys.readouterr().out

    # ------------------------------------------------------------------
    # apply parser tests
    # ------------------------------------------------------------------

    def test_apply_parses_positional_args(self) -> None:
        """apply accepts src_dir and dest_dir as positional arguments."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert ns.subcommand == "apply"
        assert ns.src_dir == Path("/src")
        assert ns.dest_dir == Path("/dest")
        assert ns.release_id == "abc-123"
        assert not ns.dry_run
        assert not ns.no_fetch_rels
        assert not ns.delete
        assert not ns.verbose

    def test_apply_requires_release_id(self) -> None:
        """apply exits with code 2 when --release-id is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "/s", "/d", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_apply_requires_src_dir(self) -> None:
        """apply exits with code 2 when src_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--release-id", "x", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_apply_requires_dest_dir(self) -> None:
        """apply exits with code 2 when dest_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "/src", "--release-id", "x", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_apply_requires_user_agent_email(self) -> None:
        """apply exits with code 2 when --user-agent-email is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "/s", "/d", "--release-id", "x"])
        assert exc.value.code == 2

    def test_apply_dry_run_flag(self) -> None:
        """apply --dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "--dry-run"])
        assert ns.dry_run

    def test_apply_no_fetch_rels_flag(self) -> None:
        """apply --no-fetch-rels sets no_fetch_rels=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "--no-fetch-rels"])
        assert ns.no_fetch_rels

    def test_apply_delete_long_flag(self) -> None:
        """apply --delete sets delete=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "--delete"])
        assert ns.delete

    def test_apply_delete_short_flag(self) -> None:
        """apply -d sets delete=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "-d"])
        assert ns.delete

    def test_apply_delete_default_false(self) -> None:
        """apply delete defaults to False when flag is absent."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert not ns.delete

    def test_apply_no_cache_flag(self) -> None:
        """apply --no-cache sets no_cache=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "--no-cache"])
        assert ns.no_cache

    def test_apply_no_cache_default_false(self) -> None:
        """apply no_cache defaults to False when flag is absent."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert not ns.no_cache

    def test_apply_disc_flag(self) -> None:
        """apply --disc N sets disc=N."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "--disc", "2"])
        assert ns.disc == 2

    def test_apply_disc_default_none(self) -> None:
        """apply disc defaults to None when flag is absent."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert ns.disc is None

    def test_apply_verbose_flag(self) -> None:
        """-v before the subcommand sets verbose=True."""
        parser = _build_parser()
        ns = parser.parse_args(["-v", *self._APPLY_BASE])
        assert ns.verbose

    def test_no_color_long_flag(self) -> None:
        """--no-color before the subcommand sets no_color=True."""
        parser = _build_parser()
        ns = parser.parse_args(["--no-color", *self._APPLY_BASE])
        assert ns.no_color

    def test_no_color_short_flag(self) -> None:
        """-C before the subcommand sets no_color=True."""
        parser = _build_parser()
        ns = parser.parse_args(["-C", *self._APPLY_BASE])
        assert ns.no_color

    def test_no_color_default_false(self) -> None:
        """no_color defaults to False when flag is absent."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert not ns.no_color

    def test_apply_custom_user_agent_app(self) -> None:
        """apply --user-agent-app overrides the default."""
        parser = _build_parser()
        ns = parser.parse_args([*self._APPLY_BASE, "--user-agent-app", "MyApp/2.0"])
        assert ns.user_agent_app == "MyApp/2.0"

    def test_apply_default_user_agent_app_contains_version(self) -> None:
        """apply user_agent_app default includes the package version."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert ns.user_agent_app == _DEFAULT_USER_AGENT_APP
        assert _VERSION in ns.user_agent_app

    def test_apply_user_agent_email_stored(self) -> None:
        """apply --user-agent-email is stored on the namespace."""
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert ns.user_agent_email == "t@x.com"

    # ------------------------------------------------------------------
    # search parser tests
    # ------------------------------------------------------------------

    def test_search_parses_positional_args(self) -> None:
        """search accepts one or more src_dir positionals and a dest_dir positional."""
        parser = _build_parser()
        ns = parser.parse_args(self._SEARCH_BASE)
        assert ns.subcommand == "search"
        assert ns.src_dirs == [Path("/src")]
        assert ns.dest_dir == Path("/dest")
        assert ns.limit == 10
        assert not ns.dry_run
        assert not ns.delete

    def test_search_requires_src_dir(self) -> None:
        """search exits with code 2 when src_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_search_requires_dest_dir(self) -> None:
        """search exits with code 2 when dest_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "/src", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_search_requires_user_agent_email(self) -> None:
        """search exits with code 2 when --user-agent-email is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "/src", "/dest"])
        assert exc.value.code == 2

    def test_search_custom_limit(self) -> None:
        """search --limit overrides the default of 10."""
        parser = _build_parser()
        ns = parser.parse_args([*self._SEARCH_BASE, "--limit", "5"])
        assert ns.limit == 5

    def test_search_dry_run_flag(self) -> None:
        """search --dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._SEARCH_BASE, "--dry-run"])
        assert ns.dry_run

    def test_search_no_fetch_rels_flag(self) -> None:
        """search --no-fetch-rels sets no_fetch_rels=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._SEARCH_BASE, "--no-fetch-rels"])
        assert ns.no_fetch_rels

    def test_search_no_cache_flag(self) -> None:
        """search --no-cache sets no_cache=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._SEARCH_BASE, "--no-cache"])
        assert ns.no_cache

    def test_search_no_cache_default_false(self) -> None:
        """search no_cache defaults to False when flag is absent."""
        parser = _build_parser()
        ns = parser.parse_args(self._SEARCH_BASE)
        assert not ns.no_cache

    def test_search_delete_flag(self) -> None:
        """search --delete sets delete=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._SEARCH_BASE, "--delete"])
        assert ns.delete

    def test_search_delete_default_false(self) -> None:
        """search delete defaults to False when flag is absent."""
        parser = _build_parser()
        ns = parser.parse_args(self._SEARCH_BASE)
        assert not ns.delete

    # ------------------------------------------------------------------
    # prune parser tests
    # ------------------------------------------------------------------

    def test_prune_parses_positional_args(self) -> None:
        """prune accepts one or more src_dir positionals and a dest_dir positional."""
        parser = _build_parser()
        ns = parser.parse_args(self._PRUNE_BASE)
        assert ns.subcommand == "prune"
        assert ns.src_dirs == [Path("/src")]
        assert ns.dest_dir == Path("/dest")
        assert not ns.yes

    def test_prune_requires_src_dir(self) -> None:
        """prune exits with code 2 when src_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["prune"])
        assert exc.value.code == 2

    def test_prune_requires_dest_dir(self) -> None:
        """prune exits with code 2 when dest_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["prune", "/src"])
        assert exc.value.code == 2

    def test_prune_yes_long_flag(self) -> None:
        """prune --yes sets yes=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._PRUNE_BASE, "--yes"])
        assert ns.yes

    def test_prune_yes_short_flag(self) -> None:
        """prune -y sets yes=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._PRUNE_BASE, "-y"])
        assert ns.yes

    # ------------------------------------------------------------------
    # repath parser tests
    # ------------------------------------------------------------------

    _REPATH_BASE = ["repath", "/dest"]

    def test_repath_parses_dest_dir(self) -> None:
        """repath accepts dest_dir as a positional argument."""
        parser = _build_parser()
        ns = parser.parse_args(self._REPATH_BASE)
        assert ns.subcommand == "repath"
        assert ns.dest_dir == Path("/dest")
        assert not ns.dry_run

    def test_repath_dry_run_flag(self) -> None:
        """repath --dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._REPATH_BASE, "--dry-run"])
        assert ns.dry_run

    def test_repath_requires_dest_dir(self) -> None:
        """repath exits with code 2 when dest_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["repath"])
        assert exc.value.code == 2

    # ------------------------------------------------------------------
    # regroup parser tests
    # ------------------------------------------------------------------

    _REGROUP_BASE = ["regroup", "/dest"]

    def test_regroup_parses_dest_dir(self) -> None:
        """regroup accepts dest_dir as a positional argument and defaults to no flags."""
        parser = _build_parser()
        ns = parser.parse_args(self._REGROUP_BASE)
        assert ns.subcommand == "regroup"
        assert ns.dest_dir == Path("/dest")
        assert not ns.dry_run
        assert not ns.yes

    def test_regroup_dry_run_flag(self) -> None:
        """regroup --dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._REGROUP_BASE, "--dry-run"])
        assert ns.dry_run

    def test_regroup_yes_long_flag(self) -> None:
        """regroup --yes sets yes=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._REGROUP_BASE, "--yes"])
        assert ns.yes

    def test_regroup_yes_short_flag(self) -> None:
        """regroup -y sets yes=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._REGROUP_BASE, "-y"])
        assert ns.yes

    def test_regroup_requires_dest_dir(self) -> None:
        """regroup exits with code 2 when dest_dir positional is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["regroup"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for the main() entry point."""

    def _patch_common(self, mocker: MockerFixture) -> None:
        """Patch logging and structlog so tests don't reconfigure the process logger.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")

    # Minimal argv lists for each subcommand.
    _APPLY_ARGV = [
        "music-annotator",
        "apply",
        "/src",
        "/d",
        "--release-id",
        "x",
        "--user-agent-email",
        "t@x.com",
    ]
    _SEARCH_ARGV = ["music-annotator", "search", "/src", "/d", "--user-agent-email", "t@x.com"]
    _PRUNE_ARGV = ["music-annotator", "prune", "/src", "/d"]

    # ------------------------------------------------------------------
    # apply subcommand
    # ------------------------------------------------------------------

    def test_apply_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() apply exits with code 1 when src_dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        with patch.object(
            sys, "argv", ["music-annotator", "apply", "/no/such", "/d", "--release-id", "x", "--user-agent-email", "t@x.com"]
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_apply_exits_0_on_success(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() apply exits cleanly when run() succeeds.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_run = mocker.patch("music_annotator.run")
        with patch.object(sys, "argv", [*self._APPLY_ARGV, "--dry-run"]):
            main()
        mock_run.assert_called_once()

    def test_apply_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() apply exits with code 1 when run() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", self._APPLY_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_apply_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() apply exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", self._APPLY_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_apply_no_fetch_rels_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply --no-fetch-rels is translated to fetch_rels=False in run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_run = mocker.patch("music_annotator.run")
        with patch.object(sys, "argv", [*self._APPLY_ARGV, "--no-fetch-rels"]):
            main()
        _, kwargs = mock_run.call_args
        assert kwargs["fetch_rels"] is False

    def test_apply_no_cache_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply --no-cache is passed as no_cache=True to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_run = mocker.patch("music_annotator.run")
        with patch.object(sys, "argv", [*self._APPLY_ARGV, "--no-cache"]):
            main()
        _, kwargs = mock_run.call_args
        assert kwargs["no_cache"] is True

    def test_apply_disc_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply --disc N is passed as disc_override=N to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_run = mocker.patch("music_annotator.run")
        with patch.object(sys, "argv", [*self._APPLY_ARGV, "--disc", "2"]):
            main()
        _, kwargs = mock_run.call_args
        assert kwargs["disc_override"] == 2

    def test_apply_disc_none_by_default(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply without --disc passes disc_override=None to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_run = mocker.patch("music_annotator.run")
        with patch.object(sys, "argv", self._APPLY_ARGV):
            main()
        _, kwargs = mock_run.call_args
        assert kwargs["disc_override"] is None

    def test_apply_user_agent_assembled_correctly(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """user_agent passed to run() is '{app} {email}'.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_run = mocker.patch("music_annotator.run")
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "apply",
                "/src",
                "/d",
                "--release-id",
                "x",
                "--user-agent-app",
                "MyApp/2.0",
                "--user-agent-email",
                "me@example.com",
            ],
        ):
            main()
        _, kwargs = mock_run.call_args
        assert kwargs["user_agent"] == "MyApp/2.0 me@example.com"

    def test_apply_verbose_passed_to_configure_logging(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """-v before apply causes _configure_logging to be called with verbose=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mock_cfg = mocker.patch("music_annotator.__main__._configure_logging")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.run")
        fs.create_dir("/src")
        with patch.object(sys, "argv", ["-v", *self._APPLY_ARGV[1:]]):
            with patch.object(
                sys,
                "argv",
                ["music-annotator", "-v", "apply", "/src", "/d", "--release-id", "x", "--user-agent-email", "t@x.com"],
            ):
                main()
        mock_cfg.assert_called_once_with(True, no_color=False)

    def test_apply_delete_calls_prompt_delete_src(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply --delete calls prompt_delete_src after a successful non-dry-run copy.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run")
        mock_prompt = mocker.patch("music_annotator.prompt_delete_src")
        with patch.object(sys, "argv", [*self._APPLY_ARGV, "--delete"]):
            main()
        mock_prompt.assert_called_once_with(Path("/src"))

    def test_apply_delete_not_called_without_flag(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply without --delete does not call prompt_delete_src.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run")
        mock_prompt = mocker.patch("music_annotator.prompt_delete_src")
        with patch.object(sys, "argv", self._APPLY_ARGV):
            main()
        mock_prompt.assert_not_called()

    def test_apply_delete_suppressed_on_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply --delete --dry-run does not call prompt_delete_src.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run")
        mock_prompt = mocker.patch("music_annotator.prompt_delete_src")
        with patch.object(sys, "argv", [*self._APPLY_ARGV, "--delete", "--dry-run"]):
            main()
        mock_prompt.assert_not_called()

    # ------------------------------------------------------------------
    # search subcommand
    # ------------------------------------------------------------------

    def test_search_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() search exits with code 1 when src_dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        with patch.object(sys, "argv", ["music-annotator", "search", "/no/such", "/d", "--user-agent-email", "t@x.com"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_search_exits_0_on_success(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() search exits cleanly when discover() succeeds.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", self._SEARCH_ARGV):
            main()
        mock_discover.assert_called_once()

    def test_search_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() search exits with code 1 when discover() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.discover", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", self._SEARCH_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_search_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() search exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.discover", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", self._SEARCH_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_search_no_fetch_rels_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """search --no-fetch-rels is translated to fetch_rels=False in discover().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", [*self._SEARCH_ARGV, "--no-fetch-rels"]):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["fetch_rels"] is False

    def test_search_no_cache_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """search --no-cache is passed as no_cache=True to discover().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", [*self._SEARCH_ARGV, "--no-cache"]):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["no_cache"] is True

    def test_search_limit_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """search --limit N is forwarded to discover().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", [*self._SEARCH_ARGV, "--limit", "5"]):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["limit"] == 5

    def test_search_user_agent_assembled_correctly(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """user_agent passed to discover() is '{app} {email}'.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "search",
                "/src",
                "/d",
                "--user-agent-app",
                "MyApp/2.0",
                "--user-agent-email",
                "me@example.com",
            ],
        ):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["user_agent"] == "MyApp/2.0 me@example.com"

    def test_search_src_dir_passed_as_list(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A single search src_dir is forwarded to discover() as a one-element list.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", self._SEARCH_ARGV):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["src_dirs"] == [Path("/src")]

    def test_search_multiple_src_dirs_passed_to_discover(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Multiple search src_dirs are all forwarded to discover() in order.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src1")
        fs.create_dir("/src2")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", ["music-annotator", "search", "/src1", "/src2", "/d", "--user-agent-email", "t@x.com"]):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["src_dirs"] == [Path("/src1"), Path("/src2")]

    def test_search_exits_1_when_any_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() search exits with code 1 when any src_dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src1")
        # /src2 does not exist
        with patch.object(sys, "argv", ["music-annotator", "search", "/src1", "/src2", "/d", "--user-agent-email", "t@x.com"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_search_delete_passed_to_discover(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """search --delete passes delete=True to discover().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", [*self._SEARCH_ARGV, "--delete"]):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["delete"] is True

    def test_search_no_delete_by_default(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """search without --delete passes delete=False to discover().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(sys, "argv", self._SEARCH_ARGV):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["delete"] is False

    def test_search_verbose_passed_to_configure_logging(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """-v before search causes _configure_logging to be called with verbose=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mock_cfg = mocker.patch("music_annotator.__main__._configure_logging")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.discover")
        fs.create_dir("/src")
        with patch.object(sys, "argv", ["music-annotator", "-v", "search", "/src", "/d", "--user-agent-email", "t@x.com"]):
            main()
        mock_cfg.assert_called_once_with(True, no_color=False)

    def test_no_color_passed_to_configure_logging(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """-C before search causes _configure_logging to be called with no_color=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mock_cfg = mocker.patch("music_annotator.__main__._configure_logging")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.discover")
        fs.create_dir("/src")
        with patch.object(sys, "argv", ["music-annotator", "-C", "search", "/src", "/d", "--user-agent-email", "t@x.com"]):
            main()
        mock_cfg.assert_called_once_with(False, no_color=True)

    # ------------------------------------------------------------------
    # prune subcommand
    # ------------------------------------------------------------------

    def test_prune_dispatches_to_prune_sources(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() prune calls prune_sources with correct arguments.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch("music_annotator.prune_sources")
        with patch.object(sys, "argv", self._PRUNE_ARGV):
            main()
        mock_prune.assert_called_once_with(src_dir=Path("/src"), dest_root=Path("/d"), yes=False)  # single src

    def test_prune_yes_flag_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() prune -y passes yes=True to prune_sources.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch("music_annotator.prune_sources")
        with patch.object(sys, "argv", [*self._PRUNE_ARGV, "-y"]):
            main()
        _, kwargs = mock_prune.call_args
        assert kwargs["yes"] is True

    def test_prune_logs_error_and_continues_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() prune logs the error and completes normally (no exit 1) when prune_sources raises.

        Each src_dir is processed independently; an error on one does not abort the others.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.prune_sources", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", self._PRUNE_ARGV):
            main()  # should not raise or sys.exit(1)

    def test_prune_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() prune exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.prune_sources", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", self._PRUNE_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_prune_multiple_src_dirs_calls_prune_sources_for_each(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() prune calls prune_sources once per src_dir.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch("music_annotator.prune_sources")
        with patch.object(sys, "argv", ["music-annotator", "prune", "/src1", "/src2", "/d"]):
            main()
        assert mock_prune.call_count == 2
        calls = mock_prune.call_args_list
        assert calls[0].kwargs["src_dir"] == Path("/src1")
        assert calls[1].kwargs["src_dir"] == Path("/src2")
        assert calls[0].kwargs["dest_root"] == Path("/d")
        assert calls[1].kwargs["dest_root"] == Path("/d")

    def test_prune_continues_after_error_on_first_src(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() prune logs error and continues to next src_dir when one raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch(
            "music_annotator.prune_sources",
            side_effect=[RuntimeError("boom"), None],
        )
        with patch.object(sys, "argv", ["music-annotator", "prune", "/src1", "/src2", "/d"]):
            main()  # should not raise or exit 1
        assert mock_prune.call_count == 2

    def test_parser_search_multiple_src_dirs(self) -> None:
        """search parser stores multiple src_dirs as a list.

        :param fs: pyfakefs fixture.
        """
        parser = _build_parser()
        ns = parser.parse_args(["search", "/a", "/b", "/c", "/dest", "--user-agent-email", "t@x.com"])
        assert ns.src_dirs == [Path("/a"), Path("/b"), Path("/c")]
        assert ns.dest_dir == Path("/dest")

    def test_parser_prune_multiple_src_dirs(self) -> None:
        """prune parser stores multiple src_dirs as a list."""
        parser = _build_parser()
        ns = parser.parse_args(["prune", "/a", "/b", "/dest"])
        assert ns.src_dirs == [Path("/a"), Path("/b")]
        assert ns.dest_dir == Path("/dest")

    # ------------------------------------------------------------------
    # repath subcommand (main dispatch)
    # ------------------------------------------------------------------

    _REPATH_ARGV = ["music-annotator", "repath", "/d"]

    def test_repath_dispatches_to_repath(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() repath calls music_annotator.repath with dest_root and dry_run=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_repath = mocker.patch("music_annotator.repath")
        with patch.object(sys, "argv", self._REPATH_ARGV):
            main()
        mock_repath.assert_called_once_with(dest_root=Path("/d"), dry_run=False)

    def test_repath_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() repath --dry-run passes dry_run=True to repath().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_repath = mocker.patch("music_annotator.repath")
        with patch.object(sys, "argv", [*self._REPATH_ARGV, "--dry-run"]):
            main()
        _, kwargs = mock_repath.call_args
        assert kwargs["dry_run"] is True

    def test_repath_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() repath exits with code 1 when repath() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.repath", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", self._REPATH_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_repath_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() repath exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.repath", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", self._REPATH_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    # ------------------------------------------------------------------
    # regroup dispatch tests
    # ------------------------------------------------------------------

    _REGROUP_ARGV = ["music-annotator", "regroup", "/d"]

    def test_regroup_dispatches_to_regroup(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() regroup calls music_annotator.regroup with dest_root, yes=False, dry_run=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_regroup = mocker.patch("music_annotator.regroup")
        with patch.object(sys, "argv", self._REGROUP_ARGV):
            main()
        mock_regroup.assert_called_once_with(dest_root=Path("/d"), yes=False, dry_run=False)

    def test_regroup_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() regroup --dry-run passes dry_run=True to regroup().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_regroup = mocker.patch("music_annotator.regroup")
        with patch.object(sys, "argv", [*self._REGROUP_ARGV, "--dry-run"]):
            main()
        _, kwargs = mock_regroup.call_args
        assert kwargs["dry_run"] is True

    def test_regroup_yes_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() regroup --yes passes yes=True to regroup().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_regroup = mocker.patch("music_annotator.regroup")
        with patch.object(sys, "argv", [*self._REGROUP_ARGV, "--yes"]):
            main()
        _, kwargs = mock_regroup.call_args
        assert kwargs["yes"] is True

    def test_regroup_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() regroup exits with code 1 when regroup() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.regroup", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", self._REGROUP_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_regroup_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() regroup exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.regroup", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", self._REGROUP_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# read_journal
# ---------------------------------------------------------------------------


class TestReadJournal:
    """Tests for read_journal."""

    def test_reads_existing_journal(self, fs: FakeFilesystem) -> None:
        """read_journal parses a valid journal file correctly.

        :param fs: pyfakefs fixture.
        """

        journal_path = Path("/dest/music_annotator_journal.json")
        fs.create_file(
            str(journal_path),
            contents=json.dumps(
                [
                    {
                        "timestamp": "2024-01-01T00:00:00Z",
                        "release_id": "r1",
                        "source": "/src/01.flac",
                        "destination": "/dest/Track/01.flac",
                        "action": "tagged",
                    }
                ]
            ),
        )
        log = music_annotator.read_journal(journal_path)
        assert len(log.entries) == 1
        assert log.entries[0].action == "tagged"
        assert log.entries[0].source == "/src/01.flac"

    def test_returns_empty_when_file_absent(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """read_journal returns empty TransactionLog when the file does not exist.

        :param fs: pyfakefs fixture.
        """

        log = music_annotator.read_journal(Path("/no/such/journal.json"))
        assert log.entries == []

    def test_returns_empty_on_corrupt_file(self, fs: FakeFilesystem) -> None:
        """read_journal returns empty TransactionLog when the file contains invalid JSON.

        :param fs: pyfakefs fixture.
        """

        journal_path = Path("/dest/journal.json")
        fs.create_file(str(journal_path), contents="not valid json {{{")
        log = music_annotator.read_journal(journal_path)
        assert log.entries == []

    def test_returns_empty_on_non_list_json(self, fs: FakeFilesystem) -> None:
        """read_journal returns empty TransactionLog when the JSON root is not a list.

        :param fs: pyfakefs fixture.
        """

        journal_path = Path("/dest/journal.json")
        fs.create_file(str(journal_path), contents='{"not": "a list"}')
        log = music_annotator.read_journal(journal_path)
        assert log.entries == []


# ---------------------------------------------------------------------------
# prompt_delete_src
# ---------------------------------------------------------------------------


class TestPromptDeleteSrc:
    """Tests for prompt_delete_src."""

    def test_deletes_when_confirmed(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prompt_delete_src deletes src_dir when user confirms.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = True
        music_annotator.prompt_delete_src(src, ui=mock_ui)
        assert not src.exists()

    def test_keeps_when_not_confirmed(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prompt_delete_src keeps src_dir when user declines.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        fs.create_dir(str(src))
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = False
        music_annotator.prompt_delete_src(src, ui=mock_ui)
        assert src.exists()

    def test_default_ui_instantiated_when_none(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prompt_delete_src instantiates TerminalDiscoverUI when ui=None.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        fs.create_dir(str(src))
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = False
        mocker.patch("music_annotator._discover.TerminalDiscoverUI", return_value=mock_ui)
        music_annotator.prompt_delete_src(src, ui=None)
        mock_ui.confirm_delete.assert_called_once_with(src)


# ---------------------------------------------------------------------------
# prune_sources
# ---------------------------------------------------------------------------


class TestPruneSources:
    """Tests for prune_sources."""

    def _write_journal(self, fs: FakeFilesystem, dest_root: Path, entries: list[dict[str, str]]) -> None:
        """Write a minimal journal file to dest_root.

        :param fs: pyfakefs fixture.
        :param dest_root: Destination root directory.
        :param entries: List of raw entry dicts to serialise.
        """
        journal_path = dest_root / "music_annotator_journal.json"
        fs.create_file(str(journal_path), contents=json.dumps(entries))

    def test_src_dir_already_deleted_logs_info(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """prune_sources logs info and returns when src_dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        dest = Path("/dest")
        fs.create_dir(str(dest))
        self._write_journal(fs, dest, [])
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(Path("/no/such"), dest, ui=mock_ui)
        mock_ui.confirm_delete.assert_not_called()

    def test_src_not_a_directory_logs_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources logs error and returns when src_dir path is a file, not a directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        dest = Path("/dest")
        fs.create_dir(str(dest))
        src_file = Path("/src_file")
        fs.create_file(str(src_file), contents=b"x")
        self._write_journal(fs, dest, [])
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src_file, dest, ui=mock_ui)
        mock_ui.confirm_delete.assert_not_called()

    def test_no_journal_entries_logs_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources logs warning and returns when no journal entries match src_dir.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        self._write_journal(fs, dest, [])
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src, dest, ui=mock_ui)
        mock_ui.confirm_delete.assert_not_called()

    def test_source_file_missing_logs_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources logs error and skips when a journal source file is missing from disk.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # Journal says 01.flac was copied but it doesn't exist on disk
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/W/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src, dest, ui=mock_ui)
        mock_ui.confirm_delete.assert_not_called()

    def test_extra_audio_file_in_src_logs_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources logs error and skips when src_dir has an extra audio file not in journal.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(src / "02.flac"), contents=b"x")  # extra file not in journal
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src, dest, ui=mock_ui)
        mock_ui.confirm_delete.assert_not_called()

    def test_destination_file_missing_logs_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources logs error and skips when a journal destination file is missing from disk.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        # destination file does NOT exist
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/W/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src, dest, ui=mock_ui)
        mock_ui.confirm_delete.assert_not_called()

    def test_all_checks_pass_yes_deletes_without_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources deletes src_dir immediately when yes=True and all checks pass.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src, dest, yes=True, ui=mock_ui)
        assert not src.exists()
        mock_ui.confirm_delete.assert_not_called()

    def test_all_checks_pass_confirmed_deletes(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources deletes src_dir when user confirms via prompt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = True
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        assert not src.exists()

    def test_all_checks_pass_not_confirmed_keeps(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources keeps src_dir when user declines deletion prompt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = False
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        assert src.exists()

    def test_disc_info_files_ignored_in_src(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources ignores '00 - disc info.yaml' and '00 - disc TOC.flac' in src_dir.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(src / "00 - disc TOC.flac"), contents=b"x")
        fs.create_file(str(src / "00 - disc info.yaml"), contents=b"x")
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = True
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        # Disc info files did not trigger an error — deletion proceeded
        assert not src.exists()

    def test_default_ui_instantiated_when_none(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources instantiates TerminalDiscoverUI when ui=None.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                }
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = False
        mocker.patch("music_annotator._discover.TerminalDiscoverUI", return_value=mock_ui)
        music_annotator.prune_sources(src, dest, ui=None)
        mock_ui.confirm_delete.assert_called_once_with(src)

    def test_sidecar_dest_existence_checked(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources checks destination existence of 'sidecar' entries alongside 'tagged' entries.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        dest_audio = dest / "01.flac"
        dest_yaml = dest / "freedb_disc_1.yaml"
        fs.create_file(str(dest_audio), contents=b"x")
        fs.create_file(str(dest_yaml), contents=b"yaml")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": str(dest_audio),
                    "action": "tagged",
                },
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/00 - disc info.yaml",
                    "destination": str(dest_yaml),
                    "action": "sidecar",
                },
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = True
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        # All checks passed → deletion was offered.
        mock_ui.confirm_delete.assert_called_once()

    def test_sidecar_dest_missing_blocks_deletion(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources blocks deletion when a sidecar destination file is missing.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        dest_audio = dest / "01.flac"
        fs.create_file(str(dest_audio), contents=b"x")
        # Note: dest_yaml is NOT created.
        dest_yaml = dest / "freedb_disc_1.yaml"
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": str(dest_audio),
                    "action": "tagged",
                },
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/00 - disc info.yaml",
                    "destination": str(dest_yaml),
                    "action": "sidecar",
                },
            ],
        )
        mock_ui = mocker.MagicMock()
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        # Missing sidecar dest → deletion prompt must NOT be called.
        mock_ui.confirm_delete.assert_not_called()

    def test_sidecar_source_not_included_in_src_audio_check(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Sidecar source paths are excluded from the audio source-side presence check.

        find_source_files excludes 00 - disc info.yaml, so the source-side check only validates
        audio files from 'tagged' entries and a sidecar's source path never causes a mismatch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        # 00 - disc info.yaml present in src but NOT counted as an audio file.
        fs.create_file(str(src / "00 - disc info.yaml"), contents=b"yaml")
        dest_audio = dest / "01.flac"
        dest_yaml = dest / "freedb_disc_1.yaml"
        fs.create_file(str(dest_audio), contents=b"x")
        fs.create_file(str(dest_yaml), contents=b"yaml")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": str(dest_audio),
                    "action": "tagged",
                },
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": str(src / "00 - disc info.yaml"),
                    "destination": str(dest_yaml),
                    "action": "sidecar",
                },
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = True
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        # Should succeed — sidecar source is not in the audio set comparison.
        mock_ui.confirm_delete.assert_called_once()

    def test_only_copied_entries_considered(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """prune_sources ignores 'skipped' and 'dry_run' journal entries.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=b"x")
        fs.create_file(str(dest / "01.flac"), contents=b"x")
        self._write_journal(
            fs,
            dest,
            [
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/01.flac",
                    "destination": "/dest/01.flac",
                    "action": "tagged",
                },
                {
                    "timestamp": "t",
                    "release_id": "r",
                    "source": "/src/02.flac",
                    "destination": "/dest/02.flac",
                    "action": "skipped",
                },
            ],
        )
        mock_ui = mocker.MagicMock()
        mock_ui.confirm_delete.return_value = True
        # The skipped entry for 02.flac is ignored; 02.flac is not on disk but doesn't matter
        music_annotator.prune_sources(src, dest, yes=False, ui=mock_ui)
        assert not src.exists()


# ---------------------------------------------------------------------------
# repath()
# ---------------------------------------------------------------------------


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
        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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
        music_annotator.repath(dest_root=dest_root, dry_run=False)  # should not raise

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
        music_annotator.repath(dest_root=dest_root, dry_run=False)  # should not raise
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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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
        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=_failing_replace)

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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
            "music_annotator._pipeline.os.replace",
            side_effect=OSError(errno.EPERM, "operation not permitted", str(old_path1)),
        )

        with pytest.raises(OSError) as exc_info:
            music_annotator.repath(dest_root=dest_root, dry_run=False)
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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=_failing_replace)
        mocker.patch("music_annotator._pipeline.shutil.copy2", side_effect=_corrupt_copy)

        with pytest.raises(RuntimeError, match="cross-fs copy integrity failure"):
            music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=_replace_then_corrupt)

        with pytest.raises(RuntimeError, match="repath integrity failure"):
            music_annotator.repath(dest_root=dest_root, dry_run=False)

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
        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=Exception("corrupt tags"))

        music_annotator.repath(dest_root=dest_root, dry_run=False)  # should not raise

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
        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=_read_tags_side_effect)

        with pytest.raises(RuntimeError, match="repath tag re-read failure"):
            music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

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
        tags = TrackTags(
            cwp_composer_lastnames="Mozart",
            cwp_work_top="Don Giovanni",
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
        # The correct path must include an intermediate directory (3 levels below dest_root).
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

        music_annotator.repath(dest_root=dest_root, dry_run=False)

        # File must be at the correct 3-level path (intermediate directory added).
        assert correct_path.exists(), f"File was not moved to correct path {correct_path.relative_to(dest_root)}"
        assert not old_path.exists(), "File still exists at stale (missing-inter-index) path"

        # Journal has a "repathed" entry recording the move.
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 1
        assert repathed[0].source == str(old_path)
        assert repathed[0].destination == str(correct_path)


# ---------------------------------------------------------------------------
# audit()
# ---------------------------------------------------------------------------


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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        case (a) nor case (b) fires.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create destination FLAC files so the new identity passes find no issues.
        for dp in [
            "/lib/Beethoven - Karajan/Symphony No 5 [2020]/01 - Mvt1.flac",
            "/lib/Beethoven - Karajan/Symphony No 5 [2020]/02 - Mvt2.flac",
        ]:
            p = Path(dp)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_MINIMAL_FLAC)

        _write_library_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/01.flac",
                    "destination": "/lib/Beethoven - Karajan/Symphony No 5 [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-1",
                    "source": "/src/02.flac",
                    "destination": "/lib/Beethoven - Karajan/Symphony No 5 [2020]/02 - Mvt2.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        music_annotator.audit(dest_root=dest_root)

        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    def test_audit_skips_malformed_destination_not_under_dest_root(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() skips entries whose destination is not under dest_root without crashing.

        A destination outside ``dest_root`` (e.g. ``/other/Work-X/01.flac`` when dest_root is
        ``/lib``) raises ``ValueError`` in ``Path.relative_to``.  The entry must be skipped and
        the audit must complete as though the entry were absent.

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
        valid_dest = Path("/lib/Beethoven - Karajan/Work-A [2020]/01 - Mvt1.flac")
        valid_dest.parent.mkdir(parents=True, exist_ok=True)
        valid_dest.write_bytes(_MINIMAL_FLAC)

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
                    "destination": "/lib/Beethoven - Karajan/Work-A [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        music_annotator.audit(dest_root=dest_root)

        # The shallow entry was skipped; no tagged entries qualify → clean log
        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    def test_audit_ignores_non_tagged_actions(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() only considers ``action == "tagged"`` entries; other actions are ignored.

        Entries with actions ``"skipped"``, ``"dry_run"``, ``"repathed"`` etc. must not
        contribute to the fragmentation groupings.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create the tagged destination FLAC file so the new identity passes find no issues.
        tagged_dest = Path("/lib/Beethoven - Karajan/Work-C [2020]/01 - Mvt1.flac")
        tagged_dest.parent.mkdir(parents=True, exist_ok=True)
        tagged_dest.write_bytes(_MINIMAL_FLAC)

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
                    "destination": "/lib/Beethoven - Karajan/Work-C [2020]/01 - Mvt1.flac",
                    "action": "tagged",
                    "audio_hash": "flac-md5:00000000000000000000000000000000",
                    "acoustid_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                },
            ],
        )

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        music_annotator.audit(dest_root=dest_root)

        info_events = [call.args[0] for call in mock_log.info.call_args_list]
        assert "audit_clean" in info_events
        mock_log.warning.assert_not_called()

    def test_audit_dispatches_from_main(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit subcommand dispatches to music_annotator.audit with dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mock_audit = mocker.patch("music_annotator.audit")
        with patch.object(sys, "argv", ["music-annotator", "audit", "/d"]):
            main()
        mock_audit.assert_called_once_with(dest_root=Path("/d"))

    def test_audit_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit exits with code 1 when audit() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.audit", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", ["music-annotator", "audit", "/d"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_audit_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.audit", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", ["music-annotator", "audit", "/d"]):
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

    def test_audit_diff_dispatches_from_main(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --diff dispatches to music_annotator.diff_journal with dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mock_diff = mocker.patch("music_annotator.diff_journal")
        with patch.object(sys, "argv", ["music-annotator", "audit", "/d", "--diff"]):
            main()
        mock_diff.assert_called_once_with(dest_root=Path("/d"))


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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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

    def test_pass1_empty_audio_hash_logs_needs_enrich(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        _audit_journal_scan(entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_needs_enrich" in info_events
        assert counts["needs_enrich"] == 1
        assert counts["total"] == 1

    def test_pass1_empty_acoustid_logs_acoustid_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        _audit_journal_scan(entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_acoustid_missing" in info_events
        assert counts["acoustid_missing"] == 1
        assert counts["total"] == 1

    def test_pass1_both_non_empty_no_findings(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        _audit_journal_scan(entries, counts)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_needs_enrich" not in info_events
        assert "audit_acoustid_missing" not in info_events
        assert counts["needs_enrich"] == 0
        assert counts["acoustid_missing"] == 0
        assert counts["total"] == 1

    def test_pass1_only_tagged_and_enriched_scanned(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mocker.patch("music_annotator._pipeline_io.log")
        _audit_journal_scan(entries, counts)

        # Only the "enriched" entry is scanned; "repathed" is ignored.
        assert counts["total"] == 1
        assert counts["needs_enrich"] == 0
        assert counts["acoustid_missing"] == 0

    def test_pass1_duplicate_destination_counted_once(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mocker.patch("music_annotator._pipeline_io.log")
        _audit_journal_scan(entries, counts)

        assert counts["total"] == 1

    # ------------------------------------------------------------------
    # Pass 2 (_audit_tag_adjudication) branch coverage
    # ------------------------------------------------------------------

    def test_pass2_file_missing_logs_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        _audit_audio_anchor(entries, counts)

        debug_events = [c.args[0] for c in mock_log.debug.call_args_list]
        assert "audit_audio_stable" in debug_events
        assert counts["audio_stable"] == 1

    def test_pass3_file_missing_skipped_silently(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mocker.patch("music_annotator._pipeline_io._audio_hash", return_value="")
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
# _read_albumid_tag and audit() tag-confirmation (S7)
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

    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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
    """Tests for audit()'s S7 tag-confirmation layer.

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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
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
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        music_annotator.audit(dest_root=dest_root)

        work_a_calls = [c for c in mock_log.warning.call_args_list if c.kwargs.get("work_dir") == "Work-A [2020]"]
        assert len(work_a_calls) == 1
        assert work_a_calls[0].kwargs["confirmed"] is False

    def test_confirmed_existing_s6_test_still_reports_warning_events(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Backward-compat: prior S6 warning events still appear; confirmed kwarg is now present too.

        The S6 KAT (test_audit_reports_mixed_mbid_and_split_release) asserts on
        ``audit_multiple_release_ids`` and ``audit_split_release`` event names.  This test verifies
        that those events still fire after S7's changes, and that each carries the new
        ``confirmed`` kwarg (which S6 was unaware of).  No audio files are created so all
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

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        music_annotator.audit(dest_root=dest_root)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_multiple_release_ids" in warning_events
        assert "audit_split_release" in warning_events

        # All candidates are stale (no audio files to confirm); confirmed kwarg must be present
        for call in mock_log.warning.call_args_list:
            if call.args[0] in {"audit_multiple_release_ids", "audit_split_release"}:
                assert "confirmed" in call.kwargs


# ---------------------------------------------------------------------------
# regroup() — S8 KAT and full branch coverage
# ---------------------------------------------------------------------------


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

        This is the KAT for S8.  Constructs a confirmed case-(b) split-release scenario (one
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

        mocker.patch("music_annotator._pipeline.input", return_value="y")

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

        mocker.patch("music_annotator._pipeline.input", return_value="n")

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

        mock_input = mocker.patch("music_annotator._pipeline.input")

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

        mocker.patch("music_annotator._pipeline._sha256_file", side_effect=_fake_sha256)

        with pytest.raises(RuntimeError, match="regroup integrity failure"):
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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=_fake_replace)

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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=_fake_replace)

        # After the EXDEV copy, the cross-hash check will compare real hashes (which match).
        # To force a failure we additionally patch _sha256_file to return mismatched values on the
        # cross-fs verification call (the second sha256 call within the EXDEV branch).
        sha_calls = {"n": 0}

        def _fake_sha(_path: Path) -> str:
            sha_calls["n"] += 1
            if sha_calls["n"] <= 2:  # noqa: PLR2004
                return "x" * 64 if sha_calls["n"] == 2 else "a" * 64  # mismatch on second call
            return "a" * 64

        mocker.patch("music_annotator._pipeline._sha256_file", side_effect=_fake_sha)

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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=OSError(errno.EPERM, "permission denied"))

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
            "music_annotator._pipeline._confirm_fragmentation",
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

        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=OSError("unreadable"))

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

        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=_fake_read)

        with pytest.raises(RuntimeError, match="regroup tag re-read failure"):
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
# _read_audio_hash_tag
# ---------------------------------------------------------------------------


class TestReadAudioHashTag:
    """Unit tests for :func:`music_annotator._pipeline_io._read_audio_hash_tag`.

    Exercises the FLAC present, FLAC absent, MP3 present, MP3 absent, unsupported-suffix, and
    read-error arms.
    """

    def test_flac_with_audio_hash_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_audio_hash_tag returns the embedded audio_hash for a tagged FLAC.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(audio_hash="flac-md5:aabbccdd")
        apply_tags_flac(path, tags)

        assert _read_audio_hash_tag(path) == "flac-md5:aabbccdd"

    def test_flac_without_audio_hash_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_audio_hash_tag returns "" when the FLAC has no audio_hash tag.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(title="No Hash")
        apply_tags_flac(path, tags)

        assert _read_audio_hash_tag(path) == ""

    def test_mp3_with_audio_hash_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_audio_hash_tag returns the embedded audio_hash for a tagged MP3.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.mp3"
        path.write_bytes(_MINIMAL_MP3)
        tags = TrackTags(audio_hash="mp3-stream-sha256:deadbeef")
        apply_tags_mp3(path, tags)

        assert _read_audio_hash_tag(path) == "mp3-stream-sha256:deadbeef"

    def test_mp3_without_audio_hash_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_audio_hash_tag returns "" when the MP3 has no audio_hash TXXX frame.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.mp3"
        path.write_bytes(_MINIMAL_MP3)
        tags = TrackTags(title="No Hash")
        apply_tags_mp3(path, tags)

        assert _read_audio_hash_tag(path) == ""

    def test_unsupported_suffix_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_audio_hash_tag returns "" for a file with an unsupported extension.

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.ogg")
        fs.create_file(str(path), contents="dummy")

        assert _read_audio_hash_tag(path) == ""

    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """_read_audio_hash_tag returns "" when the file read raises an exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/broken.flac")
        mocker.patch("music_annotator._pipeline_io.FLAC", side_effect=OSError("corrupt"))

        assert _read_audio_hash_tag(path) == ""


# ---------------------------------------------------------------------------
# _read_chromaprint_fp_tag
# ---------------------------------------------------------------------------


class TestReadChromaprintFpTag:
    """Unit tests for :func:`music_annotator._pipeline_io._read_chromaprint_fp_tag`.

    Exercises the FLAC present, FLAC absent, MP3 present, MP3 absent, unsupported-suffix, and
    read-error arms.
    """

    def test_flac_with_chromaprint_fp_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_chromaprint_fp_tag returns the embedded chromaprint_fp for a tagged FLAC.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(chromaprint_fp="AQADtMmybckm")
        apply_tags_flac(path, tags)

        assert _read_chromaprint_fp_tag(path) == "AQADtMmybckm"

    def test_flac_without_chromaprint_fp_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_chromaprint_fp_tag returns "" when the FLAC has no chromaprint_fp tag.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.flac"
        path.write_bytes(_MINIMAL_FLAC)
        tags = TrackTags(title="No FP")
        apply_tags_flac(path, tags)

        assert _read_chromaprint_fp_tag(path) == ""

    def test_mp3_with_chromaprint_fp_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_chromaprint_fp_tag returns the embedded chromaprint_fp for a tagged MP3.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.mp3"
        path.write_bytes(_MINIMAL_MP3)
        tags = TrackTags(chromaprint_fp="AQADtMmybckm")
        apply_tags_mp3(path, tags)

        assert _read_chromaprint_fp_tag(path) == "AQADtMmybckm"

    def test_mp3_without_chromaprint_fp_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_chromaprint_fp_tag returns "" when the MP3 has no chromaprint_fp TXXX frame.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        path = dest_root / "track.mp3"
        path.write_bytes(_MINIMAL_MP3)
        tags = TrackTags(title="No FP")
        apply_tags_mp3(path, tags)

        assert _read_chromaprint_fp_tag(path) == ""

    def test_unsupported_suffix_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_chromaprint_fp_tag returns "" for a file with an unsupported extension.

        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/track.ogg")
        fs.create_file(str(path), contents="dummy")

        assert _read_chromaprint_fp_tag(path) == ""

    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """_read_chromaprint_fp_tag returns "" when the file read raises an exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/broken.flac")
        mocker.patch("music_annotator._pipeline_io.FLAC", side_effect=OSError("corrupt"))

        assert _read_chromaprint_fp_tag(path) == ""


# ---------------------------------------------------------------------------
# _needs_enrich
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


# ---------------------------------------------------------------------------
# enrich() — full pipeline tests
# ---------------------------------------------------------------------------


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
        mock_log = mocker.patch("music_annotator._pipeline.log")

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

    def test_enrich_empty_journal_is_noop(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """enrich() is a no-op when the journal has no entries.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        _write_library_journal(dest_root, [])

        mock_log = mocker.patch("music_annotator._pipeline.log")

        music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)

        nothing_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_nothing_to_enrich"]
        assert len(nothing_calls) == 1

    # ------------------------------------------------------------------
    # file not on disk → skipped gracefully
    # ------------------------------------------------------------------

    def test_enrich_skips_file_not_on_disk(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
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

        mock_log = mocker.patch("music_annotator._pipeline.log")

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

        mock_log = mocker.patch("music_annotator._pipeline.log")

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
        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=OSError("corrupt"))
        mock_log = mocker.patch("music_annotator._pipeline.log")

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
# CLI wiring: audit --enrich dispatches to music_annotator.enrich
# ---------------------------------------------------------------------------


class TestAuditEnrichCLI:
    """Tests for the ``audit --enrich`` CLI dispatch path.

    Verifies that ``main()`` routes ``audit --enrich`` to :func:`music_annotator.enrich` with the
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

    _AUDIT_ARGV = ["music-annotator", "audit", "/d"]

    def test_audit_enrich_dispatches_to_enrich(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --enrich calls music_annotator.enrich with dest_root, re_resolve, dry_run.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--enrich"]):
            main()
        mock_enrich.assert_called_once_with(dest_root=Path("/d"), re_resolve=False, dry_run=False, acoustid_key="")

    def test_audit_enrich_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --enrich --dry-run passes dry_run=True to enrich().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--enrich", "--dry-run"]):
            main()
        _, kwargs = mock_enrich.call_args
        assert kwargs["dry_run"] is True

    def test_audit_enrich_re_resolve_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --enrich --re-resolve passes re_resolve=True to enrich().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_enrich = mocker.patch("music_annotator.enrich")
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--enrich", "--re-resolve"]):
            main()
        _, kwargs = mock_enrich.call_args
        assert kwargs["re_resolve"] is True

    def test_audit_enrich_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --enrich exits with code 1 when enrich() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--enrich"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_audit_enrich_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --enrich exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--enrich"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_audit_enrich_parser_flags(self) -> None:
        """audit parser accepts --enrich, --re-resolve, and --dry-run flags.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["audit", "/dest", "--enrich", "--re-resolve", "--dry-run"])
        assert ns.enrich is True
        assert ns.re_resolve is True
        assert ns.dry_run is True


# ---------------------------------------------------------------------------
# _needs_enrich: missing branch coverage
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
# enrich(): journal entry with unrecognised action is ignored
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
# regroup(): enriched entry updates current_lib (lines 1813-1814)
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
# enrich(): MutagenError on tag write raises RuntimeError (lines 2110-2111)
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
        mocker.patch("music_annotator._pipeline.apply_tags_flac", side_effect=MutagenError("write failed"))

        with pytest.raises(RuntimeError, match="enrich tag write failure"):
            music_annotator.enrich(dest_root=dest_root, re_resolve=False, dry_run=False)


# ---------------------------------------------------------------------------
# enrich() — acoustid_id re-resolve via _fetch_acoustid_lookup_raw
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
            "music_annotator._pipeline._fetch_acoustid_lookup_raw",
            return_value=(["rec-mbid"], "new-acoustid-uuid"),
        )
        mocker.patch("music_annotator._pipeline._read_duration_ms", return_value=180000)

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
        mock_lookup = mocker.patch("music_annotator._pipeline._fetch_acoustid_lookup_raw")

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
            "music_annotator._pipeline._fetch_acoustid_lookup_raw",
            return_value=([], ""),
        )
        mocker.patch("music_annotator._pipeline._read_duration_ms", return_value=180000)

        music_annotator.enrich(dest_root=dest_root, re_resolve=True, dry_run=False, acoustid_key="my-api-key")

        # acoustid_id should remain unchanged (lookup returned no results)
        audio = MutagenFLAC(str(path))
        acoustid_vals = audio.get("acoustid_id") or []
        assert acoustid_vals and acoustid_vals[0] == "old-acoustid-uuid"


# ---------------------------------------------------------------------------
# audit --origin-time CLI dispatch
# ---------------------------------------------------------------------------


class TestAuditOriginTimeCLI:
    """Tests for the ``audit --origin-time`` CLI dispatch path.

    Verifies that ``main()`` routes ``audit --origin-time`` to
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

    _AUDIT_ARGV = ["music-annotator", "audit", "/d"]

    def test_origin_time_dispatches_to_enrich_origin_time(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --origin-time calls music_annotator.enrich_origin_time with dest_root and dry_run=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_fn = mocker.patch("music_annotator.enrich_origin_time")
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--origin-time"]):
            main()
        mock_fn.assert_called_once_with(dest_root=Path("/d"), dry_run=False)

    def test_origin_time_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --origin-time --dry-run passes dry_run=True to enrich_origin_time().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_fn = mocker.patch("music_annotator.enrich_origin_time")
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--origin-time", "--dry-run"]):
            main()
        _, kwargs = mock_fn.call_args
        assert kwargs["dry_run"] is True

    def test_origin_time_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --origin-time exits with code 1 when enrich_origin_time() raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich_origin_time", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--origin-time"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_origin_time_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() audit --origin-time exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.enrich_origin_time", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", [*self._AUDIT_ARGV, "--origin-time"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_origin_time_parser_flag(self) -> None:
        """audit parser accepts --origin-time flag and sets origin_time=True.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["audit", "/dest", "--origin-time"])
        assert ns.origin_time is True

    def test_origin_time_default_false(self) -> None:
        """audit parser sets origin_time=False by default.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["audit", "/dest"])
        assert ns.origin_time is False

    # ------------------------------------------------------------------
    # rebuild dispatch tests
    # ------------------------------------------------------------------

    _REBUILD_ARGV = ["music-annotator", "rebuild", "/d"]

    def test_rebuild_dispatches_to_rebuild_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() rebuild calls music_annotator.rebuild_journal with dest_root and dry_run=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_rebuild = mocker.patch("music_annotator.rebuild_journal")
        with patch.object(sys, "argv", self._REBUILD_ARGV):
            main()
        mock_rebuild.assert_called_once_with(dest_root=Path("/d"), dry_run=True)

    def test_rebuild_write_flag_passes_dry_run_false(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() rebuild --write passes dry_run=False to rebuild_journal().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_rebuild = mocker.patch("music_annotator.rebuild_journal")
        with patch.object(sys, "argv", [*self._REBUILD_ARGV, "--write"]):
            main()
        _, kwargs = mock_rebuild.call_args
        assert kwargs["dry_run"] is False

    def test_rebuild_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() rebuild exits with code 1 when rebuild_journal() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.rebuild_journal", side_effect=RuntimeError("boom"))
        with patch.object(sys, "argv", self._REBUILD_ARGV):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_rebuild_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() rebuild exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.rebuild_journal", side_effect=KeyboardInterrupt)
        with patch.object(sys, "argv", self._REBUILD_ARGV):
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
        assert ns.write is False

    def test_rebuild_parser_write_flag(self) -> None:
        """rebuild parser accepts --write flag and sets write=True.

        :param mocker: Not used — pure parser test.
        """
        parser = _build_parser()
        ns = parser.parse_args(["rebuild", "/dest", "--write"])
        assert ns.write is True


# ---------------------------------------------------------------------------
# TestUnify
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

        mocker.patch("music_annotator._pipeline.input", return_value="y")

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

        mocker.patch("music_annotator._pipeline.input", return_value="n")

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

        mock_input = mocker.patch("music_annotator._pipeline.input")

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

        mocker.patch("music_annotator._pipeline._sha256_file", side_effect=_fake_sha256)

        with pytest.raises(RuntimeError, match="unify integrity failure"):
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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=_fake_replace)

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
        with patch.object(sys, "argv", self._UNIFY_ARGV):
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
        with patch.object(sys, "argv", self._UNIFY_ARGV):
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
        with patch.object(sys, "argv", self._UNIFY_ARGV):
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

        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=RuntimeError("unreadable"))

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
            "music_annotator._pipeline._assess_collisions",
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

        mocker.patch("music_annotator._pipeline.os.replace", side_effect=OSError(errno.EACCES, "Permission denied"))

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
        mocker.patch("music_annotator._pipeline.os.replace", side_effect=OSError(errno.EXDEV, "Cross-device link"))

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

        mocker.patch("music_annotator._pipeline._sha256_file", side_effect=_fake_sha256)

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

        mocker.patch("music_annotator._pipeline._read_tags_flac", side_effect=_fake_read)

        with pytest.raises(RuntimeError, match="unify tag re-read failure"):
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
        composer component can be derived from it.

        :param composer: The per-track CEA_COMPOSER_LASTNAMES value.
        :param album_artist_sort: The ALBUMARTISTSORT value (uniform across the release).
        :returns: A :class:`TrackTags` instance.
        """
        return TrackTags(
            cea_composer_lastnames=composer,
            albumartistsort=album_artist_sort,
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
        # All moved destinations should be under the canonical top_dir
        for dest_str in moved_dests:
            dest_path = Path(dest_str)
            top_dir = dest_path.relative_to(dest_root).parts[0]
            # The canonical composer component is last_name("Goodman, Benny") = "Goodman"
            assert top_dir.startswith("Goodman"), f"Expected top_dir to start with 'Goodman', got {top_dir!r}"

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

        # All moved destinations must be under a top_dir starting with "Goodman"
        # (last_name("Goodman, Benny") == "Goodman")
        for entry in unified:
            top_dir = Path(entry.destination).relative_to(dest_root).parts[0]
            assert top_dir.startswith("Goodman"), f"Expected canonical top_dir to start with 'Goodman', got {top_dir!r}"

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

        # All moved destinations must be under a top_dir starting with "Various"
        for entry in unified:
            top_dir = Path(entry.destination).relative_to(dest_root).parts[0]
            assert top_dir.startswith("Various"), f"Expected 'Various' top_dir, got {top_dir!r}"

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

        for entry in unified:
            top_dir = Path(entry.destination).relative_to(dest_root).parts[0]
            assert top_dir.startswith("Various"), f"Expected 'Various' top_dir, got {top_dir!r}"

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

        # Non-classical release with a work link but genre "Jazz" (not "Classical")
        tags_a = TrackTags(
            cea_composer_lastnames="Goodman",
            albumartistsort="Goodman, Benny",
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

        for entry in unified:
            top_dir = Path(entry.destination).relative_to(dest_root).parts[0]
            assert top_dir.startswith("Goodman"), (
                f"Expected composer-split rule to fire for Jazz genre; got top_dir={top_dir!r}"
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

        # The composer-split rule must NOT have fired: no "Goodman, Benny" top_dir should appear
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        for entry in unified:
            top_dir = Path(entry.destination).relative_to(dest_root).parts[0]
            assert "Goodman" not in top_dir, (
                f"Composer-split rule must not fire for classical releases; got top_dir={top_dir!r}"
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
        path_mvt1 = _make_library_flac(dest_root, "Mozart - Karajan/Requiem K. 626 [rec 1962]/01 - Introitus.flac", tags_mvt1)
        path_mvt2 = _make_library_flac(
            dest_root, "Mozart; Süßmayr - Karajan/Requiem K. 626 [rec 1962]/02 - Kyrie.flac", tags_mvt2
        )
        path_mvt3 = _make_library_flac(dest_root, "Mozart - Karajan/Requiem K. 626 [rec 1962]/03 - Lacrimosa.flac", tags_mvt3)

        music_annotator.unify(dest_root=dest_root, yes=True)

        # After W2c unification, movement 2 should have moved to the "Mozart" top_dir
        journal = music_annotator.read_journal(dest_root / "music_annotator_journal.json")
        unified = [e for e in journal.entries if e.action == "unified"]
        assert len(unified) >= 1

        # The moved file (mvt2) should now be under the "Mozart" top_dir
        moved_dests = {e.destination for e in unified}
        for dest_str in moved_dests:
            dest_path = Path(dest_str)
            top_dir = dest_path.relative_to(dest_root).parts[0]
            assert top_dir.startswith("Mozart"), (
                f"W2c KAT failed: arranger-only movement landed in top_dir={top_dir!r}, expected top_dir starting with 'Mozart'"
            )

        # The "Mozart; Süßmayr" top_dir should no longer contain the movement 2 file
        assert not path_mvt2.exists()

        # Movements 1 and 3 (already at canonical path) should be untouched
        assert path_mvt1.exists()
        assert path_mvt3.exists()

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
