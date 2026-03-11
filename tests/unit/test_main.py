"""Unit tests for music_annotator.__main__."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

from music_annotator.__main__ import _build_parser, _configure_logging, main

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

    def test_apply_parses_minimal_args(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """apply subcommand accepts required args and returns correct Namespace.

        :param fs: pyfakefs fixture (ensures Path exists check is independent of real FS).
        """
        parser = _build_parser()
        ns = parser.parse_args(["apply", "--release-id", "abc-123", "--src-dir", "/src", "--dest-dir", "/dest"])
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
            parser.parse_args(["apply", "--src-dir", "/s", "--dest-dir", "/d"])
        assert exc.value.code == 2

    def test_apply_requires_src_dir(self) -> None:
        """apply exits with code 2 when --src-dir is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--release-id", "x", "--dest-dir", "/d"])
        assert exc.value.code == 2

    def test_apply_requires_dest_dir(self) -> None:
        """apply exits with code 2 when --dest-dir is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["apply", "--release-id", "x", "--src-dir", "/s"])
        assert exc.value.code == 2

    def test_apply_dry_run_flag(self) -> None:
        """apply --dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args(["apply", "--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d", "--dry-run"])
        assert ns.dry_run

    def test_apply_no_fetch_rels_flag(self) -> None:
        """apply --no-fetch-rels sets no_fetch_rels=True."""
        parser = _build_parser()
        ns = parser.parse_args(["apply", "--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d", "--no-fetch-rels"])
        assert ns.no_fetch_rels

    def test_apply_verbose_flag(self) -> None:
        """-v before the subcommand sets verbose=True."""
        parser = _build_parser()
        ns = parser.parse_args(["-v", "apply", "--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d"])
        assert ns.verbose

    def test_apply_custom_user_agent(self) -> None:
        """apply --user-agent overrides the default."""
        parser = _build_parser()
        ns = parser.parse_args(
            [
                "apply",
                "--release-id",
                "x",
                "--src-dir",
                "/s",
                "--dest-dir",
                "/d",
                "--user-agent",
                "MyApp/2.0 me@example.com",
            ]
        )
        assert ns.user_agent == "MyApp/2.0 me@example.com"

    def test_apply_default_user_agent(self) -> None:
        """apply user_agent has a non-empty default."""
        parser = _build_parser()
        ns = parser.parse_args(["apply", "--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d"])
        assert ns.user_agent

    def test_search_parses_minimal_args(self) -> None:
        """search subcommand accepts positional dirs and --dest-dir."""
        parser = _build_parser()
        ns = parser.parse_args(["search", "--dest-dir", "/dest", "/src1", "/src2"])
        assert ns.subcommand == "search"
        assert ns.dest_dir == Path("/dest")
        assert ns.src_dirs == [Path("/src1"), Path("/src2")]
        assert ns.limit == 10
        assert not ns.dry_run

    def test_search_requires_dest_dir(self) -> None:
        """search exits with code 2 when --dest-dir is missing."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "/src1"])
        assert exc.value.code == 2

    def test_search_requires_at_least_one_src_dir(self) -> None:
        """search exits with code 2 when no source directories are provided."""
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["search", "--dest-dir", "/dest"])
        assert exc.value.code == 2

    def test_search_custom_limit(self) -> None:
        """search --limit overrides the default of 10."""
        parser = _build_parser()
        ns = parser.parse_args(["search", "--dest-dir", "/d", "--limit", "5", "/src"])
        assert ns.limit == 5

    def test_search_dry_run_flag(self) -> None:
        """search --dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args(["search", "--dest-dir", "/d", "--dry-run", "/src"])
        assert ns.dry_run

    def test_search_no_fetch_rels_flag(self) -> None:
        """search --no-fetch-rels sets no_fetch_rels=True."""
        parser = _build_parser()
        ns = parser.parse_args(["search", "--dest-dir", "/d", "--no-fetch-rels", "/src"])
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

    def test_apply_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() apply exits with code 1 when --src-dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "apply", "--release-id", "x", "--src-dir", "/no/such", "--dest-dir", "/d"],
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "apply", "--release-id", "x", "--src-dir", "/src", "--dest-dir", "/d"],
        ):
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "apply", "--release-id", "x", "--src-dir", "/src", "--dest-dir", "/d"],
        ):
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
                "--no-fetch-rels",
            ],
        ):
            main()
        _, kwargs = mock_run.call_args
        assert kwargs["fetch_rels"] is False

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
            ],
        ):
            main()
        mock_cfg.assert_called_once_with(True)

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
            ["music-annotator", "search", "--dest-dir", "/d", "/no/such"],
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "search", "--dest-dir", "/dest", "/src"],
        ):
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "search", "--dest-dir", "/d", "/src"],
        ):
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "search", "--dest-dir", "/d", "/src"],
        ):
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "search", "--dest-dir", "/d", "--no-fetch-rels", "/src"],
        ):
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
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "search", "--dest-dir", "/d", "--limit", "5", "/src"],
        ):
            main()
        _, kwargs = mock_discover.call_args
        assert kwargs["limit"] == 5

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
            ["music-annotator", "search", "--dest-dir", "/d", "/src1", "/src2"],
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
            ["music-annotator", "-v", "search", "--dest-dir", "/d", "/src"],
        ):
            main()
        mock_cfg.assert_called_once_with(True)
