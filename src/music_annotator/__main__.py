"""CLI entry point for music_annotator.

Configures structlog for human-friendly console output and delegates to
:func:`~music_annotator.run`.

Usage::

    python -m music_annotator \\
        --release-id  53c4d36c-1032-4f78-baba-fc972249d7d1 \\
        --src-dir "/path/to/source/album" \\
        --dest-dir /tmp/music_library \\
        [--user-agent "MyApp/1.0 contact@example.com"] \\
        [--dry-run] [--no-fetch-rels]
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
from pathlib import Path

import structlog

import music_annotator


def _configure_logging(verbose: bool) -> None:
    """Set up structlog with a human-readable console renderer.

    Args:
        verbose: When ``True``, set the root log level to ``DEBUG``; otherwise
            use ``INFO``.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
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
    """Combined formatter: shows argument defaults and preserves raw epilog/description."""


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser` instance.
    """
    parser = argparse.ArgumentParser(
        prog="music-annotator",
        description=(
            "Copy and tag a classical music album directory using MusicBrainz metadata, following Classical Extras conventions."
        ),
        formatter_class=_Formatter,
        epilog=textwrap.dedent("""\
            Examples:
              python -m music_annotator \\
                  --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
                  --src-dir "/mnt/music/Respighi - Pini di Roma" \\
                  --dest-dir /tmp/music_library

              python -m music_annotator \\
                  --release-id 53c4d36c-1032-4f78-baba-fc972249d7d1 \\
                  --src-dir "/mnt/music/Respighi - Pini di Roma" \\
                  --dest-dir /tmp/music_library \\
                  --user-agent "MyTagger/1.0 tagger@example.com" \\
                  --dry-run
            """),
    )
    parser.add_argument(
        "--release-id",
        required=True,
        metavar="MBID",
        help="MusicBrainz release MBID (UUID) to fetch metadata for.",
    )
    parser.add_argument(
        "--src-dir",
        required=True,
        metavar="DIR",
        type=Path,
        help="Directory containing the source audio files to copy and tag.",
    )
    parser.add_argument(
        "--dest-dir",
        required=True,
        metavar="DIR",
        type=Path,
        help="Root destination directory for the annotated music library.",
    )
    parser.add_argument(
        "--user-agent",
        default="MusicAnnotator/0.1 music-annotator@example.com",
        metavar="STRING",
        help='MusicBrainz user-agent string in the form "AppName/Version contact@example.com".',
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
            "Skip per-recording relationship lookups (faster but produces minimal tags). "
            "Composer, conductor, work hierarchy, and Classical Extras tags will be absent."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser


def main() -> None:
    """Parse CLI arguments, configure logging, and invoke :func:`~music_annotator.run`.

    This function is the entry point registered as ``music-annotator`` in
    ``pyproject.toml``.  It exits with code 1 if an unrecoverable error occurs.

    Returns:
        None.

    Raises:
        SystemExit: With code 0 on success, code 1 on unrecoverable error.
    """
    parser = _build_parser()
    args = parser.parse_args()

    _configure_logging(args.verbose)

    log = structlog.get_logger(__name__)

    if not args.src_dir.is_dir():
        log.error("src_dir_not_found", path=str(args.src_dir))
        sys.exit(1)

    try:
        music_annotator.run(
            release_id=args.release_id,
            src_dir=args.src_dir,
            dest_root=args.dest_dir,
            user_agent=args.user_agent,
            dry_run=args.dry_run,
            fetch_rels=not args.no_fetch_rels,
        )
    except KeyboardInterrupt:
        log.warning("interrupted")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.error("fatal_error", error=str(exc), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
