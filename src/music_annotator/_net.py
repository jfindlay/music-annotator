"""Policy-parameterized network retrieval core for music-annotator.

Provides a single retry/backoff/terminal engine — :func:`retrieve` — parameterized by a
:class:`NetPolicy` that carries a structured retryable-classifier, logging context, and backoff
knobs.  Every remote fetch in the codebase routes through this module.

The universal terminal rule (C-NET-TERM): a retrieval that *might* have succeeded but failed to
complete is an error, never a silent empty.  The three-outcome :class:`RetryDecision` enum encodes
this rule; :func:`retrieve` is the single shared choke point that actions it uniformly across MB,
CAA, and AcoustID.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_T = TypeVar("_T")


class RetryDecision(enum.Enum):
    """Classifier verdict for one failed fetch attempt — the load-bearing three-outcome shape.

    The three outcomes encode the universal terminal rule (C-NET-TERM).  Classifiers supplied by
    callers map a raised exception to one of these outcomes by reading *typed* attributes
    of the exception — status codes / exception types — never ``str(exc)``.

    See the ordering rule on :data:`RetryableClassifier`: ``HTTPError`` is a subclass of ``OSError``;
    classifiers must extract the typed status code before any broad ``OSError`` check.
    """

    RETRY = enum.auto()  # transient; back off and re-attempt (exhaustion → FATAL terminal)
    NO_DATA = enum.auto()  # server authoritatively answered "no data" → warning + return None
    FATAL = enum.auto()  # permanent, non-no-data failure → error + raise immediately


type RetryableClassifier = Callable[[Exception], RetryDecision]
"""Structured retryable-classifier mapping a raised exception to a :class:`RetryDecision`.

Must read *typed* attributes of the exception — status codes / exception types — **never**
``str(exc)``.

**Ordering rule (load-bearing, KAT-pinned).** ``urllib.error.HTTPError`` is a subclass of
``URLError`` which is a subclass of ``OSError``; ``mb.ResponseError`` wraps the original
``HTTPError`` on its ``.cause`` attribute.  A classifier that tests ``isinstance(exc, OSError)``
before extracting an HTTP status code will misclassify a 4xx as a transient transport failure.
Every classifier MUST extract the typed status code (via ``exc.code`` for ``HTTPError``, or
``exc.cause.code`` for ``mb.ResponseError``) before any broad ``OSError``/transport check.
Classifiers are supplied by the consuming call site; the core never inspects exception
structure itself.
"""


@dataclass(frozen=True)
class NetPolicy:
    """Caller-supplied retrieval policy: classifier + logging context + backoff knobs.

    Frozen dataclass; carries the structured retryable-classifier, logging context, and the
    over-specified backoff knobs.  Over-specification per the substrate-row rule: ``max_attempts``
    and ``backoff_base`` are tunable even though MB's historical values (6 / 2.0) are the only
    callers today — adapters will want to tune these.

    :ivar classify: Structured retryable-classifier mapping a raised exception to a RetryDecision.
        Must read typed status codes / exception types, never str(exc). See the ordering rule on
        RetryableClassifier.
    :ivar event: structlog event name for this retrieval class (e.g. "mb_retrieve", "caa_retrieve",
        "acoustid_retrieve"); used as the base event for warning (NO_DATA) and error (FATAL) logs.
    :ivar log_fields: Static rich fields merged into every log line for this retrieval (e.g.
        {"release_id": ...}); the terminal choke point adds attempt/wait/outcome fields.
    :ivar max_attempts: Maximum fetch attempts before exhaustion → FATAL terminal. Over-specified per
        the substrate-row rule; MB's historical value is 6.
    :ivar backoff_base: Base of the exponential back-off; sleep before attempt n is backoff_base ** n
        seconds. Over-specified; MB's historical value is 2.
    :ivar polite_delay_s: Seconds to sleep after a successful fetch to honour the 1 req/s posture
        (folds the generic body of the old _mb_call). Defaults to 1.0.
    """

    classify: RetryableClassifier
    event: str
    log_fields: dict[str, object] = field(default_factory=dict)
    max_attempts: int = 6
    backoff_base: float = 2.0
    polite_delay_s: float = 1.0


def retrieve(fetch: Callable[[], _T], policy: NetPolicy) -> _T | None:
    """Execute a zero-arg fetch under the policy's retry/backoff and universal terminal rule.

    Loops up to ``policy.max_attempts``.  Each raised exception is handed to ``policy.classify``:

    - ``RETRY``   → sleep ``policy.backoff_base ** attempt`` seconds, re-attempt; exhausting all
                    attempts is treated as the FATAL terminal (cannot-determine → raise).
    - ``NO_DATA`` → emit exactly one warning log (``"<event>_no_data"``), return ``None`` (no raise).
    - ``FATAL``   → emit exactly one error log (``"<event>_fatal"``), re-raise the original exception.

    On success, sleep ``policy.polite_delay_s`` (the folded polite delay) and return the fetch value.
    The polite delay is slept on the success path only — not on RETRY backoff sleeps, mirroring the
    old ``_mb_call`` / ``_mb_retry`` separation.

    Exhaustion re-raises the **last** caught exception (preserving the transport ``.cause`` chain)
    rather than synthesising a bare ``RuntimeError``.  Both RETRY exhaustion and an explicit FATAL
    classification share the single error-log choke point and both raise.

    :param fetch: Zero-arg callable performing exactly one network request; may raise.
    :param policy: The NetPolicy governing classification, logging, backoff, and polite delay.
    :returns: The fetch's value on success, or None when the server authoritatively answered no-data.
    :raises Exception: Re-raises the original exception on a FATAL classification or on RETRY
        exhaustion (the cannot-determine terminal). The raise propagates to the per-release error
        boundary in discover().
    """
    last_exc: Exception | None = None

    for attempt in range(policy.max_attempts):
        try:
            result = fetch()
            time.sleep(policy.polite_delay_s)
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            decision = policy.classify(exc)
            match decision:
                case RetryDecision.RETRY:
                    wait = policy.backoff_base**attempt
                    log.warning(
                        f"{policy.event}_retry",
                        attempt=attempt,
                        wait_s=wait,
                        **policy.log_fields,
                    )
                    time.sleep(wait)
                case RetryDecision.NO_DATA:
                    log.warning(f"{policy.event}_no_data", **policy.log_fields)
                    return None
                case RetryDecision.FATAL:
                    log.error(f"{policy.event}_fatal", **policy.log_fields)
                    raise
                case _:  # pragma: no cover
                    raise

    # RETRY exhaustion: cannot-determine → FATAL terminal (exactly one error log, then raise).
    log.error(f"{policy.event}_fatal", **policy.log_fields)
    assert last_exc is not None  # loop ran at least once (max_attempts >= 1)
    raise last_exc
