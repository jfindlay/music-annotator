"""Unit tests for music_annotator._net — the policy-parameterized retrieval core.

Each test class pins one behavioural clause of C-NET-TERM (the universal terminal rule).  The four
KATs required by the contract are:

1. :class:`TestRetryExhaustion` — RETRY classification retries ``max_attempts`` times, then raises
   (cannot-determine → fatal).
2. :class:`TestNoData` — NO_DATA classification returns ``None`` with exactly one warning, no raise.
3. :class:`TestFatalImmediate` — FATAL classification raises immediately (no retry loop).
4. :class:`TestExactlyOneLogOnFatalExhaustion` — the terminal choke point emits exactly one
   ``log.error`` on fatal exhaustion.
"""

from __future__ import annotations

from urllib.error import HTTPError

import pytest
from pytest_mock import MockerFixture

from music_annotator._net import NetPolicy, RetryableClassifier, RetryDecision, retrieve

# ---------------------------------------------------------------------------
# Classifier helpers
# ---------------------------------------------------------------------------


def _always_retry(_exc: Exception) -> RetryDecision:
    """Classifier that always returns RETRY — models a transient 5xx / OSError condition.

    :param _exc: The raised exception (ignored; outcome is unconditional).
    :returns: RetryDecision.RETRY.
    """
    return RetryDecision.RETRY


def _always_no_data(_exc: Exception) -> RetryDecision:
    """Classifier that always returns NO_DATA — models an authoritative 4xx "unknown MBID" answer.

    :param _exc: The raised exception (ignored; outcome is unconditional).
    :returns: RetryDecision.NO_DATA.
    """
    return RetryDecision.NO_DATA


def _always_fatal(_exc: Exception) -> RetryDecision:
    """Classifier that always returns FATAL — models a permanent non-no-data failure.

    :param _exc: The raised exception (ignored; outcome is unconditional).
    :returns: RetryDecision.FATAL.
    """
    return RetryDecision.FATAL


def _http_status_classifier(exc: Exception) -> RetryDecision:
    """Structured classifier that reads the typed HTTP status code — never str(exc).

    Ordering rule: HTTPError status code is extracted before any broad OSError check.

    :param exc: The raised exception.
    :returns: RetryDecision based on the HTTP status code or exception type.
    """
    if isinstance(exc, HTTPError):
        if exc.code >= 500:
            return RetryDecision.RETRY
        if exc.code == 404:
            return RetryDecision.NO_DATA
        return RetryDecision.FATAL
    if isinstance(exc, OSError):
        return RetryDecision.RETRY
    return RetryDecision.FATAL


def _make_policy(
    classify: RetryableClassifier,
    *,
    max_attempts: int = 3,
    backoff_base: float = 1.0,
    polite_delay_s: float = 0.0,
    event: str = "test_retrieve",
    log_fields: dict[str, object] | None = None,
) -> NetPolicy:
    """Build a NetPolicy for testing with fast backoff and no polite delay.

    :param classify: The classifier callable to use.
    :param max_attempts: Maximum retry attempts (default 3 for fast tests).
    :param backoff_base: Backoff base (default 1.0 for fast tests).
    :param polite_delay_s: Polite delay in seconds (default 0.0 for fast tests).
    :param event: Event name prefix for log lines.
    :param log_fields: Optional static log fields.
    :returns: A configured NetPolicy.
    """
    return NetPolicy(
        classify=classify,
        event=event,
        log_fields=log_fields or {},
        max_attempts=max_attempts,
        backoff_base=backoff_base,
        polite_delay_s=polite_delay_s,
    )


# ---------------------------------------------------------------------------
# KAT 1 — RETRY exhaustion raises (cannot-determine → fatal)
# ---------------------------------------------------------------------------


