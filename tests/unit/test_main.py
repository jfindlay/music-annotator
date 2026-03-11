"""Unit tests for music_annotator.__main__."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

from music_annotator.__main__ import (
    _DEFAULT_USER_AGENT_APP,
    _VERSION,
    _build_parser,
    _configure_logging,
    _resolve_path,
    main,
)

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
        _configure_logging(verbose=False)
        _, kwargs = mock_basic.call_args
        assert kwargs["level"] == logging.INFO

    def test_structlog_configure_called(self, mocker: MockerFixture) -> None:
        """structlog.configure is called exactly once.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mock_cfg = mocker.patch("music_annotator.__main__.structlog.configure")
        _configure_logging(verbose=False)
        mock_cfg.assert_called_once()

    def test_no_color_calls_configure_color_disabled(self, mocker: MockerFixture) -> None:
        """no_color=True calls music_annotator.configure_color(enabled=False).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mock_cc = mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        _configure_logging(verbose=False, no_color=True)
        mock_cc.assert_called_once_with(enabled=False)

    def test_color_enabled_by_default(self, mocker: MockerFixture) -> None:
        """no_color defaults to False, so configure_color is called with enabled=True.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mock_cc = mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        _configure_logging(verbose=False)
        mock_cc.assert_called_once_with(enabled=True)

    def test_no_color_disables_console_renderer_colors(self, mocker: MockerFixture) -> None:
        """no_color=True passes colors=False to structlog ConsoleRenderer.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.__main__.logging.basicConfig")
        mocker.patch("music_annotator.__main__.structlog.configure")
        mocker.patch("music_annotator.__main__.music_annotator.configure_color")
        mock_renderer = mocker.patch("music_annotator.__main__.structlog.dev.ConsoleRenderer")
        _configure_logging(verbose=False, no_color=True)
        mock_renderer.assert_called_once_with(colors=False)

    def test_color_on_enables_console_renderer_colors(self, mocker: MockerFixture) -> None:
        """no_color=False (default) passes colors=True to structlog ConsoleRenderer.

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

    # Minimal common args that satisfy both --dest-dir and --user-agent-email requirements.
    _APPLY_BASE = [
        "apply",
        "--release-id",
        "abc-123",
        "--src-dir",
        "/src",
        "--dest-dir",
        "/dest",
        "--user-agent-email",
        "t@x.com",
    ]
    _SEARCH_BASE = ["search", "--dest-dir", "/dest", "--user-agent-email", "t@x.com", "/src1"]

    def test_apply_parses_minimal_args(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """apply subcommand accepts required args and returns correct Namespace.

        :param fs: pyfakefs fixture (ensures Path exists check is independent of real FS).
        """
        parser = _build_parser()
        ns = parser.parse_args(self._APPLY_BASE)
        assert ns.subcommand == "apply"
        assert ns.release_id == "abc-123"
        assert ns.src_dir == Path("/src")
        assert ns.dest_dir == Path("/dest")
        assert not ns.dry_run
        assert not ns.no_fetch_rels
        assert not ns.verbose

    def test_apply_requires_release_id(self) -> None:
        """apply exits with code 2 when --release-id is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--src-dir", "/s", "--dest-dir", "/d", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_apply_requires_src_dir(self) -> None:
        """apply exits with code 2 when --src-dir is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--release-id", "x", "--dest-dir", "/d", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_apply_requires_dest_dir(self) -> None:
        """apply exits with code 2 when --dest-dir is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--release-id", "x", "--src-dir", "/s", "--user-agent-email", "t@x.com"])
        assert exc.value.code == 2

    def test_apply_requires_user_agent_email(self) -> None:
        """apply exits with code 2 when --user-agent-email is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d"])
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

    def test_search_parses_minimal_args(self) -> None:
        """search subcommand accepts positional dirs and --dest-dir."""
        parser = _build_parser()
        ns = parser.parse_args(["search", "--dest-dir", "/dest", "--user-agent-email", "t@x.com", "/src1", "/src2"])
        assert ns.subcommand == "search"
        assert ns.dest_dir == Path("/dest")
        assert ns.src_dirs == [Path("/src1"), Path("/src2")]
        assert ns.limit == 10
        assert not ns.dry_run

    def test_search_requires_dest_dir(self) -> None:
        """search exits with code 2 when --dest-dir is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "--user-agent-email", "t@x.com", "/src1"])
        assert exc.value.code == 2

    def test_search_requires_user_agent_email(self) -> None:
        """search exits with code 2 when --user-agent-email is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "--dest-dir", "/dest", "/src1"])
        assert exc.value.code == 2

    def test_search_requires_at_least_one_src_dir(self) -> None:
        """search exits with code 2 when no source directories are provided."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "--dest-dir", "/dest", "--user-agent-email", "t@x.com"])
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

    # ------------------------------------------------------------------
    # apply subcommand
    # ------------------------------------------------------------------

    # Minimal argv lists that satisfy all required args for each subcommand.
    _APPLY_ARGV = [
        "music-annotator",
        "apply",
        "--release-id",
        "x",
        "--src-dir",
        "/src",
        "--dest-dir",
        "/d",
        "--user-agent-email",
        "t@x.com",
    ]
    _SEARCH_ARGV = ["music-annotator", "search", "--dest-dir", "/d", "--user-agent-email", "t@x.com", "/src"]

    def test_apply_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() apply exits with code 1 when --src-dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "apply",
                "--release-id",
                "x",
                "--src-dir",
                "/no/such",
                "--dest-dir",
                "/d",
                "--user-agent-email",
                "t@x.com",
            ],
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_apply_exits_0_on_success(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() apply exits cleanly (no SystemExit) when run succeeds.

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
                "--release-id",
                "x",
                "--src-dir",
                "/src",
                "--dest-dir",
                "/dest",
                "--user-agent-email",
                "t@x.com",
                "--dry-run",
            ],
        ):
            main()  # should not raise
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
                "--release-id",
                "x",
                "--src-dir",
                "/src",
                "--dest-dir",
                "/d",
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
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "-v",
                "apply",
                "--release-id",
                "x",
                "--src-dir",
                "/src",
                "--dest-dir",
                "/d",
                "--user-agent-email",
                "t@x.com",
            ],
        ):
            main()
        mock_cfg.assert_called_once_with(True, no_color=False)

    # ------------------------------------------------------------------
    # search subcommand
    # ------------------------------------------------------------------

    def test_search_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() search exits with code 1 when a source directory does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "search",
                "--dest-dir",
                "/d",
                "--user-agent-email",
                "t@x.com",
                "/no/such",
            ],
        ):
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
                "--dest-dir",
                "/d",
                "--user-agent-app",
                "MyApp/2.0",
                "--user-agent-email",
                "me@example.com",
                "/src",
            ],
        ):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["user_agent"] == "MyApp/2.0 me@example.com"

    def test_search_multiple_src_dirs_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Multiple positional source directories are forwarded to discover().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src1")
        fs.create_dir("/src2")
        mock_discover = mocker.patch("music_annotator.discover")
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "search",
                "--dest-dir",
                "/d",
                "--user-agent-email",
                "t@x.com",
                "/src1",
                "/src2",
            ],
        ):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["src_dirs"] == [Path("/src1"), Path("/src2")]

    def test_search_verbose_passed_to_configure_logging(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """-v before search causes _configure_logging to be called with verbose=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mock_cfg = mocker.patch("music_annotator.__main__._configure_logging")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.discover")
        fs.create_dir("/src")
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "-v",
                "search",
                "--dest-dir",
                "/d",
                "--user-agent-email",
                "t@x.com",
                "/src",
            ],
        ):
            main()
        mock_cfg.assert_called_once_with(True, no_color=False)

    def test_no_color_passed_to_configure_logging(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """-C before apply causes _configure_logging to be called with no_color=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mock_cfg = mocker.patch("music_annotator.__main__._configure_logging")
        mocker.patch("music_annotator.__main__.structlog.get_logger")
        mocker.patch("music_annotator.discover")
        fs.create_dir("/src")
        with patch.object(
            sys,
            "argv",
            [
                "music-annotator",
                "-C",
                "search",
                "--dest-dir",
                "/d",
                "--user-agent-email",
                "t@x.com",
                "/src",
            ],
        ):
            main()
        mock_cfg.assert_called_once_with(False, no_color=True)
