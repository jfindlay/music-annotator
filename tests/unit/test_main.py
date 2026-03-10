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

    def test_required_args_present(self) -> None:
        """Parser includes --release-id, --src-dir, --dest-dir as required."""
        parser = _build_parser()
        # Missing required args raises SystemExit(2)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([])
        assert exc.value.code == 2

    def test_parses_minimal_args(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Parser accepts required args and returns correct Namespace.

        :param fs: pyfakefs fixture (ensures Path exists check is independent of real FS).
        """
        parser = _build_parser()
        ns = parser.parse_args(["--release-id", "abc-123", "--src-dir", "/src", "--dest-dir", "/dest"])
        assert ns.release_id == "abc-123"
        assert ns.src_dir == Path("/src")
        assert ns.dest_dir == Path("/dest")
        assert not ns.dry_run
        assert not ns.no_fetch_rels
        assert not ns.verbose

    def test_dry_run_flag(self) -> None:
        """--dry-run sets dry_run=True."""
        parser = _build_parser()
        ns = parser.parse_args(["--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d", "--dry-run"])
        assert ns.dry_run

    def test_no_fetch_rels_flag(self) -> None:
        """--no-fetch-rels sets no_fetch_rels=True."""
        parser = _build_parser()
        ns = parser.parse_args(["--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d", "--no-fetch-rels"])
        assert ns.no_fetch_rels

    def test_verbose_flag(self) -> None:
        """-v sets verbose=True."""
        parser = _build_parser()
        ns = parser.parse_args(["--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d", "-v"])
        assert ns.verbose

    def test_custom_user_agent(self) -> None:
        """--user-agent overrides the default."""
        parser = _build_parser()
        ns = parser.parse_args(
            [
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

    def test_default_user_agent(self) -> None:
        """user_agent has a non-empty default."""
        parser = _build_parser()
        ns = parser.parse_args(["--release-id", "x", "--src-dir", "/s", "--dest-dir", "/d"])
        assert ns.user_agent


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

    def test_exits_1_when_src_dir_missing(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """main() exits with code 1 when --src-dir does not exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "--release-id", "x", "--src-dir", "/no/such", "--dest-dir", "/d"],
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_exits_0_on_success(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() exits cleanly (no SystemExit) when run succeeds.

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

    def test_exits_1_on_exception(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() exits with code 1 when run() raises an unexpected exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run", side_effect=RuntimeError("boom"))
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "--release-id", "x", "--src-dir", "/src", "--dest-dir", "/d"],
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_exits_1_on_keyboard_interrupt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """main() exits with code 1 on KeyboardInterrupt.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        self._patch_common(mocker)
        fs.create_dir("/src")
        mocker.patch("music_annotator.run", side_effect=KeyboardInterrupt)
        with patch.object(
            sys,
            "argv",
            ["music-annotator", "--release-id", "x", "--src-dir", "/src", "--dest-dir", "/d"],
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_no_fetch_rels_passed_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """--no-fetch-rels is translated to fetch_rels=False in run().

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

    def test_verbose_passed_to_configure_logging(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """--verbose causes _configure_logging to be called with verbose=True.

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
                "--release-id",
                "x",
                "--src-dir",
                "/src",
                "--dest-dir",
                "/d",
                "-v",
            ],
        ):
            main()
        mock_cfg.assert_called_once_with(True)
