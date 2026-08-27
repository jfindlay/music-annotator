"""CLI entry point for music-annotator.

Configures structlog for human-friendly console output and exposes fifteen subcommands:

* ``apply``                    — copy and tag a directory of tracks for a known MusicBrainz release MBID.
* ``search``                   — search MusicBrainz for a release matching a source directory, prompt for
  confirmation, then apply tags.
* ``prune``                    — read the journal, verify source and destination file presence, and prompt to
  delete the source directory.
* ``repath``                   — re-path all verified library files to their corrected destinations under
  the current path-construction policy, using only embedded tags (no network calls).
* ``regroup``                  — consolidate confirmed split-release files into their canonical destinations
  (acts on case-(b) fragmentation detected by ``audit``).
* ``audit``                    — read the journal and report release-fragmentation anomalies (no network calls,
  no filesystem writes).  Read-only.
* ``enrich``                   — retroactively backfill fingerprint fields (audio_hash, acoustid_fingerprint,
  acoustid_id) into library files that are missing them.  Idempotent.
* ``diff``                     — diff the on-disk journal against a freshly-rebuilt in-memory cache and report
  matches, stale, and leaked entries.
* ``origin-time``              — migrate rip/download origin-time from the journal into authoritative sidecar
  YAML files (freedb_disc_N.yaml or music_annotator_provenance.yaml).  Idempotent.
* ``rebuild``                  — walk the library, read tags and sidecars per file, and emit a new journal
  (dry-run by default; use ``--apply`` to replace the on-disk journal).
* ``unify``                    — consolidate performer-split fragmented releases into their canonical top_dirs
  (detects releases with ≥2 top_dirs sharing the same ``MUSICBRAINZ_ALBUMID`` tag).
* ``repatch-acoustid``         — migrate the legacy ``CHROMAPRINT_FP`` fingerprint key to the Picard-aligned
  ``ACOUSTID_FINGERPRINT`` key, and (when ``--acoustid-key`` is supplied) re-source ``ACOUSTID_ID``
  from the fingerprint ``/v2/lookup`` endpoint.  Idempotent.
* ``repatch-catalogue-colon``  — rewrite ``CWP_PART_*`` / ``CWP_GROUPHEADING`` tags corrupted by the
  pre-fix bare-``":"`` split (catalogue-colon labels such as Hoboken ``"Hob. III:31"``).  Idempotent.
  Supports ``--dry-run`` to preview planned repatches without writing any tags or journal entries.
* ``preflight``                — run all maintenance passes with ``dry_run=True`` over ``dest_dir`` and
  emit a consolidated report of planned changes, cross-pass overlaps, journal capacity, and
  ``Reference/`` snapshot evidence.  Supports ``--json PATH`` to serialise the report to JSON.
* ``reconstruct-xrefs``        — census the journal for destructive-choice shapes (SKIP and OVERWRITE
  collision policies) and write secondary release MBIDs as cross-reference tags on surviving files.
  Offline; supports ``--dry-run`` to preview findings without writing tags or journal entries.

Usage::

    music-annotator apply \\
        <src_dir> <dest_dir> \\
        --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
        --user-agent-email contact@example.com \\
        [--user-agent-app "MyApp/1.0"] \\
        [--dry-run] [--no-fetch-rels] [--delete] [--no-cache] [--disc N]

    music-annotator search \\
        <src_dir> <dest_dir> \\
        --user-agent-email contact@example.com \\
        [--user-agent-app "MyApp/1.0"] \\
        [--limit 10] [--dry-run] [--no-fetch-rels] [--delete] [--no-cache]

    music-annotator prune \\
        <src_dir> <dest_dir> \\
        [-y/--yes]

    music-annotator repath \\
        <dest_dir> \\
        [--dry-run] [-y/--yes]

    music-annotator regroup \\
        <dest_dir> \\
        [--dry-run] [-y/--yes]

    music-annotator audit \\
        <dest_dir>

    music-annotator enrich \\
        <dest_dir> \\
        [--dry-run] [--re-resolve] [--acoustid-key KEY]

    music-annotator diff \\
        <dest_dir>

    music-annotator origin-time \\
        <dest_dir> \\
        [--dry-run]

    music-annotator rebuild \\
        <dest_dir> \\
        [--dry-run | --apply]

    music-annotator unify \\
        <dest_dir> \\
        [--user-agent-app "AppName/Version"] [--user-agent-email contact@example.com] \\
        [--dry-run] [-y/--yes]

    music-annotator repatch-acoustid \\
        <dest_dir> \\
        [--acoustid-key KEY] [--dry-run]

    music-annotator repatch-catalogue-colon \\
        <dest_dir> \\
        [--dry-run]

    music-annotator preflight \\
        <dest_dir> \\
        [--user-agent-app "AppName/Version"] [--user-agent-email contact@example.com] \\
        [--journal-path PATH] [--json PATH]

    music-annotator reconstruct-xrefs \\
        <dest_dir> \\
        [--dry-run]
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import structlog

import music_annotator

_VERSION: str = importlib.metadata.version("music-annotator")
_DEFAULT_USER_AGENT_APP: str = f"MusicAnnotator/{_VERSION}"


def _resolve_path(value: str) -> Path:
    """Expand user home directory, resolve symlinks, and make a path absolute.

    Applied as the ``type=`` callable on every path argument so that all paths
    reaching the rest of the program are fully resolved before any existence
    checks occur.

    :param value: The raw string value supplied on the command line.
    :returns: A fully resolved :class:`~pathlib.Path`.
    """
    return Path(value).expanduser().resolve()


def _configure_logging(verbose: bool, no_color: bool = False) -> None:
    """Set up structlog with a human-readable console renderer writing to stderr.

    Processors are chained in the standard structlog order: level filter, logger name, log level, positional argument
    formatting, ISO timestamp, stack-info rendering, exception formatting, and finally the console renderer.

    :param verbose: When ``True``, set the root log level to ``DEBUG``; otherwise use ``INFO``.
    :param no_color: When ``True``, disable color in :mod:`music_annotator` interactive output and in structlog log lines.
    """
    music_annotator.configure_color(enabled=not no_color)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )
    # musicbrainzngs uses logger "musicbrainzngs" (defined in mbxml.py) and logs INFO messages for every XML field its parser
    # has no explicit handler for.  Most of these are genuinely harmless (direction/begin/end/ended/artist/work on relations ARE
    # parsed correctly; the messages fire due to a name collision between the elements list and inner_els dict).  Two fields are
    # real data losses: artist/label/etc. "type-id" attribute (UUID form of the type string) and recording "first-release-date".
    # Neither is currently used by music-annotator.
    #
    # The upstream fix in musicbrainzngs: add type-id to parse_artist attribs list, add first-release-date to parse_recording
    # elements list — consider forking or contributing.
    logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=not no_color),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _dispatch(fn: Callable[[], object], log_event: str, **log_kwargs: str) -> None:
    """Call *fn* and convert any exception or keyboard interrupt into a logged error and ``SystemExit(1)``.

    Encapsulates the repetitive ``try/except`` pattern shared by all subcommand dispatch arms
    (excluding ``prune``, which continues on per-file errors rather than aborting).

    On :class:`KeyboardInterrupt`, logs ``"interrupted"`` at WARNING level and exits with code 1.
    On any other exception, logs *log_event* at ERROR level with ``error=str(exc)``,
    ``exc_info=True``, and any additional *log_kwargs*, then exits with code 1.

    :param fn: Zero-argument callable that performs the subcommand's work (use a lambda or
        :func:`functools.partial` to pre-bind arguments).  The return value is discarded.
    :param log_event: The structlog event key passed to ``log.error()``.
    :param log_kwargs: Additional keyword arguments forwarded to ``log.error()`` alongside
        ``error`` and ``exc_info``.
    :raises SystemExit: With code 1 on any exception or keyboard interrupt.
    """
    log = structlog.get_logger(__name__)
    try:
        fn()
    except KeyboardInterrupt:
        log.warning("interrupted")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.error(log_event, error=str(exc), exc_info=True, **log_kwargs)
        sys.exit(1)


class _Formatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Combined formatter that shows argument defaults and preserves raw epilog/description formatting."""


