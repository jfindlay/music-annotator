"""Work hierarchy and date/key extraction helpers for music-annotator.

Implements the Classical Extras (CE) ``_cwp_`` conventions for walking the MusicBrainz work parent
chain and extracting composed/published/premiered dates, key signatures, and folksonomy tags.
"""

from __future__ import annotations

import re

import structlog

from music_annotator._mb_api import fetch_work_detail
from music_annotator.models import ArtistEntry, MBAttribute, MBWork, RoleBuckets, WorkDates

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Classical Extras default period map: (name, start_year_inclusive, end_year_inclusive).
PERIOD_MAP: list[tuple[str, int, int]] = [
    ("Early", -3000, 800),
    ("Medieval", 800, 1400),
    ("Renaissance", 1400, 1600),
    ("Baroque", 1600, 1750),
    ("Classical", 1750, 1820),
    ("Early Romantic", 1800, 1850),
    ("Late Romantic", 1850, 1910),
    ("20th Century", 1910, 1975),
    ("Contemporary", 1975, 2525),
]

#: Mapping from MB work type to genre string (CE worktype_genres logic).
WORKTYPE_GENRES: dict[str, str] = {
    "Symphony": "Symphony",
    "Concerto": "Concerto",
    "Opera": "Opera",
    "Oratorio": "Oratorio",
    "Cantata": "Cantata",
    "Mass": "Mass",
    "Motet": "Motet",
    "Ballet": "Ballet",
    "Symphonic poem": "Symphonic poem",
    "Suite": "Suite",
    "Overture": "Overture",
    "Chamber music": "Chamber music",
    "Sonata": "Sonata",
    "Song cycle": "Song-cycle",
    "Choral": "Choral",
    "Partita": "Partita",
    "Aria": "Aria",
}


def build_work_hierarchy(work: MBWork, visited: set[str] | None = None) -> list[MBWork]:
    """Walk up the MusicBrainz work parent chain and return the full hierarchy list.

    The returned list runs from the recording's direct (bottom) work at index 0 to the root (top) work at the highest
    index, matching the Classical Extras ``cwp_work_0`` … ``cwp_work_N`` tag convention.  Only the first backward
    ``"parts"`` or ``"part of"`` relation at each level is followed; cycle detection via ``visited`` prevents infinite
    loops on circular parent references.

    :param work: The bottom-level :class:`~music_annotator.models.MBWork` as returned by :func:`fetch_work_detail`.
    :param visited: Set of work MBIDs already visited in this traversal.  Pass ``None`` to start a fresh traversal.
    :returns: A list of :class:`~music_annotator.models.MBWork` instances from bottom (index 0) to top (last index).
    :raises mb.ResponseError: If fetching a parent work fails with a non-retryable error.
    :raises RuntimeError: If all retry attempts for a parent work fetch are exhausted.
    """
    if visited is None:
        visited = set()
    if work.id in visited:
        return [work]
    visited.add(work.id)

    hierarchy: list[MBWork] = [work]

    for rel in work.work_relation_list:
        if rel.direction == "backward" and rel.type in ("parts", "part of"):
            parent_id = rel.work.id
            if parent_id and parent_id not in visited:
                log.debug("fetch_parent_work", parent_id=parent_id, child_id=work.id)
                parent = fetch_work_detail(parent_id)
                hierarchy.extend(build_work_hierarchy(parent, visited))
                break  # take first parent only

    return hierarchy


def strip_common_prefix(child: str, parent: str) -> str:
    """Remove from ``child`` any text that duplicates ``parent``, producing a short movement label.

    Implements the CE ``cwp_part_N`` stripping logic:

    1. If ``child`` starts with ``parent`` (case-insensitive), strip that prefix and any leading punctuation or whitespace.
    2. Otherwise, if ``child`` contains a colon, return the portion after the first colon.
    3. Otherwise return ``child`` unchanged.

    :param child: The full work name at the lower hierarchy level
        (e.g. ``"Fontane di Roma, P 106: I. La fontana di Valle Giulia all'alba"``).
    :param parent: The work name at the parent level (e.g. ``"Fontane di Roma, P 106"``).
    :returns: The stripped part label (e.g. ``"I. La fontana di Valle Giulia all'alba"``),
        or ``child`` unchanged when no prefix is found.
    """
    if not parent or not child:
        return child
    if child.lower().startswith(parent.lower()):
        stripped = child[len(parent) :].lstrip(" :.-–—,")
        return stripped if stripped else child
    if ":" in child:
        after_colon = child.split(":", 1)[1].strip()
        return after_colon if after_colon else child
    return child


