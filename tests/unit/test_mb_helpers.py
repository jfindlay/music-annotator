"""Unit tests for MusicBrainz API helper functions in music_annotator."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import musicbrainzngs as mb
import musicbrainzngs.mbxml as _mbxml
import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    fetch_cover_art,
    fetch_recording_detail,
    fetch_release,
    fetch_work_detail,
    write_transaction_log,
)
from music_annotator._mb_api import _infer_mime, _mb_call, _mb_retry, _patched_parse_recording, _sidecar_filename
from music_annotator._pipeline_io import _check_collisions
from music_annotator.models import MBRecording, TransactionEntry

# ---------------------------------------------------------------------------
# _mb_retry
# ---------------------------------------------------------------------------


class TestMbRetry:
    """Tests for the _mb_retry decorator."""

    def test_success_on_first_attempt(self, mocker: MockerFixture) -> None:
        """Returns immediately when the decorated function succeeds on first call.

        :param mocker: pytest-mock fixture.
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

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
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

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
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

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
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

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
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

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_release_by_id",
            return_value={"release": {"id": "rel-1", "title": "Test"}},
        )
        result = fetch_release("rel-1")
        assert result.id == "rel-1"
        assert result.title == "Test"

    def test_passes_includes(self, mocker: MockerFixture) -> None:
        """Calls get_release_by_id with the required includes list.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_api = mocker.patch(
            "music_annotator._mb_api.mb.get_release_by_id",
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

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_recording_by_id",
            return_value={"recording": {"id": "rec-1", "title": "Adagio"}},
        )
        result = fetch_recording_detail("rec-1")
        assert result.id == "rec-1"

    def test_returns_empty_mbrecording_on_missing_key(self, mocker: MockerFixture) -> None:
        """Returns a default MBRecording when 'recording' key is absent from response.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_recording_by_id",
            return_value={},
        )
        result = fetch_recording_detail("rec-1")
        assert result.id == ""
        assert result.title == ""


# ---------------------------------------------------------------------------
# fetch_cover_art
# ---------------------------------------------------------------------------


def _make_listing(*entries: tuple[str, str]) -> dict[str, Any]:
    """Build a fake CAA image-list response dict.

    :param entries: Pairs of (image_id, type_string), e.g. ``("111", "Front")``.
    :returns: A dict matching the shape of ``mb.get_image_list`` JSON output.
    """
    return {
        "images": [
            {"id": img_id, "types": [img_type], "front": img_type == "Front", "back": img_type == "Back"}
            for img_id, img_type in entries
        ]
    }