def _add_acoustid_arg(parser: argparse.ArgumentParser) -> None:
    """Add the ``--acoustid-key`` argument to a parser.

    Extracted so the argument can be registered on both the ``apply``/``search`` common-args
    group (via :func:`_add_common_args`) and the ``audit`` subcommand parser independently.

    :param parser: The parser or argument group to which the argument is added.
    """
    parser.add_argument(
        "--acoustid-key",
        metavar="KEY",
        default="",
        help="AcoustID API key for keyed fingerprint lookup (rung 5).",
    )


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by the ``apply`` and ``search`` subcommands.

    Shared arguments are: ``--user-agent-app``, ``--user-agent-email``, ``--dry-run``,
    ``--no-fetch-rels``, ``-d``/``--delete``, ``--no-cache``, and ``--acoustid-key``.
    ``-v``/``--verbose`` lives on the top-level parser so it must appear before the subcommand token.

    :param parser: The subcommand parser to which the arguments are added.
    """
    parser.add_argument(
        "--user-agent-app",
        default=_DEFAULT_USER_AGENT_APP,
        metavar="STRING",
        help='MusicBrainz user-agent app token in the form "AppName/Version" (default: %(default)s).',
    )
    parser.add_argument(
        "--user-agent-email",
        required=True,
        metavar="EMAIL",
        help="Contact e-mail address included in the MusicBrainz user-agent string.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned operations without copying or writing any files.",
    )
    parser.add_argument(
        "--no-fetch-rels",
        action="store_true",
        help=(
            "Skip per-recording relationship lookups (faster but produces minimal tags). Composer, conductor, work hierarchy, "
            "and Classical Extras tags will be absent."
        ),
    )
    parser.add_argument(
        "-d",
        "--delete",
        action="store_true",
        help="After a successful copy, prompt to delete the source directory.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass all on-disk metadata and image caches; always fetch from the network.",
    )
    _add_acoustid_arg(parser)


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level CLI argument parser with all subcommands.

    ``-v``/``--verbose`` is registered on the top-level parser and must appear before the subcommand token.
    ``apply`` takes a single ``src_dir`` positional (one release per invocation, paired with ``--release-id``).
    ``search`` and ``prune`` accept one or more ``src_dir`` positionals so an entire staging area can be
    processed in a single invocation.  ``dest_dir`` is always singular — all sources share the same library
    root.  ``apply`` and ``search`` share ``--user-agent-app``, ``--user-agent-email``, ``--dry-run``,
    ``--no-fetch-rels``, and ``--delete`` via :func:`_add_common_args`.  ``prune`` only adds ``-y``/``--yes``.
    ``repath`` takes only ``dest_dir`` and an optional ``--dry-run`` flag.
    ``audit`` takes only ``dest_dir`` and requires no network credentials (read-only journal analysis).
    ``enrich`` retroactively backfills fingerprint fields; accepts ``--dry-run``, ``--re-resolve``, and
    ``--acoustid-key`` (via :func:`_add_acoustid_arg`).
    ``diff`` diffs the on-disk journal against a freshly-rebuilt in-memory cache and reports matches, stale,
    and leaked entries.
    ``origin-time`` migrates rip/download origin-time from the journal into authoritative sidecar YAML files
    (idempotent); accepts ``--dry-run``.
    ``rebuild`` walks the library and reconstructs the journal from embedded tags and sidecars; default is
    dry-run (no write); pass ``--apply`` to replace the on-disk journal.
    ``unify`` scans the library for releases fragmented across ≥2 top_dirs (by ``MUSICBRAINZ_ALBUMID`` tag),
    computes the canonical top_dir for each, and moves the fragments.  Supports ``--dry-run`` and ``-y``/``--yes``.
    ``repatch-acoustid`` migrates the legacy ``CHROMAPRINT_FP`` fingerprint key to the Picard-aligned
    ``ACOUSTID_FINGERPRINT`` key; accepts ``--acoustid-key`` and ``--dry-run``.

    :returns: A fully configured :class:`argparse.ArgumentParser` instance.
    """
    parser = argparse.ArgumentParser(
        prog="music-annotator",
        description=("Copy and tag classical music albums using MusicBrainz metadata and Classical Extras conventions."),
        formatter_class=_Formatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {_VERSION}",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging (must appear before the subcommand).",
    )
    parser.add_argument(
        "-C",
        "--no-color",
        action="store_true",
        help="Disable color output (must appear before the subcommand).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    subparsers.required = True

    # ------------------------------------------------------------------
    # apply subcommand
    # ------------------------------------------------------------------
    apply_parser = subparsers.add_parser(
        "apply",
        help="Copy and tag a source directory for a known MusicBrainz release MBID.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Examples:
              music-annotator apply \\
                  "/mnt/music/Respighi - Pini di Roma" /tmp/music_library \\
                  --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
                  --user-agent-email tagger@example.com

              music-annotator apply \\
                  "/mnt/music/Respighi - Pini di Roma" /tmp/music_library \\
                  --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
                  --user-agent-email tagger@example.com \\
                  --user-agent-app "MyTagger/1.0" \\
                  --dry-run --delete
            """),
    )
    apply_parser.add_argument(
        "src_dir",
        metavar="src_dir",
        type=_resolve_path,
        help="Directory containing the source audio files to copy and tag.",
    )
    apply_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root destination directory for the annotated music library.",
    )
    apply_parser.add_argument(
        "--release-id",
        required=True,
        metavar="MBID",
        help="MusicBrainz release MBID (UUID) to fetch metadata for.",
    )
    apply_parser.add_argument(
        "--disc",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Force selection of disc number N (1-based position) in a multi-disc release, "
            "bypassing automatic medium-selection heuristics.  The source file count must still "
            "match the selected disc's track count."
        ),
    )
    _add_common_args(apply_parser)

    # ------------------------------------------------------------------
    # search subcommand
    # ------------------------------------------------------------------
    search_parser = subparsers.add_parser(
        "search",
        help="Search MusicBrainz for a release matching a source directory, confirm, and apply.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Examples:
              music-annotator search \\
                  "/mnt/music/Respighi - Pini di Roma" /tmp/music_library \\
                  --user-agent-email tagger@example.com

              music-annotator search \\
                  "/mnt/music/Brahms - Sym 1" \\
                  "/mnt/music/Brahms - Sym 2" \\
                  /tmp/music_library \\
                  --user-agent-email tagger@example.com \\
                  --limit 5 --dry-run --delete
            """),
    )
    search_parser.add_argument(
        "src_dirs",
        metavar="src_dir",
        type=_resolve_path,
        nargs="+",
        help="One or more source directories to search and tag.",
    )
    search_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root destination directory for the annotated music library.",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of MusicBrainz search candidates to display.",
    )
    _add_common_args(search_parser)

    # ------------------------------------------------------------------
    # prune subcommand
    # ------------------------------------------------------------------
    prune_parser = subparsers.add_parser(
        "prune",
        help="Verify journal entries and prompt to delete an already-annotated source directory.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Reads the journal at <dest_dir>/music_annotator_journal.json, checks that all source
            and destination files for each <src_dir> are present and exactly as recorded, then
            offers to delete each source directory in turn.

            Examples:
              music-annotator prune \\
                  "/mnt/music/Respighi - Pini di Roma" /tmp/music_library

              music-annotator prune \\
                  "/mnt/music/Respighi - Pini di Roma" \\
                  "/mnt/music/Brahms - Sym 1" \\
                  /tmp/music_library \\
                  --yes
            """),
    )
    prune_parser.add_argument(
        "src_dirs",
        metavar="src_dir",
        type=_resolve_path,
        nargs="+",
        help="One or more source directories to inspect and potentially delete.",
    )
    prune_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root destination directory where the journal lives.",
    )
    prune_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and delete the source directory immediately.",
    )

    # ------------------------------------------------------------------
    # repath subcommand
    # ------------------------------------------------------------------
    repath_parser = subparsers.add_parser(
        "repath",
        help="Re-path all verified library files to corrected destinations (no network calls).",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            WARNING: A bare 'repath <dest_dir>' invocation MASS-RELOCATES the entire library.
            The action="repathed" journal entries in music_annotator_journal.json are the
            complete recovery record.  Use --dry-run first to preview all planned moves.

            repath walks the library at <dest_dir>, reads the journal to identify verified
            library files (action "tagged" or "repathed"), recomputes each file's destination
            from its embedded tags alone (no MusicBrainz lookups), and moves files whose
            current path differs from the recomputed path.

            Use this after a path-policy change (e.g. the L0/L1 leaf-numbering fix) to bring
            an existing library forward without re-ingesting from source.

            Examples:
              music-annotator repath /tmp/music_library --dry-run
              music-annotator repath /tmp/music_library
            """),
    )
    repath_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    repath_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned moves without performing any filesystem operations or writing journal entries.",
    )
    repath_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and move files immediately.",
    )

    # ------------------------------------------------------------------
    # regroup subcommand
    # ------------------------------------------------------------------
    regroup_parser = subparsers.add_parser(
        "regroup",
        help="Consolidate confirmed split-release files into their canonical destinations.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Reads the journal at <dest_dir>/music_annotator_journal.json, identifies release MBIDs
            whose tracks are confirmed (via embedded MUSICBRAINZ_ALBUMID tags) to be scattered
            across more than one work directory, and moves those files to the canonical destination
            implied by their embedded tags.

            No MusicBrainz network calls are made.  Only confirmed split-release candidates
            (case-b from 'audit') are acted on.

            Use --dry-run first to preview all planned moves.  Use -y/--yes to skip the
            confirmation prompt.

            Examples:
              music-annotator regroup /tmp/music_library --dry-run
              music-annotator regroup /tmp/music_library
              music-annotator regroup /tmp/music_library --yes
            """),
    )
    regroup_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    regroup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned moves without performing any filesystem operations or writing journal entries.",
    )
    regroup_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and move files immediately.",
    )

    # ------------------------------------------------------------------
    # audit subcommand (read-only)
    # ------------------------------------------------------------------
    audit_parser = subparsers.add_parser(
        "audit",
        help="Report release-fragmentation anomalies in the journal (no network calls, no writes).",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Reads the journal at <dest_dir>/music_annotator_journal.json and reports:

              (a) work directories populated from more than one MusicBrainz release MBID
                  (regrouping candidates).
              (b) release MBIDs whose tracks landed in more than one work directory
                  (split releases).

            No network calls are made.  No files are moved.  No journal entries are written.

            To backfill fingerprint fields, use: music-annotator enrich <dest_dir>
            To diff the journal against a rebuild, use: music-annotator diff <dest_dir>
            To migrate origin-time provenance, use: music-annotator origin-time <dest_dir>

            Examples:
              music-annotator audit /tmp/music_library
            """),
    )
    audit_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )

    # ------------------------------------------------------------------
    # enrich subcommand
    # ------------------------------------------------------------------
    enrich_parser = subparsers.add_parser(
        "enrich",
        help="Retroactively backfill fingerprint fields into library files that are missing them.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Retroactively backfills fingerprint fields (audio_hash, acoustid_fingerprint, acoustid_id)
            into library files that are missing them.  Idempotent: re-running on an already-enriched
            library is a no-op.

            Use --re-resolve to recompute acoustid_fingerprint even when already present.
            audio_hash is never recomputed (anchor rule).

            Use --acoustid-key to enable keyed AcoustID fingerprint lookup (rung 5).

            Examples:
              music-annotator enrich /tmp/music_library
              music-annotator enrich /tmp/music_library --dry-run
              music-annotator enrich /tmp/music_library --re-resolve
              music-annotator enrich /tmp/music_library --acoustid-key MY_KEY
            """),
    )
    enrich_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    enrich_parser.add_argument(
        "--re-resolve",
        action="store_true",
        dest="re_resolve",
        help=("Recompute acoustid_fingerprint even when already present.  audio_hash is never recomputed (anchor rule)."),
    )
    enrich_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned changes without writing any files.",
    )
    _add_acoustid_arg(enrich_parser)

    # ------------------------------------------------------------------
    # diff subcommand
    # ------------------------------------------------------------------
    diff_parser = subparsers.add_parser(
        "diff",
        help="Diff the on-disk journal against a freshly-rebuilt in-memory cache.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Diffs the on-disk journal against a freshly-rebuilt in-memory cache, field by field
            per destination path.  Reports three buckets:

              matches — all fields agree between journal and rebuild.
              stale   — journal path absent from rebuild (expected after repath/regroup).
              leaked  — journal has a field value not reproducible by rebuild (authority leak).

            No files are written.

            Examples:
              music-annotator diff /tmp/music_library
            """),
    )
    diff_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )

    # ------------------------------------------------------------------
    # origin-time subcommand
    # ------------------------------------------------------------------
    origin_time_parser = subparsers.add_parser(
        "origin-time",
        help="Migrate rip/download origin-time from the journal into authoritative sidecar YAML files.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Migrates rip/download origin-time from the journal into authoritative sidecar YAML files
            (freedb_disc_N.yaml or music_annotator_provenance.yaml).  Idempotent: re-running on a
            library where all sidecars already carry the provenance fields is a no-op.

            Examples:
              music-annotator origin-time /tmp/music_library
              music-annotator origin-time /tmp/music_library --dry-run
            """),
    )
    origin_time_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    origin_time_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned changes without writing any files.",
    )

    # ------------------------------------------------------------------
    # rebuild subcommand
    # ------------------------------------------------------------------
    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="Reconstruct the journal from embedded tags and sidecars (dry-run by default).",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Walks the library at <dest_dir>, reads embedded tags and sidecar YAML files per
            FLAC/MP3 file, and emits a new music_annotator_journal.json in the existing format.

            Default is dry-run: the rebuilt journal is computed and logged but the on-disk
            journal is NOT replaced.  Pass --apply to replace the journal.

            Use rebuild (without --apply) to prove the database-as-infrastructure claim: run it,
            diff against the existing journal.  Any unexplained non-match is a candidate authority
            leak or expected staleness (repathed/regrouped entries not yet re-scanned).

            Examples:
              music-annotator rebuild /tmp/music_library
              music-annotator rebuild /tmp/music_library --dry-run
              music-annotator rebuild /tmp/music_library --apply
            """),
    )
    rebuild_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    _rebuild_mode = rebuild_parser.add_mutually_exclusive_group()
    _rebuild_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Compute the rebuilt journal without writing it to disk (default).",
    )
    _rebuild_mode.add_argument(
        "--apply",
        action="store_true",
        help="Replace music_annotator_journal.json with the rebuilt journal.",
    )

    # ------------------------------------------------------------------
    # unify subcommand
    # ------------------------------------------------------------------
    unify_parser = subparsers.add_parser(
        "unify",
        help="Consolidate performer-split fragmented releases into their canonical top_dirs.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Scans the library at <dest_dir> for releases whose tracks are spread across ≥2
            distinct top_dirs (detected by reading MUSICBRAINZ_ALBUMID from embedded tags).
            For each fragmented release, computes the canonical destination for every file
            using build_dest_path over the full release group, and moves fragments to the
            canonical path.

            The join key is the embedded MUSICBRAINZ_ALBUMID tag, not the journal.  unify
            reads the embedded MUSICBRAINZ_ALBUMID join key and effects a determinate re-layout
            of the current library state.  The canonical name-form for each performer is the MB
            artist name field, read from embedded tags alone — no MusicBrainz network calls are
            made (NORM-2 as revised; alias hydration has been removed from the maintenance path).

            Use --dry-run first to preview all planned moves.  Use -y/--yes to skip the
            confirmation prompt.

            Examples:
              music-annotator unify /tmp/music_library --dry-run
              music-annotator unify /tmp/music_library
              music-annotator unify /tmp/music_library --yes
            """),
    )
    unify_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    unify_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned moves without performing any filesystem operations or writing journal entries.",
    )
    unify_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and move files immediately.",
    )
    unify_parser.add_argument(
        "--user-agent-app",
        default=_DEFAULT_USER_AGENT_APP,
        metavar="STRING",
        help='MusicBrainz user-agent app token in the form "AppName/Version" (default: %(default)s).',
    )
    unify_parser.add_argument(
        "--user-agent-email",
        default="",
        metavar="EMAIL",
        help=(
            "Contact e-mail address included in the MusicBrainz user-agent string.  "
            "Accepted for forward compatibility; unify is genuinely offline and does not "
            "require the user-agent for correct operation."
        ),
    )

    # ------------------------------------------------------------------
    # repatch-acoustid subcommand
    # ------------------------------------------------------------------
    repatch_acoustid_parser = subparsers.add_parser(
        "repatch-acoustid",
        help="Migrate the legacy CHROMAPRINT_FP fingerprint key to the Picard-aligned ACOUSTID_FINGERPRINT key.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Scans the library at <dest_dir> for files still carrying the legacy CHROMAPRINT_FP
            Vorbis Comment key (FLAC) or TXXX "Chromaprint Fingerprint" frame (MP3), migrates
            the fingerprint value to the Picard-aligned ACOUSTID_FINGERPRINT key, and removes
            the legacy key.

            When --acoustid-key is supplied and a fingerprint is present, re-sources ACOUSTID_ID
            from the fingerprint /v2/lookup endpoint (the same path 'enrich --re-resolve' uses).
            Without --acoustid-key, ACOUSTID_ID is left unchanged.

            Idempotent: a second run on a fully-migrated library is a no-op.
            Use --dry-run first to preview all planned migrations.

            Examples:
              music-annotator repatch-acoustid /tmp/music_library --dry-run
              music-annotator repatch-acoustid /tmp/music_library
              music-annotator repatch-acoustid /tmp/music_library --acoustid-key MY_KEY
            """),
    )
    repatch_acoustid_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    repatch_acoustid_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned migrations without writing any tags or journal entries.",
    )
    _add_acoustid_arg(repatch_acoustid_parser)

    # ------------------------------------------------------------------
    # repatch-catalogue-colon subcommand
    # ------------------------------------------------------------------
    repatch_cat_colon_parser = subparsers.add_parser(
        "repatch-catalogue-colon",
        help="Rewrite CWP_PART_* / CWP_GROUPHEADING tags corrupted by the pre-fix bare-':' split.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Scans the library at <dest_dir> for CWP_PART_{i} values produced by the retired
            bare-':' fallback in strip_common_prefix — a colon inside a catalogue number (e.g.
            Hoboken "Hob. III:31") caused the old split to truncate the label to a bare fragment
            ("31").  The forward fix keys on ": " (colon-followed-by-space) so new ingests are
            correct; this pass re-derives the corrected label offline from the CWP_WORK_{i} /
            CWP_WORK_{i+1} pair already embedded in the file — no MusicBrainz network call needed.

            Idempotent: a second run on a library where all labels are already correct is a no-op.
            Use --dry-run first to preview all planned repatches.

            Examples:
              music-annotator repatch-catalogue-colon /tmp/music_library --dry-run
              music-annotator repatch-catalogue-colon /tmp/music_library
            """),
    )
    repatch_cat_colon_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    repatch_cat_colon_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log planned repatches without writing any tags or journal entries.",
    )

    # ------------------------------------------------------------------
    # preflight subcommand
    # ------------------------------------------------------------------
    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Run all maintenance passes with dry_run=True and emit a consolidated preflight report.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Runs each of the six maintenance passes (repath, regroup, unify, enrich,
            repatch-catalogue-colon, repatch-acoustid) with dry_run=True over <dest_dir> and
            assembles a consolidated report containing:

              - Per-pass planned-change counts and cross-pass overlap counts.
              - Cross-pass overlap map: files appearing in more than one pass's plan.
              - Journal capacity: current entry count, on-disk size, and projected growth.
              - Reference/ evidence: presence and footprint of the Reference/ snapshot directory.

            When <dest_dir> is not mounted or is empty, prints a "scan not run" message and
            exits cleanly.  This is structurally distinct from a scan that ran and found nothing.

            Non-mutating: no files are moved, no tags are written, no journal entries are appended.

            Examples:
              music-annotator preflight /tmp/music_library
              music-annotator preflight /tmp/music_library --json /tmp/preflight.json
            """),
    )
    preflight_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    preflight_parser.add_argument(
        "--user-agent-app",
        default=_DEFAULT_USER_AGENT_APP,
        metavar="STRING",
        help='MusicBrainz user-agent app token in the form "AppName/Version" (default: %(default)s).',
    )
    preflight_parser.add_argument(
        "--user-agent-email",
        default="",
        metavar="EMAIL",
        help=(
            "Contact e-mail address included in the MusicBrainz user-agent string.  "
            "Accepted for forward compatibility; all maintenance passes are genuinely offline "
            "and do not require the user-agent for correct operation."
        ),
    )
    preflight_parser.add_argument(
        "--journal-path",
        metavar="PATH",
        type=_resolve_path,
        default=None,
        help=("Path to the journal file.  Defaults to <dest_dir>/music_annotator_journal.json when not supplied."),
    )
    preflight_parser.add_argument(
        "--json",
        metavar="PATH",
        type=_resolve_path,
        default=None,
        dest="json_path",
        help="Serialise the preflight report to JSON at the given path.",
    )

    # ------------------------------------------------------------------
    # reconstruct-xrefs subcommand
    # ------------------------------------------------------------------
    reconstruct_xrefs_parser = subparsers.add_parser(
        "reconstruct-xrefs",
        help="Census the journal for destructive-choice shapes and write secondary release MBIDs.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Reads the journal at <dest_dir>/music_annotator_journal.json and identifies two
            shapes of destructive collision choices made during ingest:

              SKIP policy:      a "skipped" entry at the same destination as a surviving
                                "tagged" entry.  The skipped entry's release MBID is a
                                secondary MBID for the surviving file.
              OVERWRITE policy: multiple "tagged" entries at one destination with distinct
                                release_ids.  The chronological-last entry is the primary;
                                earlier entries' release_ids are secondary MBIDs.

            Presents grouped findings; on operator confirmation, writes secondary release MBIDs
            into MUSICBRAINZ_SECONDARY_ALBUMID on each surviving file and journals each mutation
            as action="cross-referenced".

            Idempotent: secondary MBIDs already present in the file's tag or already journalled
            as "cross-referenced" are silently skipped.

            Also reports evidence-gap candidates: files where the journal shows only one
            "tagged" entry but the file currently carries a MUSICBRAINZ_SECONDARY_ALBUMID tag
            (suggesting a cross-reference was written outside the journal).

            The operator confirmation prompt is NOT suppressed by --yes (integrity prompts are
            not bulk consent).  Use --dry-run to preview findings without writing or prompting.

            Offline: no MusicBrainz network calls are made.

            Examples:
              music-annotator reconstruct-xrefs /tmp/music_library --dry-run
              music-annotator reconstruct-xrefs /tmp/music_library
            """),
    )
    reconstruct_xrefs_parser.add_argument(
        "dest_dir",
        metavar="dest_dir",
        type=_resolve_path,
        help="Root of the annotated music library (contains music_annotator_journal.json).",
    )
    reconstruct_xrefs_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report findings without writing tags, prompting, or appending journal entries.",
    )

    return parser


def main() -> None:
    """Parse CLI arguments, configure logging, and dispatch to subcommands.

    Supported subcommands: ``apply``, ``search``, ``prune``, ``repath``, ``regroup``, ``audit``,
    ``enrich``, ``diff``, ``origin-time``, ``rebuild``, ``unify``, ``repatch-acoustid``,
    ``repatch-catalogue-colon``, ``preflight``, ``reconstruct-xrefs``.

    The ``repath`` subcommand dispatches to :func:`~music_annotator.repath` with ``dry_run`` and
    ``yes`` forwarded from the parsed arguments.  The ``regroup`` subcommand dispatches to
    :func:`~music_annotator.regroup` with ``yes`` and ``dry_run`` forwarded.  The ``audit``
    subcommand dispatches to :func:`~music_annotator.audit` (read-only).  The ``enrich``
    subcommand dispatches to :func:`~music_annotator.enrich` with ``re_resolve``, ``dry_run``,
    and ``acoustid_key`` forwarded.  The ``diff`` subcommand dispatches to
    :func:`~music_annotator.diff_journal`.  The ``origin-time`` subcommand dispatches to
    :func:`~music_annotator.enrich_origin_time` with ``dry_run`` forwarded.  The ``rebuild``
    subcommand dispatches to :func:`~music_annotator.rebuild_journal` with ``dry_run=True``
    (default) or ``dry_run=False`` when ``--apply`` is passed.      The ``unify`` subcommand
    calls :func:`~music_annotator.init_mb` when a user-agent email was supplied (for forward
    compatibility), then dispatches to :func:`~music_annotator.unify`.  The unify pass is
    genuinely offline — it reads embedded tags alone and does not call
    :func:`~music_annotator._mb_api.fetch_artist_aliases` (NORM-2 as revised; alias hydration
    has been removed from the maintenance path).  The ``repatch-acoustid`` subcommand dispatches
    to :func:`~music_annotator.repatch_acoustid_tags` with ``acoustid_key`` and ``dry_run``
    forwarded.  The ``repatch-catalogue-colon`` subcommand dispatches to
    :func:`~music_annotator.repatch_catalogue_colon` with ``dry_run`` forwarded.  The
    ``preflight`` subcommand calls :func:`~music_annotator.init_mb` when a user-agent email was
    supplied (for forward compatibility), then dispatches to
    :func:`~music_annotator.compose_preflight_report` and prints the consolidated report; when
    ``scan_ran`` is ``False`` (root not mounted or empty), prints a clear "scan not run" message
    and exits cleanly.  The ``reconstruct-xrefs`` subcommand dispatches to
    :func:`~music_annotator.reconstruct_cross_references` with ``dry_run`` forwarded; the
    operator confirmation prompt is never suppressed by ``--yes`` (integrity prompts are not
    bulk consent).

    This function is the entry point registered as ``music-annotator`` in ``pyproject.toml``.  It validates source directories
    before delegating.  All subcommands except ``prune`` use :func:`_dispatch` to convert any unhandled exception or keyboard
    interrupt into a logged error with exit code 1.  ``prune`` handles errors per-file (continue-on-error) and only exits 1 on
    :class:`KeyboardInterrupt`.

    :raises SystemExit: With code 0 on success, code 1 on unrecoverable error.
    """
    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.verbose, no_color=args.no_color)

    log = structlog.get_logger(__name__)

    match args.subcommand:
        case "apply":
            if not args.src_dir.is_dir():
                log.error("src_dir_not_found", path=str(args.src_dir))
                sys.exit(1)
            user_agent = f"{args.user_agent_app} {args.user_agent_email}"
            _dispatch(
                lambda: music_annotator.run(
                    release_id=args.release_id,
                    src_dir=args.src_dir,
                    dest_root=args.dest_dir,
                    user_agent=user_agent,
                    dry_run=args.dry_run,
                    fetch_rels=not args.no_fetch_rels,
                    no_cache=args.no_cache,
                    disc_override=args.disc,
                    acoustid_key=args.acoustid_key,
                ),
                "fatal_error",
            )
            if args.delete and not args.dry_run:
                music_annotator.prompt_delete_src(args.src_dir)

        case "search":
            for src in args.src_dirs:
                if not src.is_dir():
                    log.error("src_dir_not_found", path=str(src))
                    sys.exit(1)
            user_agent = f"{args.user_agent_app} {args.user_agent_email}"
            _dispatch(
                lambda: music_annotator.discover(
                    src_dirs=args.src_dirs,
                    dest_root=args.dest_dir,
                    user_agent=user_agent,
                    dry_run=args.dry_run,
                    fetch_rels=not args.no_fetch_rels,
                    limit=args.limit,
                    delete=args.delete,
                    no_cache=args.no_cache,
                    acoustid_key=args.acoustid_key,
                ),
                "fatal_error",
            )

        case "prune":
            # prune is per-file: KeyboardInterrupt aborts, but other exceptions log and continue.
            for src in args.src_dirs:
                try:
                    music_annotator.prune_sources(
                        src_dir=src,
                        dest_root=args.dest_dir,
                        yes=args.yes,
                    )
                except KeyboardInterrupt:
                    log.warning("interrupted")
                    sys.exit(1)
                except Exception as exc:  # noqa: BLE001
                    log.error("prune_error", src_dir=str(src), error=str(exc), exc_info=True)

        case "repath":
            _dispatch(
                lambda: music_annotator.repath(dest_root=args.dest_dir, dry_run=args.dry_run, yes=args.yes),
                "repath_error",
                dest_root=str(args.dest_dir),
            )

        case "regroup":
            _dispatch(
                lambda: music_annotator.regroup(dest_root=args.dest_dir, yes=args.yes, dry_run=args.dry_run),
                "regroup_error",
                dest_root=str(args.dest_dir),
            )

        case "audit":
            _dispatch(
                lambda: music_annotator.audit(dest_root=args.dest_dir),
                "audit_error",
                dest_root=str(args.dest_dir),
            )

        case "enrich":
            _dispatch(
                lambda: music_annotator.enrich(
                    dest_root=args.dest_dir,
                    re_resolve=args.re_resolve,
                    dry_run=args.dry_run,
                    acoustid_key=args.acoustid_key,
                ),
                "enrich_error",
                dest_root=str(args.dest_dir),
            )

        case "diff":
            _dispatch(
                lambda: music_annotator.diff_journal(dest_root=args.dest_dir),
                "diff_error",
                dest_root=str(args.dest_dir),
            )

        case "origin-time":
            _dispatch(
                lambda: music_annotator.enrich_origin_time(
                    dest_root=args.dest_dir,
                    dry_run=args.dry_run,
                ),
                "origin_time_error",
                dest_root=str(args.dest_dir),
            )

        case "rebuild":
            _dispatch(
                lambda: music_annotator.rebuild_journal(
                    dest_root=args.dest_dir,
                    dry_run=not args.apply,
                ),
                "rebuild_error",
                dest_root=str(args.dest_dir),
            )

        case "unify":

            def _run_unify() -> None:
                """Initialise the MusicBrainz user-agent (if supplied) and run the unify pass.

                Calls :func:`~music_annotator.init_mb` when a user-agent email was supplied
                (for forward compatibility), then dispatches to :func:`~music_annotator.unify`.
                The unify pass is genuinely offline — it reads embedded tags alone and does not
                call :func:`~music_annotator._mb_api.fetch_artist_aliases` (NORM-2 as revised;
                alias hydration has been removed from the maintenance path).
                """
                music_annotator.init_mb(f"{args.user_agent_app} {args.user_agent_email}".strip())
                music_annotator.unify(dest_root=args.dest_dir, yes=args.yes, dry_run=args.dry_run)

            _dispatch(_run_unify, "unify_error", dest_root=str(args.dest_dir))

        case "repatch-acoustid":
            _dispatch(
                lambda: music_annotator.repatch_acoustid_tags(
                    journal=args.dest_dir / music_annotator.JOURNAL_FILENAME,
                    dest_root=args.dest_dir,
                    acoustid_key=args.acoustid_key,
                    dry_run=args.dry_run,
                ),
                "repatch_acoustid_error",
                dest_root=str(args.dest_dir),
            )

        case "repatch-catalogue-colon":
            _dispatch(
                lambda: music_annotator.repatch_catalogue_colon(
                    dest_root=args.dest_dir,
                    dry_run=args.dry_run,
                ),
                "repatch_catalogue_colon_error",
                dest_root=str(args.dest_dir),
            )

        case "preflight":
            _preflight_journal_path = (
                args.journal_path if args.journal_path is not None else args.dest_dir / music_annotator.JOURNAL_FILENAME
            )
            _preflight_json_path = args.json_path

            def _run_preflight() -> None:
                """Run compose_preflight_report and emit the consolidated report.

                Initialises the MusicBrainz user-agent via :func:`~music_annotator.init_mb`
                when a user-agent email was supplied (for forward compatibility), then calls
                :func:`~music_annotator.compose_preflight_report`.  All maintenance passes
                are genuinely offline — they read embedded tags alone and do not require the
                user-agent for correct operation (NORM-2 as revised).

                Calls :func:`~music_annotator.compose_preflight_report` with the resolved
                ``dest_root`` and ``journal_path``, prints a human-readable summary, and
                optionally serialises the report to JSON when ``--json`` was supplied.
                When ``scan_ran`` is ``False`` (root not mounted or empty), prints a clear
                "scan not run" message and returns without printing pass summaries.
                """
                music_annotator.init_mb(f"{args.user_agent_app} {args.user_agent_email}".strip())
                report = music_annotator.compose_preflight_report(
                    dest_root=args.dest_dir,
                    journal_path=_preflight_journal_path,
                )
                if not report.scan_ran:
                    print(
                        f"preflight: scan not run — '{args.dest_dir}' is not mounted or is empty.\n"
                        "Mount the library root and re-run to obtain preflight evidence."
                    )
                else:
                    print(f"\nPreflight report for: {args.dest_dir}\n")
                    print("Pass summaries:")
                    for summary in report.pass_summaries:
                        overlap_note = f"  ({summary.overlap_count} overlap)" if summary.overlap_count else ""
                        print(f"  {summary.pass_name:<30} {summary.count:>5} planned{overlap_note}")
                    total = sum(s.count for s in report.pass_summaries)
                    print(f"\n  {'TOTAL':<30} {total:>5} planned")
                    if report.overlaps:
                        print(f"\nCross-pass overlaps ({len(report.overlaps)} file(s) in >1 pass):")
                        for overlap in report.overlaps:
                            print(f"  {overlap.current_path}")
                            print(f"    passes: {', '.join(overlap.pass_names)}")
                    else:
                        print("\nCross-pass overlaps: none")
                    cap = report.journal_capacity
                    print("\nJournal capacity:")
                    print(f"  Current entries : {cap.current_entry_count}")
                    print(f"  Current size    : {cap.current_size_bytes} bytes")
                    print(f"  Projected delta : +{cap.projected_delta_entries} entries")
                    ref = report.reference_evidence
                    ref_status = f"present ({ref.size_bytes} bytes)" if ref.present else "absent"
                    print(f"\nReference/ snapshot: {ref_status}")
                if _preflight_json_path is not None:
                    _preflight_json_path.parent.mkdir(parents=True, exist_ok=True)
                    _preflight_json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
                    print(f"\nReport written to: {_preflight_json_path}")

            _dispatch(_run_preflight, "preflight_error", dest_root=str(args.dest_dir))

        case "reconstruct-xrefs":
            _dispatch(
                lambda: music_annotator.reconstruct_cross_references(
                    journal_path=args.dest_dir / music_annotator.JOURNAL_FILENAME,
                    dest_root=args.dest_dir,
                    dry_run=args.dry_run,
                ),
                "reconstruct_xrefs_error",
                dest_root=str(args.dest_dir),
            )

        case _:  # pragma: no cover
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
