"""CEA/CWP builder and track-tag assembly functions for music-annotator.

Implements the Classical Extras performer classification (``build_cea_performers``), the CWP tag
builder (``build_cwp_tags``), and the central ``build_track_tags`` function that combines all
metadata sources into a :class:`~music_annotator.models.TrackTags` ready for writing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import structlog

from music_annotator._artists import (
    artist_credit_phrase,
    artist_ids,
    artist_sort_names,
    last_name,
)
from music_annotator._mb_api import _extract_session_date
from music_annotator._works import (
    WORKTYPE_GENRES,
    collect_work_dates,
    collect_work_tags_and_key,
    collect_work_urls,
    extract_work_artist_rels,
    parse_year,
    period_for_year,
    strip_common_prefix,
)
from music_annotator.models import (
    ArtistEntry,
    CeaPerformers,
    CwpTags,
    MBArtistCredit,
    MBAttribute,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
    RoleBuckets,
    TrackTags,
    WorkHierarchyLevel,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _work_aliases(work: MBWork) -> tuple[str, str]:
    """Extract the English alias and unlocaled aliases from a work's ``alias-list``.

    Returns two strings for use as companion tags alongside the canonical work title:

    - **english**: the first alias with ``locale == "en"`` and ``type == "Work name"``, or ``""`` if none.
    - **alt**: all aliases with ``locale is None``, deduplicated and excluding any that equal the
      canonical ``work.title``, joined with ``"; "``.  These cover search hints, legal names, and
      alternate spellings that MB has not attributed to a specific locale.

    The canonical ``work.title`` is never repeated in either companion string — it is already the
    primary value of ``CWP_WORK_TOP`` / ``CWP_WORK_{i}``.

    :param work: The :class:`~music_annotator.models.MBWork` instance.
    :returns: A ``(english, alt)`` tuple of strings, either of which may be ``""``.
    """
    english = ""
    for alias in work.alias_list:
        if alias.locale == "en" and alias.type == "Work name" and alias.name:
            english = alias.name
            break

    seen: set[str] = set()
    alt_parts: list[str] = []
    for alias in work.alias_list:
        if alias.locale is None and alias.name and alias.name != work.title:
            if alias.name not in seen:
                seen.add(alias.name)
                alt_parts.append(alias.name)

    return english, "; ".join(alt_parts)


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Regex matching filesystem-unsafe characters for :func:`safe_name`.
_SAFE_RE: re.Pattern[str] = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Maximum byte length of a single path component on standard filesystems (ext4, btrfs, XFS, NTFS, HFS+, APFS, …).
#: Unusual filesystems (e.g. eCryptfs at 143) are deliberately excluded — the common case is always 255.
_NAME_MAX: int = 255

#: Regex matching a ``[rec YYYY]`` or ``[rel YYYY-YYYY]``-style date suffix produced by :func:`build_dest_path`.
_DATE_SUFFIX_RE: re.Pattern[str] = re.compile(r"( \[(?:rec|rel) \d{4}(?:-\d{4})?\])$")

#: Regex matching a zero-padded ``nn - `` numeric prefix used on leaf and intermediate path components.
_NN_PREFIX_RE: re.Pattern[str] = re.compile(r"^(\d+ - )")


def safe_name(s: str) -> str:
    """Sanitise a string for use as a filesystem path component.

    Replaces characters forbidden on common filesystems (Windows/POSIX) with underscores, strips leading and trailing
    spaces, and replaces leading dots with underscores to prevent hidden files on POSIX systems.  Trailing dots are
    intentionally preserved — they are legal in filenames and carry semantic meaning (e.g. ``"op."`` in a work title
    that MB records without an opus number).  No length truncation is applied; length enforcement is the caller's
    responsibility via :func:`_proposed_short` and :func:`~music_annotator._pipeline._resolve_long_names`.

    :param s: The raw name string.
    :returns: A sanitised string safe for use as a directory or file name.
    """
    s = _SAFE_RE.sub("_", s).strip(" ")
    # Replace each leading dot with an underscore to avoid creating hidden files on POSIX filesystems.
    i = 0
    while i < len(s) and s[i] == ".":
        i += 1
    if i:
        s = "_" * i + s[i:]
    return s


def _proposed_short(component: str) -> str:
    """Return a shortened version of ``component`` that fits within :data:`_NAME_MAX` UTF-8 bytes.

    Applies a sequence of structure-aware strategies in order, stopping as soon as the result fits.
    The strategies are ordered from least to most destructive:

    1. **Work-dir subtitle drop**: if the component ends with a ``[rec …]`` or ``[rel …]`` date suffix,
       try removing the subtitle portion (everything from the last ``" _ "`` before the suffix).
    2. **Leaf / intermediate subtitle drop**: if the component starts with a numeric ``nn - `` prefix,
       try removing the subtitle after the last ``" _ "`` in the title portion.
    3. **Top-dir performer drop**: if the component contains `` - `` (composer–performer separator),
       try dropping semicolon-separated performer entries from the right until it fits.
    4. **Word-boundary ellipsis**: truncate at the last space that leaves room for ``"…"`` (U+2026,
       3 bytes in UTF-8).
    5. **Hard byte-boundary ellipsis**: encode as UTF-8, cut to ``_NAME_MAX - 3`` bytes at a valid
       UTF-8 character boundary, then append ``"…"``.

    The returned value is always at most :data:`_NAME_MAX` bytes when encoded as UTF-8.

    :param component: The sanitised path component string that exceeds :data:`_NAME_MAX` bytes.
    :returns: A shortened component string that fits within :data:`_NAME_MAX` UTF-8 bytes.
    """
    limit = _NAME_MAX

    def _fits(s: str) -> bool:
        return len(s.encode("utf-8")) <= limit

    if _fits(component):
        return component

    ellipsis_char = "\u2026"  # "…", 3 bytes in UTF-8

    # Strategy 1: work-dir — protect "[rec YYYY]" / "[rel YYYY]" / "[rec YYYY-YYYY]" suffix.
    m = _DATE_SUFFIX_RE.search(component)
    if m:
        suffix = m.group(1)
        title_part = component[: m.start()]
        sep_idx = title_part.rfind(" _ ")
        if sep_idx != -1:
            candidate = title_part[:sep_idx] + suffix
            if _fits(candidate):
                return candidate

    # Strategy 2: leaf / intermediate — protect "nn - " numeric prefix.
    mp = _NN_PREFIX_RE.match(component)
    if mp:
        prefix = mp.group(1)
        body = component[len(prefix) :]
        sep_idx = body.rfind(" _ ")
        if sep_idx != -1:
            candidate = prefix + body[:sep_idx]
            if _fits(candidate):
                return candidate

    # Strategy 3: top-dir — protect everything up to and including " - " (composer separator);
    # drop performer entries from the right.
    sep = " - "
    dash_idx = component.find(sep)
    if dash_idx != -1:
        composer_part = component[: dash_idx + len(sep)]
        performers_str = component[dash_idx + len(sep) :]
        performers = performers_str.split("; ")
        while len(performers) > 1:
            performers.pop()
            candidate = composer_part + "; ".join(performers)
            if _fits(candidate):
                return candidate

    # Strategy 4: word-boundary truncation with ellipsis.
    encoded = component.encode("utf-8")
    # Budget: limit bytes total; "…" is 3 bytes.
    budget = limit - 3
    # Walk backwards through the string looking for a space whose UTF-8 offset fits in budget.
    truncated = encoded[:budget].decode("utf-8", errors="ignore")
    last_space = truncated.rfind(" ")
    if last_space > 0:
        candidate = truncated[:last_space] + ellipsis_char
        if _fits(candidate):  # pragma: no branch — always fits: prefix ≤ budget bytes + "…" (3 bytes) = limit
            return candidate

    # Strategy 5: hard byte-boundary cut.
    raw = encoded[:budget]
    # Trim to valid UTF-8 boundary (drop trailing incomplete multi-byte sequences).
    while raw and (raw[-1] & 0b11000000) == 0b10000000:
        raw = raw[:-1]
    return raw.decode("utf-8", errors="ignore") + ellipsis_char


def _rec_title(track: MBTrack) -> str:
    """Return the recording title for a track, falling back to ``"Unknown"``.

    :param track: An :class:`~music_annotator.models.MBTrack` instance.
    :returns: The title of the nested recording, or ``"Unknown"`` when absent.
    """
    return track.recording.title or "Unknown"


#: Closed vocabulary of top-level library class names (C-CLASS).
#: Used by :func:`_top_level_class` and :func:`_work_top_dir` to discriminate
#: class-prefixed (three-level) paths from legacy two-level paths.
_CLASS_VOCAB: frozenset[str] = frozenset({"Spoken Word", "Soundtracks", "Classical", "Compilations", "Popular", "Unsorted"})


def _top_level_class(tags: TrackTags) -> str:
    """Derive the top-level library class from embedded tags only (C-CLASS, frozen at S1).

    Routing table (first match wins; evaluated in order):

    1. ``releasetype_secondary`` contains ``Audiobook``, ``Spokenword``, ``Audio drama``, or
       ``Interview`` → ``"Spoken Word"``.
    2. ``releasetype_secondary`` contains ``Soundtrack`` → ``"Soundtracks"``.
    3. CE-classical predicate: ``cwp_work_top`` non-empty AND ``cwp_worktype_genres_top`` contains
       ``"Classical"`` → ``"Classical"``.
    4. ``releasetype_secondary`` contains ``Compilation`` (and not classical) → ``"Compilations"``.
    5. ``releasetype`` in {``Album``, ``Single``, ``EP``, ``Broadcast``, ``Other``} (non-classical,
       no other signal) → ``"Popular"``.
    6. No usable signal → ``"Unsorted"`` (honest fallback; never silently forces ``Classical``).

    **Tag-derivable source requirement (substrate correctness core):** this function reads only
    ``tags.releasetype``, ``tags.releasetype_secondary``, ``tags.cwp_work_top``, and
    ``tags.cwp_worktype_genres_top`` — all of which survive ``to_file_dict()`` and round-trip via
    ``_tags_from_file_dict``.  It never reads ``release.release_group`` so that
    ``repath``/``regroup``/``unify`` (which call ``build_dest_path`` with empty ``MBRelease()``
    stubs) reconstruct the correct class from embedded tags alone.

    **Forward-path-only posture:** this function changes only newly-computed destination paths.
    Already-annotated two-level releases are not retro-migrated by R4a; the whole-library
    re-derivation is deferred to R6b/R6d.

    :param tags: The :class:`~music_annotator.models.TrackTags` instance for this track.
    :returns: A single path-component string from the closed C-CLASS vocabulary.
    """
    secondary = tags.releasetype_secondary
    primary = tags.releasetype

    # Arms 1–2: non-classical secondary types short-circuit before the classical predicate.
    spoken_word_types = {"Audiobook", "Spokenword", "Audio drama", "Interview"}
    secondary_parts = {p.strip() for p in secondary.split(";")} if secondary else set()

    match True:
        case _ if secondary_parts & spoken_word_types:
            return "Spoken Word"
        case _ if "Soundtrack" in secondary_parts:
            return "Soundtracks"
        case _ if tags.cwp_work_top and "Classical" in tags.cwp_worktype_genres_top:
            return "Classical"
        case _ if "Compilation" in secondary_parts:
            return "Compilations"
        case _ if primary in {"Album", "Single", "EP", "Broadcast", "Other"}:
            return "Popular"
        case _:
            return "Unsorted"


def _classical_top_dir(tags: TrackTags) -> str | None:
    """Derive the within-classical initial path component (C-INIT, frozen at S2).

    Encodes the C-INIT rule: what is the primary attribution for a classical release?  Three cases
    evaluated in order (first match wins):

    1. **Multi-composer classical compilation** — ``releasetype_secondary`` contains ``"Compilation"``:
       use ``<albumartist_last_name or "Various"> - <album>`` (CE: primary attribution in path; the
       album artist is the canonical identity for a compilation).  Aligns with
       ``_is_composer_split_release`` / ``_canonical_composer_component`` in ``_pipeline_maint.py``
       (same ``albumartistsort`` → ``last_name`` derivation).

    2. **Recital** (performer-led, no single dominant composer) — ``CWP_COMPOSER_LASTNAMES`` and
       ``CEA_COMPOSER_LASTNAMES`` are both empty: use ``<albumartist> - <album>`` (performer-first).
       The album artist is the canonical identity for a recital (e.g. "Mitsuko Uchida" or
       "Berliner Philharmoniker").  CE divergence note: CE uses the composer-first shape even for
       recitals when a composer can be inferred; here we use the album artist when no composer is
       linked in MB, which is the honest tag-derivable signal.

    3. **Single-composer** (dominant population) — returns ``None`` to signal the caller should use
        the default ``<composer> - <performers>`` shape.

    **Tag-derivable source requirement (C-INIT substrate correctness):** reads only
    ``tags.releasetype_secondary``, ``tags.albumartistsort``, ``tags.albumartist``, and the
    ``CWP_COMPOSER_LASTNAMES`` / ``CEA_COMPOSER_LASTNAMES`` / ``ALBUM`` / ``ALBUMARTIST`` /
    ``ARTIST`` keys from ``tags.to_file_dict()`` — all of which survive ``to_file_dict()`` and
    round-trip via ``_tags_from_file_dict``.  Never reads ``release.release_group`` so that
    ``repath``/``regroup``/``unify`` reconstruct the correct within-classical component from
    embedded tags alone.

    :param tags: The :class:`~music_annotator.models.TrackTags` instance for this track.
    :returns: The within-classical ``top_dir`` component string for cases 1 and 2, or ``None`` for
        case 3 (single-composer default — caller uses ``<composer> - <performers>``).
    """
    file_dict = tags.to_file_dict()
    secondary_parts = {p.strip() for p in tags.releasetype_secondary.split(";")} if tags.releasetype_secondary else set()

    # Case 1: Multi-composer classical compilation.
    # releasetype_secondary contains "Compilation" → albumartist-based shape.
    # Aligns with _canonical_composer_component in _pipeline_maint.py (same last_name derivation).
    if "Compilation" in secondary_parts:
        album_artist_sort = tags.albumartistsort.strip()
        if not album_artist_sort or album_artist_sort == "Various Artists":
            artist_component = "Various"
        else:
            artist_component = last_name(album_artist_sort)
        album = file_dict.get("ALBUM", "") or "Unknown Album"
        return safe_name(f"{artist_component} - {album}")

    # Case 2: Recital (performer-led, no single dominant composer).
    # Signal: CWP_COMPOSER_LASTNAMES and CEA_COMPOSER_LASTNAMES are both empty.
    # This is the honest tag-derivable signal: if no composer is linked in the MB work hierarchy,
    # the release is performer-led and the album artist is the primary attribution.
    raw_composer = file_dict.get("CWP_COMPOSER_LASTNAMES") or file_dict.get("CEA_COMPOSER_LASTNAMES", "")
    if not raw_composer:
        albumartist = file_dict.get("ALBUMARTIST") or file_dict.get("ARTIST", "")
        album = file_dict.get("ALBUM", "") or "Unknown Album"
        if albumartist:
            return safe_name(f"{albumartist} - {album}")
        return safe_name(album)

    # Case 3: Single-composer (dominant population) — caller uses <composer> - <performers>.
    return None


def _work_dir_component(rel_parts: Sequence[str]) -> str:
    """Return the work-dir component name from the relative path parts of a destination file.

    Handles both legacy two-level paths (``<top_dir>/<work_dir>/…``) and class-prefixed
    three-level paths (``<class>/<top_dir>/<work_dir>/…``).  Discriminates by testing whether
    ``parts[0]`` is a known class name from the closed C-CLASS vocabulary.

    :param rel_parts: The ``parts`` of the destination path relative to ``dest_root``.
    :returns: The work-dir component string (``parts[1]`` for legacy, ``parts[2]`` for class-prefixed).
    """
    if rel_parts[0] in _CLASS_VOCAB:
        return rel_parts[2]
    return rel_parts[1]


def _work_top_dir(dest_file: Path, dest_root: Path) -> Path:
    """Return the work top directory (two components immediately beneath ``dest_root``) for ``dest_file``.

    Handles both legacy two-level paths (``dest_root/<top_dir>/<work_dir>/…``) and class-prefixed
    three-level paths (``dest_root/<class>/<top_dir>/<work_dir>/…``).  Discriminates by testing
    whether the first relative component is a known class name from the closed C-CLASS vocabulary.

    For a class-prefixed path returns ``dest_root / parts[1] / parts[2]``.
    For a legacy two-level path returns ``dest_root / parts[0] / parts[1]``.

    :param dest_file: Absolute path to the destination audio file.
    :param dest_root: Root of the annotated music library.
    :returns: The absolute work top directory :class:`~pathlib.Path`.
    """
    rel = dest_file.relative_to(dest_root)
    parts = rel.parts
    if parts[0] in _CLASS_VOCAB:
        return dest_root / parts[1] / parts[2]
    return dest_root / parts[0] / parts[1]


def build_cea_performers(recording_detail: MBRecording) -> CeaPerformers:
    """Classify recording-level artist relations into CE ``cea_*`` performer buckets.

    Iterates the ``artist-relation-list`` of the recording and routes each entry into the appropriate bucket of the
    returned :class:`~music_annotator.models.CeaPerformers` instance.  For ``"performer"``-type relations the first
    ``attribute-list`` entry is used as the instrument label; entries matching :func:`is_ensemble` go to ``ensembles``;
    entries with a vocal keyword in the instrument label go to ``vocalists``; all others go to ``instrumentalists`` (with
    an instrument label) or ``other_soloists`` (without).

    Duplicate relations — where MusicBrainz returns the same artist MBID more than once for the same relation type on a
    single recording — are silently suppressed.  This occurs in the wild (e.g. the conductor and performing-orchestra
    relations on some DG recordings have duplicate entries) and would otherwise produce repeated values in all derived
    tags and in the filesystem path.  Deduplication is per-bucket and keyed on MBID; entries without an MBID are always
    appended because MBID-based deduplication is not possible for them.

    :param recording_detail: The :class:`~music_annotator.models.MBRecording` instance as returned by
        :func:`fetch_recording_detail`.
    :returns: A populated :class:`~music_annotator.models.CeaPerformers` instance.
    """
    from music_annotator._artists import is_ensemble  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

    cea = CeaPerformers()
    # Per-bucket seen-sets keyed on MBID to suppress duplicate MB relations for the same artist+type.
    seen: dict[str, set[str]] = {
        "conductors": set(),
        "chorusmasters": set(),
        "leaders": set(),
        "arrangers": set(),
        "orchestrators": set(),
        "composers": set(),
        "producers": set(),
        "engineers": set(),
        "ensembles": set(),
        "vocalists": set(),
        "instrumentalists": set(),
        "other_soloists": set(),
    }

    def _append(bucket: str, entry: ArtistEntry) -> None:
        """Append ``entry`` to the named bucket, skipping entries whose MBID is already present.

        :param bucket: Name of the :class:`~music_annotator.models.CeaPerformers` list attribute to append to.
        :param entry: The :class:`~music_annotator.models.ArtistEntry` to conditionally append.
        """
        if entry.mbid and entry.mbid in seen[bucket]:
            return
        if entry.mbid:
            seen[bucket].add(entry.mbid)
        getattr(cea, bucket).append(entry)

    for rel in recording_detail.artist_relation_list:
        name = rel.artist.name
        sort = rel.artist.sort_name or name
        mid = rel.artist.id
        entry = ArtistEntry(name=name, sort=sort, mbid=mid)

        match rel.type:
            case "conductor":
                _append("conductors", entry)
            case "chorus master":
                _append("chorusmasters", entry)
            case "concertmaster":
                _append("leaders", entry)
            case "arranger" | "instrument arranger" | "vocal arranger":
                _append("arrangers", entry)
            case "orchestrator":
                _append("orchestrators", entry)
            case "composer" | "writer":
                _append("composers", entry)  # recording-level: CE merges both into composer host tag
            case "producer":
                _append("producers", entry)
            case "balance" | "engineer" | "mix" | "recording" | "audio" | "sound":
                _append("engineers", entry)
            case "performer" | "instrument" | "vocal" | "performing orchestra":
                if is_ensemble(name):
                    _append("ensembles", entry)
                else:
                    first_attr = rel.attribute_list[0] if rel.attribute_list else ""
                    instr: str = first_attr.value if isinstance(first_attr, MBAttribute) else first_attr
                    entry = ArtistEntry(name=name, sort=sort, mbid=mid, instrument=instr)
                    vocal_keywords = ("soprano", "mezzo", "tenor", "baritone", "bass", "contralto", "voice", "vocal", "singer")
                    if any(v in instr.lower() for v in vocal_keywords):
                        _append("vocalists", entry)
                    elif instr:
                        _append("instrumentalists", entry)
                    else:
                        _append("other_soloists", entry)
            case _:
                pass

    return cea


def build_cwp_tags(
    work_hierarchy: list[MBWork],
    role_buckets: RoleBuckets,
) -> CwpTags:
    """Build Classical Extras ``cwp_*`` tag values from the resolved work hierarchy.

    Constructs the full :class:`~music_annotator.models.CwpTags` model by:

    - Setting ``work_top``/``workid_top`` from the root work.
    - Collecting dates, key, and work-type genre from the bottom work.
    - Stripping common name prefixes to produce per-level ``part_title`` values.
    - Building ``groupheading`` from the top work title and all intermediate part titles.
    - Mapping composed date to a CE period name via :func:`period_for_year`.
    - Populating all artist role strings from ``role_buckets``.

    :param work_hierarchy: List of :class:`~music_annotator.models.MBWork` from bottom (index 0) to top (last index), as
        returned by :func:`build_work_hierarchy`.
    :param role_buckets: A :class:`~music_annotator.models.RoleBuckets` already populated by
        :func:`extract_work_artist_rels` for every level of the hierarchy.
    :returns: A :class:`~music_annotator.models.CwpTags` instance with all ``cwp_*`` fields populated.
    """
    cwp = CwpTags()
    if not work_hierarchy:
        return cwp

    n_levels = len(work_hierarchy)
    cwp.part_levels = n_levels - 1

    # Build per-level name/id maps before stripping
    work_names: dict[int, str] = {}
    work_ids: dict[int, str] = {}
    for i, w in enumerate(work_hierarchy):
        work_names[i] = w.title
        work_ids[i] = w.id

    top_work = work_hierarchy[-1]
    cwp.work_top = top_work.title
    cwp.workid_top = top_work.id

    # Dates and key from bottom-level work
    dates = collect_work_dates(work_hierarchy[0])
    cwp.composed_dates = dates.composed
    cwp.published_dates = dates.published
    cwp.premiered_dates = dates.premiered
    _, key = collect_work_tags_and_key(work_hierarchy[0])
    cwp.keys = key
    cwp.worktype_genres = work_hierarchy[0].type
    cwp.worktype_genres_top = top_work.type

    # Strip part names
    part_names: dict[int, str] = {}
    for i in range(n_levels):
        parent_name = work_names.get(i + 1, "") if i < n_levels - 1 else ""
        part_names[i] = strip_common_prefix(work_names[i], parent_name)

    # Extract the MB ordering-key for each level: the integer from the parts/backward relation
    # connecting that level to its parent.  Level n-1 (root) has no parent within the hierarchy
    # so its ordering_key stays 0.
    ordering_keys: dict[int, int] = {}
    for i, w in enumerate(work_hierarchy):
        parent_rel = next(
            (r for r in w.work_relation_list if r.direction == "backward" and r.type in ("parts", "part of")),
            None,
        )
        ordering_keys[i] = parent_rel.ordering_key if parent_rel is not None else 0

    # Assemble levels list, populating alias companion strings for each level.
    cwp.levels = [
        WorkHierarchyLevel(
            index=i,
            work_id=work_ids[i],
            work_title=work_names[i],
            part_title=part_names[i],
            ordering_key=ordering_keys[i],
            work_en=_work_aliases(work_hierarchy[i])[0],
            work_alt=_work_aliases(work_hierarchy[i])[1],
        )
        for i in range(n_levels)
    ]

    # Store root-level alias companions directly on cwp for top-level tag fields.
    cwp.work_top_en, cwp.work_top_alt = _work_aliases(work_hierarchy[-1])

    if n_levels == 1:
        cwp.work = work_names[0]
        cwp.groupheading = work_names[0]
        cwp.part = ""
    else:
        cwp.work = cwp.work_top
        gh_parts = [cwp.work_top]
        for j in range(n_levels - 2, 0, -1):
            inter_part = part_names.get(j, work_names.get(j, ""))
            if inter_part:
                gh_parts.append(inter_part)
        bottom_part = part_names.get(0, "")
        if bottom_part:
            gh_parts.append(bottom_part)
        cwp.groupheading = " :: ".join(gh_parts)
        cwp.part = bottom_part

    if n_levels > 2:
        inter_parts = [part_names.get(j, work_names.get(j, "")) for j in range(1, n_levels - 1)]
        cwp.inter_work = " :: ".join(p for p in inter_parts if p)

    # Period
    year = parse_year(cwp.composed_dates)
    cwp.period = period_for_year(year)

    # Work-level artist roles.
    # When only additional/assistant composers are present (no plain primary composer), fall back to those
    # so that directory naming and tag fields are still populated rather than left blank.
    effective_composers = role_buckets.composers or role_buckets.additional_composers
    if effective_composers:
        cwp.composers = "; ".join(e.name for e in effective_composers)
        cwp.composers_sort = "; ".join(e.sort for e in effective_composers)
        cwp.composer_lastnames = "; ".join(last_name(e.sort) for e in effective_composers)
    if role_buckets.writers:
        cwp.writers = "; ".join(e.name for e in role_buckets.writers)
        cwp.writers_sort = "; ".join(e.sort for e in role_buckets.writers)
    for role_name in ("arrangers", "orchestrators", "reconstructors", "revisors", "lyricists", "librettists", "translators"):
        bucket: list[ArtistEntry] = getattr(role_buckets, role_name)
        if bucket:
            setattr(cwp, role_name, "; ".join(e.name for e in bucket))
            setattr(cwp, f"{role_name}_sort", "; ".join(e.sort for e in bucket))
    # Plain (un-annotated) arranger names — parallel to cwp.arrangers but without instrument/role annotations.
    # Merges work-level arrangers and orchestrators into a single de-duplicated list of display names.
    arranger_name_seen: set[str] = set()
    arranger_name_parts: list[str] = []
    for e in role_buckets.arrangers + role_buckets.orchestrators:
        if e.name not in arranger_name_seen:
            arranger_name_seen.add(e.name)
            arranger_name_parts.append(e.name)
    if arranger_name_parts:
        cwp.arranger_names = "; ".join(arranger_name_parts)

    # work_part_levels: equals part_levels for a single-medium run; stored explicitly to match CE tag output.
    cwp.work_part_levels = cwp.part_levels

    return cwp


def build_track_tags(
    release: MBRelease,
    track: MBTrack,
    medium_pos: int,
    recording_detail: MBRecording,
    work_hierarchy: list[MBWork],
) -> TrackTags:
    """Build the complete tag model for one track, implementing all CE conventions.

    This is the central function that combines release, recording, and work-hierarchy data into a
    :class:`~music_annotator.models.TrackTags` instance ready for writing to an audio file.  The movement-number fields
    (``movementnumber``, ``movementtotal``, ``cwp_movt_num``, ``cwp_movt_tot``, ``cwp_single_work_album``) are left as
    empty strings at this stage; they are filled in by :func:`run` after all tracks have been processed and grouped by
    top-work MBID.

    :param release: The :class:`~music_annotator.models.MBRelease` from :func:`fetch_release`.
    :param track: The :class:`~music_annotator.models.MBTrack` for this track.
    :param medium_pos: The 1-based disc/medium position (typically ``1`` for single-disc releases).
    :param recording_detail: The :class:`~music_annotator.models.MBRecording` from :func:`fetch_recording_detail`.
    :param work_hierarchy: The work hierarchy list from :func:`build_work_hierarchy`, or an empty list when no work link
        was found.
    :returns: A :class:`~music_annotator.models.TrackTags` instance with all fields populated except movement-number
        fields.
    """
    rec = track.recording

    # Release-level artists
    album_artist_phrase = artist_credit_phrase(release.artist_credit)
    album_artist_ids_str = "/".join(artist_ids(release.artist_credit))
    album_artist_sort = "; ".join(artist_sort_names(release.artist_credit))

    # Recording artist credit (from the recording stub on the track)
    rec_artist_phrase = artist_credit_phrase(rec.artist_credit)
    rec_artist_ids_str = "/".join(artist_ids(rec.artist_credit))
    rec_artist_sort = "; ".join(artist_sort_names(rec.artist_credit))

    # Label / catalogue
    label_info = release.label_info_list[0] if release.label_info_list else None
    label_name = label_info.label.name if label_info else ""
    catalog_number = label_info.catalog_number if label_info else ""

    # Track counts
    medium = next((m for m in release.medium_list if m.position == medium_pos), None)
    total_tracks = str(len(medium.track_list) if medium else 0)

    # CEA classification
    cea = build_cea_performers(recording_detail)
    all_soloists = cea.all_soloists

    # Work hierarchy + roles
    role_buckets = RoleBuckets()
    for w in work_hierarchy:
        extract_work_artist_rels(w, role_buckets)
    cwp = build_cwp_tags(work_hierarchy, role_buckets)

    # Session date range from artist relations (conductor/engineer begin/end dates).
    # Stored as ISO 8601 interval ("1984-01-27/1984-02-21") when begin ≠ end, or as the
    # single begin date ("1984-01-27") when begin == end or no end is available.
    session_begin, session_end = _extract_session_date(recording_detail.artist_relation_list)
    if session_begin and session_end and session_begin != session_end:
        session_date = f"{session_begin}/{session_end}"
    else:
        session_date = session_begin

    # Work-level URL relations (IMSLP, Wikidata, etc.) — use bottom work if available
    _work_for_urls = work_hierarchy[0] if work_hierarchy else None
    work_urls = collect_work_urls(_work_for_urls) if _work_for_urls else {}
    work_imslp_url = work_urls.get("download for free", "")
    work_wikidata_url = work_urls.get("wikidata", "")
    work_annotation = _work_for_urls.annotation if _work_for_urls else ""
    work_disambiguation = _work_for_urls.disambiguation if _work_for_urls else ""
    work_iswc = _work_for_urls.iswc if _work_for_urls else ""

    # Fallback work identity used only when work_hierarchy is empty (no work detail was fetched).
    # In that case cwp.work_top and cwp.levels are both empty, so direct_work_id / direct_work_title
    # provide a minimal WORK tag and MUSICBRAINZ_WORKID from the first performance relation stub.
    # When work_hierarchy is non-empty these values are shadowed by cwp fields and never used.
    direct_work_id = ""
    direct_work_title = ""
    for rel in recording_detail.work_relation_list:
        if rel.type == "performance" and rel.work.id:
            direct_work_id = rel.work.id
            direct_work_title = rel.work.title
            break

    # Derive COMPOSER.
    # Prefer plain primary composers; fall back to additional_composers when no primary composer is linked
    # (e.g. a recording where only a completion credit carries the "additional" attribute).
    effective_work_composers = role_buckets.composers or role_buckets.additional_composers
    composer_name = composer_sort = composer_id = ""
    if effective_work_composers:
        composer_name = "; ".join(e.name for e in effective_work_composers)
        composer_sort = "; ".join(e.sort for e in effective_work_composers)
        composer_id = "/".join(e.mbid for e in effective_work_composers)
    elif cea.composers:
        composer_name = "; ".join(e.name for e in cea.composers)
        composer_sort = "; ".join(e.sort for e in cea.composers)
        composer_id = "/".join(e.mbid for e in cea.composers)

    conductor_name = "; ".join(e.name for e in cea.conductors)
    conductor_id = "/".join(e.mbid for e in cea.conductors)
    chorusmaster = "; ".join(e.name for e in cea.chorusmasters)
    leader = "; ".join(e.name for e in cea.leaders)

    # Arranger string (annotated with role in parens per CE convention)
    arranger_seen: set[str] = set()
    arranger_parts: list[str] = []
    for e in cea.arrangers:
        arranger_parts.append(e.name)
        arranger_seen.add(e.name)
    for e in role_buckets.arrangers:
        if e.name not in arranger_seen:
            arranger_parts.append(e.name)
            arranger_seen.add(e.name)
    for e in role_buckets.orchestrators:
        if e.name not in arranger_seen:
            arranger_parts.append(f"{e.name} (orch.)")
            arranger_seen.add(e.name)
    for e in role_buckets.reconstructors:
        arranger_parts.append(f"{e.name} (reconstructed)")
    for e in role_buckets.revisors:
        arranger_parts.append(f"{e.name} (revised)")
    arranger_str = "; ".join(arranger_parts)

    lyricist_str = "; ".join(e.name for e in role_buckets.lyricists + role_buckets.librettists)
    translator_str = "; ".join(e.name for e in role_buckets.translators)

    # Performer strings
    soloist_names = [e.name for e in all_soloists]
    soloist_str = "; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in all_soloists)
    ensemble_names = [e.name for e in cea.ensembles]
    ensemble_str = "; ".join(ensemble_names)
    vocalist_str = "; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in cea.vocalists)
    instrumentalist_str = "; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in cea.instrumentalists)
    instruments_str = "; ".join(e.instrument for e in all_soloists if e.instrument)

    # CEA_RECORDING_ARTIST is CE's *assembled* performer composite (billing order: soloists → conductors →
    # ensembles; STYLEGUIDE 4.2/REND-14).  The verbatim MB recording credit is CEA_MB_ARTISTS/ARTIST.
    recording_artist_names = [e.name for e in all_soloists + cea.conductors + cea.ensembles]
    cea_recording_artist = "; ".join(recording_artist_names) or rec_artist_phrase

    # CEA_RECORDING_ARTISTS (multi-value equivalent) and sort names — same data as cea_recording_artist but
    # stored so downstream tag-mapping scripts can access the raw list.
    cea_recording_artists = cea_recording_artist
    cea_recording_artists_sort = "; ".join(e.sort for e in all_soloists + cea.conductors + cea.ensembles) or rec_artist_sort

    # CEA_MB_ARTISTS: raw MB recording artist-credit phrase, preserved before any replacement.
    cea_mb_artists = rec_artist_phrase

    # CEA plain-name variants (without instrument/voice in brackets).
    cea_vocalist_names = "; ".join(e.name for e in cea.vocalists)
    cea_instrumentalist_names = "; ".join(e.name for e in cea.instrumentalists)

    # CEA sort names
    cea_soloists_sort = "; ".join(e.sort for e in all_soloists)
    cea_ensembles_sort = "; ".join(e.sort for e in cea.ensembles)

    # CEA_INSTRUMENTS_ALL: instruments from recording-level soloists only; work-level instruments are in
    # cwp_keys / cwp_worktype_genres.  CE defines this as the union of recording and work instrument tags; for
    # music-annotator (which stores work-level role names as CWP tags, not instrument names) this is identical
    # to the recording-level instruments string.
    instruments_all_str = instruments_str

    # CEA_ALBUM_* and CEA_SUPPORT_PERFORMERS.
    # "Album artist" means: credited at the release level in MB (release.artist_credit).
    release_artist_names: set[str] = {
        c.artist.name for c in release.artist_credit if isinstance(c, MBArtistCredit) and c.artist.name
    }
    release_artist_sorts: set[str] = {
        c.artist.sort_name for c in release.artist_credit if isinstance(c, MBArtistCredit) and c.artist.sort_name
    }

    def _is_album_artist(entry: ArtistEntry) -> bool:
        """Return True when ``entry`` is credited at the MB release level."""
        return entry.name in release_artist_names or entry.sort in release_artist_sorts

    album_soloists = [e for e in all_soloists if _is_album_artist(e)]
    album_conductors = [e for e in cea.conductors if _is_album_artist(e)]
    album_ensembles = [e for e in cea.ensembles if _is_album_artist(e)]
    # For composers: prefer primary work-level composers, then additional, then recording-level cea.composers.
    all_composers = role_buckets.composers or role_buckets.additional_composers or cea.composers
    album_composers = [e for e in all_composers if _is_album_artist(e)]

    # Support performers: soloists and ensembles who are NOT album artists (conductors excluded per CE).
    support_performers = [e for e in all_soloists + cea.ensembles if not _is_album_artist(e)]

    # Final work/movement tags
    _level0_title = cwp.levels[0].work_id and cwp.levels[0].work_title if cwp.levels else ""
    work_tag = cwp.work_top or _level0_title or direct_work_title or ""
    groupheading = cwp.groupheading or work_tag
    part_tag = cwp.part or (cwp.levels[0].part_title if cwp.levels else "")
    wtype_genre = WORKTYPE_GENRES.get(cwp.worktype_genres, "")
    genre = wtype_genre or "Classical"

    # Track number: use the physical track label for non-CD formats (e.g. "A1" for vinyl),
    # fall back to the integer position string for CD and unknown formats.
    tracknumber_str = track.number or str(track.position)

    # Track length in ms: prefer track-specific length, fall back to recording length.
    track_length_ms = track.length or recording_detail.length

    # Release series membership (semicolon-joined series names).
    series_names = "; ".join(s.series.name for s in release.series_relation_list if s.series.name)

    # Label code from label-info.
    label_info_obj = release.label_info_list[0] if release.label_info_list else None
    label_code_str = label_info_obj.label.label_code if label_info_obj else ""
    label_mbid = label_info_obj.label.id if label_info_obj else ""

    # Cover art archive availability flags.
    caa_front_flag = "1" if release.cover_art_archive.front else ""
    caa_back_flag = "1" if release.cover_art_archive.back else ""

    # Performer credited-as companion: where a performer's credited name differs from canonical.
    credited_parts: list[str] = []
    for arel in recording_detail.artist_relation_list:
        credited_name = arel.target_credit or arel.source_credit
        if credited_name and credited_name != arel.artist.name:
            credited_parts.append(f"{arel.artist.name} [as {credited_name}]")
    cea_performers_credited_str = "; ".join(credited_parts)

    tags = TrackTags(
        cea_conductors_list=cea.conductors,
        cea_ensembles_list=cea.ensembles,
        cea_album_conductors_list=album_conductors,
        cea_album_ensembles_list=album_ensembles,
        title=rec.title,
        artist=rec_artist_phrase,
        artists=rec_artist_phrase,
        artistsort=rec_artist_sort,
        albumartist=album_artist_phrase,
        albumartistsort=album_artist_sort,
        album=release.title,
        tracknumber=tracknumber_str,
        totaltracks=total_tracks,
        totaldiscs=str(len(release.medium_list)),
        discnumber=str(medium_pos),
        date=release.date,
        originaldate=release.release_group.first_release_date,
        recording_first_release_date=recording_detail.first_release_date,
        isrc=recording_detail.isrc_list[0] if recording_detail.isrc_list else "",
        length=str(track_length_ms) if track_length_ms else "",
        discsubtitle=medium.title if medium else "",
        releasecountry=release.country,
        releasetype_secondary="; ".join(release.release_group.secondary_type_list),
        media=medium.format or "CD" if medium else "CD",
        script=release.text_representation.script,
        language=release.text_representation.language,
        releasetype=release.release_group.primary_type,
        releasestatus=release.status,
        organization=label_name,
        label=label_name,
        label_code=label_code_str,
        catalognumber=catalog_number,
        barcode=release.barcode,
        asin=release.asin,
        packaging=release.packaging,
        musicbrainz_labelid=label_mbid,
        comment=recording_detail.disambiguation,
        releasedisambiguation=release.disambiguation,
        recording_date=session_date,
        iswc=work_iswc,
        work_disambiguation=work_disambiguation,
        work_annotation=work_annotation,
        work_imslp_url=work_imslp_url,
        work_wikidata_url=work_wikidata_url,
        musicbrainz_series=series_names,
        caa_front=caa_front_flag,
        caa_back=caa_back_flag,
        cea_performers_credited=cea_performers_credited_str,
        work=work_tag,
        groupheading=groupheading,
        top_work=cwp.work_top or work_tag,
        part=part_tag,
        movement=part_tag,
        subtitle=part_tag,
        composer=composer_name,
        composersort=composer_sort,
        conductor=conductor_name,
        lyricist=lyricist_str,
        translator=translator_str,
        arranger=arranger_str,
        chorusmaster=chorusmaster,
        leader=leader,
        soloists=soloist_str,
        ensemble=ensemble_str,
        band=ensemble_str,
        vocalists=vocalist_str,
        instrumentalists=instrumentalist_str,
        instrument=instruments_str,
        genre=genre,
        period=cwp.period,
        key=cwp.keys,
        is_classical="1",
        work_year=cwp.composed_dates or cwp.published_dates or cwp.premiered_dates,
        composed_date=cwp.composed_dates,
        published_date=cwp.published_dates,
        premiered_date=cwp.premiered_dates,
        producer="; ".join(e.name for e in cea.producers),
        engineer="; ".join(e.name for e in cea.engineers),
        musicbrainz_albumid=release.id,
        musicbrainz_trackid=track.id,
        musicbrainz_recordingid=rec.id,
        musicbrainz_releasegroupid=release.release_group.id,
        musicbrainz_albumartistid=album_artist_ids_str,
        musicbrainz_artistid=rec_artist_ids_str,
        musicbrainz_workid=direct_work_id or (cwp.levels[0].work_id if cwp.levels else ""),
        musicbrainz_conductorid=conductor_id,
        musicbrainz_composerid=composer_id,
        musicbrainz_releasetrackid=track.id,
        cea_recording_artist=cea_recording_artist,
        cea_recording_artists=cea_recording_artists,
        cea_recording_artists_sort=cea_recording_artists_sort,
        cea_mb_artists=cea_mb_artists,
        cea_soloists=soloist_str,
        cea_soloist_names="; ".join(soloist_names),
        cea_soloists_sort=cea_soloists_sort,
        cea_vocalists=vocalist_str,
        cea_vocalist_names=cea_vocalist_names,
        cea_instrumentalists=instrumentalist_str,
        cea_instrumentalist_names=cea_instrumentalist_names,
        cea_other_soloists="; ".join(e.name for e in cea.other_soloists),
        cea_ensembles=ensemble_str,
        cea_ensemble_names="; ".join(ensemble_names),
        cea_ensembles_sort=cea_ensembles_sort,
        cea_album_soloists="; ".join(e.name for e in album_soloists),
        cea_album_soloists_sort="; ".join(e.sort for e in album_soloists),
        cea_album_conductors="; ".join(e.name for e in album_conductors),
        cea_album_conductors_sort="; ".join(e.sort for e in album_conductors),
        cea_album_ensembles="; ".join(e.name for e in album_ensembles),
        cea_album_ensembles_sort="; ".join(e.sort for e in album_ensembles),
        cea_album_composers="; ".join(e.name for e in album_composers),
        cea_album_composers_sort="; ".join(e.sort for e in album_composers),
        cea_support_performers="; ".join(f"{e.name} ({e.instrument})" if e.instrument else e.name for e in support_performers),
        cea_support_performers_sort="; ".join(e.sort for e in support_performers),
        cea_conductors=conductor_name,
        cea_composers=cwp.composers or composer_name,
        cea_composer_lastnames=cwp.composer_lastnames or last_name(composer_sort),
        cea_performers=rec_artist_phrase,
        cea_arrangers=arranger_str,
        cea_orchestrators="; ".join(e.name for e in role_buckets.orchestrators),
        cea_chorusmasters=chorusmaster,
        cea_leaders=leader,
        cea_instruments=instruments_str,
        cea_instruments_all=instruments_all_str,
        cwp_work_top=cwp.work_top,
        cwp_workid_top=cwp.workid_top,
        cwp_work_top_en=cwp.work_top_en,
        cwp_work_top_alt=cwp.work_top_alt,
        cwp_part_levels=str(cwp.part_levels),
        cwp_work_part_levels=str(cwp.work_part_levels),
        cwp_part=cwp.part,
        cwp_work=cwp.work,
        cwp_groupheading=cwp.groupheading,
        cwp_inter_work=cwp.inter_work,
        cwp_composers=cwp.composers,
        cwp_composers_sort=cwp.composers_sort,
        cwp_composer_lastnames=cwp.composer_lastnames,
        cwp_writers=cwp.writers,
        cwp_writers_sort=cwp.writers_sort,
        cwp_arrangers=cwp.arrangers,
        cwp_arrangers_sort=cwp.arrangers_sort,
        cwp_arranger_names=cwp.arranger_names,
        cwp_orchestrators=cwp.orchestrators,
        cwp_orchestrators_sort=cwp.orchestrators_sort,
        cwp_reconstructors=cwp.reconstructors,
        cwp_reconstructors_sort=cwp.reconstructors_sort,
        cwp_revisors=cwp.revisors,
        cwp_revisors_sort=cwp.revisors_sort,
        cwp_lyricists=cwp.lyricists,
        cwp_lyricists_sort=cwp.lyricists_sort,
        cwp_librettists=cwp.librettists,
        cwp_librettists_sort=cwp.librettists_sort,
        cwp_translators=cwp.translators,
        cwp_translators_sort=cwp.translators_sort,
        cwp_keys=cwp.keys,
        cwp_composed_dates=cwp.composed_dates,
        cwp_published_dates=cwp.published_dates,
        cwp_premiered_dates=cwp.premiered_dates,
        cwp_worktype_genres=cwp.worktype_genres,
        cwp_worktype_genres_top=cwp.worktype_genres_top,
    )

    # Mark when cwp_composers was populated from the additional_composers fallback rather than
    # from a plain primary-composer relation.  The cross-track composer unification pass in
    # _pipeline.py uses this flag to identify movements that should inherit the primary composer
    # from another movement in the same work group.
    if cwp.composers and not role_buckets.composers:
        tags.cwp_composers_is_fallback = "1"

    # Add per-level fields as model_extra
    for level in cwp.levels:
        i = level.index
        tags.model_extra[f"cwp_work_{i}"] = level.work_title  # type: ignore[index]
        tags.model_extra[f"cwp_workid_{i}"] = level.work_id  # type: ignore[index]
        tags.model_extra[f"cwp_part_{i}"] = level.part_title  # type: ignore[index]
        tags.model_extra[f"cwp_ordering_key_{i}"] = str(level.ordering_key)  # type: ignore[index]
        tags.model_extra[f"cwp_work_{i}_en"] = level.work_en  # type: ignore[index]
        tags.model_extra[f"cwp_work_{i}_alt"] = level.work_alt  # type: ignore[index]

    return tags


def build_dest_path(  # pylint: disable=unused-argument  # release kept for API stability; C-INIT removed the last internal use
    dest_root: Path, release: MBRelease, track: MBTrack, tags: TrackTags, global_track_idx: int = 0
) -> Path:
    """Compute the destination path (without extension) for one annotated track.

    The first path component is the top-level library class derived by :func:`_top_level_class`
    (C-CLASS, frozen at S1).  Classical releases use the within-classical initial component derived
    by :func:`_classical_top_dir` (C-INIT, frozen at S2); non-classical releases use a
    class-appropriate ``top_dir`` shape.

    Classical layout — single-composer (dominant population, 2-level work hierarchy)::

        <dest_root>/
          Classical/
            <Composer last names> - <Conductor; Ensemble>/
              <Work title> [YYYY]/
                <nn> - <movement title>

    Classical layout — single-composer (3-level — e.g. opera with acts and numbers)::

        <dest_root>/
          Classical/
            <Composer last names> - <Conductor; Ensemble>/
              <Work title> [YYYY]/
                <nn> - <Act title>/
                  <nn> - <number title>

    Classical layout — recital (performer-led, no single composer)::

        <dest_root>/
          Classical/
            <ALBUMARTIST> - <ALBUM>/
              <Work title> [YYYY]/
                <nn> - <movement title>

    Classical layout — multi-composer compilation::

        <dest_root>/
          Classical/
            <albumartist_last_name or "Various"> - <ALBUM>/
              <Work title> [YYYY]/
                <nn> - <movement title>

    Non-classical layouts::

        <dest_root>/
          Spoken Word|Popular|Compilations/
            <ALBUMARTIST or ARTIST> - <ALBUM>/
              <work_dir>/…
          Soundtracks/
            <ALBUM>/
              <work_dir>/…
          Unsorted/
            <ALBUM or "Unknown Album">/
              <work_dir>/…

    The ``<Conductor; Ensemble>`` component (classical only) uses the **album-level** performers —
    i.e. those credited at the release level in ``release.artist_credit``
    (``tags.cea_album_conductors_list`` and ``tags.cea_album_ensembles_list``).  This prevents the
    top-level directory from forking when MB credits performers inconsistently across movements
    (e.g. conductor on some tracks only, or parent ensemble on some tracks and a named subgroup on
    others).  Support performers (credited per-track but not at release level) are captured in tags
    only, not in the directory path.

    Fallback when album-level yields no conductors or ensembles (e.g. composer-only or
    Various-Artists release): uses the per-track union of all conductors + ensembles from
    ``tags.cea_conductors_list`` + ``tags.cea_ensembles_list``, mirroring the original behaviour.
    If that is also empty, falls back to ``CEA_ENSEMBLE_NAMES``, then ``ARTIST``.

    One intermediate directory is introduced for each compositional subdivision level between the
    root work and the leaf (i.e. when ``CWP_PART_LEVELS`` ≥ 2).  Intermediate ``nn`` prefixes are
    directory-scoped zero-padded integers derived from the MB ``ordering-key`` (stored as
    ``CWP_ORDERING_KEY_{i}`` for i ≥ 1).

    ``MOVEMENTNUMBER`` in the tag/title string is the composer's global numbering across the whole
    work (e.g. No. 39 in the Handel Messiah) and is distinct from the directory-local ``nn`` prefix.

    The leaf ``nn`` fallback chain is: ``CWP_MOVT_NUM`` (per-group, gap-free, playback-ordered,
    disc-spanning track index set by ``run()``'s top-work-group pass) → ``global_track_idx``
    (1-based global running index of the source file across all media in the session) →
    ``track.position``.  ``CWP_ORDERING_KEY_0`` is deliberately NOT used for the leaf: it is
    constant across all recordings sharing one MB bottom work, causing leaf collisions when a bottom
    work holds multiple recordings (e.g. Mahler 9 movement I = 8 recordings, all ordering-key=1).
    Using ``global_track_idx`` as the penultimate fallback — rather than ``track.position``, which
    resets to 1 for every disc — guarantees globally unique, monotonically increasing filenames even
    when ``CWP_MOVT_NUM`` is absent and the work spans multiple discs.

    The year suffix uses ``[rec YYYY]`` when a session date is known — sourced from
    ``tags.recording_date_work`` (the minimum interval spanning all movements of the work on
    this medium, set by :func:`~music_annotator._pipeline.run`) or, when that is absent, from
    the per-track ``RECORDING_DATE`` (artist-relation begin/end dates).  Falls back to
    ``[rel YYYY]`` using ``RECORDING_FIRST_RELEASE_DATE`` (year this audio first appeared
    commercially), ``ORIGINALDATE`` (``release_group.first_release_date``), or ``DATE``
    (``release.date``) — in that priority order — when no session date is available.
    Omitted entirely when no date is available.

    :param dest_root: The root destination directory.
    :param release: The :class:`~music_annotator.models.MBRelease` from :func:`fetch_release`.
    :param track: The :class:`~music_annotator.models.MBTrack` for this track.
    :param tags: The :class:`~music_annotator.models.TrackTags` instance for this track, which must already have
        ``movementnumber`` and ``movementtotal`` filled in.
    :param global_track_idx: 1-based global index of this track across all source files in the session.
        Used as the penultimate leaf ``nn`` fallback when ``CWP_MOVT_NUM`` is absent.  Defaults to
        ``0``, which causes the function to fall back to ``track.position`` (the legacy behaviour used
        when the caller does not supply this argument).
    :returns: A :class:`~pathlib.Path` for the destination file *without* extension (callers append ``.flac``, ``.mp3``,
        etc.).
    """
    file_dict = tags.to_file_dict()

    # Composer directory component (tag-derivable; used only for the Classical single-composer case).
    # When raw_composer is empty, _classical_top_dir returns the recital shape (not None), so
    # composer="" is never used in the path — the recital/compilation branch fires first.
    raw_composer = file_dict.get("CWP_COMPOSER_LASTNAMES") or file_dict.get("CEA_COMPOSER_LASTNAMES", "")
    if raw_composer:
        seen: set[str] = set()
        unique: list[str] = []
        for part in raw_composer.split("; "):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                unique.append(part)
        composer = "; ".join(unique)
    else:
        composer = ""

    # Performers directory component.
    #
    # Primary: album-level conductors and ensembles — those credited at the release level in MB
    # (release.artist_credit).  Using the album-level subset prevents the top-level directory from
    # forking when MB credits performers inconsistently across movements (e.g. conductor on some
    # tracks but not others, or parent ensemble on some tracks and named subgroup on others).
    #
    # Fallback (album-level yields nothing): use all per-track conductors + ensembles so that
    # composer-only releases and Various-Artists compilations still produce a meaningful directory.
    # When even the fallback is empty, use CEA_ENSEMBLE_NAMES, then ARTIST.
    album_conductors_path = [e.name for e in tags.cea_album_conductors_list]
    album_ensembles_path = [e.name for e in tags.cea_album_ensembles_list]
    if album_conductors_path or album_ensembles_path:
        performers = "; ".join(album_conductors_path + album_ensembles_path)
    else:
        # Fallback: per-track union (mirrors existing behaviour for releases where no
        # conductor/ensemble is credited at release level).
        all_conductors = [e.name for e in tags.cea_conductors_list]
        all_ensembles = [e.name for e in tags.cea_ensembles_list]
        if all_conductors or all_ensembles:
            performers = "; ".join(all_conductors + all_ensembles)
        else:
            performers = file_dict.get("CEA_ENSEMBLE_NAMES") or file_dict.get("ARTIST", "Unknown Performers")

    # Work directory component — title + [rec YYYY] or [rel YYYY] year suffix.
    #
    # [rec YYYY]: session date derived from artist relation begin dates (conductor/engineer/etc.),
    #   stored in the RECORDING_DATE tag.  This is the actual studio/concert session date.
    #
    # [rel YYYY]: publication-era year from one of three MB fields (most-granular-first):
    #   RECORDING_FIRST_RELEASE_DATE  — year this specific audio first appeared on any release
    #   ORIGINALDATE                  — year the album (release group) was first published
    #   DATE                          — year of this specific pressing
    # Fall back through ALBUM so that non-classical releases (no CWP work hierarchy, no WORK tag)
    # still produce a non-empty work_dir.  An empty work_dir causes Path to collapse the component,
    # reducing the relative path to two parts and corrupting work_top_dir derivation downstream.
    work_title = file_dict.get("CWP_WORK_TOP") or file_dict.get("WORK") or file_dict.get("ALBUM", "")
    work_dir = safe_name(work_title) if work_title else "Unknown Album"

    def _extract_year(raw: str) -> str:
        """Return the 4-digit year prefix of ``raw``, or ``""`` if absent or non-numeric.

        :param raw: A date string such as ``"1963"`` or ``"1963-05-01"``.
        :returns: A 4-digit year string, or ``""`` when unavailable.
        """
        return raw[:4] if len(raw) >= 4 and raw[:4].isdigit() else ""

    # Recording session date label.
    # RECORDING_DATE stores an ISO 8601 date or interval (e.g. "1984-01-27/1984-02-21").
    # The directory label uses CE-convention year or year-range:
    #   - Single year:    [rec 1984]
    #   - Multi-year:     [rec 1983-1984]
    # rel_year falls back to publication-era MB fields when no session date is known.
    #
    # Prefer the work-level union date (tags.recording_date_work, set by run() to the minimum
    # interval spanning all movements of the work on this medium) so that all movements land in
    # the same destination directory even when individual recordings have different session date
    # ranges.  recording_date_work is excluded from to_file_dict() — it is a path-construction
    # helper only — so it must be read directly from the tags object, not via file_dict.
    #
    # Note: recording_date_work only spans movements on the same medium.  Cross-medium
    # unification (e.g. a work split across two CDs) is not supported.
    rec_date = tags.recording_date_work or file_dict.get("RECORDING_DATE", "")
    rec_label = ""
    if rec_date:
        if "/" in rec_date:
            parts = rec_date.split("/", 1)
            begin_y = _extract_year(parts[0])
            end_y = _extract_year(parts[1])
            if begin_y and end_y and begin_y != end_y:
                rec_label = f"[rec {begin_y}-{end_y}]"
            elif begin_y:
                rec_label = f"[rec {begin_y}]"
        else:
            y = _extract_year(rec_date)
            rec_label = f"[rec {y}]" if y else ""

    rel_year = (
        _extract_year(file_dict.get("RECORDING_FIRST_RELEASE_DATE", ""))
        or _extract_year(file_dict.get("ORIGINALDATE", ""))
        or _extract_year(file_dict.get("DATE", ""))
    )

    if rec_label:
        work_dir = f"{work_dir} {rec_label}"
    elif rel_year:
        work_dir = f"{work_dir} [rel {rel_year}]"

    # Hierarchy depth: CWP_PART_LEVELS = n_levels - 1, so >=2 means 3+ levels total.
    part_levels = int(file_dict.get("CWP_PART_LEVELS") or "0")

    # Movement number prefix width (leaf level, scoped to work or nearest parent)
    movt_tot = int(file_dict.get("MOVEMENTTOTAL") or "1")
    width = 3 if movt_tot > 99 else 2

    def _nn(primary_str: str, fallback: int, w: int = 2) -> str:
        """Return zero-padded ``nn`` from the primary string key, or fallback integer.

        For the leaf, ``primary_str`` is ``CWP_MOVT_NUM`` (the per-group track index).
        For intermediate directories, ``primary_str`` is ``CWP_ORDERING_KEY_{i}`` (i ≥ 1).

        :param primary_str: String value of the primary key (``"0"`` or ``""`` when absent).
        :param fallback: 1-based ordinal used when the primary key is zero/absent.
        :param w: Zero-pad width.
        :returns: Zero-padded string.
        """
        key = int(primary_str) if primary_str.isdigit() else 0
        return str(key if key > 0 else fallback).zfill(w)

    # Top-level library class (C-CLASS, frozen at S1).
    # Derived from embedded tags only so that repath/regroup/unify (which call build_dest_path
    # with empty MBRelease() stubs) reconstruct the correct class from tags alone.
    class_dir = safe_name(_top_level_class(tags))

    # top_dir: class-appropriate first component beneath the class prefix.
    # Classical: C-INIT rule (frozen at S2) — compilation, recital, or single-composer default.
    # Non-classical: simple honest shapes per C-CLASS (R-3 — population is thin).
    match class_dir:
        case "Classical":
            # C-INIT: _classical_top_dir returns the top_dir for compilation/recital cases,
            # or None for single-composer (dominant population) → use <composer> - <performers>.
            # When _classical_top_dir returns None, raw_composer is non-empty (that is the
            # single-composer gate), so composer is already set from raw_composer above.
            _cinit = _classical_top_dir(tags)
            top_dir = _cinit if _cinit is not None else safe_name(f"{composer} - {performers}")
        case "Soundtracks":
            # Soundtrack title is the primary identity; composer/artist is unreliable across a VA score.
            top_dir = safe_name(file_dict.get("ALBUM", "") or "Unknown Album")
        case "Unsorted":
            top_dir = safe_name(file_dict.get("ALBUM", "") or "Unknown Album")
        case _:
            # Spoken Word, Popular, Compilations: author/narrator or artist, then title.
            artist_part = file_dict.get("ALBUMARTIST") or file_dict.get("ARTIST", "")
            album_part = file_dict.get("ALBUM", "")
            if artist_part and album_part:
                top_dir = safe_name(f"{artist_part} - {album_part}")
            elif album_part:
                top_dir = safe_name(album_part)
            else:
                top_dir = safe_name(artist_part or "Unknown")

    track_title = safe_name(file_dict.get("TITLE") or _rec_title(track))

    if part_levels >= 2:
        # Build intermediate directory path components for levels 1 … part_levels-1
        # (level 0 = leaf, level part_levels = root/top — already the work_dir).
        intermediate: list[str] = []
        for i in range(part_levels - 1, 0, -1):
            # Levels are stored innermost-first (index 0 = leaf), so level i is the i-th ancestor.
            part_title = file_dict.get(f"CWP_PART_{i}", "") or file_dict.get(f"CWP_WORK_{i}", "")
            # Intermediate nn = per-group sibling index (C-L1), gap-free, mirroring the leaf's
            # cwp_movt_num authority; raw ordering-key is the fallback only (no-group/no-hierarchy).
            inter_idx = file_dict.get(f"CWP_INTER_INDEX_{i}", "")
            ok_str = inter_idx if inter_idx else file_dict.get(f"CWP_ORDERING_KEY_{i}", "0")
            nn = _nn(ok_str, i)
            intermediate.append(safe_name(f"{nn} - {part_title}") if part_title else nn)

        # Leaf nn: CWP_MOVT_NUM (per-group gap-free index) → global_track_idx → track.position.
        # CWP_MOVT_NUM is used here, NOT MOVEMENTNUMBER: they hold identical values today but are
        # distinct vocabularies — MOVEMENTNUMBER is the standard movement tag that CE conventions
        # may later repurpose, while CWP_MOVT_NUM is the CWP path-construction index and is the
        # permanent leaf authority (C-L0).  CWP_ORDERING_KEY_0 is deliberately NOT used: it is
        # constant across all recordings sharing one MB bottom work, causing leaf collisions.
        # global_track_idx (1-based, monotonically increasing across all media in the session) is
        # the correct penultimate fallback for multi-disc works where track.position resets per disc.
        # When global_track_idx is 0 (caller omitted it) fall back to track.position.
        leaf_movt_num = file_dict.get("CWP_MOVT_NUM", "")
        leaf_fallback = global_track_idx or track.position
        leaf_nn = _nn(leaf_movt_num, leaf_fallback, width)

        path: Path = dest_root / class_dir / top_dir / work_dir
        for d in intermediate:
            path = path / d
        return path / f"{leaf_nn} - {track_title}"

    # 1- or 2-level hierarchy: single work directory + leaf file.
    # Leaf nn: CWP_MOVT_NUM (per-group gap-free index) → global_track_idx → track.position.
    # CWP_MOVT_NUM is used here, NOT MOVEMENTNUMBER: they hold identical values today but are
    # distinct vocabularies — MOVEMENTNUMBER is the standard movement tag that CE conventions
    # may later repurpose, while CWP_MOVT_NUM is the CWP path-construction index and is the
    # permanent leaf authority (C-L0).  CWP_ORDERING_KEY_0 is deliberately NOT used: it is
    # constant across all recordings sharing one MB bottom work, causing leaf collisions.
    leaf_movt_num = file_dict.get("CWP_MOVT_NUM", "")
    leaf_fallback = global_track_idx or track.position
    track_num = _nn(leaf_movt_num, leaf_fallback, width)
    return dest_root / class_dir / top_dir / work_dir / f"{track_num} - {track_title}"
