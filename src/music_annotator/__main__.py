"""CLI entry point for music-annotator.

Configures structlog for human-friendly console output and exposes five subcommands:

* ``apply``  — copy and tag a directory of tracks for a known MusicBrainz release MBID.
* ``search`` — search MusicBrainz for a release matching a source directory, prompt for
  confirmation, then apply tags.
* ``prune``  — read the journal, verify source and destination file presence, and prompt to
  delete the source directory.
* ``repath`` — re-path all verified library files to their corrected destinations under
  the current path-construction policy, using only embedded tags (no network calls).
* ``audit``  — read the journal and report release-fragmentation anomalies (no network calls,
  no filesystem writes).

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
        [--dry-run]

    music-annotator audit \\
        <dest_dir>
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import sys
import textwrap
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


class _Formatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    """Combined formatter that shows argument defaults and preserves raw epilog/description formatting."""


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by the ``apply`` and ``search`` subcommands.

    Shared arguments are: ``--user-agent-app``, ``--user-agent-email``, ``--dry-run``,
    ``--no-fetch-rels``, and ``-d``/``--delete``.  ``-v``/``--verbose`` lives on the top-level
    parser so it must appear before the subcommand token.

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
        help="Bypass the cover art download cache; always fetch images from the network.",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level CLI argument parser with ``apply``, ``search``, ``prune``, ``repath``, and ``audit``
    subcommands.

    ``-v``/``--verbose`` is registered on the top-level parser and must appear before the subcommand token.
    ``apply`` takes a single ``src_dir`` positional (one release per invocation, paired with ``--release-id``).
    ``search`` and ``prune`` accept one or more ``src_dir`` positionals so an entire staging area can be
    processed in a single invocation.  ``dest_dir`` is always singular — all sources share the same library
    root.  ``apply`` and ``search`` share ``--user-agent-app``, ``--user-agent-email``, ``--dry-run``,
    ``--no-fetch-rels``, and ``--delete`` via :func:`_add_common_args`.  ``prune`` only adds ``-y``/``--yes``.
    ``repath`` takes only ``dest_dir`` and an optional ``--dry-run`` flag.  ``audit`` takes only ``dest_dir``
    and requires no network credentials (read-only journal analysis).

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
    # audit subcommand
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

    return parser


def main() -> None:
    """Parse CLI arguments, configure logging, and dispatch to subcommands.

    Supported subcommands: ``apply``, ``search``, ``prune``, ``repath``, ``regroup``, ``audit``.

    This function is the entry point registered as ``music-annotator`` in ``pyproject.toml``.  It validates source directories
    before delegating and converts any unhandled exception or keyboard interrupt into a logged error with exit code 1.

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
            try:
                music_annotator.run(
                    release_id=args.release_id,
                    src_dir=args.src_dir,
                    dest_root=args.dest_dir,
                    user_agent=user_agent,
                    dry_run=args.dry_run,
                    fetch_rels=not args.no_fetch_rels,
                    no_cache=args.no_cache,
                    disc_override=args.disc,
                )
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("fatal_error", error=str(exc), exc_info=True)
                sys.exit(1)
            if args.delete and not args.dry_run:
                music_annotator.prompt_delete_src(args.src_dir)

        case "search":
            for src in args.src_dirs:
                if not src.is_dir():
                    log.error("src_dir_not_found", path=str(src))
                    sys.exit(1)
            user_agent = f"{args.user_agent_app} {args.user_agent_email}"
            try:
                music_annotator.discover(
                    src_dirs=args.src_dirs,
                    dest_root=args.dest_dir,
                    user_agent=user_agent,
                    dry_run=args.dry_run,
                    fetch_rels=not args.no_fetch_rels,
                    limit=args.limit,
                    delete=args.delete,
                    no_cache=args.no_cache,
                )
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("fatal_error", error=str(exc), exc_info=True)
                sys.exit(1)

        case "prune":
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
            try:
                music_annotator.repath(dest_root=args.dest_dir, dry_run=args.dry_run)
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("repath_error", dest_root=str(args.dest_dir), error=str(exc), exc_info=True)
                sys.exit(1)

        case "regroup":
            try:
                music_annotator.regroup(dest_root=args.dest_dir, yes=args.yes, dry_run=args.dry_run)
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("regroup_error", dest_root=str(args.dest_dir), error=str(exc), exc_info=True)
                sys.exit(1)

        case "audit":
            try:
                music_annotator.audit(dest_root=args.dest_dir)
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("audit_error", dest_root=str(args.dest_dir), error=str(exc), exc_info=True)
                sys.exit(1)

        case _:  # pragma: no cover
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
