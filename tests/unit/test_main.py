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
from music_annotator._pipeline_io import _read_tags_flac
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator._tags import build_dest_path
from music_annotator.models import MBRelease, MBTrack, TrackTags

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