class TestFetchCoverArt:
    """Tests for fetch_cover_art."""

    def test_returns_jpeg_front(self, mocker: MockerFixture) -> None:
        """Front image with JPEG magic bytes is placed in CoverArt.front with mime image/jpeg.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("111", "Front")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg_bytes)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert len(result.front) == 1
        assert result.mime == "image/jpeg"

    def test_returns_png_front(self, mocker: MockerFixture) -> None:
        """Front image with PNG magic bytes gets mime image/png.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("222", "Front")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=png_bytes)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert result.mime == "image/png"

    def test_infer_mime_fallback_to_jpeg(self, mocker: MockerFixture) -> None:
        """Unknown magic bytes default to image/jpeg MIME type.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        bmp_bytes = b"BM" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("333", "Front")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=bmp_bytes)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert result.mime == "image/jpeg"

    def test_all_four_types_collected(self, mocker: MockerFixture) -> None:
        """Front, Back, Booklet, and Medium images are each placed in the correct list.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 10
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            return_value=_make_listing(("1", "Front"), ("2", "Back"), ("3", "Booklet"), ("4", "Medium")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1")
        assert len(result.front) == 1
        assert len(result.back) == 1
        assert len(result.booklet) == 1
        assert len(result.medium) == 1

    def test_multiple_booklet_pages(self, mocker: MockerFixture) -> None:
        """Multiple Booklet images are all collected into result.booklet.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 10
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            return_value=_make_listing(("10", "Booklet"), ("11", "Booklet"), ("12", "Booklet")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1")
        assert len(result.booklet) == 3
        assert not result.front

    def test_truly_unknown_type_fetched_into_unknown_bucket(self, mocker: MockerFixture) -> None:
        """Images with type strings not in the 18 known CAA types are fetched into CoverArt.unknown.

        The warning is still logged so the operator knows an unrecognised type was encountered,
        but the image is preserved rather than silently discarded.

        :param mocker: pytest-mock fixture.
        """
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            return_value=_make_listing(("99", "CustomUnknown")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1")
        assert result.available
        assert len(result.unknown) == 1
        assert result.unknown[0].filename == "unknown.jpg"

    def test_unknown_type_bucket_field_in_log(self, mocker: MockerFixture) -> None:
        """The bucket field in the cover_art_image_fetched log matches the destination bucket.

        :param mocker: pytest-mock fixture.
        """
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            return_value=_make_listing(("42", "Back")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        log_calls: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._mb_api.log.info",
            side_effect=lambda event, **kw: log_calls.append({"event": event, **kw}),
        )
        fetch_cover_art("rel-1")
        fetched = [c for c in log_calls if c["event"] == "cover_art_image_fetched"]
        assert fetched
        assert fetched[0]["bucket"] == "back"

    def test_spine_type_fetched_as_sidecar(self, mocker: MockerFixture) -> None:
        """Images with type 'Spine' are now fetched and stored in CoverArt.spine.

        :param mocker: pytest-mock fixture.
        """
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("99", "Spine")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1")
        assert len(result.spine) == 1
        assert result.spine[0].filename == "spine.jpg"

    def test_tray_type_fetched_as_sidecar(self, mocker: MockerFixture) -> None:
        """Images with type 'Tray' are now fetched and stored in CoverArt.tray.

        :param mocker: pytest-mock fixture.
        """
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("100", "Tray")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1")
        assert len(result.tray) == 1
        assert result.tray[0].filename == "tray.jpg"

    def test_multi_type_image_fetched_once_shared_filename(self, mocker: MockerFixture) -> None:
        """An image with types ['Back', 'Spine'] is fetched once; both buckets share the filename.

        :param mocker: pytest-mock fixture.
        """
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.time.sleep")
        # Listing has one image with two types
        listing = {
            "images": [{"types": ["Back", "Spine"], "id": "77", "image": "https://caa/77"}],
            "release": "https://mb/release/r1",
        }
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1")
        # Fetched only once despite appearing in two buckets
        assert mock_get.call_count == 1
        # Both buckets populated, same filename
        assert len(result.back) == 1
        assert len(result.spine) == 1
        assert result.back[0].filename == "back.jpg"
        assert result.spine[0].filename == "back.jpg"  # secondary bucket reuses primary filename
        # Same CoverImage object
        assert result.back[0] is result.spine[0]

    def test_multi_type_primary_fetch_failure_skips_secondary(self, mocker: MockerFixture) -> None:
        """When a multi-type image fails to fetch in its primary bucket, secondary buckets also skip it.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        listing = {
            "images": [{"types": ["Back", "Spine"], "id": "77", "image": "https://caa/77"}],
            "release": "https://mb/release/r1",
        }
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        mocker.patch("music_annotator._mb_api.mb.get_image", side_effect=mb.ResponseError(cause=Exception("503")))
        result = fetch_cover_art("rel-1")
        assert result.back == []
        assert result.spine == []

    def test_image_fetch_error_skipped(self, mocker: MockerFixture) -> None:
        """A ResponseError on a single image fetch is logged and that image is skipped.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("55", "Front")))
        mocker.patch(
            "music_annotator._mb_api.mb.get_image",
            side_effect=mb.ResponseError(cause=Exception("503 error")),
        )
        result = fetch_cover_art("rel-1")
        assert not result.available

    def test_image_fetch_returns_empty_bytes_skipped(self, mocker: MockerFixture) -> None:
        """An image that returns empty bytes is not added to the result.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("66", "Front")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=b"")
        result = fetch_cover_art("rel-1")
        assert not result.available

    def test_listing_404_falls_back_to_release_group(self, mocker: MockerFixture) -> None:
        """When the release has no CAA listing (404), falls back to release-group front.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_release_group_image_front", return_value=jpeg_bytes)
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert result.available
        assert len(result.front) == 1

    def test_listing_404_no_release_group_returns_empty(self, mocker: MockerFixture) -> None:
        """When the release has no CAA listing and no release_group_id, returns empty.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        result = fetch_cover_art("rel-1", release_group_id="")
        assert not result.available

    def test_listing_non_404_error_returns_empty(self, mocker: MockerFixture) -> None:
        """A non-404 listing error returns empty CoverArt (no fallback attempted).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("500 Internal Server Error")),
        )
        result = fetch_cover_art("rel-1")
        assert not result.available

    def test_release_group_fallback_fails_returns_empty(self, mocker: MockerFixture) -> None:
        """When both release listing and release-group fallback fail, returns empty.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        mocker.patch(
            "music_annotator._mb_api.mb.get_release_group_image_front",
            side_effect=mb.ResponseError(cause=Exception("500 error")),
        )
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert not result.available

    def test_release_group_fallback_returns_empty_bytes(self, mocker: MockerFixture) -> None:
        """When release-group fallback returns empty bytes, result is empty.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_release_group_image_front", return_value=b"")
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert not result.available

    def test_listing_missing_id_skipped(self, mocker: MockerFixture) -> None:
        """Image entries with no 'id' field are silently skipped.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        listing = {"images": [{"types": ["Front"], "front": True, "back": False}]}
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image")
        result = fetch_cover_art("rel-1")
        mock_get.assert_not_called()
        assert not result.available

    def test_listing_non_list_images_returns_empty(self, mocker: MockerFixture) -> None:
        """A listing response where 'images' is not a list returns empty CoverArt.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value={"images": "bad"})
        result = fetch_cover_art("rel-1")
        assert not result.available

    def test_image_entry_with_non_list_types_skipped(self, mocker: MockerFixture) -> None:
        """An image entry where the 'types' field is not a list is silently skipped.

        This covers the ``continue`` branch at the isinstance(types_raw, list) guard.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        listing = {"images": [{"id": "77", "types": "Front", "front": True}]}
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image")
        result = fetch_cover_art("rel-1")
        mock_get.assert_not_called()
        assert not result.available

    def test_back_booklet_medium_fetch_returns_empty_skipped(self, mocker: MockerFixture) -> None:
        """When back/booklet/medium image fetches return empty bytes, those entries are skipped.

        This covers the ``if image:`` False branches for back, booklet, and medium.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            return_value=_make_listing(("2", "Back"), ("3", "Booklet"), ("4", "Medium")),
        )
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=b"")
        result = fetch_cover_art("rel-1")
        assert not result.back
        assert not result.booklet
        assert not result.medium


# ---------------------------------------------------------------------------
# fetch_work_detail
# ---------------------------------------------------------------------------


class TestFetchWorkDetail:
    """Tests for fetch_work_detail and its cache."""

    def setup_method(self) -> None:
        """Clear the module-level work cache before each test."""
        music_annotator._mb_api._WORK_CACHE.clear()  # pylint: disable=protected-access

    def test_fetches_and_returns_work(self, mocker: MockerFixture) -> None:
        """Fetches work from API and returns an MBWork instance.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value={"work": {"id": "w1", "title": "Fontane di Roma"}},
        )
        result = fetch_work_detail("w1")
        assert result.title == "Fontane di Roma"

    def test_caches_result(self, mocker: MockerFixture) -> None:
        """Second call returns cached result without calling the API again.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_api = mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value={"work": {"id": "w1", "title": "Cached Work"}},
        )
        fetch_work_detail("w1")
        fetch_work_detail("w1")
        mock_api.assert_called_once()

    def test_cache_hit_returns_correct_value(self, mocker: MockerFixture) -> None:
        """Cached value is returned on second call.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value={"work": {"id": "w2", "title": "Symphony"}},
        )
        first = fetch_work_detail("w2")
        second = fetch_work_detail("w2")
        assert first is second

    def test_returns_empty_mbwork_on_missing_key(self, mocker: MockerFixture) -> None:
        """Returns a default MBWork when 'work' key is absent from API response.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_work_by_id", return_value={})
        result = fetch_work_detail("w-missing")
        assert result.id == ""
        assert result.title == ""


# ---------------------------------------------------------------------------
# _check_collisions
# ---------------------------------------------------------------------------


class TestCheckCollisions:
    """Tests for _check_collisions helper."""

    def test_no_collisions_when_all_new(self, fs: FakeFilesystem) -> None:
        """Returns empty list when none of the destination files exist.

        :param fs: pyfakefs fixture.
        """
        paths = [Path("/dest/a.flac"), Path("/dest/b.flac")]
        fs.create_dir("/dest")
        assert _check_collisions(paths) == []

    def test_returns_existing_files(self, fs: FakeFilesystem) -> None:
        """Returns only the paths that already exist.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        fs.create_file("/dest/exists.flac")
        paths = [Path("/dest/exists.flac"), Path("/dest/new.flac")]
        result = _check_collisions(paths)
        assert result == [Path("/dest/exists.flac")]

    def test_all_existing(self, fs: FakeFilesystem) -> None:
        """Returns all paths when every destination file already exists.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        fs.create_file("/dest/a.flac")
        fs.create_file("/dest/b.flac")
        paths = [Path("/dest/a.flac"), Path("/dest/b.flac")]
        result = _check_collisions(paths)
        assert result == paths

    def test_empty_list(self) -> None:
        """Returns empty list for empty input.

        No filesystem access needed.
        """
        assert _check_collisions([]) == []


# ---------------------------------------------------------------------------
# write_transaction_log
# ---------------------------------------------------------------------------


def _entry(action: str = "copied", src: str = "/src/01.flac", dest: str = "/dest/01.flac") -> TransactionEntry:
    """Build a minimal TransactionEntry for testing.

    :param action: The action string.
    :param src: Source path string.
    :param dest: Destination path string.
    :returns: A :class:`~music_annotator.models.TransactionEntry`.
    """
    return TransactionEntry(
        timestamp="2026-01-01T00:00:00+00:00",
        release_id="rel-1",
        source=src,
        destination=dest,
        action=action,
    )


class TestWriteTransactionLog:
    """Tests for write_transaction_log."""

    def test_creates_new_journal_file(self, fs: FakeFilesystem) -> None:
        """Creates the journal file when it does not yet exist.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        entry = _entry()
        write_transaction_log(journal, [entry])
        assert journal.exists()
        data = json.loads(journal.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["action"] == "copied"
        assert data[0]["source"] == "/src/01.flac"

    def test_appends_to_existing_journal(self, fs: FakeFilesystem) -> None:
        """New entries are appended to existing journal contents.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        first = _entry(action="copied", src="/src/01.flac", dest="/dest/01.flac")
        write_transaction_log(journal, [first])

        second = _entry(action="skipped", src="/src/02.flac", dest="/dest/02.flac")
        write_transaction_log(journal, [second])

        data = json.loads(journal.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["action"] == "copied"
        assert data[1]["action"] == "skipped"

    def test_corrupt_journal_is_reset(self, fs: FakeFilesystem) -> None:
        """A corrupt journal file is overwritten with only the new entries.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        journal.write_text("NOT VALID JSON", encoding="utf-8")
        entry = _entry()
        write_transaction_log(journal, [entry])
        data = json.loads(journal.read_text(encoding="utf-8"))
        assert len(data) == 1

    def test_non_list_json_is_reset(self, fs: FakeFilesystem) -> None:
        """A journal containing valid JSON but not a list is overwritten.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        journal.write_text('{"not": "a list"}', encoding="utf-8")
        entry = _entry()
        write_transaction_log(journal, [entry])
        data = json.loads(journal.read_text(encoding="utf-8"))
        assert len(data) == 1

    def test_multiple_entries_written(self, fs: FakeFilesystem) -> None:
        """Multiple entries are all written in a single call.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        entries = [_entry(src=f"/src/0{i}.flac", dest=f"/dest/0{i}.flac") for i in range(1, 4)]
        write_transaction_log(journal, entries)
        data = json.loads(journal.read_text(encoding="utf-8"))
        assert len(data) == 3

    def test_dry_run_entries_preserved(self, fs: FakeFilesystem) -> None:
        """Entries with action='dry_run' are written and preserved correctly.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        entry = _entry(action="dry_run")
        write_transaction_log(journal, [entry])
        data = json.loads(journal.read_text(encoding="utf-8"))
        assert data[0]["action"] == "dry_run"


# ---------------------------------------------------------------------------
# _mb_call
# ---------------------------------------------------------------------------


class TestMbCall:
    """Tests for the _mb_call rate-limit helper."""

    def test_calls_fn_and_returns_result(self, mocker: MockerFixture) -> None:
        """_mb_call invokes fn() and returns its result.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        fn = mocker.MagicMock(return_value={"key": "value"})
        result = _mb_call(fn)
        fn.assert_called_once()
        assert result == {"key": "value"}

    def test_sleeps_one_second_after_call(self, mocker: MockerFixture) -> None:
        """_mb_call sleeps exactly 1 second after fn() returns.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        _mb_call(lambda: None)
        mock_sleep.assert_called_once_with(1)

    def test_propagates_exception_without_sleeping(self, mocker: MockerFixture) -> None:
        """_mb_call propagates exceptions from fn() and does not sleep.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")

        def _boom() -> None:
            raise ValueError("api error")

        with pytest.raises(ValueError, match="api error"):
            _mb_call(_boom)
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# _patch_mbxml_parse_recording  (musicbrainzngs workaround)
# ---------------------------------------------------------------------------


class TestPatchMbxmlParseRecording:
    """Tests for the _patch_mbxml_parse_recording workaround that recovers first-release-date."""

    _NS = "http://musicbrainz.org/ns/mmd-2.0#"

    def _make_recording_element(self, title: str = "Test", first_release_date: str | None = None) -> ET.Element:
        """Build a minimal <recording> XML element for use with _mbxml.parse_recording.

        :param title: Recording title text.
        :param first_release_date: If provided, adds a <first-release-date> child element.
        :returns: An :class:`xml.etree.ElementTree.Element` representing a recording.
        """
        rec = ET.Element(f"{{{self._NS}}}recording")
        rec.set("id", "test-id")
        title_el = ET.SubElement(rec, f"{{{self._NS}}}title")
        title_el.text = title
        if first_release_date is not None:
            frd_el = ET.SubElement(rec, f"{{{self._NS}}}first-release-date")
            frd_el.text = first_release_date
        return rec

    def test_first_release_date_extracted_when_present(self) -> None:
        """The patch recovers first-release-date from the XML element when present."""
        rec_el = self._make_recording_element(first_release_date="1990")
        result = _patched_parse_recording(rec_el)
        assert result.get("first-release-date") == "1990"

    def test_first_release_date_full_date_extracted(self) -> None:
        """The patch recovers a full date string (not just year) verbatim."""
        rec_el = self._make_recording_element(first_release_date="1990-04-03")
        result = _patched_parse_recording(rec_el)
        assert result.get("first-release-date") == "1990-04-03"

    def test_first_release_date_absent_when_not_in_xml(self) -> None:
        """first-release-date is absent from the result when not in the XML."""
        rec_el = self._make_recording_element()
        result = _patched_parse_recording(rec_el)
        assert "first-release-date" not in result

    def test_original_fields_still_parsed(self) -> None:
        """The patch does not break existing fields like title."""
        rec_el = self._make_recording_element(title="Symphony No. 5", first_release_date="1963")
        result = _patched_parse_recording(rec_el)
        assert result.get("title") == "Symphony No. 5"
        assert result.get("first-release-date") == "1963"


# ---------------------------------------------------------------------------
# MBRecording.first_release_date
# ---------------------------------------------------------------------------


class TestMBRecordingFirstReleaseDate:
    """Tests for the first_release_date field on MBRecording."""

    def test_first_release_date_populated_from_dict(self) -> None:
        """MBRecording.first_release_date is populated from the first-release-date key."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "first-release-date": "1963"})
        assert rec.first_release_date == "1963"

    def test_first_release_date_defaults_to_empty(self) -> None:
        """MBRecording.first_release_date defaults to empty string when absent."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T"})
        assert rec.first_release_date == ""


