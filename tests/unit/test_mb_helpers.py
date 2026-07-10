"""Unit tests for MusicBrainz API helper functions in music_annotator."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import xml.etree.ElementTree as ET
from http.client import BadStatusLine, HTTPException, HTTPMessage
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import musicbrainzngs as mb
import musicbrainzngs.mbxml as _mbxml
import musicbrainzngs.musicbrainz as mzmz
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
from music_annotator._mb_api import (
    _cover_art_cache_dir,
    _cover_art_cache_key,
    _fetch_acoustid_lookup_raw,
    _get_bottom_work,
    _infer_mime,
    _mb_call,
    _mb_retry,
    _metadata_cache_dir,
    _patched_parse_recording,
    _patched_safe_read,
    _sidecar_filename,
    fetch_acoustid_id,
    fetch_acoustid_lookup,
)
from music_annotator._pipeline_io import _check_collisions
from music_annotator.models import JSON, MBAttribute, MBRecording, MBWork, TransactionEntry

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
        def wrapped() -> dict[str, object]:
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
        def wrapped() -> dict[str, object]:
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
        def wrapped() -> dict[str, object]:
            return inner()  # type: ignore[no-any-return]

        result = wrapped()
        assert result == {"ok": True}

    def test_retries_on_307(self, mocker: MockerFixture) -> None:
        """Retries on 307 (redirect loop) and succeeds on subsequent attempt.

        307 is a transient CAA/Internet Archive condition and is included in the retry set alongside
        503/429/500.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        inner = mocker.MagicMock(side_effect=[err, {"ok": True}])
        inner.__name__ = "mock_fn"

        @_mb_retry
        def wrapped() -> dict[str, object]:
            return inner()  # type: ignore[no-any-return]

        result = wrapped()
        assert result == {"ok": True}
        assert inner.call_count == 2

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
# fetch_cover_art — retry behaviour
# ---------------------------------------------------------------------------