class TestRetryExhaustion:
    """KAT 1: RETRY classification retries max_attempts times, then raises.

    Pins C-NET-TERM: "RETRY → back off, re-attempt; exhaustion → FATAL terminal (raise)."
    """

    def test_retry_calls_fetch_max_attempts_times(self, mocker: MockerFixture) -> None:
        """Fetch is called exactly max_attempts times before exhaustion raises.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = OSError("transient network error")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_retry, max_attempts=4)

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        assert fetch.call_count == 4

    def test_retry_exhaustion_raises_last_exception(self, mocker: MockerFixture) -> None:
        """Exhaustion re-raises the last caught exception, not a synthesised RuntimeError.

        Preserves the transport .cause chain for inspection at the per-release boundary.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_retry, max_attempts=3)

        with pytest.raises(HTTPError) as exc_info:
            retrieve(fetch, policy)

        assert exc_info.value is err

    def test_retry_sleeps_backoff_between_attempts(self, mocker: MockerFixture) -> None:
        """Backoff sleep is called between retry attempts with backoff_base ** attempt seconds.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._net.time.sleep")
        err = OSError("transient")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_retry, max_attempts=3, backoff_base=2.0)

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        # Backoff sleeps: 2**0=1, 2**1=2, 2**2=4 (one per attempt before exhaustion)
        sleep_args = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_args == [1.0, 2.0, 4.0]

    def test_retry_success_after_transient_failure(self, mocker: MockerFixture) -> None:
        """Fetch succeeds on the second attempt after one transient failure.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = OSError("transient")
        fetch = mocker.MagicMock(side_effect=[err, "success_value"])
        policy = _make_policy(_always_retry, max_attempts=3)

        result = retrieve(fetch, policy)

        assert result == "success_value"
        assert fetch.call_count == 2

    def test_retry_with_http_503_classifier(self, mocker: MockerFixture) -> None:
        """Structured classifier correctly routes 503 to RETRY; exhaustion raises HTTPError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_http_status_classifier, max_attempts=3)

        with pytest.raises(HTTPError) as exc_info:
            retrieve(fetch, policy)

        assert exc_info.value.code == 503
        assert fetch.call_count == 3

    def test_retry_with_oserror_classifier(self, mocker: MockerFixture) -> None:
        """Structured classifier correctly routes OSError to RETRY; exhaustion raises OSError.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = OSError("connection refused")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_http_status_classifier, max_attempts=2)

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        assert fetch.call_count == 2


# ---------------------------------------------------------------------------
# KAT 2 — NO_DATA returns None with exactly one warning, no raise
# ---------------------------------------------------------------------------


class TestNoData:
    """KAT 2: NO_DATA classification returns None with exactly one warning, no raise.

    Pins C-NET-TERM: "NO_DATA → return None (no raise); exactly one log.warning('<event>_no_data')."
    """

    def test_no_data_returns_none(self, mocker: MockerFixture) -> None:
        """NO_DATA classification returns None without raising.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_no_data)

        result = retrieve(fetch, policy)

        assert result is None

    def test_no_data_does_not_raise(self, mocker: MockerFixture) -> None:
        """NO_DATA classification does not raise any exception.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_no_data)

        # Must not raise
        retrieve(fetch, policy)

    def test_no_data_emits_exactly_one_warning(self, mocker: MockerFixture) -> None:
        """NO_DATA emits exactly one log.warning('<event>_no_data') — not zero, not two.

        Pins the exactly-one-log invariant from C-NET-TERM.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_warning = mocker.patch("music_annotator._net.log.warning")
        err = HTTPError("https://example.com/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_no_data, event="mb_retrieve")

        retrieve(fetch, policy)

        no_data_calls = [c for c in mock_warning.call_args_list if c.args and c.args[0] == "mb_retrieve_no_data"]
        assert len(no_data_calls) == 1

    def test_no_data_fetch_called_once_no_retry(self, mocker: MockerFixture) -> None:
        """NO_DATA classification does not retry — fetch is called exactly once.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_no_data, max_attempts=6)

        retrieve(fetch, policy)

        fetch.assert_called_once()

    def test_no_data_with_http_404_classifier(self, mocker: MockerFixture) -> None:
        """Structured classifier correctly routes 404 to NO_DATA; returns None.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_http_status_classifier)

        result = retrieve(fetch, policy)

        assert result is None

    def test_no_data_log_fields_included(self, mocker: MockerFixture) -> None:
        """log_fields from NetPolicy are included in the NO_DATA warning log line.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_warning = mocker.patch("music_annotator._net.log.warning")
        err = HTTPError("https://example.com/", 404, "Not Found", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_no_data, event="caa_retrieve", log_fields={"release_id": "rel-123"})

        retrieve(fetch, policy)

        no_data_calls = [c for c in mock_warning.call_args_list if c.args and c.args[0] == "caa_retrieve_no_data"]
        assert len(no_data_calls) == 1
        assert no_data_calls[0].kwargs.get("release_id") == "rel-123"