def period_for_year(year: int | None) -> str:
    """Map a composition year to the corresponding Classical Extras period name.

    Iterates :data:`PERIOD_MAP` and returns the name of the first entry whose inclusive ``[start, end]`` range contains
    ``year``.

    :param year: The four-digit composition year, or ``None`` when unknown.
    :returns: A period name from :data:`PERIOD_MAP` (e.g. ``"Late Romantic"``), or an empty string when ``year`` is
        ``None`` or falls outside all defined ranges.
    """
    if year is None:
        return ""
    for name, start, end in PERIOD_MAP:
        if start <= year <= end:
            return name
    return ""


def extract_work_artist_rels(work: MBWork, role_buckets: RoleBuckets) -> None:
    """Fill ``role_buckets`` in-place from the work's ``artist-relation-list``.

    Follows the CE ``cwp_`` convention.  Deduplicates entries by MBID so that the same composer credited at every level
    of the work hierarchy (movement → symphonic poem → collection) appears only once.  Unknown relation types are silently
    ignored.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :param role_buckets: The :class:`~music_annotator.models.RoleBuckets` instance to populate in-place.
    """
    for rel in work.artist_relation_list:
        entry = ArtistEntry(name=rel.artist.name, sort=rel.artist.sort_name or rel.artist.name, mbid=rel.artist.id)
        match rel.type:
            case "composer":
                role_buckets.add_unique("composers", entry)
            case "writer":
                role_buckets.add_unique("writers", entry)
            case "lyricist":
                role_buckets.add_unique("lyricists", entry)
            case "librettist":
                role_buckets.add_unique("librettists", entry)
            case "translator":
                role_buckets.add_unique("translators", entry)
            case "arranger" | "instrument arranger" | "vocal arranger":
                role_buckets.add_unique("arrangers", entry)
            case "orchestrator":
                role_buckets.add_unique("orchestrators", entry)
            case "reconstructed by":
                role_buckets.add_unique("reconstructors", entry)
            case "revised by":
                role_buckets.add_unique("revisors", entry)


def collect_work_dates(work: MBWork) -> WorkDates:
    """Extract composed, published, and premiered dates from a work's attributes.

    Checks both the ``attribute-list`` (for typed date attributes) and the ``life-span.begin`` field (used as a
    composed-date fallback by MB when no explicit composition-date attribute is present).

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A :class:`~music_annotator.models.WorkDates` instance with any dates found.  Fields default to empty
        strings when not present.
    """
    composed = published = premiered = ""
    for attr in work.attribute_list:
        if not isinstance(attr, MBAttribute):
            continue
        val = attr.value
        t = attr.type.lower()
        match t:
            case s if "composed" in s or "composition" in s:
                composed = val
            case s if "published" in s or "publish" in s:
                published = val
            case s if "premiered" in s or "premiere" in s:
                premiered = val
    if not composed and work.life_span.begin:
        composed = work.life_span.begin[:4]
    return WorkDates(composed=composed, published=published, premiered=premiered)


def parse_year(date_str: str) -> int | None:
    """Parse the first four-digit year from a date string.

    :param date_str: A date string such as ``"1900"``, ``"1900-06-15"``, or ``""``.
    :returns: The integer year, or ``None`` when no four-digit sequence is found or the input is empty.
    """
    if not date_str:
        return None
    m = re.match(r"(\d{4})", date_str)
    return int(m.group(1)) if m else None


def collect_work_tags_and_key(work: MBWork) -> tuple[list[str], str]:
    """Return the folksonomy tag list and key signature from a work.

    The key is taken from :attr:`~music_annotator.models.MBWork.key` and overridden by any ``attribute-list`` entry
    whose ``type`` is ``"key"`` or ``"key signature"``.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A 2-tuple ``(tag_names, key_signature)`` where ``tag_names`` is a list of folksonomy tag name strings and
        ``key_signature`` is the key signature string (e.g. ``"G minor"``), or ``""`` when absent.
    """
    tags: list[str] = [t.name for t in work.tag_list]
    key: str = work.key
    for attr in work.attribute_list:
        if isinstance(attr, MBAttribute) and attr.type.lower() in ("key", "key signature"):
            key = attr.value
    return tags, key