class TestFetchCoverArtRetry:
    """Tests for retry behaviour in fetch_cover_art — listing, image, and release-group fetches."""

    def test_listing_307_retried_and_succeeds(self, mocker: MockerFixture) -> None:
        """A transient 307 on the image listing is retried; success on the second attempt returns art.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg = b"\xff\xd8" + b"\x00" * 100
        listing = {"images": [{"types": ["Front"], "id": "1", "image": "https://caa/1"}]}
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=[err, listing])
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg)
        result = fetch_cover_art("rel-1", no_cache=True)
        assert result.available
        assert len(result.front) == 1

    def test_listing_307_exhausts_retries_raises(self, mocker: MockerFixture) -> None:
        """Six consecutive 307 errors on the listing exhaust _mb_retry and raise RuntimeError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=err)
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", no_cache=True)

    def test_image_fetch_307_retried_and_succeeds(self, mocker: MockerFixture) -> None:
        """A transient 307 on an individual image fetch is retried; success on second attempt returns art.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("1", "Front")))
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        mocker.patch("music_annotator._mb_api.mb.get_image", side_effect=[err, jpeg, err, jpeg])
        result = fetch_cover_art("rel-1", no_cache=True)
        assert result.available
        assert len(result.front) == 1

    def test_image_fetch_307_exhausts_retries_raises(self, mocker: MockerFixture) -> None:
        """Six consecutive 307 errors on an image fetch exhaust _mb_retry and raise RuntimeError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("1", "Front")))
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        mocker.patch("music_annotator._mb_api.mb.get_image", side_effect=err)
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", no_cache=True)

    def test_rg_image_fetch_307_retried_and_succeeds(self, mocker: MockerFixture) -> None:
        """A transient 307 on the release-group fallback image is retried; success on second attempt returns art.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        jpeg = b"\xff\xd8" + b"\x00" * 100
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        mocker.patch("music_annotator._mb_api.mb.get_release_group_image_front", side_effect=[err, jpeg, err, jpeg])
        result = fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)
        assert result.available
        assert len(result.front) == 1

    def test_rg_image_fetch_307_exhausts_retries_raises(self, mocker: MockerFixture) -> None:
        """Six consecutive 307 errors on the release-group fallback exhaust _mb_retry and raise RuntimeError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        err = mb.ResponseError(cause=Exception("307 Temporary Redirect"))
        mocker.patch("music_annotator._mb_api.mb.get_release_group_image_front", side_effect=err)
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)

    def test_image_fetch_non_retryable_non_404_raises_response_error(self, mocker: MockerFixture) -> None:
        """A non-retryable, non-404 error on an image fetch (e.g. 400) re-raises ResponseError immediately.

        400 is not in the _mb_retry retry set and not a 404, so it passes straight through _mb_retry
        and then through the ``if '404' in str(exc): ... raise`` branch in _fetch_raw.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("1", "Front")))
        err = mb.ResponseError(cause=Exception("400 Bad Request"))
        mocker.patch("music_annotator._mb_api.mb.get_image", side_effect=err)
        with pytest.raises(mb.ResponseError):
            fetch_cover_art("rel-1", no_cache=True)

    def test_rg_image_fetch_non_retryable_non_404_raises_response_error(self, mocker: MockerFixture) -> None:
        """A non-retryable, non-404 error on the RG fallback (e.g. 400) re-raises ResponseError immediately.

        400 is not in the _mb_retry retry set and not a 404, so _fetch_rg_image re-raises it.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        err = mb.ResponseError(cause=Exception("400 Bad Request"))
        mocker.patch("music_annotator._mb_api.mb.get_release_group_image_front", side_effect=err)
        with pytest.raises(mb.ResponseError):
            fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)

    def test_listing_non_retryable_non_404_raises_runtime_error(self, mocker: MockerFixture) -> None:
        """A non-retryable, non-404 listing error (e.g. 400) re-raises ResponseError wrapped as RuntimeError.

        400 is not in the _mb_retry retry set and not a 404, so _mb_retry re-raises it immediately as
        ResponseError which the ``case _:`` arm then re-raises as RuntimeError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        err = mb.ResponseError(cause=Exception("400 Bad Request"))
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=err)
        with pytest.raises(RuntimeError, match="cover art listing failed"):
            fetch_cover_art("rel-1", no_cache=True)


# ---------------------------------------------------------------------------
# fetch_acoustid_id
# ---------------------------------------------------------------------------


class TestFetchAcoustidId:
    """Tests for fetch_acoustid_id HTTP error handling."""

    def test_4xx_returns_empty_immediately(self, mocker: MockerFixture) -> None:
        """A 4xx HTTP error returns '' immediately without retrying.

        4xx errors are permanent client errors; retrying them is wasteful and incorrect.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("https://acoustid.org/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        mock_urlopen = mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            side_effect=http_exc,
        )
        result = fetch_acoustid_id("rec-1")
        assert result == ""
        # Only one attempt — no retry on 4xx.
        mock_urlopen.assert_called_once()

    def test_5xx_retried(self, mocker: MockerFixture) -> None:
        """A 5xx HTTP error is retried up to three times before returning ''.

        5xx errors are transient server errors; retry is appropriate.  After all three attempts fail,
        the function returns '' rather than raising.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("https://acoustid.org/", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        mock_urlopen = mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            side_effect=http_exc,
        )
        result = fetch_acoustid_id("rec-1")
        assert result == ""
        # All three attempts were made before giving up.
        assert mock_urlopen.call_count == 3


# ---------------------------------------------------------------------------
# fetch_acoustid_lookup / _fetch_acoustid_lookup_raw
# ---------------------------------------------------------------------------


class TestFetchAcoustidLookup:
    """Tests for fetch_acoustid_lookup and _fetch_acoustid_lookup_raw HTTP error handling."""

    def _make_resp(self, mocker: MockerFixture, body: bytes) -> MagicMock:
        """Build a mock context manager that returns ``body`` from ``.read()``.

        :param mocker: pytest-mock fixture.
        :param body: Bytes to return from the context manager's read().
        :returns: A mock usable as a context manager.
        """
        ctx: MagicMock = mocker.MagicMock()
        ctx.__enter__ = mocker.MagicMock(return_value=ctx)
        ctx.__exit__ = mocker.MagicMock(return_value=False)
        ctx.read = mocker.MagicMock(return_value=body)
        return ctx

    def test_acoustid_lookup_seeds_release_search(self, mocker: MockerFixture) -> None:
        """KAT: fetch_acoustid_lookup returns score-ordered, flattened recording MBIDs.

        Mocks urlopen to return a valid /v2/lookup JSON response with two results.
        Asserts the returned list is score-ordered and flattened, and that time.sleep(1)
        is called for the polite delay.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps(
            {
                "status": "ok",
                "results": [
                    {"id": "acoustid-uuid-1", "score": 0.95, "recordings": [{"id": "rec-mbid-1"}, {"id": "rec-mbid-2"}]},
                    {"id": "acoustid-uuid-2", "score": 0.80, "recordings": [{"id": "rec-mbid-3"}]},
                ],
            }
        ).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == ["rec-mbid-1", "rec-mbid-2", "rec-mbid-3"]
        mock_sleep.assert_called_once_with(1)

    def test_empty_api_key_returns_empty_no_network(self, mocker: MockerFixture) -> None:
        """api_key == '' returns [] without any network call.

        :param mocker: pytest-mock fixture.
        """
        mock_urlopen = mocker.patch("music_annotator._mb_api.urllib.request.urlopen")
        result = fetch_acoustid_lookup("fp", 180, "")
        assert result == []
        mock_urlopen.assert_not_called()

    def test_empty_fingerprint_returns_empty_no_network(self, mocker: MockerFixture) -> None:
        """fingerprint == '' returns [] without any network call.

        :param mocker: pytest-mock fixture.
        """
        mock_urlopen = mocker.patch("music_annotator._mb_api.urllib.request.urlopen")
        result = fetch_acoustid_lookup("", 180, "key")
        assert result == []
        mock_urlopen.assert_not_called()

    def test_zero_duration_returns_empty_no_network(self, mocker: MockerFixture) -> None:
        """duration_s <= 0 returns [] without any network call.

        :param mocker: pytest-mock fixture.
        """
        mock_urlopen = mocker.patch("music_annotator._mb_api.urllib.request.urlopen")
        result = fetch_acoustid_lookup("fp", 0, "key")
        assert result == []
        mock_urlopen.assert_not_called()

    def test_negative_duration_returns_empty_no_network(self, mocker: MockerFixture) -> None:
        """duration_s < 0 returns [] without any network call.

        :param mocker: pytest-mock fixture.
        """
        mock_urlopen = mocker.patch("music_annotator._mb_api.urllib.request.urlopen")
        result = fetch_acoustid_lookup("fp", -1, "key")
        assert result == []
        mock_urlopen.assert_not_called()

    def test_4xx_returns_empty_after_single_attempt(self, mocker: MockerFixture) -> None:
        """A 4xx HTTP error returns [] after a single attempt (no retry).

        4xx errors are permanent client errors; retrying them is wasteful and incorrect.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("https://api.acoustid.org/", 400, "Bad Request", {}, None)  # type: ignore[arg-type]
        mock_urlopen = mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            side_effect=http_exc,
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []
        mock_urlopen.assert_called_once()

    def test_5xx_retries_three_times_returns_empty(self, mocker: MockerFixture) -> None:
        """A 5xx HTTP error is retried up to three times before returning [].

        5xx errors are transient server errors; retry is appropriate.  After all three attempts fail,
        the function returns [] rather than raising.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("https://api.acoustid.org/", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        mock_urlopen = mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            side_effect=http_exc,
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []
        assert mock_urlopen.call_count == 3
        # Polite delay (sleep(1)) is called on success; backoff sleeps are called on 5xx.
        # On 5xx, time.sleep is called with 2**attempt for each failed attempt (0, 1, 2).
        sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
        assert 1 in sleep_args or 2 in sleep_args  # backoff sleeps present

    def test_oserror_retries_returns_empty(self, mocker: MockerFixture) -> None:
        """An OSError is retried and returns [] after all attempts.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_urlopen = mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            side_effect=OSError("network error"),
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []
        assert mock_urlopen.call_count == 3

    def test_malformed_json_returns_empty(self, mocker: MockerFixture) -> None:
        """Malformed JSON in the response returns [] without retrying.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, b"not valid json {{{"),
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []

    def test_status_not_ok_returns_empty(self, mocker: MockerFixture) -> None:
        """A response with status != 'ok' returns [].

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps({"status": "error", "error": {"code": 3, "message": "Invalid fingerprint"}}).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []

    def test_empty_results_list_returns_empty(self, mocker: MockerFixture) -> None:
        """A response with an empty results list returns [].

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps({"status": "ok", "results": []}).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []

    def test_result_without_recordings_key_handled_gracefully(self, mocker: MockerFixture) -> None:
        """A result entry with no 'recordings' key is handled gracefully (returns []).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps({"status": "ok", "results": [{"id": "uuid", "score": 0.9}]}).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        result = fetch_acoustid_lookup("fp", 180, "key")
        assert result == []

    def test_fetch_acoustid_lookup_raw_returns_top_uuid(self, mocker: MockerFixture) -> None:
        """_fetch_acoustid_lookup_raw returns (recording_mbids, top_acoustid_uuid).

        The top UUID is the id of the highest-scoring result (results[0]["id"]).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps(
            {
                "status": "ok",
                "results": [
                    {"id": "acoustid-uuid-1", "score": 0.95, "recordings": [{"id": "rec-mbid-1"}, {"id": "rec-mbid-2"}]},
                    {"id": "acoustid-uuid-2", "score": 0.80, "recordings": [{"id": "rec-mbid-3"}]},
                ],
            }
        ).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        recording_mbids, top_uuid = _fetch_acoustid_lookup_raw("fp", 180, "key")
        assert recording_mbids == ["rec-mbid-1", "rec-mbid-2", "rec-mbid-3"]
        assert top_uuid == "acoustid-uuid-1"

    def test_non_dict_result_item_skipped(self, mocker: MockerFixture) -> None:
        """Non-dict items in the results list are skipped (covers _score else branch and loop continue).

        A results list containing a non-dict item alongside a valid dict item exercises:
        - The ``_score`` function's ``return 0.0`` else branch (line 913).
        - The ``if not isinstance(result, dict): continue`` branch (line 923).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps(
            {
                "status": "ok",
                "results": [
                    "not-a-dict",
                    {"id": "acoustid-uuid-1", "score": 0.95, "recordings": [{"id": "rec-mbid-1"}]},
                ],
            }
        ).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        recording_mbids, top_uuid = _fetch_acoustid_lookup_raw("fp", 180, "key")
        # The non-dict item is skipped; only the valid dict item contributes
        assert "rec-mbid-1" in recording_mbids
        assert top_uuid == "acoustid-uuid-1"

    def test_non_dict_recording_item_skipped(self, mocker: MockerFixture) -> None:
        """Non-dict items in a result's recordings list are skipped (covers line 929 continue).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps(
            {
                "status": "ok",
                "results": [
                    {"id": "acoustid-uuid-1", "score": 0.95, "recordings": ["not-a-dict", {"id": "rec-mbid-1"}]},
                ],
            }
        ).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        recording_mbids, _ = _fetch_acoustid_lookup_raw("fp", 180, "key")
        # The non-dict recording is skipped; only the valid dict recording contributes
        assert recording_mbids == ["rec-mbid-1"]

    def test_empty_rec_id_skipped(self, mocker: MockerFixture) -> None:
        """Recording entries with empty id are skipped (covers line 931->927 branch).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        body = json.dumps(
            {
                "status": "ok",
                "results": [
                    {"id": "acoustid-uuid-1", "score": 0.95, "recordings": [{"id": ""}, {"id": "rec-mbid-1"}]},
                ],
            }
        ).encode()
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            return_value=self._make_resp(mocker, body),
        )
        recording_mbids, _ = _fetch_acoustid_lookup_raw("fp", 180, "key")
        # The empty-id recording is skipped
        assert recording_mbids == ["rec-mbid-1"]


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

    def setup_method(self) -> None:
        """Redirect XDG_CACHE_HOME to a temporary directory so tests do not touch the real disk cache."""
        self._cache_tmpdir = tempfile.mkdtemp()  # pylint: disable=attribute-defined-outside-init
        self._orig_xdg = os.environ.get("XDG_CACHE_HOME")  # pylint: disable=attribute-defined-outside-init
        os.environ["XDG_CACHE_HOME"] = self._cache_tmpdir

    def teardown_method(self) -> None:
        """Restore XDG_CACHE_HOME and remove the temporary cache directory."""
        if self._orig_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._orig_xdg
        shutil.rmtree(self._cache_tmpdir, ignore_errors=True)

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


def _make_listing(*entries: tuple[str, str]) -> JSON:
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
        # Fetched only once despite appearing in two buckets
        assert mock_get.call_count == 1
        # Both buckets populated, same filename
        assert len(result.back) == 1
        assert len(result.spine) == 1
        assert result.back[0].filename == "back.jpg"
        assert result.spine[0].filename == "back.jpg"  # secondary bucket reuses primary filename
        # Same CoverImage object
        assert result.back[0] is result.spine[0]

    def test_multi_type_primary_fetch_failure_raises(self, mocker: MockerFixture) -> None:
        """When a multi-type image fetch fails all retries, RuntimeError propagates to the caller.

        The inner _call() is decorated with @_mb_retry, so a persistent 503 error exhausts all six
        retry attempts and raises RuntimeError rather than mb.ResponseError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        listing = {
            "images": [{"types": ["Back", "Spine"], "id": "77", "image": "https://caa/77"}],
            "release": "https://mb/release/r1",
        }
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        mocker.patch("music_annotator._mb_api.mb.get_image", side_effect=mb.ResponseError(cause=Exception("503")))
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", no_cache=True)

    def test_multi_type_primary_empty_bytes_skips_secondary(self, mocker: MockerFixture) -> None:
        """When a multi-type image returns empty bytes in the primary bucket, secondary buckets skip it.

        This exercises the ``if existing is not None`` False branch in the non-front bucket loop
        where the coverid is already recorded as ``None`` (empty-bytes result) from a primary fetch.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        listing = {
            "images": [{"types": ["Back", "Spine"], "id": "77", "image": "https://caa/77"}],
            "release": "https://mb/release/r1",
        }
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        # Return empty bytes so _fetch_raw returns None without raising
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=b"")
        result = fetch_cover_art("rel-1", no_cache=True)
        assert result.back == []
        assert result.spine == []

    def test_image_fetch_error_raises(self, mocker: MockerFixture) -> None:
        """A persistent non-404 ResponseError on an image fetch raises RuntimeError after retries.

        The inner _call() in _fetch_raw is decorated with @_mb_retry, so a persistent 503 error
        exhausts all six retry attempts and raises RuntimeError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("55", "Front")))
        mocker.patch(
            "music_annotator._mb_api.mb.get_image",
            side_effect=mb.ResponseError(cause=Exception("503 error")),
        )
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", no_cache=True)

    def test_front_image_404_returns_empty(self, mocker: MockerFixture) -> None:
        """A 404 on a front image fetch is treated as unavailable; result has no front image.

        This models the CAA data-integrity condition where the MB listing references an image
        that has since been deleted from object storage.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("55", "Front")))
        mocker.patch(
            "music_annotator._mb_api.mb.get_image",
            side_effect=mb.ResponseError(cause=Exception("HTTP Error 404: NOT FOUND")),
        )
        result = fetch_cover_art("rel-1", no_cache=True)
        assert not result.available
        assert result.front == []

    def test_non_front_image_404_returns_empty(self, mocker: MockerFixture) -> None:
        """A 404 on a non-front (back) image fetch is treated as unavailable; result has no back image.

        Non-front images use the same ``_fetch_raw`` path as front images, so the same 404 handling
        applies.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("77", "Back")))
        mocker.patch(
            "music_annotator._mb_api.mb.get_image",
            side_effect=mb.ResponseError(cause=Exception("HTTP Error 404: NOT FOUND")),
        )
        result = fetch_cover_art("rel-1", no_cache=True)
        assert not result.available
        assert result.back == []

    def test_release_group_fallback_404_returns_empty(self, mocker: MockerFixture) -> None:
        """A 404 on the release-group fallback is treated as unavailable; result has no front image.

        This is the exact scenario from the error trace: listing returns 404 triggering the RG
        fallback, then the RG front image URL also returns 404.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        mocker.patch(
            "music_annotator._mb_api.mb.get_release_group_image_front",
            side_effect=mb.ResponseError(cause=Exception("HTTP Error 404: NOT FOUND")),
        )
        result = fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)
        assert not result.available
        assert result.front == []

    def test_image_fetch_returns_empty_bytes_skipped(self, mocker: MockerFixture) -> None:
        """An image that returns empty bytes is not added to the result.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=_make_listing(("66", "Front")))
        mocker.patch("music_annotator._mb_api.mb.get_image", return_value=b"")
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", release_group_id="", no_cache=True)
        assert not result.available

    def test_listing_non_404_error_raises(self, mocker: MockerFixture) -> None:
        """A persistent non-404 listing error raises RuntimeError after all retries are exhausted.

        The inner _get_image_list() is decorated with @_mb_retry, so a persistent 500 error exhausts
        all six retry attempts; _mb_retry raises RuntimeError("MB request failed after retries: ...")
        which propagates unchanged through the ResponseError handler.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_image_list",
            side_effect=mb.ResponseError(cause=Exception("500 Internal Server Error")),
        )
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", no_cache=True)

    def test_release_group_fallback_fails_raises(self, mocker: MockerFixture) -> None:
        """When the release-group fallback call fails all retries, RuntimeError propagates.

        The inner _call() in _fetch_rg_image is decorated with @_mb_retry, so a persistent 500 error
        exhausts all six retry attempts and raises RuntimeError.

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
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)

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
        result = fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)
        assert not result.available

    def test_listing_missing_id_skipped(self, mocker: MockerFixture) -> None:
        """Image entries with no 'id' field are silently skipped.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        listing = {"images": [{"types": ["Front"], "front": True, "back": False}]}
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=listing)
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image")
        result = fetch_cover_art("rel-1", no_cache=True)
        mock_get.assert_not_called()
        assert not result.available

    def test_listing_non_list_images_returns_empty(self, mocker: MockerFixture) -> None:
        """A listing response where 'images' is not a list returns empty CoverArt.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value={"images": "bad"})
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
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
        result = fetch_cover_art("rel-1", no_cache=True)
        assert not result.back
        assert not result.booklet
        assert not result.medium


