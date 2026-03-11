"""CLI entry point for music-annotator.

Configures structlog for human-friendly console output and exposes two subcommands:

* ``apply`` — copy and tag a directory of tracks for a known MusicBrainz release MBID.
* ``search`` — search MusicBrainz for releases matching one or more source directories, prompt for confirmation, then apply tags
  interactively.

Usage::

    music-annotator apply \\
        --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
        --src-dir "/path/to/source/album" \\
        --dest-dir /tmp/music_library \\
        --user-agent-email contact@example.com \\
        [--user-agent-app "MyApp/1.0"] \\
        [--dry-run] [--no-fetch-rels]

    music-annotator search \\
        --dest-dir /tmp/music_library \\
        --user-agent-email contact@example.com \\
        /path/to/album1 /path/to/album2 \\
        [--user-agent-app "MyApp/1.0"] \\
        [--limit 10] [--dry-run] [--no-fetch-rels]
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
    """Add arguments shared by both the ``apply`` and ``search`` subcommands.

    Shared arguments are: ``--dest-dir``, ``--user-agent-app``, ``--user-agent-email``, ``--dry-run``, and
    ``--no-fetch-rels``.  ``-v``/``--verbose`` lives on the top-level parser so it must appear before the subcommand token.

    :param parser: The subcommand parser to which the arguments are added.
    """
    parser.add_argument(
        "--dest-dir",
        required=True,
        metavar="DIR",
        type=_resolve_path,
        help="Root destination directory for the annotated music library.",
    )
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


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level CLI argument parser with ``apply`` and ``search`` subcommands.

    ``-v``/``--verbose`` is registered on the top-level parser and must appear before the subcommand token (e.g.
    ``music-annotator -v apply ...``).  Both subcommands share ``--dest-dir``, ``--user-agent-app``, ``--user-agent-email``,
    ``--dry-run``, and ``--no-fetch-rels``.  The ``apply`` subcommand additionally requires ``--release-id`` and ``--src-dir``.
    The ``search`` subcommand takes one or more positional source directories and an optional ``--limit``.

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
                  --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
                  --src-dir "/mnt/music/Respighi - Pini di Roma" \\
                  --dest-dir /tmp/music_library \\
                  --user-agent-email tagger@example.com

              music-annotator apply \\
                  --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
                  --src-dir "/mnt/music/Respighi - Pini di Roma" \\
                  --dest-dir /tmp/music_library \\
                  --user-agent-email tagger@example.com \\
                  --user-agent-app "MyTagger/1.0" \\
                  --dry-run
            """),
    )
    apply_parser.add_argument(
        "--release-id",
        required=True,
        metavar="MBID",
        help="MusicBrainz release MBID (UUID) to fetch metadata for.",
    )
    apply_parser.add_argument(
        "--src-dir",
        required=True,
        metavar="DIR",
        type=_resolve_path,
        help="Directory containing the source audio files to copy and tag.",
    )
    _add_common_args(apply_parser)

    # ------------------------------------------------------------------
    # search subcommand
    # ------------------------------------------------------------------
    search_parser = subparsers.add_parser(
        "search",
        help="Search MusicBrainz for releases matching source directories, confirm, and apply.",
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Examples:
              music-annotator search \\
                  --dest-dir /tmp/music_library \\
                  --user-agent-email tagger@example.com \\
                  "/mnt/music/Respighi - Pini di Roma" \\
                  "/mnt/music/Brahms - Symphonies"

              music-annotator search \\
                  --dest-dir /tmp/music_library \\
                  --user-agent-email tagger@example.com \\
                  --limit 5 --dry-run \\
                  /mnt/music/untagged/*
            """),
    )
    search_parser.add_argument(
        "src_dirs",
        nargs="+",
        metavar="DIR",
        type=_resolve_path,
        help="One or more source directories to search and tag.",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of MusicBrainz search candidates to display per directory.",
    )
    _add_common_args(search_parser)

    return parser


def main() -> None:
    """Parse CLI arguments, configure logging, and dispatch to ``apply`` or ``search``.

    This function is the entry point registered as ``music-annotator`` in ``pyproject.toml``.  It validates source directories
    before delegating and converts any unhandled exception or keyboard interrupt into a logged error with exit code 1.

    :raises SystemExit: With code 0 on success, code 1 on unrecoverable error.
    """
    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.verbose, no_color=args.no_color)

    log = structlog.get_logger(__name__)

    user_agent = f"{args.user_agent_app} {args.user_agent_email}"

    match args.subcommand:
        case "apply":
            if not args.src_dir.is_dir():
                log.error("src_dir_not_found", path=str(args.src_dir))
                sys.exit(1)
            try:
                music_annotator.run(
                    release_id=args.release_id,
                    src_dir=args.src_dir,
                    dest_root=args.dest_dir,
                    user_agent=user_agent,
                    dry_run=args.dry_run,
                    fetch_rels=not args.no_fetch_rels,
                )
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("fatal_error", error=str(exc), exc_info=True)
                sys.exit(1)

        case "search":
            missing = [str(d) for d in args.src_dirs if not d.is_dir()]
            if missing:
                for path in missing:
                    log.error("src_dir_not_found", path=path)
                sys.exit(1)
            try:
                music_annotator.discover(
                    src_dirs=args.src_dirs,
                    dest_root=args.dest_dir,
                    user_agent=user_agent,
                    dry_run=args.dry_run,
                    fetch_rels=not args.no_fetch_rels,
                    limit=args.limit,
                )
            except KeyboardInterrupt:
                log.warning("interrupted")
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001
                log.error("fatal_error", error=str(exc), exc_info=True)
                sys.exit(1)

        case _:  # pragma: no cover
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
