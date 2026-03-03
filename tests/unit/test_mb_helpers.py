"""Unit tests for MusicBrainz API helper functions in music_annotator."""

from __future__ import annotations

from typing import Any

import musicbrainzngs as mb
import pytest
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    _mb_retry,
    fetch_cover_art,
    fetch_recording_detail,
    fetch_release,
    fetch_work_detail,
)

# ---------------------------------------------------------------------------
# _mb_retry
# ---------------------------------------------------------------------------


class TestMbRetry:
    """Tests for the _mb_retry decorator."""

    def test_success_on_first_attempt(self, mocker: MockerFixture) -> None:
        """Returns immediately when the decorated function succeeds on first call.

        Args:
            mocker: pytest-mock fixture.
        """
        inner = mocker.MagicMock(return_value={"release": {}})
        inner.__name__ = "mock_fn"

        @_mb_retry
        def wrapped() -> dict[str, Any]:
            return inner()  # type: ignore[no-any-return]

        result = wrapped()
        assert result == {"release": {}}
        inner.assert_called_once()

    def test_retries_on_503(self, mocker: MockerFixture) -> None:
        """Retries on 503 error and succeeds on subsequent attempt.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        err = mb.ResponseError(cause=Exception("503 Service Unavailable"))
        inner = mocker.MagicMock(side_effect=[err, {"ok": True}])
        inner.__name__ = "mock_fn"

        @_mb_retry
        def wrapped() -> dict[str, Any]:
            return inner()  # type: ignore[no-any-return]

        result = wrapped()
        assert result == {"ok": True}
        assert inner.call_count == 2

    def test_retries_on_429(self, mocker: MockerFixture) -> None:
        """Retries on 429 rate-limit error.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        err = mb.ResponseError(cause=Exception("429 Too Many Requests"))
        inner = mocker.MagicMock(side_effect=[err, {"ok": True}])
        inner.__name__ = "mock_fn"

        @_mb_retry
        def wrapped() -> dict[str, Any]:
            return inner()  # type: ignore[no-any-return]

        result = wrapped()
        assert result == {"ok": True}

    def test_raises_immediately_on_non_retryable_error(self, mocker: MockerFixture) -> None:
        """Raises ResponseError immediately on a non-retryable status code.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        err = mb.ResponseError(cause=Exception("404 Not Found"))
        inner = mocker.MagicMock(side_effect=err)
        inner.__name__ = "mock_fn"

        @_mb_retry
        def wrapped() -> None:
            inner()

        with pytest.raises(mb.ResponseError):
            wrapped()
        inner.assert_called_once()

    def test_raises_runtime_error_after_all_retries(self, mocker: MockerFixture) -> None:
        """Raises RuntimeError after all 6 retry attempts are exhausted.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        err = mb.ResponseError(cause=Exception("503 Service Unavailable"))
        inner = mocker.MagicMock(side_effect=err)
        inner.__name__ = "mock_fn"

        @_mb_retry
        def wrapped() -> None:
            inner()

        with pytest.raises(RuntimeError, match="after retries"):
            wrapped()
        assert inner.call_count == 6


# ---------------------------------------------------------------------------
# fetch_release
# ---------------------------------------------------------------------------


class TestFetchRelease:
    """Tests for fetch_release."""

    def test_returns_mbrelease(self, mocker: MockerFixture) -> None:
        """Returns an MBRelease populated from the 'release' key of the MB response.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_release_by_id",
            return_value={"release": {"id": "rel-1", "title": "Test"}},
        )
        result = fetch_release("rel-1")
        assert result.id == "rel-1"
        assert result.title == "Test"

    def test_passes_includes(self, mocker: MockerFixture) -> None:
        """Calls get_release_by_id with the required includes list.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mock_api = mocker.patch(
            "music_annotator.mb.get_release_by_id",
            return_value={"release": {}},
        )
        fetch_release("rel-1")
        _, kwargs = mock_api.call_args
        assert "recordings" in kwargs["includes"]
        assert "artists" in kwargs["includes"]


# ---------------------------------------------------------------------------
# fetch_recording_detail
# ---------------------------------------------------------------------------


class TestFetchRecordingDetail:
    """Tests for fetch_recording_detail."""

    def test_returns_mbrecording(self, mocker: MockerFixture) -> None:
        """Returns an MBRecording populated from the 'recording' key of the MB response.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_recording_by_id",
            return_value={"recording": {"id": "rec-1", "title": "Adagio"}},
        )
        result = fetch_recording_detail("rec-1")
        assert result.id == "rec-1"

    def test_returns_empty_mbrecording_on_missing_key(self, mocker: MockerFixture) -> None:
        """Returns a default MBRecording when 'recording' key is absent from response.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_recording_by_id",
            return_value={},
        )
        result = fetch_recording_detail("rec-1")
        assert result.id == ""
        assert result.title == ""


# ---------------------------------------------------------------------------
# fetch_cover_art
# ---------------------------------------------------------------------------