# ---------------------------------------------------------------------------
# fetch_work_detail
# ---------------------------------------------------------------------------


class TestFetchWorkDetail:
    """Tests for fetch_work_detail and its cache."""

    def setup_method(self) -> None:
        """Clear the module-level work cache and redirect XDG_CACHE_HOME to an isolated temp dir."""
        music_annotator._mb_api._WORK_CACHE.clear()  # pylint: disable=protected-access
        self._cache_tmpdir = tempfile.mkdtemp()  # pylint: disable=attribute-defined-outside-init
        self._orig_xdg = os.environ.get("XDG_CACHE_HOME")  # pylint: disable=attribute-defined-outside-init
        os.environ["XDG_CACHE_HOME"] = self._cache_tmpdir

    def teardown_method(self) -> None:
        """Restore XDG_CACHE_HOME and remove the temporary cache directory."""
        if self._orig_xdg is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._orig_xdg
        shutil.rmtree(self._cache_tmpdir, ignore_errors=True)

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


def _entry(action: str = "tagged", src: str = "/src/01.flac", dest: str = "/dest/01.flac") -> TransactionEntry:
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
        assert data[0]["action"] == "tagged"
        assert data[0]["source"] == "/src/01.flac"

    def test_appends_to_existing_journal(self, fs: FakeFilesystem) -> None:
        """New entries are appended to existing journal contents.

        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/dest")
        journal = Path("/dest/music_annotator_journal.json")
        first = _entry(action="tagged", src="/src/01.flac", dest="/dest/01.flac")
        write_transaction_log(journal, [first])

        second = _entry(action="skipped", src="/src/02.flac", dest="/dest/02.flac")
        write_transaction_log(journal, [second])

        data = json.loads(journal.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["action"] == "tagged"
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
# _patched_safe_read
# ---------------------------------------------------------------------------


class TestPatchedSafeRead:
    """Tests for the _patched_safe_read workaround that fast-fails on non-retryable HTTP codes."""

    def _make_opener(self, mocker: MockerFixture, exc: Exception | None = None, data: bytes = b"") -> MagicMock:
        """Build a mock opener that raises ``exc`` or returns a mock file object yielding ``data``.

        :param mocker: pytest-mock fixture.
        :param exc: If provided, ``open()`` raises this exception.
        :param data: Bytes returned by ``open().read()`` when ``exc`` is ``None``.
        :returns: A MagicMock with an ``open`` method matching the ``_HttpOpener`` protocol.
        """
        opener: MagicMock = mocker.MagicMock()
        if exc is not None:
            opener.open.side_effect = exc
        else:
            fake_file: MagicMock = mocker.MagicMock()
            fake_file.read.return_value = data
            opener.open.return_value = fake_file
        return opener

    def test_success_returns_bytes(self, mocker: MockerFixture) -> None:
        """Returns the bytes from the response when the request succeeds.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, data=b"ok")
        assert _patched_safe_read(opener, mocker.MagicMock()) == b"ok"

    def test_success_with_body_uses_post(self, mocker: MockerFixture) -> None:
        """When a request body is provided, ``open()`` is called with it (POST path).

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, data=b"post-ok")
        req = mocker.MagicMock()
        result = _patched_safe_read(opener, req, body=b"payload")
        assert result == b"post-ok"
        opener.open.assert_called_once_with(req, b"payload")

    def test_307_redirect_raises_response_error_immediately(self, mocker: MockerFixture) -> None:
        """A 307 redirect loop raises ResponseError on the first attempt with no retries.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("http://example.com", 307, "Temporary Redirect", HTTPMessage(), None)
        opener = self._make_opener(mocker, exc=http_exc)
        req = mocker.MagicMock()
        with pytest.raises(mzmz.ResponseError):
            _patched_safe_read(opener, req)
        # open() was called exactly once — no retries
        assert opener.open.call_count == 1

    def test_unknown_code_raises_response_error_immediately(self, mocker: MockerFixture) -> None:
        """An arbitrary unknown HTTP code raises ResponseError on the first attempt with no retries.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("http://example.com", 418, "I'm a teapot", HTTPMessage(), None)
        opener = self._make_opener(mocker, exc=http_exc)
        req = mocker.MagicMock()
        with pytest.raises(mzmz.ResponseError):
            _patched_safe_read(opener, req)
        assert opener.open.call_count == 1

    def test_404_raises_response_error_immediately(self, mocker: MockerFixture) -> None:
        """HTTP 404 raises ResponseError on the first attempt with no retries.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("http://example.com", 404, "Not Found", HTTPMessage(), None)
        opener = self._make_opener(mocker, exc=http_exc)
        with pytest.raises(mzmz.ResponseError):
            _patched_safe_read(opener, mocker.MagicMock())
        assert opener.open.call_count == 1

    def test_401_raises_authentication_error(self, mocker: MockerFixture) -> None:
        """HTTP 401 raises AuthenticationError on the first attempt.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("http://example.com", 401, "Unauthorized", HTTPMessage(), None)
        opener = self._make_opener(mocker, exc=http_exc)
        with pytest.raises(mzmz.AuthenticationError):
            _patched_safe_read(opener, mocker.MagicMock())
        assert opener.open.call_count == 1

    def test_503_retries_and_raises_network_error(self, mocker: MockerFixture) -> None:
        """HTTP 503 is retried up to max_retries times before raising NetworkError.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        http_exc = HTTPError("http://example.com", 503, "Service Unavailable", HTTPMessage(), None)
        opener = self._make_opener(mocker, exc=http_exc)
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock(), max_retries=3, retry_delay_delta=0.0)
        assert opener.open.call_count == 3
        # Sleep is called for retries 1 and 2 (not the first attempt).
        assert mock_sleep.call_count == 2

    def test_url_error_non_104_raises_network_error(self, mocker: MockerFixture) -> None:
        """A URLError with a non-104 socket error raises NetworkError immediately.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        reason = socket.error(111, "Connection refused")
        opener = self._make_opener(mocker, exc=URLError(reason))
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock())

    def test_url_error_non_socket_reason_raises_network_error(self, mocker: MockerFixture) -> None:
        """A URLError with a string reason (not a socket.error) raises NetworkError.

        This exercises the ``if isinstance(exc.reason, socket.error)`` False branch.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, exc=URLError("name or service not known"))
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock())

    def test_url_error_104_retries(self, mocker: MockerFixture) -> None:
        """A URLError with errno 104 (connection reset) triggers a retry.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        reason = socket.error(104, "Connection reset by peer")
        opener = mocker.MagicMock()
        # First call raises URLError(104), second call succeeds.
        fake_file = mocker.MagicMock()
        fake_file.read.return_value = b"ok"
        opener.open.side_effect = [URLError(reason), fake_file]
        result = _patched_safe_read(opener, mocker.MagicMock(), max_retries=2)
        assert result == b"ok"

    def test_socket_error_104_retries(self, mocker: MockerFixture) -> None:
        """A socket.error with errno 104 triggers a retry.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        sock_err = socket.error(104, "Connection reset by peer")
        opener = mocker.MagicMock()
        fake_file = mocker.MagicMock()
        fake_file.read.return_value = b"ok"
        opener.open.side_effect = [sock_err, fake_file]
        result = _patched_safe_read(opener, mocker.MagicMock(), max_retries=2)
        assert result == b"ok"

    def test_socket_error_non_104_raises_network_error(self, mocker: MockerFixture) -> None:
        """A socket.error with non-104 errno raises NetworkError immediately.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        sock_err = socket.error(111, "Connection refused")
        opener = self._make_opener(mocker, exc=sock_err)
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock())

    def test_oserror_raises_network_error(self, mocker: MockerFixture) -> None:
        """A generic OSError raises NetworkError immediately.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, exc=OSError("disk gone"))
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock())

    def test_timeout_error_retries_and_raises_network_error(self, mocker: MockerFixture) -> None:
        """A TimeoutError is retried up to max_retries times before raising NetworkError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, exc=TimeoutError("timed out"))
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock(), max_retries=2, retry_delay_delta=0.0)
        assert opener.open.call_count == 2

    def test_bad_status_line_retries_and_raises_network_error(self, mocker: MockerFixture) -> None:
        """A BadStatusLine is retried up to max_retries times before raising NetworkError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, exc=BadStatusLine(""))
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock(), max_retries=2, retry_delay_delta=0.0)
        assert opener.open.call_count == 2

    def test_http_exception_retries_and_raises_network_error(self, mocker: MockerFixture) -> None:
        """A generic HTTPException is retried up to max_retries times before raising NetworkError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")
        opener = self._make_opener(mocker, exc=HTTPException("misc"))
        with pytest.raises(mzmz.NetworkError):
            _patched_safe_read(opener, mocker.MagicMock(), max_retries=2, retry_delay_delta=0.0)
        assert opener.open.call_count == 2


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

    def _make_jpeg_ctx(self, mocker: MockerFixture, data: bytes) -> MagicMock:
        """Build a mock context manager that returns ``data`` from ``.read()``.

        :param mocker: pytest-mock fixture.
        :param data: Bytes to return from the context manager.
        :returns: A mock usable as a context manager.
        """
        ctx: MagicMock = mocker.MagicMock()
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
        result = fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)
        assert len(result.front) == 1
        assert result.front[0].data == jpeg_500
        assert len(result.front_full) == 1
        assert result.front_full[0].data == jpeg_orig
        assert result.front_full[0].filename == "cover.jpg"
        assert mock_rg.call_count == 2

    def test_release_group_fallback_error_raises(self, mocker: MockerFixture) -> None:
        """A persistent error from the release-group fallback raises RuntimeError after retries.

        The inner _call() in _fetch_rg_image is decorated with @_mb_retry, so a persistent 503 error
        exhausts all six retry attempts and raises RuntimeError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=mb.ResponseError("404 Not Found"))
        mocker.patch(
            "music_annotator._mb_api.mb.get_release_group_image_front",
            side_effect=mb.ResponseError("503"),
        )
        mocker.patch("music_annotator._mb_api.time.sleep")
        with pytest.raises(RuntimeError, match="MB request failed after retries"):
            fetch_cover_art("rel-1", release_group_id="rg-1", no_cache=True)


