"""Work hierarchy and date/key extraction helpers for music-annotator.

Implements the Classical Extras (CE) ``_cwp_`` conventions for walking the MusicBrainz work parent
chain and extracting composed/published/premiered dates, key signatures, and folksonomy tags.
"""

from __future__ import annotations

import re

import structlog

from music_annotator._mb_api import fetch_work_detail
from music_annotator.models import ArtistEntry, MBAttribute, MBWork, MBWorkRelation, PeriodEntry, RoleBuckets, WorkDates

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Classical Extras default period map: each entry gives the period name and its inclusive year range.
PERIOD_MAP: list[PeriodEntry] = [
    PeriodEntry(name="Early", start=-3000, end=800),
    PeriodEntry(name="Medieval", start=800, end=1400),
    PeriodEntry(name="Renaissance", start=1400, end=1600),
    PeriodEntry(name="Baroque", start=1600, end=1750),
    PeriodEntry(name="Classical", start=1750, end=1820),
    PeriodEntry(name="Early Romantic", start=1800, end=1850),
    PeriodEntry(name="Late Romantic", start=1850, end=1910),
    PeriodEntry(name="20th Century", start=1910, end=1975),
    PeriodEntry(name="Contemporary", start=1975, end=2525),
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


def _score_top_work(top: MBWork) -> int:
    """Score a top-level work for suitability as the principal performance target.

    Used by :func:`select_primary_performance_work` to rank the root of each candidate's
    ``parts/backward`` parent chain.  A higher score means a more self-standing primary
    composition.

    Scoring:

    * ``+2`` if the work has a non-empty MB work type (e.g. ``"Concerto"``, ``"Symphony"``).
      A typed work is an explicitly classified primary composition.
    * ``+1`` if the work has **no** ``based on`` relation with direction ``"backward"``.
      A ``based on/backward`` relation means the work exists *in reference to* another work
      (e.g. a cadenza collection is ``based on/backward`` the concerto it was written for),
      indicating subsidiary status.

    :param top: The root :class:`~music_annotator.models.MBWork` of a candidate's parent chain.
    :returns: An integer score in the range ``[0, 3]``.
    """
    score = 0
    if top.type:
        score += 2
    has_based_on_backward = any(rel.type == "based on" and rel.direction == "backward" for rel in top.work_relation_list)
    if not has_based_on_backward:
        score += 1
    return score


def select_primary_performance_work(candidates: list[MBWork]) -> MBWork:
    """Choose the principal work from a list of performance-linked works.

    When a recording is linked via multiple ``performance`` relations to more than one work
    (e.g. both a Beethoven concerto movement and a Kreisler cadenza to that movement),
    this function selects the work that should be treated as the primary musical subject for
    tagging and directory naming.

    Algorithm:

    1. For each candidate, walk its ``parts/backward`` parent chain (fetching parents via
       :func:`~music_annotator._mb_api.fetch_work_detail`, using the in-process cache) to
       find the root (top-level) work.
    2. Score the top-level work using :func:`_score_top_work`:

       * ``+2`` for a non-empty MB work type.
       * ``+1`` for absence of a ``based on/backward`` relation.

    3. Return the candidate whose root scores highest.  Ties are broken by preferring the
       candidate that appears first in ``candidates`` (i.e. first ``performance`` link in the
       recording's relation list).

    This is an extension beyond Classical Extras, which generates multi-valued tags for all
    performance-linked works without choosing a primary.  music-annotator requires a single
    principal work per recording for unambiguous directory naming.

    :param candidates: Non-empty list of :class:`~music_annotator.models.MBWork` instances,
        one per ``performance`` relation on the recording.  Must contain at least one element.
    :returns: The selected primary :class:`~music_annotator.models.MBWork`.
    """
    if len(candidates) == 1:
        return candidates[0]

    best_work = candidates[0]
    best_score = -1

    for work in candidates:
        # Walk to the top of this work's parts/backward chain.
        top = work
        visited: set[str] = set()
        while True:
            if top.id in visited:
                break
            visited.add(top.id)
            parent_rel: MBWorkRelation | None = next(
                (r for r in top.work_relation_list if r.direction == "backward" and r.type in ("parts", "part of")),
                None,
            )
            if parent_rel is None or not parent_rel.work.id:
                break
            top = fetch_work_detail(parent_rel.work.id)

        score = _score_top_work(top)
        log.debug(
            "work_primary_score",
            work_id=work.id,
            work_title=work.title,
            top_id=top.id,
            top_title=top.title,
            score=score,
        )
        if score > best_score:
            best_score = score
            best_work = work

    return best_work


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
    for entry in PERIOD_MAP:
        if entry.start <= year <= entry.end:
            return entry.name
    return ""


def extract_work_artist_rels(work: MBWork, role_buckets: RoleBuckets) -> None:
    """Fill ``role_buckets`` in-place from the work's ``artist-relation-list``.

    Follows the CE ``cwp_`` convention.  Deduplicates entries by MBID so that the same composer credited at every level
    of the work hierarchy (movement → symphonic poem → collection) appears only once.  Unknown relation types are silently
    ignored.

    Composer relations that carry the MB ``"additional"`` or ``"assistant"`` attribute string in their ``attribute-list``
    are routed to ``role_buckets.additional_composers`` rather than ``role_buckets.composers``.  This is an extension
    beyond Classical Extras; it allows subsidiary completion/ghost-writer credits (e.g. Süssmayr on the Mozart Requiem)
    to be distinguished from the primary composer(s) so that directory naming is unambiguous.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :param role_buckets: The :class:`~music_annotator.models.RoleBuckets` instance to populate in-place.
    """
    for rel in work.artist_relation_list:
        entry = ArtistEntry(name=rel.artist.name, sort=rel.artist.sort_name or rel.artist.name, mbid=rel.artist.id)
        match rel.type:
            case "composer":
                if "additional" in rel.attribute_list or "assistant" in rel.attribute_list:
                    role_buckets.add_unique("additional_composers", entry)
                else:
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
            case "adapter":
                # An adapter transforms a work for a different medium; treat as an arranger.
                role_buckets.add_unique("arrangers", entry)
            case "dedication":
                role_buckets.add_unique("dedicatees", entry)
            case "choreographer":
                role_buckets.add_unique("choreographers", entry)


def _date_range(begin: str, end: str) -> str:
    """Format a begin/end date pair as a CE-compatible year or year-range string.

    Uses the Classical Extras convention: a single ``"YYYY"`` when begin and end fall in the same
    year (or end is absent), or ``"YYYY-YYYY"`` when they span different years.  Only the 4-digit
    year prefix of each date is used so that full ISO dates (``"1984-01-27"``) and year-only
    values (``"1984"``) are both handled consistently.

    :param begin: ISO date string for the start of the range (e.g. ``"1822"`` or ``"1822-06-01"``).
    :param end: ISO date string for the end of the range, or ``""`` when absent.
    :returns: A formatted date or range string, or ``""`` when ``begin`` is empty.
    """
    if not begin:
        return ""
    start_year = begin[:4]
    end_year = end[:4] if end else ""
    if end_year and end_year != start_year:
        return f"{start_year}-{end_year}"
    return start_year


def collect_work_dates(work: MBWork) -> WorkDates:
    """Extract composed, published, and premiered dates from a work's attributes and relations.

    Checks three sources in priority order:

    1. ``attribute-list`` — typed date attributes (``"Composed"``, ``"Published"``, ``"Premiered"``).
       The value is stored verbatim; if the MB editor entered a range it is preserved as-is.
    2. Artist/label/place relations — CE-compatible date extraction using begin **and** end:
       - Composed: range across all ``"composer"`` artist relations (``min(begin)``–``max(end)``).
       - Published: begin–end from the first ``"publishing"`` label relation.
       - Premiered: begin–end from the first ``"premiere"`` place relation.
    3. ``life-span.begin``/``end`` — MB life-span fallback for composed date.

    Ranges are formatted using the CE convention (``"YYYY"`` or ``"YYYY-YYYY"`` via
    :func:`_date_range`) so that ``CWP_COMPOSED_DATES`` etc. are consistent with the Classical
    Extras Picard plugin output.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A :class:`~music_annotator.models.WorkDates` instance with any dates found.  Fields default to empty
        strings when not present.
    """
    composed = published = premiered = ""

    # Source 1: attribute-list (verbatim — CE convention does not apply here since
    # MBAttribute.value is a single opaque string; MB may or may not encode a range within it).
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

    # Source 2a: composed date from composer artist relation begin/end dates.
    # Use the earliest begin and latest end across all composer relations.
    if not composed:
        composer_rels = [rel for rel in work.artist_relation_list if rel.type == "composer" and rel.begin]
        if composer_rels:
            earliest = min(rel.begin for rel in composer_rels)
            ends = [rel.end for rel in composer_rels if rel.end]
            latest = max(ends) if ends else ""
            composed = _date_range(earliest, latest)

    # Source 2b: published date from publishing label relation begin/end dates.
    if not published:
        for lrel in work.label_relation_list:
            if lrel.type == "publishing" and lrel.begin:
                published = _date_range(lrel.begin, lrel.end)
                break

    # Source 2c: premiered date from premiere place relation begin/end dates.
    if not premiered:
        for prel in work.place_relation_list:
            if prel.type == "premiere" and prel.begin:
                premiered = _date_range(prel.begin, prel.end)
                break

    # Source 3: life-span.begin/end fallback for composed date.
    if not composed and work.life_span.begin:
        composed = _date_range(work.life_span.begin, work.life_span.end)

    return WorkDates(composed=composed, published=published, premiered=premiered)


def collect_work_urls(work: MBWork) -> dict[str, str]:
    """Extract notable external URLs from a work's URL relations.

    Returns a dict mapping relation type to URL for well-known relation types:
    ``"download for free"`` (IMSLP), ``"wikidata"``, ``"allmusic"``, ``"VIAF"``.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A ``dict[str, str]`` of ``{relation_type: url}``.  Empty when the work has no URL relations.
    """
    result: dict[str, str] = {}
    notable: frozenset[str] = frozenset({"download for free", "wikidata", "allmusic", "VIAF"})
    for rel in work.url_relation_list:
        if rel.type in notable and rel.url and rel.type not in result:
            result[rel.type] = rel.url
    return result


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