class TestFetchCoverArt:
    """Tests for fetch_cover_art."""

    def test_returns_jpeg_cover_art(self, mocker: MockerFixture) -> None:
        """Returns CoverArt with image/jpeg when data starts with FF D8.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        mocker.patch("music_annotator.mb.get_image_front", return_value=jpeg_bytes)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert result.mime == "image/jpeg"

    def test_returns_png_cover_art(self, mocker: MockerFixture) -> None:
        """Returns CoverArt with image/png when data starts with 89PNG.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mocker.patch("music_annotator.mb.get_image_front", return_value=png_bytes)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert result.mime == "image/png"

    def test_falls_back_to_release_group_on_404(self, mocker: MockerFixture) -> None:
        """Falls back to release-group art when release returns 404.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        mocker.patch(
            "music_annotator.mb.get_image_front",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        mocker.patch("music_annotator.mb.get_release_group_image_front", return_value=jpeg_bytes)
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert result.available

    def test_returns_empty_on_non_404_error(self, mocker: MockerFixture) -> None:
        """Returns empty CoverArt when release returns a non-404 error.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_image_front",
            side_effect=mb.ResponseError(cause=Exception("500 Internal Server Error")),
        )
        result = fetch_cover_art("rel-1")
        assert not result.available

    def test_returns_empty_when_no_release_group_id(self, mocker: MockerFixture) -> None:
        """Returns empty CoverArt when 404 and no release_group_id provided.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_image_front",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        result = fetch_cover_art("rel-1", release_group_id="")
        assert not result.available

    def test_returns_empty_when_release_group_also_fails(self, mocker: MockerFixture) -> None:
        """Returns empty CoverArt when both release and release-group art fail.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        err = mb.ResponseError(cause=Exception("404 Not Found"))
        mocker.patch("music_annotator.mb.get_image_front", side_effect=err)
        mocker.patch(
            "music_annotator.mb.get_release_group_image_front",
            side_effect=mb.ResponseError(cause=Exception("500 error")),
        )
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert not result.available

    def test_returns_empty_when_raw_is_falsy(self, mocker: MockerFixture) -> None:
        """Returns empty CoverArt when get_image_front returns empty bytes.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch("music_annotator.mb.get_image_front", return_value=b"")
        result = fetch_cover_art("rel-1")
        assert not result.available

    def test_infer_mime_fallback_to_jpeg(self, mocker: MockerFixture) -> None:
        """Returns CoverArt with image/jpeg MIME when image data is not JPEG or PNG.

        The _infer_mime helper defaults to 'image/jpeg' for unknown image types.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        # Data starts with neither b'\xff\xd8' (JPEG) nor b'\x89PNG' (PNG)
        bmp_bytes = b"BM" + b"\x00" * 100
        mocker.patch("music_annotator.mb.get_image_front", return_value=bmp_bytes)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert result.mime == "image/jpeg"

    def test_release_group_returns_empty_raw(self, mocker: MockerFixture) -> None:
        """Returns empty CoverArt when release-group image fetch returns empty bytes.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_image_front",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        mocker.patch("music_annotator.mb.get_release_group_image_front", return_value=b"")
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert not result.available


# ---------------------------------------------------------------------------
# fetch_work_detail
# ---------------------------------------------------------------------------


class TestFetchWorkDetail:
    """Tests for fetch_work_detail and its cache."""

    def setup_method(self) -> None:
        """Clear the module-level work cache before each test."""
        music_annotator._WORK_CACHE.clear()  # pylint: disable=protected-access

    def test_fetches_and_returns_work(self, mocker: MockerFixture) -> None:
        """Fetches work from API and returns an MBWork instance.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_work_by_id",
            return_value={"work": {"id": "w1", "title": "Fontane di Roma"}},
        )
        result = fetch_work_detail("w1")
        assert result.title == "Fontane di Roma"

    def test_caches_result(self, mocker: MockerFixture) -> None:
        """Second call returns cached result without calling the API again.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mock_api = mocker.patch(
            "music_annotator.mb.get_work_by_id",
            return_value={"work": {"id": "w1", "title": "Cached Work"}},
        )
        fetch_work_detail("w1")
        fetch_work_detail("w1")
        mock_api.assert_called_once()

    def test_cache_hit_returns_correct_value(self, mocker: MockerFixture) -> None:
        """Cached value is returned on second call.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch(
            "music_annotator.mb.get_work_by_id",
            return_value={"work": {"id": "w2", "title": "Symphony"}},
        )
        first = fetch_work_detail("w2")
        second = fetch_work_detail("w2")
        assert first is second

    def test_returns_empty_mbwork_on_missing_key(self, mocker: MockerFixture) -> None:
        """Returns a default MBWork when 'work' key is absent from API response.

        Args:
            mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator.time.sleep")
        mocker.patch("music_annotator.mb.get_work_by_id", return_value={})
        result = fetch_work_detail("w-missing")
        assert result.id == ""
        assert result.title == ""