# ---------------------------------------------------------------------------
# _infer_mime
# ---------------------------------------------------------------------------


class TestInferMime:
    """Tests for _infer_mime magic-byte detection."""

    def test_jpeg_magic(self) -> None:
        """JPEG magic bytes produce image/jpeg."""
        assert _infer_mime(b"\xff\xd8\xff\xe0...") == "image/jpeg"

    def test_png_magic(self) -> None:
        """PNG magic bytes produce image/png."""
        assert _infer_mime(b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_pdf_magic(self) -> None:
        """PDF magic bytes produce application/pdf."""
        assert _infer_mime(b"%PDF-1.4 content") == "application/pdf"

    def test_tiff_le_magic(self) -> None:
        """TIFF little-endian magic bytes produce image/tiff."""
        assert _infer_mime(b"II\x2a\x00extra") == "image/tiff"

    def test_tiff_be_magic(self) -> None:
        """TIFF big-endian magic bytes produce image/tiff."""
        assert _infer_mime(b"MM\x00\x2aextra") == "image/tiff"

    def test_unknown_defaults_to_jpeg(self) -> None:
        """Unrecognised magic bytes default to image/jpeg."""
        assert _infer_mime(b"\x00\x00\x00\x00") == "image/jpeg"


# ---------------------------------------------------------------------------
# _sidecar_filename
# ---------------------------------------------------------------------------


class TestSidecarFilename:
    """Tests for _sidecar_filename."""

    def test_single_front(self) -> None:
        """Single front image → cover.jpg."""
        assert _sidecar_filename("front", 1, 1, "image/jpeg") == "cover.jpg"

    def test_single_back_jpeg(self) -> None:
        """Single back JPEG → back.jpg (no index)."""
        assert _sidecar_filename("back", 1, 1, "image/jpeg") == "back.jpg"

    def test_single_back_pdf(self) -> None:
        """Single back PDF → back.pdf (no index)."""
        assert _sidecar_filename("back", 1, 1, "application/pdf") == "back.pdf"

    def test_multiple_booklet_pdf(self) -> None:
        """Multiple booklet PDFs get 1-based index suffix."""
        assert _sidecar_filename("booklet", 2, 1, "application/pdf") == "booklet-1.pdf"
        assert _sidecar_filename("booklet", 2, 2, "application/pdf") == "booklet-2.pdf"

    def test_multiple_booklet_jpeg(self) -> None:
        """Multiple booklet JPEGs get 1-based index suffix."""
        assert _sidecar_filename("booklet", 3, 2, "image/jpeg") == "booklet-2.jpg"

    def test_medium_single(self) -> None:
        """Single medium image → medium.jpg."""
        assert _sidecar_filename("medium", 1, 1, "image/jpeg") == "medium.jpg"

    def test_png_extension(self) -> None:
        """PNG MIME type produces .png extension."""
        assert _sidecar_filename("back", 1, 1, "image/png") == "back.png"

    def test_tiff_extension(self) -> None:
        """TIFF MIME type produces .tiff extension."""
        assert _sidecar_filename("booklet", 1, 1, "image/tiff") == "booklet.tiff"

    def test_unknown_mime_extension(self) -> None:
        """Unknown MIME type produces .bin extension."""
        assert _sidecar_filename("medium", 1, 1, "application/octet-stream") == "medium.bin"


# ---------------------------------------------------------------------------
# fetch_cover_art — release-group fallback two-fetch
# ---------------------------------------------------------------------------


class TestFetchCoverArtReleaseGroupFallback:
    """Tests for the release-group fallback dual-fetch in fetch_cover_art."""

    def _make_jpeg_ctx(self, mocker: MockerFixture, data: bytes) -> Any:
        """Build a mock context manager that returns ``data`` from ``.read()``.

        :param mocker: pytest-mock fixture.
        :param data: Bytes to return from the context manager.
        :returns: A mock usable as a context manager.
        """
        ctx = mocker.MagicMock()
        ctx.__enter__ = mocker.MagicMock(return_value=ctx)
        ctx.__exit__ = mocker.MagicMock(return_value=False)
        ctx.read = mocker.MagicMock(return_value=data)
        return ctx

    def test_release_group_fallback_fetches_500px_and_original(self, mocker: MockerFixture) -> None:
        """When the release has no listing, both 500px and original are fetched for front.

        :param mocker: pytest-mock fixture.
        """
        jpeg_500 = b"\xff\xd8" + b"\x00" * 100
        jpeg_orig = b"\xff\xd8" + b"\x00" * 2000
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=mb.ResponseError("404 Not Found"))
        mock_rg = mocker.patch(
            "music_annotator._mb_api.mb.get_release_group_image_front",
            side_effect=[jpeg_500, jpeg_orig],
        )
        mocker.patch("music_annotator._mb_api.time.sleep")
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert len(result.front) == 1
        assert result.front[0].data == jpeg_500
        assert len(result.front_full) == 1
        assert result.front_full[0].data == jpeg_orig
        assert result.front_full[0].filename == "cover.jpg"
        assert mock_rg.call_count == 2

    def test_release_group_fallback_500_error_still_fetches_original(self, mocker: MockerFixture) -> None:
        """If 500px fetch fails, original fetch still proceeds.

        :param mocker: pytest-mock fixture.
        """
        jpeg_orig = b"\xff\xd8" + b"\x00" * 2000
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=mb.ResponseError("404 Not Found"))
        mocker.patch(
            "music_annotator._mb_api.mb.get_release_group_image_front",
            side_effect=[mb.ResponseError("503"), jpeg_orig],
        )
        mocker.patch("music_annotator._mb_api.time.sleep")
        result = fetch_cover_art("rel-1", release_group_id="rg-1")
        assert result.front == []
        assert len(result.front_full) == 1
        assert result.front_full[0].filename == "cover.jpg"
