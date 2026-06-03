"""Unit tests for music_annotator.__main__."""

# pylint: disable=duplicate-code  # test setup patterns are intentionally similar across test modules

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

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
from music_annotator._pipeline_io import (
    _read_audio_hash_tag,
    _read_chromaprint_fp_tag,
)
from music_annotator._tagger import apply_tags_flac, apply_tags_mp3
from music_annotator.models import TrackTags
from tests.conftest import _MINIMAL_FLAC, _MINIMAL_MP3

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

    @pytest.mark.parametrize("flag", ["-V", "--version"])
    def test_version_flag_exits_0(self, flag: str, capsys: pytest.CaptureFixture[str]) -> None:
        """-V/--version prints the version string and exits with code 0.

        :param flag: The version flag to test (``-V`` or ``--version``).
        :param capsys: pytest stdout/stderr capture fixture.
        """
        parser = _build_parser()
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

    def test_repath_yes_long_flag(self) -> None:
        """repath --yes sets yes=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._REPATH_BASE, "--yes"])
        assert ns.yes

    def test_repath_yes_short_flag(self) -> None:
        """repath -y sets yes=True."""
        parser = _build_parser()
        ns = parser.parse_args([*self._REPATH_BASE, "-y"])
        assert ns.yes

    def test_repath_yes_defaults_false(self) -> None:
        """repath without --yes defaults yes=False."""
        parser = _build_parser()
        ns = parser.parse_args(self._REPATH_BASE)
        assert not ns.yes

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

    # pylint: disable-next=unused-argument
    def test_apply_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() apply exits with code 1 when src_dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        argv = ["music-annotator", "apply", "/no/such", "/d", "--release-id", "x", "--user-agent-email", "t@x.com"]
        mocker.patch.object(sys, "argv", new=argv)
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
        mocker.patch.object(sys, "argv", new=[*self._APPLY_ARGV, "--dry-run"])
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
        mocker.patch.object(sys, "argv", new=self._APPLY_ARGV)
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
        mocker.patch.object(sys, "argv", new=self._APPLY_ARGV)
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
        mocker.patch.object(sys, "argv", new=[*self._APPLY_ARGV, "--no-fetch-rels"])
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
        mocker.patch.object(sys, "argv", new=[*self._APPLY_ARGV, "--no-cache"])
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
        mocker.patch.object(sys, "argv", new=[*self._APPLY_ARGV, "--disc", "2"])
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
        mocker.patch.object(sys, "argv", new=self._APPLY_ARGV)
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
        mocker.patch.object(
            sys,
            "argv",
            new=[
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
        )
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
        mocker.patch.object(
            sys,
            "argv",
            new=[
                "music-annotator",
                "-v",
                "apply",
                "/src",
                "/d",
                "--release-id",
                "x",
                "--user-agent-email",
                "t@x.com",
            ],
        )
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
        mocker.patch.object(sys, "argv", new=[*self._APPLY_ARGV, "--delete"])
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
        mocker.patch.object(sys, "argv", new=self._APPLY_ARGV)
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
        mocker.patch.object(sys, "argv", new=[*self._APPLY_ARGV, "--delete", "--dry-run"])
        main()
        mock_prompt.assert_not_called()

    # ------------------------------------------------------------------
    # search subcommand
    # ------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_search_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() search exits with code 1 when src_dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch.object(sys, "argv", new=["music-annotator", "search", "/no/such", "/d", "--user-agent-email", "t@x.com"])
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
        mocker.patch.object(sys, "argv", new=self._SEARCH_ARGV)
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
        mocker.patch.object(sys, "argv", new=self._SEARCH_ARGV)
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
        mocker.patch.object(sys, "argv", new=self._SEARCH_ARGV)
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
        mocker.patch.object(sys, "argv", new=[*self._SEARCH_ARGV, "--no-fetch-rels"])
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
        mocker.patch.object(sys, "argv", new=[*self._SEARCH_ARGV, "--no-cache"])
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
        mocker.patch.object(sys, "argv", new=[*self._SEARCH_ARGV, "--limit", "5"])
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
        mocker.patch.object(
            sys,
            "argv",
            new=[
                "music-annotator",
                "search",
                "/src",
                "/d",
                "--user-agent-app",
                "MyApp/2.0",
                "--user-agent-email",
                "me@example.com",
            ],
        )
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
        mocker.patch.object(sys, "argv", new=self._SEARCH_ARGV)
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
        argv = ["music-annotator", "search", "/src1", "/src2", "/d", "--user-agent-email", "t@x.com"]
        mocker.patch.object(sys, "argv", new=argv)
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
        argv = ["music-annotator", "search", "/src1", "/src2", "/d", "--user-agent-email", "t@x.com"]
        mocker.patch.object(sys, "argv", new=argv)
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
        mocker.patch.object(sys, "argv", new=[*self._SEARCH_ARGV, "--delete"])
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
        mocker.patch.object(sys, "argv", new=self._SEARCH_ARGV)
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
        argv = ["music-annotator", "-v", "search", "/src", "/d", "--user-agent-email", "t@x.com"]
        mocker.patch.object(sys, "argv", new=argv)
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
        argv = ["music-annotator", "-C", "search", "/src", "/d", "--user-agent-email", "t@x.com"]
        mocker.patch.object(sys, "argv", new=argv)
        main()
        mock_cfg.assert_called_once_with(False, no_color=True)

    # ------------------------------------------------------------------
    # prune subcommand
    # ------------------------------------------------------------------

    # pylint: disable-next=unused-argument
    def test_prune_dispatches_to_prune_sources(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() prune calls prune_sources with correct arguments.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch("music_annotator.prune_sources")
        mocker.patch.object(sys, "argv", new=self._PRUNE_ARGV)
        main()
        mock_prune.assert_called_once_with(src_dir=Path("/src"), dest_root=Path("/d"), yes=False)  # single src

    # pylint: disable-next=unused-argument
    def test_prune_yes_flag_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() prune -y passes yes=True to prune_sources.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch("music_annotator.prune_sources")
        mocker.patch.object(sys, "argv", new=[*self._PRUNE_ARGV, "-y"])
        main()
        _, kwargs = mock_prune.call_args
        assert kwargs["yes"] is True

    # pylint: disable-next=unused-argument
    def test_prune_logs_error_and_continues_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() prune logs the error and completes normally (no exit 1) when prune_sources raises.

        Each src_dir is processed independently; an error on one does not abort the others.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.prune_sources", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._PRUNE_ARGV)
        main()  # should not raise or sys.exit(1)

    # pylint: disable-next=unused-argument
    def test_prune_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() prune exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.prune_sources", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._PRUNE_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_prune_multiple_src_dirs_calls_prune_sources_for_each(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() prune calls prune_sources once per src_dir.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch("music_annotator.prune_sources")
        mocker.patch.object(sys, "argv", new=["music-annotator", "prune", "/src1", "/src2", "/d"])
        main()
        assert mock_prune.call_count == 2
        calls = mock_prune.call_args_list
        assert calls[0].kwargs["src_dir"] == Path("/src1")
        assert calls[1].kwargs["src_dir"] == Path("/src2")
        assert calls[0].kwargs["dest_root"] == Path("/d")
        assert calls[1].kwargs["dest_root"] == Path("/d")

    # pylint: disable-next=unused-argument
    def test_prune_continues_after_error_on_first_src(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() prune logs error and continues to next src_dir when one raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_prune = mocker.patch(
            "music_annotator.prune_sources",
            side_effect=[RuntimeError("boom"), None],
        )
        mocker.patch.object(sys, "argv", new=["music-annotator", "prune", "/src1", "/src2", "/d"])
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

    # pylint: disable-next=unused-argument
    def test_repath_dispatches_to_repath(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() repath calls music_annotator.repath with dest_root, dry_run=False, yes=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_repath = mocker.patch("music_annotator.repath")
        mocker.patch.object(sys, "argv", new=self._REPATH_ARGV)
        main()
        mock_repath.assert_called_once_with(dest_root=Path("/d"), dry_run=False, yes=False)

    # pylint: disable-next=unused-argument
    def test_repath_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() repath --dry-run passes dry_run=True to repath().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_repath = mocker.patch("music_annotator.repath")
        mocker.patch.object(sys, "argv", new=[*self._REPATH_ARGV, "--dry-run"])
        main()
        _, kwargs = mock_repath.call_args
        assert kwargs["dry_run"] is True

    # pylint: disable-next=unused-argument
    def test_repath_yes_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() repath --yes passes yes=True to repath().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_repath = mocker.patch("music_annotator.repath")
        mocker.patch.object(sys, "argv", new=[*self._REPATH_ARGV, "--yes"])
        main()
        _, kwargs = mock_repath.call_args
        assert kwargs["yes"] is True

    # pylint: disable-next=unused-argument
    def test_repath_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() repath exits with code 1 when repath() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.repath", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._REPATH_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_repath_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() repath exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.repath", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._REPATH_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # ------------------------------------------------------------------
    # regroup dispatch tests
    # ------------------------------------------------------------------

    _REGROUP_ARGV = ["music-annotator", "regroup", "/d"]

    # pylint: disable-next=unused-argument
    def test_regroup_dispatches_to_regroup(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() regroup calls music_annotator.regroup with dest_root, yes=False, dry_run=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_regroup = mocker.patch("music_annotator.regroup")
        mocker.patch.object(sys, "argv", new=self._REGROUP_ARGV)
        main()
        mock_regroup.assert_called_once_with(dest_root=Path("/d"), yes=False, dry_run=False)

    # pylint: disable-next=unused-argument
    def test_regroup_dry_run_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() regroup --dry-run passes dry_run=True to regroup().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_regroup = mocker.patch("music_annotator.regroup")
        mocker.patch.object(sys, "argv", new=[*self._REGROUP_ARGV, "--dry-run"])
        main()
        _, kwargs = mock_regroup.call_args
        assert kwargs["dry_run"] is True

    # pylint: disable-next=unused-argument
    def test_regroup_yes_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() regroup --yes passes yes=True to regroup().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mock_regroup = mocker.patch("music_annotator.regroup")
        mocker.patch.object(sys, "argv", new=[*self._REGROUP_ARGV, "--yes"])
        main()
        _, kwargs = mock_regroup.call_args
        assert kwargs["yes"] is True

    # pylint: disable-next=unused-argument
    def test_regroup_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() regroup exits with code 1 when regroup() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.regroup", side_effect=RuntimeError("boom"))
        mocker.patch.object(sys, "argv", new=self._REGROUP_ARGV)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    # pylint: disable-next=unused-argument
    def test_regroup_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() regroup exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        mocker.patch("music_annotator.regroup", side_effect=KeyboardInterrupt)
        mocker.patch.object(sys, "argv", new=self._REGROUP_ARGV)
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

    # pylint: disable-next=unused-argument
    def test_src_dir_already_deleted_logs_info(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
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

    # pylint: disable-next=unused-argument
    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
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

    # pylint: disable-next=unused-argument
    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_chromaprint_fp_tag returns "" when the file read raises an exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/lib/broken.flac")
        mocker.patch("music_annotator._pipeline_io.FLAC", side_effect=OSError("corrupt"))

        assert _read_chromaprint_fp_tag(path) == ""


# ---------------------------------------------------------------------------