# ---------------------------------------------------------------------------
# KAT 3 — FATAL raises immediately (no retry loop)
# ---------------------------------------------------------------------------


class TestFatalImmediate:
    """KAT 3: FATAL classification raises immediately without retrying.

    Pins C-NET-TERM: "FATAL → re-raise the original exception; exactly one log.error('<event>_fatal')."
    """

    def test_fatal_raises_immediately(self, mocker: MockerFixture) -> None:
        """FATAL classification raises on the first attempt without retrying.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = ValueError("malformed request")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_fatal, max_attempts=6)

        with pytest.raises(ValueError):
            retrieve(fetch, policy)

        fetch.assert_called_once()

    def test_fatal_re_raises_original_exception(self, mocker: MockerFixture) -> None:
        """FATAL re-raises the exact original exception object, not a wrapper.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = RuntimeError("permanent failure")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_fatal)

        with pytest.raises(RuntimeError) as exc_info:
            retrieve(fetch, policy)

        assert exc_info.value is err

    def test_fatal_emits_exactly_one_error(self, mocker: MockerFixture) -> None:
        """FATAL emits exactly one log.error('<event>_fatal') — not zero, not two.

        Pins the exactly-one-log invariant from C-NET-TERM.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        err = RuntimeError("permanent failure")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_fatal, event="acoustid_retrieve")

        with pytest.raises(RuntimeError):
            retrieve(fetch, policy)

        fatal_calls = [c for c in mock_error.call_args_list if c.args and c.args[0] == "acoustid_retrieve_fatal"]
        assert len(fatal_calls) == 1

    def test_fatal_with_http_4xx_non_404_classifier(self, mocker: MockerFixture) -> None:
        """Structured classifier routes non-404 4xx (e.g. 400) to FATAL; raises immediately.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        err = HTTPError("https://example.com/", 400, "Bad Request", {}, None)  # type: ignore[arg-type]
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_http_status_classifier, max_attempts=6)

        with pytest.raises(HTTPError) as exc_info:
            retrieve(fetch, policy)

        assert exc_info.value.code == 400
        fetch.assert_called_once()

    def test_fatal_log_fields_included(self, mocker: MockerFixture) -> None:
        """log_fields from NetPolicy are included in the FATAL error log line.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        err = RuntimeError("permanent failure")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_fatal, event="mb_retrieve", log_fields={"recording_id": "rec-456"})

        with pytest.raises(RuntimeError):
            retrieve(fetch, policy)

        fatal_calls = [c for c in mock_error.call_args_list if c.args and c.args[0] == "mb_retrieve_fatal"]
        assert len(fatal_calls) == 1
        assert fatal_calls[0].kwargs.get("recording_id") == "rec-456"


# ---------------------------------------------------------------------------
# KAT 4 — Exactly one log.error on fatal exhaustion
# ---------------------------------------------------------------------------


class TestExactlyOneLogOnFatalExhaustion:
    """KAT 4: The terminal choke point emits exactly one log.error on RETRY exhaustion.

    Pins C-NET-TERM: "exhaustion → FATAL terminal; exactly one log.error('<event>_fatal')."
    The exactly-one-log invariant: per terminal event, exactly one log.error (FATAL / exhaustion)
    or exactly one log.warning (NO_DATA) is emitted at the choke point — not zero, not two.
    """

    def test_exhaustion_emits_exactly_one_error(self, mocker: MockerFixture) -> None:
        """RETRY exhaustion emits exactly one log.error, not zero, not two.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        err = OSError("transient")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_retry, max_attempts=3, event="mb_retrieve")

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        fatal_calls = [c for c in mock_error.call_args_list if c.args and c.args[0] == "mb_retrieve_fatal"]
        assert len(fatal_calls) == 1

    def test_exhaustion_no_error_log_before_last_attempt(self, mocker: MockerFixture) -> None:
        """log.error is not called during retry attempts — only at the exhaustion choke point.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        err = OSError("transient")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_retry, max_attempts=5, event="caa_retrieve")

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        # Exactly one error log total — not one per attempt
        assert mock_error.call_count == 1

    def test_exhaustion_warning_not_emitted_as_error(self, mocker: MockerFixture) -> None:
        """Retry-attempt warnings are log.warning, not log.error; error is only at exhaustion.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        mock_warning = mocker.patch("music_annotator._net.log.warning")
        err = OSError("transient")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(_always_retry, max_attempts=3, event="acoustid_retrieve")

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        # Per-attempt retry warnings are log.warning
        retry_warnings = [c for c in mock_warning.call_args_list if c.args and "retry" in c.args[0]]
        assert len(retry_warnings) == 3
        # Exactly one error at exhaustion
        assert mock_error.call_count == 1

    def test_exhaustion_log_fields_in_error(self, mocker: MockerFixture) -> None:
        """log_fields are included in the exhaustion error log line.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        err = OSError("transient")
        fetch = mocker.MagicMock(side_effect=err)
        policy = _make_policy(
            _always_retry,
            max_attempts=2,
            event="mb_retrieve",
            log_fields={"release_id": "rel-789"},
        )

        with pytest.raises(OSError):
            retrieve(fetch, policy)

        fatal_calls = [c for c in mock_error.call_args_list if c.args and c.args[0] == "mb_retrieve_fatal"]
        assert len(fatal_calls) == 1
        assert fatal_calls[0].kwargs.get("release_id") == "rel-789"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestSuccessPath:
    """Tests for the success path: polite delay, return value, no log.error."""

    def test_success_returns_fetch_value(self, mocker: MockerFixture) -> None:
        """A successful fetch returns the fetch callable's return value.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        fetch = mocker.MagicMock(return_value={"data": "value"})
        policy = _make_policy(_always_fatal)

        result = retrieve(fetch, policy)

        assert result == {"data": "value"}

    def test_success_sleeps_polite_delay(self, mocker: MockerFixture) -> None:
        """A successful fetch sleeps polite_delay_s after returning the value.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._net.time.sleep")
        fetch = mocker.MagicMock(return_value="ok")
        policy = _make_policy(_always_fatal, polite_delay_s=1.5)

        retrieve(fetch, policy)

        mock_sleep.assert_called_once_with(1.5)

    def test_success_emits_no_error_log(self, mocker: MockerFixture) -> None:
        """A successful fetch emits no log.error.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        mock_error = mocker.patch("music_annotator._net.log.error")
        fetch = mocker.MagicMock(return_value="ok")
        policy = _make_policy(_always_fatal)

        retrieve(fetch, policy)

        mock_error.assert_not_called()

    def test_success_fetch_called_once(self, mocker: MockerFixture) -> None:
        """A successful fetch calls the fetch callable exactly once.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._net.time.sleep")
        fetch = mocker.MagicMock(return_value=42)
        policy = _make_policy(_always_fatal)

        retrieve(fetch, policy)

        fetch.assert_called_once()
