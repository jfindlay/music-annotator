"""Shared rich :class:`~rich.console.Console` instance and colour configuration for music-annotator.

This tiny module exists to break the circular dependency that would otherwise arise if both
:mod:`music_annotator._pipeline` and :mod:`music_annotator._discover` imported ``_console`` from
``music_annotator.__init__``, while ``__init__`` itself imports from those two modules.
"""

from __future__ import annotations

from rich.console import Console

_console: Console = Console()


def configure_color(enabled: bool) -> None:
    """Replace the module-level rich :class:`~rich.console.Console` instance to enable or disable color output.

    Called by :func:`~music_annotator.__main__._configure_logging` when the ``--no-color`` CLI flag is present.

    :param enabled: When ``False``, replace the console with one that produces plain text without ANSI codes.
    """
    global _console  # noqa: PLW0603  # pylint: disable=global-statement
    _console = Console(no_color=not enabled)
