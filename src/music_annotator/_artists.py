"""Artist and performer classification helpers for music-annotator.

Implements the Classical Extras (CE) conventions for identifying orchestras, choirs, and other
ensemble types from artist display names, and for processing ``artist-credit`` lists from the
MusicBrainz API.
"""

from __future__ import annotations

from music_annotator.models import MBAlias, MBArtist, MBArtistCredit

#: Substrings identifying orchestral ensembles (from CEA_ORCHESTRAS).
ORCHESTRA_STRINGS: frozenset[str] = frozenset(
    {
        "orchestra",
        "philharmonic",
        "philharmonica",
        "philharmoniker",
        "musicians",
        "academy",
        "symphony",
        "orkester",
    }
)

#: Substrings identifying choral ensembles (from CEA_CHOIRS).
CHOIR_STRINGS: frozenset[str] = frozenset({"choir", "chorus", "singers", "domchor", "koor", "kammerkoor"})

#: Substrings identifying chamber/small-group ensembles (from CEA_GROUPS).
GROUP_STRINGS: frozenset[str] = frozenset(
    {
        "ensemble",
        "band",
        "trio",
        "quartet",
        "quintet",
        "sextet",
        "septet",
        "octet",
        "chamber",
        "consort",
        "players",
        "quartett",
    }
)

#: Union of all ensemble-identifying substrings.
ENSEMBLE_STRINGS: frozenset[str] = ORCHESTRA_STRINGS | CHOIR_STRINGS | GROUP_STRINGS

#: Annotation labels for specialist roles (cea_* annotation defaults).
ROLE_ANNOTATIONS: dict[str, str] = {
    "arranger": "arr.",
    "instrument arranger": "arr.",
    "vocal arranger": "arr.",
    "orchestrator": "orch.",
    "reconstructed by": "reconstructed",
    "revised by": "revised",
    "translator": "trans.",
    "lyricist": "lyrics",
    "librettist": "libretto",
    "writer": "writer",
    "chorus master": "choirmaster",
    "concertmaster": "leader",
    "balance": "balance",
    "engineer": "engineer",
    "mix": "mix",
    "recording": "recording",
    "audio": "audio",
    "sound": "sound",
    "producer": "producer",
}

#: Relationship types that map to the ARRANGER tag (CE convention).
ARRANGER_RELS: frozenset[str] = frozenset(
    {"arranger", "instrument arranger", "vocal arranger", "orchestrator", "reconstructed by", "revised by"}
)


def is_ensemble(name: str) -> bool:
    """Return ``True`` if the artist name contains an ensemble-identifying substring.

    Checks against the union set :data:`ENSEMBLE_STRINGS` which covers orchestras, choirs, and chamber groups.

    :param name: The artist display name.
    :returns: ``True`` when any token from :data:`ENSEMBLE_STRINGS` appears in the lowercased name.
    """
    low = name.lower()
    return any(s in low for s in ENSEMBLE_STRINGS)


def is_choir(name: str) -> bool:
    """Return ``True`` if the artist name contains a choir-identifying substring.

    :param name: The artist display name.
    :returns: ``True`` when any token from :data:`CHOIR_STRINGS` appears in the lowercased name.
    """
    low = name.lower()
    return any(s in low for s in CHOIR_STRINGS)


def is_orchestra(name: str) -> bool:
    """Return ``True`` if the artist name contains an orchestra-identifying substring.

    :param name: The artist display name.
    :returns: ``True`` when any token from :data:`ORCHESTRA_STRINGS` appears in the lowercased name.
    """
    low = name.lower()
    return any(s in low for s in ORCHESTRA_STRINGS)


def artist_credit_phrase(credit_list: list[MBArtistCredit | str]) -> str:
    """Reconstruct the display credit phrase from a MusicBrainz ``artist-credit`` list.

    The MB API returns ``artist-credit`` as a mixed list of :class:`~music_annotator.models.MBArtistCredit` instances
    (for actual artists) and plain strings (for join phrases like ``" & "``).  This function concatenates the credited name
    of each artist (falling back to the artist's canonical name) with any intervening join-phrase strings.

    :param credit_list: The ``artist-credit`` list from a MB response.
    :returns: The concatenated display string, e.g. ``"Karajan & Berliner Philharmoniker"``.
    """
    parts: list[str] = []
    for item in credit_list:
        match item:
            case str():
                parts.append(item)
            case MBArtistCredit():
                parts.append(item.name or item.artist.name)
            case _:  # pragma: no cover
                pass
    return "".join(parts)