# ---------------------------------------------------------------------------
# Cover art cache
# ---------------------------------------------------------------------------

_JPEG_BYTES: bytes = b"\xff\xd8" + b"\x00" * 100


class TestCoverArtCacheDir:
    """Tests for _cover_art_cache_dir()."""

    def test_creates_directory_under_xdg_cache_home(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Cache dir is created under $XDG_CACHE_HOME when set.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """

        fs.create_dir("/custom/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/custom/cache"})
        result = _cover_art_cache_dir()
        assert result == Path("/custom/cache/music-annotator/cover-art")
        assert result.is_dir()

    # pylint: disable-next=unused-argument
    def test_falls_back_to_home_cache_when_xdg_unset(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Falls back to ~/.cache when XDG_CACHE_HOME is not set.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """

        mocker.patch.dict(os.environ, {}, clear=True)
        mocker.patch("music_annotator._mb_api.Path.home", return_value=Path("/home/user"))
        result = _cover_art_cache_dir()
        assert result == Path("/home/user/.cache/music-annotator/cover-art")

    def test_creates_dir_if_absent(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Cache directory is created if it does not yet exist.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """

        fs.create_dir("/xdg")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/xdg"})
        result = _cover_art_cache_dir()
        assert result.is_dir()


class TestCoverArtCacheKey:
    """Tests for _cover_art_cache_key()."""

    def test_500_size(self) -> None:
        """Size '500' produces a key ending in '_500'."""
        assert _cover_art_cache_key("12345678", "500") == "12345678_500"

    def test_empty_size_is_original(self) -> None:
        """Empty size string produces a key ending in '_original'."""
        assert _cover_art_cache_key("12345678", "") == "12345678_original"

    def test_rg_key(self) -> None:
        """Release-group key format is correct."""
        assert _cover_art_cache_key("rg_abc", "500") == "rg_abc_500"


class TestFetchCoverArtCacheMissAndHit:
    """Tests for cache read/write paths in fetch_cover_art."""

    def _make_listing(self, coverid: str, image_type: str = "Front") -> dict[str, object]:
        """Build a minimal CAA image listing dict.

        :param coverid: CAA image identifier string.
        :param image_type: CAA type string.
        :returns: A listing dict consumable by the image classification logic.
        """
        return {
            "images": [{"types": [image_type], "id": coverid, "image": f"https://caa/{coverid}"}],
            "release": "https://mb/release/rel-1",
        }

    def test_cache_miss_writes_file_and_fetches_network(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """On a cache miss the image is fetched from the network and written to the cache dir.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=self._make_listing("99"))
        mock_get_image = mocker.patch("music_annotator._mb_api.mb.get_image", return_value=_JPEG_BYTES)

        result = fetch_cover_art("rel-1")

        # Network was called.
        assert mock_get_image.call_count >= 1
        # Result contains image data.
        assert result.front[0].data == _JPEG_BYTES
        # Cache file was written under the release-scoped key.
        cache_file_500 = Path("/cache/music-annotator/cover-art/rel-1_99_500.bin")
        assert cache_file_500.exists()
        assert cache_file_500.read_bytes() == _JPEG_BYTES

    def test_cache_hit_skips_network(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """On a cache hit the image is returned from disk without calling the network.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        cache_dir = Path("/cache/music-annotator/cover-art")
        fs.create_dir(str(cache_dir))
        # Pre-populate cache for both 500 and original sizes using the release-scoped key.
        (cache_dir / "rel-1_99_500.bin").write_bytes(_JPEG_BYTES)
        (cache_dir / "rel-1_99_original.bin").write_bytes(_JPEG_BYTES)

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=self._make_listing("99"))
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image")

        result = fetch_cover_art("rel-1")

        # Network was never called.
        mock_get.assert_not_called()
        assert result.front[0].data == _JPEG_BYTES

    def test_no_cache_flag_bypasses_cache(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no_cache=True the cache is bypassed even when cache files exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        cache_dir = Path("/cache/music-annotator/cover-art")
        fs.create_dir(str(cache_dir))
        (cache_dir / "rel-1_99_500.bin").write_bytes(_JPEG_BYTES)
        (cache_dir / "rel-1_99_original.bin").write_bytes(_JPEG_BYTES)

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=self._make_listing("99"))
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image", return_value=_JPEG_BYTES)

        fetch_cover_art("rel-1", no_cache=True)

        # Network was called despite cache files existing.
        assert mock_get.call_count >= 1

    def test_release_group_fallback_cache_hit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Release-group fallback images are returned from cache without network calls.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        cache_dir = Path("/cache/music-annotator/cover-art")
        fs.create_dir(str(cache_dir))
        (cache_dir / "rg_rg-1_500.bin").write_bytes(_JPEG_BYTES)
        (cache_dir / "rg_rg-1_original.bin").write_bytes(_JPEG_BYTES)

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=mb.ResponseError("404"))
        mock_rg = mocker.patch("music_annotator._mb_api.mb.get_release_group_image_front")

        result = fetch_cover_art("rel-1", release_group_id="rg-1")

        mock_rg.assert_not_called()
        assert result.front[0].data == _JPEG_BYTES
        assert result.front_full[0].data == _JPEG_BYTES

    def test_release_group_fallback_cache_miss_writes_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Release-group fallback images are written to cache on a miss.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """

        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", side_effect=mb.ResponseError("404"))
        mocker.patch(
            "music_annotator._mb_api.mb.get_release_group_image_front",
            side_effect=[_JPEG_BYTES, _JPEG_BYTES],
        )

        result = fetch_cover_art("rel-1", release_group_id="rg-1")

        assert result.front[0].data == _JPEG_BYTES
        cache_dir = Path("/cache/music-annotator/cover-art")
        assert (cache_dir / "rg_rg-1_500.bin").exists()
        assert (cache_dir / "rg_rg-1_original.bin").exists()

    def test_cache_is_scoped_to_release_id(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Cache entries for the same CAA image ID are isolated per release MBID.

        Two releases that both reference CAA image ID "99" must not share a cache entry.
        Release B's fetch must go to the network even when release A has already populated
        the cache for the same CAA image ID, and the result must contain release B's image
        data rather than release A's.

        This is the regression test for the latent bug where ``_fetch_raw`` used only the
        CAA numeric image ID as the cache key, omitting ``rel_id``.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        jpeg_a = b"\xff\xd8" + b"\xaa" * 100
        jpeg_b = b"\xff\xd8" + b"\xbb" * 100

        cache_dir = Path("/cache/music-annotator/cover-art")
        fs.create_dir(str(cache_dir))
        # Pre-populate cache as if release A was already processed.
        (cache_dir / "rel-A_99_500.bin").write_bytes(jpeg_a)
        (cache_dir / "rel-A_99_original.bin").write_bytes(jpeg_a)

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch("music_annotator._mb_api.mb.get_image_list", return_value=self._make_listing("99"))
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_image", return_value=jpeg_b)

        # Fetch for release B — same CAA image ID "99", different release MBID.
        result = fetch_cover_art("rel-B")

        # Network must have been called: "rel-B_99_*.bin" was not in the cache.
        assert mock_get.call_count >= 1
        # Result must contain release B's image, not release A's stale cached image.
        assert result.front[0].data == jpeg_b
        # Release B's cache entry must have been written.
        assert (cache_dir / "rel-B_99_500.bin").exists()
        assert (cache_dir / "rel-B_99_500.bin").read_bytes() == jpeg_b
        # Release A's cache entry must be untouched.
        assert (cache_dir / "rel-A_99_500.bin").read_bytes() == jpeg_a


# ---------------------------------------------------------------------------
# _metadata_cache_dir
# ---------------------------------------------------------------------------


class TestMetadataCacheDir:
    """Tests for _metadata_cache_dir()."""

    def test_creates_subdir_under_xdg_cache_home(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Cache dir is created under $XDG_CACHE_HOME/<subdir> when XDG_CACHE_HOME is set.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        fs.create_dir("/custom/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/custom/cache"})
        result = _metadata_cache_dir("recording")
        assert result == Path("/custom/cache/music-annotator/recording")
        assert result.is_dir()

    # pylint: disable-next=unused-argument
    def test_falls_back_to_home_cache_when_xdg_unset(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Falls back to ~/.cache when XDG_CACHE_HOME is not set.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        mocker.patch.dict(os.environ, {}, clear=True)
        mocker.patch("music_annotator._mb_api.Path.home", return_value=Path("/home/user"))
        result = _metadata_cache_dir("work")
        assert result == Path("/home/user/.cache/music-annotator/work")

    def test_cover_art_cache_dir_delegates_correctly(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """_cover_art_cache_dir() returns the cover-art subdir via the shared helper.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        fs.create_dir("/xdg")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/xdg"})
        result = _cover_art_cache_dir()
        assert result == Path("/xdg/music-annotator/cover-art")
        assert result.is_dir()


# ---------------------------------------------------------------------------
# Metadata round-trip fidelity
# ---------------------------------------------------------------------------


class TestMetadataRoundTrip:
    """Verify that model_dump_json(by_alias=True) round-trips correctly through model_validate_json."""

    def test_mbrecording_round_trip(self) -> None:
        """MBRecording with aliased fields, coerced ints, and mixed artist-credit round-trips cleanly.

        Exercises: ``first-release-date`` alias, ``length`` int coercion, ``artist-credit`` mixed list,
        ``artist-relation-list`` with ``attribute-list`` entries (``MBAttribute | str`` union).
        """
        original = MBRecording.model_validate(
            {
                "id": "rec-uuid-1",
                "title": "Allegro ma non troppo",
                "first-release-date": "1964-05-01",
                "length": "215000",
                "artist-credit": [
                    {
                        "name": "Karajan",
                        "artist": {"id": "a1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                    },
                    " & ",
                    {
                        "name": "BPO",
                        "artist": {"id": "a2", "name": "Berliner Philharmoniker", "sort-name": "Berliner Philharmoniker"},
                    },
                ],
                "artist-relation-list": [
                    {
                        "type": "conductor",
                        "direction": "backward",
                        "begin": "1964-04-28",
                        "end": "1964-04-30",
                        "ended": "true",
                        "target-credit": "Karajan",
                        "artist": {"id": "a1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                        "attribute-list": [{"type": "guest", "value": ""}, "additional"],
                    }
                ],
            }
        )
        assert original.length == 215000
        assert original.first_release_date == "1964-05-01"
        assert isinstance(original.artist_relation_list[0].attribute_list[0], MBAttribute)
        assert original.artist_relation_list[0].attribute_list[1] == "additional"

        json_str = original.model_dump_json(by_alias=True)
        restored = MBRecording.model_validate_json(json_str)
        assert restored == original

    def test_mbwork_round_trip(self) -> None:
        """MBWork with aliased fields, attribute_list MBAttribute entries, and coerced ordering-key round-trips cleanly.

        Exercises: ``artist-relation-list``, ``work-relation-list`` with ``ordering-key`` coercion,
        ``attribute-list`` with ``MBAttribute | str`` union, ``life-span`` alias.
        """
        original = MBWork.model_validate(
            {
                "id": "work-uuid-1",
                "title": "Symphony No. 9",
                "type": "Symphony",
                "language": "deu",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "direction": "backward",
                        "artist": {"id": "bv1", "name": "Beethoven", "sort-name": "Beethoven, Ludwig van"},
                        "attribute-list": [{"type": "Key", "value": "D minor"}],
                    }
                ],
                "attribute-list": [{"type": "Key", "value": "D minor"}, "historical"],
                "work-relation-list": [
                    {
                        "type": "parts",
                        "direction": "forward",
                        "ordering-key": "1",
                        "work": {"id": "mvt-1", "title": "I. Allegro ma non troppo"},
                    }
                ],
                "life-span": {"begin": "1817", "end": "1824", "ended": "true"},
            }
        )
        assert original.work_relation_list[0].ordering_key == 1
        assert isinstance(original.attribute_list[0], MBAttribute)
        assert original.attribute_list[1] == "historical"
        assert original.life_span.begin == "1817"

        json_str = original.model_dump_json(by_alias=True)
        restored = MBWork.model_validate_json(json_str)
        assert restored == original


# ---------------------------------------------------------------------------
# fetch_recording_detail disk cache
# ---------------------------------------------------------------------------


class TestFetchRecordingDetailCache:
    """Tests for the on-disk cache in fetch_recording_detail()."""

    def setup_method(self) -> None:
        """Clear the module-level work cache before each test."""
        music_annotator._mb_api._WORK_CACHE.clear()  # pylint: disable=protected-access

    def _raw_recording_dict(self) -> dict[str, object]:
        """Return a minimal recording API response dict.

        :returns: Dict suitable for ``mb.get_recording_by_id`` mock return value.
        """
        return {"recording": {"id": "rec-1", "title": "Allegro"}}

    def test_cache_miss_fetches_network_and_writes_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """On a cache miss the recording is fetched from the network and the JSON file is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_recording_by_id", return_value=self._raw_recording_dict())

        result = fetch_recording_detail("rec-1")

        mock_get.assert_called_once()
        assert result.id == "rec-1"
        cache_file = Path("/cache/music-annotator/recording/rec-1.json")
        assert cache_file.exists()
        # File must contain valid JSON that round-trips back to the same model.
        restored = MBRecording.model_validate_json(cache_file.read_text(encoding="utf-8"))
        assert restored == result

    def test_cache_hit_skips_network(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """On a cache hit the recording is returned from disk without a network call.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        cache_dir = Path("/cache/music-annotator/recording")
        fs.create_dir(str(cache_dir))
        recording = MBRecording.model_validate({"id": "rec-2", "title": "Adagio"})
        (cache_dir / "rec-2.json").write_text(recording.model_dump_json(by_alias=True), encoding="utf-8")

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_recording_by_id")

        result = fetch_recording_detail("rec-2")

        mock_get.assert_not_called()
        assert result.id == "rec-2"
        assert result.title == "Adagio"

    def test_no_cache_always_fetches_and_skips_write(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no_cache=True the cache file is never read or written even if it exists.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        cache_dir = Path("/cache/music-annotator/recording")
        fs.create_dir(str(cache_dir))
        stale = MBRecording.model_validate({"id": "rec-3", "title": "STALE"})
        (cache_dir / "rec-3.json").write_text(stale.model_dump_json(by_alias=True), encoding="utf-8")

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch(
            "music_annotator._mb_api.mb.get_recording_by_id",
            return_value={"recording": {"id": "rec-3", "title": "Fresh"}},
        )

        result = fetch_recording_detail("rec-3", no_cache=True)

        mock_get.assert_called_once()
        assert result.title == "Fresh"
        # Cache file must be unchanged (stale title still on disk).
        assert MBRecording.model_validate_json((cache_dir / "rec-3.json").read_text(encoding="utf-8")).title == "STALE"

    def test_atomic_write_produces_valid_cache_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """The atomic temp-file + os.replace write path produces a valid, complete JSON file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_recording_by_id",
            return_value={"recording": {"id": "rec-4", "title": "Presto"}},
        )

        fetch_recording_detail("rec-4")

        cache_file = Path("/cache/music-annotator/recording/rec-4.json")
        assert cache_file.exists()
        # Temp file must be gone (replaced atomically).
        assert not cache_file.with_suffix(".tmp").exists()
        # Content must be valid JSON parseable as MBRecording.
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert MBRecording.model_validate_json(cache_file.read_text(encoding="utf-8")).id == "rec-4"


# ---------------------------------------------------------------------------
# fetch_work_detail disk cache (L1 + L2)
# ---------------------------------------------------------------------------


class TestFetchWorkDetailCache:
    """Tests for the two-level cache in fetch_work_detail() and _get_bottom_work()."""

    def setup_method(self) -> None:
        """Clear the module-level work cache before each test."""
        music_annotator._mb_api._WORK_CACHE.clear()  # pylint: disable=protected-access

    def _raw_work_dict(self, work_id: str, title: str) -> dict[str, object]:
        """Return a minimal work API response dict.

        :param work_id: Work MBID.
        :param title: Work title.
        :returns: Dict suitable for ``mb.get_work_by_id`` mock return value.
        """
        return {"work": {"id": work_id, "title": title}}

    def test_cache_miss_fetches_network_and_writes_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """On a cache miss the work is fetched from the network and the JSON file is written.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_work_by_id", return_value=self._raw_work_dict("w-1", "Eroica"))

        result = fetch_work_detail("w-1")

        mock_get.assert_called_once()
        assert result.title == "Eroica"
        cache_file = Path("/cache/music-annotator/work/w-1.json")
        assert cache_file.exists()
        restored = MBWork.model_validate_json(cache_file.read_text(encoding="utf-8"))
        assert restored == result

    def test_l2_cache_hit_skips_network(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """On an L2 (disk) cache hit the work is returned from disk without a network call.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        cache_dir = Path("/cache/music-annotator/work")
        fs.create_dir(str(cache_dir))
        work = MBWork.model_validate({"id": "w-2", "title": "Pastoral"})
        (cache_dir / "w-2.json").write_text(work.model_dump_json(by_alias=True), encoding="utf-8")

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_work_by_id")

        result = fetch_work_detail("w-2")

        mock_get.assert_not_called()
        assert result.title == "Pastoral"

    def test_l1_cache_short_circuits_before_disk(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When the work is already in _WORK_CACHE (L1), neither the disk nor the network is accessed.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_work_by_id")
        # Pre-populate L1 cache.
        cached_work = MBWork.model_validate({"id": "w-3", "title": "Moonlight"})
        music_annotator._mb_api._WORK_CACHE["w-3"] = cached_work  # pylint: disable=protected-access

        result = fetch_work_detail("w-3")

        mock_get.assert_not_called()
        assert result is cached_work

    def test_no_cache_always_fetches_skips_both_layers(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no_cache=True both L1 and L2 are bypassed; a fresh network fetch is made.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        cache_dir = Path("/cache/music-annotator/work")
        fs.create_dir(str(cache_dir))
        stale = MBWork.model_validate({"id": "w-4", "title": "STALE"})
        (cache_dir / "w-4.json").write_text(stale.model_dump_json(by_alias=True), encoding="utf-8")
        music_annotator._mb_api._WORK_CACHE["w-4"] = stale  # pylint: disable=protected-access

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value=self._raw_work_dict("w-4", "Fresh"),
        )

        result = fetch_work_detail("w-4", no_cache=True)

        mock_get.assert_called_once()
        assert result.title == "Fresh"
        # Cache file must be unchanged (stale still on disk, not overwritten).
        assert MBWork.model_validate_json((cache_dir / "w-4.json").read_text(encoding="utf-8")).title == "STALE"

    def test_atomic_write_produces_valid_cache_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """The atomic temp-file + os.replace write path produces a valid, complete JSON file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value=self._raw_work_dict("w-5", "Hammerklavier"),
        )

        fetch_work_detail("w-5")

        cache_file = Path("/cache/music-annotator/work/w-5.json")
        assert cache_file.exists()
        assert not cache_file.with_suffix(".tmp").exists()
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert MBWork.model_validate_json(cache_file.read_text(encoding="utf-8")).id == "w-5"

    def test_l2_hit_populates_l1(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """After an L2 disk hit the work is stored in _WORK_CACHE for subsequent L1 hits.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        cache_dir = Path("/cache/music-annotator/work")
        fs.create_dir(str(cache_dir))
        work = MBWork.model_validate({"id": "w-6", "title": "Emperor"})
        (cache_dir / "w-6.json").write_text(work.model_dump_json(by_alias=True), encoding="utf-8")

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mock_get = mocker.patch("music_annotator._mb_api.mb.get_work_by_id")

        first_result = fetch_work_detail("w-6")
        # After the disk hit, L1 must be populated.
        assert "w-6" in music_annotator._mb_api._WORK_CACHE  # pylint: disable=protected-access
        # Second call must not hit disk or network.
        second_result = fetch_work_detail("w-6")
        mock_get.assert_not_called()
        assert first_result == second_result


# ---------------------------------------------------------------------------
# _get_bottom_work no_cache forwarding
# ---------------------------------------------------------------------------


class TestGetBottomWorkNoCache:
    """Tests that _get_bottom_work() forwards no_cache to fetch_work_detail()."""

    def setup_method(self) -> None:
        """Clear the module-level work cache before each test."""
        music_annotator._mb_api._WORK_CACHE.clear()  # pylint: disable=protected-access

    def test_inlined_work_returned_directly(self) -> None:
        """When the embedded work has relation data, it is returned without any fetch.

        _get_bottom_work must not call fetch_work_detail when the embedded work is already
        populated (artist_relation_list or work_relation_list is non-empty).
        """
        embedded = MBWork.model_validate(
            {
                "id": "w-inlined",
                "title": "Inlined",
                "artist-relation-list": [{"type": "composer", "artist": {"id": "a1", "name": "Bach"}}],
            }
        )
        result = _get_bottom_work(embedded)
        assert result is embedded

    def test_stub_work_falls_back_to_fetch(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When the embedded work has empty relation lists, fetch_work_detail is called.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        fs.create_dir("/cache")
        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value={"work": {"id": "w-stub", "title": "Full"}},
        )
        stub = MBWork.model_validate({"id": "w-stub", "title": "Stub"})

        result = _get_bottom_work(stub)

        assert result.title == "Full"

    def test_no_cache_forwarded_to_fetch_work_detail(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no_cache=True and work is a stub, fetch_work_detail is called with no_cache=True.

        This test populates the on-disk cache with stale data; with no_cache=True the network
        fetch must occur and return the fresh value rather than the cached one.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        cache_dir = Path("/cache/music-annotator/work")
        fs.create_dir(str(cache_dir))
        stale = MBWork.model_validate({"id": "w-nc", "title": "STALE"})
        (cache_dir / "w-nc.json").write_text(stale.model_dump_json(by_alias=True), encoding="utf-8")

        mocker.patch.dict(os.environ, {"XDG_CACHE_HOME": "/cache"})
        mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._mb_api.mb.get_work_by_id",
            return_value={"work": {"id": "w-nc", "title": "Fresh"}},
        )
        stub = MBWork.model_validate({"id": "w-nc", "title": "Stub"})

        result = _get_bottom_work(stub, no_cache=True)

        assert result.title == "Fresh"