def artist_ids(credit_list: list[MBArtistCredit | str]) -> list[str]:
    """Extract MBIDs from an ``artist-credit`` list, skipping plain join-phrase strings.

    :param credit_list: The ``artist-credit`` list from a MB response.
    :returns: A list of MBID strings for all :class:`~music_annotator.models.MBArtistCredit` entries that have a
        non-empty ``artist.id``, in order.
    """
    return [item.artist.id for item in credit_list if isinstance(item, MBArtistCredit) and item.artist.id]


def artist_sort_names(credit_list: list[MBArtistCredit | str]) -> list[str]:
    """Extract sort-names from an ``artist-credit`` list, skipping join-phrase-only entries.

    Entries with no artist MBID (i.e. join-phrase-only dicts such as ``{"joinphrase": " & "}``) are skipped because they
    do not represent a real credited artist.

    :param credit_list: The ``artist-credit`` list from a MB response.
    :returns: A list of sort-name strings (falling back to the display name when ``sort_name`` is absent) for all
        :class:`~music_annotator.models.MBArtistCredit` entries that have a non-empty ``artist.id``, in order.
    """
    result_names: list[str] = []
    for item in credit_list:
        if isinstance(item, MBArtistCredit) and item.artist.id:
            result_names.append(item.artist.sort_name or item.artist.name)
    return result_names


def last_name(sort_name: str) -> str:
    """Extract the last name from a MusicBrainz sort-name of the form ``"Surname, Forename"``.

    :param sort_name: A sort-name string, typically ``"Surname, Forename"`` or just a single token.
    :returns: The part of the sort-name before the first comma, stripped of whitespace.  Returns the full string when no
        comma is present.
    """
    return sort_name.split(",")[0].strip()


#: Alias type strings that represent the entity's own name (native or legal form), as opposed to
#: search hints or transliterations.  Used by :func:`canonical_artist_form` to prefer substantive
#: name-form aliases over search-optimisation entries.
_CANONICAL_ALIAS_TYPES: frozenset[str] = frozenset({"Artist name", "Legal name"})


def canonical_artist_form(artist: MBArtist) -> str:
    """Return the canonical name-form for an artist per STYLEGUIDE 3.1/NORM-2.

    Selects the entity's canonical name-form from MB's own primary-flagged aliases (authority-deference
    posture: the resolved form is always a form MB itself asserts — never a locally-authored form,
    editorial table, or new scholarly romanisation).

    Selection logic:

    1. Collect all aliases where ``primary == "primary"``.
    2. Among those, prefer aliases whose ``type`` is ``"Artist name"`` or ``"Legal name"`` (substantive
       name-form entries, as opposed to search hints or transliterations).
    3. If no typed primary alias exists, fall back to any primary alias regardless of type.
    4. If no primary alias exists at all, fall back to ``artist.name`` (the MB display name).

    The resolver is total: it never raises and always returns a non-empty string when ``artist.name``
    is non-empty.  When both ``alias_list`` and ``name`` are empty (a default-constructed
    :class:`~music_annotator.models.MBArtist`), it returns ``""`` — callers that need a guaranteed
    non-empty string should ensure the artist has a name.

    :param artist: The :class:`~music_annotator.models.MBArtist` entity to resolve.
    :returns: The canonical name-form string — a primary-flagged MB alias when one exists, otherwise
        ``artist.name``.
    """
    primary_aliases: list[MBAlias] = [a for a in artist.alias_list if a.primary == "primary"]
    if not primary_aliases:
        return artist.name

    # Prefer a substantive name-form alias (Artist name / Legal name) over other primary types.
    typed = [a for a in primary_aliases if a.type in _CANONICAL_ALIAS_TYPES]
    chosen = typed[0] if typed else primary_aliases[0]
    return chosen.name or artist.name
