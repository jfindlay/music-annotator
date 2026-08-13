"""Pydantic data models for MusicBrainz API responses and Classical Extras tag fields.

These models validate and structure the raw dict data returned by ``musicbrainzngs`` before it is consumed by the annotation
logic.  All fields that the MB API may omit default to empty strings or empty lists so callers never need to guard against
``KeyError``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Base types
# ---------------------------------------------------------------------------


type JSON = dict[str, JSON] | list[JSON] | str | float | int | bool | None  # pylint: disable=invalid-name


# ---------------------------------------------------------------------------
# Annotation-tier vocabulary (C-TIER)
# ---------------------------------------------------------------------------


class AnnotationTier(StrEnum):
    """Closed vocabulary for annotation completeness of an ingested release.

    Records *how completely a release could be annotated*, orthogonal to the archival-identity
    confidence ladder (``_IDENTITY_METHODS`` rungs in ``_pipeline_io.py``).  A release can be
    high-tier / low-rung (full MB annotation, identity only from source tags) or low-tier /
    high-rung (source-tags-only ingest, strong AcoustID identity).

    Tier ordering from lowest to highest completeness:

    ``source-tags-only`` < ``alternate-source`` < ``mb-partial``
    < ``mb-search-resolved`` < ``full-mb-verified``

    This ordering is encoded in :data:`ANNOTATION_TIER_ORDER` and enforced by
    :func:`annotation_tier_rank`.  The monotonic-upgrade rule on
    :class:`ProvenanceSidecar` uses this ordering: ``annotation_tier`` may only be
    overwritten when the new tier ranks strictly higher than the current one.
    """

    FULL_MB_VERIFIED = "full-mb-verified"
    """Identity-confirmed full MB annotation (embedded MBID or TOC disc-ID match)."""

    MB_SEARCH_RESOLVED = "mb-search-resolved"
    """Search-reconciled MB annotation, lower confidence; ``needs_spot_check`` is set."""

    MB_PARTIAL = "mb-partial"
    """MB release identified but track/structure disagrees; mismatch recorded."""

    ALTERNATE_SOURCE = "alternate-source"
    """Non-MB external identity (Discogs-style) — reserved, no census population today."""

    SOURCE_TAGS_ONLY = "source-tags-only"
    """No MB identity; provisional minimal ingest from embedded/source tags only."""


#: Tier rank mapping — higher integer = higher completeness.  Used by :func:`annotation_tier_rank`.
ANNOTATION_TIER_ORDER: dict[AnnotationTier, int] = {
    AnnotationTier.SOURCE_TAGS_ONLY: 0,
    AnnotationTier.ALTERNATE_SOURCE: 1,
    AnnotationTier.MB_PARTIAL: 2,
    AnnotationTier.MB_SEARCH_RESOLVED: 3,
    AnnotationTier.FULL_MB_VERIFIED: 4,
}


def annotation_tier_rank(tier: AnnotationTier) -> int:
    """Return the integer rank of ``tier`` (higher = more complete).

    :param tier: An :class:`AnnotationTier` value.
    :returns: Integer rank in ``[0, 4]``.
    """
    return ANNOTATION_TIER_ORDER[tier]


class CensusSignal(StrEnum):
    """Census axis-2 classification signals consumed by :func:`classify_annotation_tier`.

    These signals are produced by the census/discovery pass and map to annotation tiers.
    ``ALTERNATE_SOURCE`` has no census signal today (reserved for R3c Discogs adapter).
    """

    EMBEDDED_MBID = "embedded-mbid"
    """Source file carries an embedded MusicBrainz recording MBID — strongest identity signal."""

    ISRC_MATCH = "isrc-match"
    """Source file ISRCs match the selected medium's recording ISRC lists — offline identity confirmation."""

    SEARCH_HIT = "search-hit"
    """Release resolved via MB search (track-count reconciliation, no embedded MBID)."""

    MISMATCH = "mismatch"
    """MB release identified but track/structure disagrees."""

    NOT_IN_MB = "not-in-mb"
    """No MB identity found; ingest from source tags only."""


def classify_annotation_tier(signal: CensusSignal) -> tuple[AnnotationTier, bool]:
    """Map a census axis-2 classification signal to an annotation tier and ``needs_spot_check`` flag.

    Pure function — no I/O, fully unit-testable.  The ``alternate-source`` tier has no census
    signal today (reserved for the R3c Discogs adapter); it is not reachable from this helper.

    :param signal: A :class:`CensusSignal` value from the census/discovery pass.
    :returns: A ``(tier, needs_spot_check)`` tuple.  ``needs_spot_check`` is ``True`` only for
        ``mb-search-resolved`` entries (J1 adjudication: search-only confidence is real).
    """
    match signal:
        case CensusSignal.EMBEDDED_MBID:
            return AnnotationTier.FULL_MB_VERIFIED, False
        case CensusSignal.SEARCH_HIT:
            return AnnotationTier.MB_SEARCH_RESOLVED, True
        case CensusSignal.MISMATCH:
            return AnnotationTier.MB_PARTIAL, False
        case CensusSignal.NOT_IN_MB:
            return AnnotationTier.SOURCE_TAGS_ONLY, False
        case CensusSignal.ISRC_MATCH:
            return AnnotationTier.FULL_MB_VERIFIED, False
        case _:  # pragma: no cover
            return AnnotationTier.SOURCE_TAGS_ONLY, False


# ---------------------------------------------------------------------------
# AccurateRip provenance models (C-AR)
# ---------------------------------------------------------------------------


class AccurateRipResult(StrEnum):
    """Per-version AccurateRip verification outcome (whipper ``WhipperLogger.trackLog`` ``Result``).

    Values mirror whipper's native logger strings exactly.  Consumers that ``match``/``case`` on this
    enum must include a ``case _: # pragma: no cover`` arm per house style.
    """

    EXACT_MATCH = "exact-match"
    """Whipper "Found, exact match" — track CRC matches the AccurateRip database entry."""

    NO_EXACT_MATCH = "no-exact-match"
    """Whipper "Found, NO exact match" — track is in the database but CRC differs."""

    NOT_PRESENT = "not-present"
    """Whipper "Track not present in AccurateRip database" — no database entry for this track."""


class AccurateRipTrackResult(BaseModel):
    """One AccurateRip DB generation (v1 or v2) result for a single track.

    Carries the verification outcome, confidence counter, and both CRC values as emitted by
    whipper's ``WhipperLogger.trackLog``.  All fields default to empty/zero so a missing DB
    generation can be represented without ``None``.

    Important attributes: ``version``, ``result``, ``confidence``, ``local_crc``, ``remote_crc``.
    """

    version: str = ""
    """DB generation identifier: ``"v1"`` or ``"v2"``."""

    result: AccurateRipResult = AccurateRipResult.NOT_PRESENT
    """Verification outcome for this DB generation."""

    confidence: int = 0
    """Whipper ``DBConfidence`` counter; 0 when ``not-present``."""

    local_crc: str = ""
    """Whipper "Local CRC" (uppercase hex); empty when ``not-present``."""

    remote_crc: str = ""
    """Whipper "Remote CRC" (uppercase hex); empty when ``not-present``."""


class AccurateRipTrack(BaseModel):
    """Per-track AccurateRip container: both DB generations + rip CRCs + status.

    Carries the full per-track AccurateRip data from a whipper native log.  The ingest pipeline
    projects the structured fields onto the flat ``str`` tag fields on ``TrackTags``/``TransactionEntry``
    (the tag round-trip surface); this model is the typed intermediate.

    Important attributes: ``v1``, ``v2``, ``test_crc``, ``copy_crc``, ``status``.
    """

    v1: AccurateRipTrackResult = Field(default_factory=AccurateRipTrackResult)
    """AccurateRip v1 result for this track."""

    v2: AccurateRipTrackResult = Field(default_factory=AccurateRipTrackResult)
    """AccurateRip v2 result for this track."""

    test_crc: str = ""
    """Whipper "Test CRC" (``%08X`` uppercase hex); empty when not ripped."""

    copy_crc: str = ""
    """Whipper "Copy CRC" (``%08X`` uppercase hex); empty when not ripped."""

    status: str = ""
    """Whipper "Status" text verbatim: ``"Copy OK"``, ``"Error, CRC mismatch"``, or ``"Track not ripped (skipped)"``."""


class AccurateRipSummary(BaseModel):
    """Per-release AccurateRip summary (whipper "CD metadata" + "Conclusive status report").

    Persisted in the ``freedb_disc_N.yaml`` / ``music_annotator_provenance.yaml`` sidecar as a
    nested YAML object (the flat-``str`` constraint is a tag-layer constraint only; nested models
    are fine in the sidecar).  Subject to the monotonic-upgrade rule: an incoming empty summary
    must not overwrite a populated one (check ``log_sha256`` or any non-empty field).

    Important attributes: ``mb_disc_id``, ``cddb_disc_id``, ``log_sha256``, ``accurately_ripped``,
    ``in_ar_database``, ``summary_text``.
    """

    mb_disc_id: str = ""
    """Whipper "MusicBrainz Disc ID" from the CD metadata block."""

    cddb_disc_id: str = ""
    """Whipper "CDDB Disc ID" from the CD metadata block."""

    log_sha256: str = ""
    """Trailing "SHA-256 hash:" line from the whipper log (uppercase hex); used as the populated-summary sentinel."""

    accurately_ripped: int = 0
    """Whipper ``_accuratelyRipped`` counter from the conclusive status report."""

    in_ar_database: int = 0
    """Whipper ``_inARDatabase`` counter from the conclusive status report."""

    summary_text: str = ""
    """Whipper "AccurateRip summary" message line verbatim."""

    def is_populated(self) -> bool:
        """Return ``True`` when this summary carries real data (any non-empty/non-zero field).

        Used by the monotonic-upgrade rule: an incoming empty ``AccurateRipSummary`` must not
        overwrite a populated one.

        :returns: ``True`` if any field is non-empty or non-zero.
        """
        return bool(
            self.log_sha256
            or self.mb_disc_id
            or self.cddb_disc_id
            or self.summary_text
            or self.accurately_ripped
            or self.in_ar_database
        )


# ---------------------------------------------------------------------------
# MusicBrainz API response models
# ---------------------------------------------------------------------------


class MBAlias(BaseModel):
    """A single alias entry from the MusicBrainz ``alias-list`` on a work or artist.

    MusicBrainz stores one or more aliases per entity covering different locales, scripts, and name types.
    Each alias carries an optional ``locale`` (ISO 639-1 language code, e.g. ``"en"``, ``"ru"``), an optional
    ``type`` (e.g. ``"Work name"``, ``"Artist name"``, ``"Search hint"``), and an optional ``primary`` marker
    (the string ``"primary"`` when the alias is the primary form for its locale, ``None`` otherwise).

    The ``musicbrainzngs`` library returns the alias display text under the key ``"alias"`` (not ``"name"``).
    The ``model_validator`` below remaps ``"alias"`` → ``"name"`` before Pydantic processes the dict so
    that the ``name`` field populates correctly from raw ``mb.get_*`` output.

    Important attributes: ``name``, ``locale``, ``type``, ``primary``.
    """

    name: str = ""
    sort_name: str = Field(default="", alias="sort-name")
    locale: str | None = None
    type: str = ""
    primary: str | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def remap_alias_key(cls, data: object) -> object:
        """Remap the ``"alias"`` key to ``"name"`` before field population.

        ``musicbrainzngs`` returns the alias display text under the key ``"alias"`` in the raw dict
        (matching the MMD 2.0 XML attribute name), not ``"name"`` as the REST JSON API uses.  This
        validator remaps the key so the ``name`` field is populated correctly from ``mb.get_*`` output.

        :param data: The raw input dict (or any other value) before Pydantic field validation.
        :returns: The input unchanged when it is not a dict, or a copy with ``"alias"`` remapped to
            ``"name"`` (only when ``"name"`` is not already present).
        """
        if isinstance(data, dict) and "alias" in data and "name" not in data:
            data = dict(data)
            data["name"] = data.pop("alias")
        return data


class MBArtist(BaseModel):
    """A single artist entity as returned inside an artist-credit or relation.

    Important attributes: ``id`` (MBID), ``name`` (display name), ``sort_name`` (sortable form), ``type`` (e.g.
    ``"Person"``), ``disambiguation`` (short comment differentiating artists with the same name),
    ``alias_list`` (MB aliases for this artist, populated by a dedicated alias fetch — empty when the artist
    appears only as a nested entity in a release/recording response).
    """

    id: str = ""
    name: str = ""
    sort_name: str = Field(default="", alias="sort-name")
    type: str = ""
    disambiguation: str = ""
    alias_list: list[MBAlias] = Field(default_factory=list, alias="alias-list")

    model_config = {"populate_by_name": True}


class MBArtistCredit(BaseModel):
    """One item in an ``artist-credit`` list — either a structured artist entry or a plain join-phrase string.

    The MB API returns ``artist-credit`` as a mixed list; bare strings are join phrases (e.g. ``" & "``).

    Important attributes: ``name`` (credited name), ``artist`` (:class:`MBArtist`), ``joinphrase`` (text appended after this
    credit, e.g. ``", "``).
    """

    name: str = ""
    artist: MBArtist = Field(default_factory=MBArtist)
    joinphrase: str = ""

    model_config = {"populate_by_name": True}


class MBAttribute(BaseModel):
    """A structured attribute on a work, such as a key signature or composition date.

    Important attributes: ``type`` (attribute category, e.g. ``"Key"``), ``value`` (attribute value, e.g. ``"G minor"``).

    .. note::
        This class must be defined before :class:`MBArtistRelation` and :class:`MBWork` because both reference it in their
        ``attribute_list`` field type.
    """

    type: str = ""
    value: str = ""


class MBArtistRelation(BaseModel):
    """An entry in an ``artist-relation-list`` on a recording or work.

    Important attributes: ``type`` (relation type, e.g. ``"composer"``), ``direction``, ``begin`` / ``end`` (ISO date
    strings for when the relation was active — e.g. the recording session dates on conductor/engineer relations),
    ``ended`` (whether the relation has ended), ``target_credit`` (how the artist is credited in this specific context,
    e.g. on liner notes, when it differs from the canonical name), ``artist`` (:class:`MBArtist`), ``attribute_list``
    (list of :class:`MBAttribute` or plain strings from the MB API).

    The ``begin`` / ``end`` / ``ended`` triplet is the primary source for recording session dates when ``type`` is one of
    ``"conductor"``, ``"performing orchestra"``, ``"balance"``, ``"engineer"``, etc.
    """

    type: str = ""
    direction: str = ""
    begin: str = ""
    end: str = ""
    ended: bool = False
    target_credit: str = Field(default="", alias="target-credit")
    source_credit: str = Field(default="", alias="source-credit")
    artist: MBArtist = Field(default_factory=MBArtist)
    attribute_list: list[MBAttribute | str] = Field(default_factory=list, alias="attribute-list")

    model_config = {"populate_by_name": True}

    @field_validator("ended", mode="before")
    @classmethod
    def coerce_ended(cls, v: str | bool | None) -> bool:
        """Coerce the ``ended`` field from various MB API representations to a plain ``bool``.

        musicbrainzngs returns ``ended`` as the string ``"true"`` or ``"false"``; the JSON API returns
        a boolean.  ``None`` is treated as ``False``.

        :param v: Raw value from the API response.
        :returns: A boolean.
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"


class MBWorkStub(BaseModel):
    """Minimal work reference embedded in a ``work-relation-list`` entry.

    Important attributes: ``id`` (work MBID), ``title`` (work title).
    """

    id: str = ""
    title: str = ""

    model_config = {"populate_by_name": True}


class MBWorkRelation(BaseModel):
    """An entry in a ``work-relation-list`` on a recording or work.

    Important attributes: ``type`` (relation type, e.g. ``"parts"`` or ``"performance"``), ``direction``
    (``"forward"``/``"backward"``), ``ordering_key`` (integer sort position of the child within its parent,
    as returned by the MB API ``ordering-key`` field; ``0`` when absent), ``work`` (:class:`MBWork`).

    When the recording was fetched with the ``work-level-rels`` include, ``work`` is a fully populated
    :class:`MBWork` (with its own ``artist_relation_list`` and ``work_relation_list``).  Without that
    include it contains only ``id`` and ``title``.

    The ``ordering-key`` field is present on ``parts``/``part of`` relations and gives an explicit integer
    ordering for child works within their parent (e.g. Act I = 1, Act II = 2).  musicbrainzngs returns it as
    a string; Pydantic coerces it to ``int`` automatically.  It is ``0`` when absent or unpopulated in MB.

    .. note::
        Because :class:`MBWork` is defined after this class, Pydantic's forward-reference resolution is
        triggered by calling ``MBWorkRelation.model_rebuild()`` at module level after :class:`MBWork` is
        defined.
    """

    type: str = ""
    direction: str = ""
    begin: str = ""
    end: str = ""
    ended: bool = False
    ordering_key: int = Field(default=0, alias="ordering-key")
    work_id: str = Field(default="", alias="work-id")
    work_title: str = Field(default="", alias="work-title")
    work: MBWork = Field(default_factory=lambda: MBWork())  # pylint: disable=unnecessary-lambda

    model_config = {"populate_by_name": True}

    @field_validator("ended", mode="before")
    @classmethod
    def coerce_ended(cls, v: str | bool | None) -> bool:
        """Coerce the ``ended`` field from string/bool/None to a plain ``bool``.

        :param v: Raw value from the API response.
        :returns: A boolean.
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"

    @field_validator("ordering_key", mode="before")
    @classmethod
    def coerce_ordering_key(cls, v: str | int | None) -> int:
        """Normalise the ``ordering-key`` field from the MB API.

        The MB API returns ``ordering-key`` as a string (e.g. ``"8"``), an integer, or ``null`` when
        the field is unpopulated.  This validator converts all three cases to a plain ``int``, with
        ``None`` mapping to ``0``.

        :param v: Raw value for ``ordering-key`` from the API response.
        :returns: An integer ordering key, or ``0`` when the value is ``None`` or absent.
        """
        if v is None:
            return 0
        return int(v)


class MBTag(BaseModel):
    """A folksonomy tag attached to a MB entity.

    Important attributes: ``name`` (tag text), ``count`` (vote count).
    """

    name: str = ""
    count: int = 0


class MBLabel(BaseModel):
    """Label entity used inside ``label-info-list`` and label relations on works.

    Important attributes: ``id`` (label MBID), ``name`` (label display name), ``sort_name``,
    ``label_code`` (e.g. ``173`` for Deutsche Grammophon), ``type`` (e.g. ``"Imprint"``).
    """

    id: str = ""
    name: str = ""
    sort_name: str = Field(default="", alias="sort-name")
    label_code: str = Field(default="", alias="label-code")
    type: str = ""

    model_config = {"populate_by_name": True}


class MBPlace(BaseModel):
    """Minimal place entity embedded in a ``place-relation-list`` entry on a work.

    Important attributes: ``id`` (place MBID), ``name`` (place display name, e.g. ``"Theater am Kärntnertor"``).
    """

    id: str = ""
    name: str = ""

    model_config = {"populate_by_name": True}


class MBSeries(BaseModel):
    """Minimal series entity embedded in a ``series-relation-list`` entry on a release.

    Important attributes: ``id`` (series MBID), ``name`` (series name, e.g. ``"Karajan Gold"``), ``type``.
    """

    id: str = ""
    name: str = ""
    type: str = ""

    model_config = {"populate_by_name": True}


class MBUrlRelation(BaseModel):
    """A URL relation on a work or release, linking to external resources such as IMSLP or Discogs.

    Important attributes: ``type`` (relation type, e.g. ``"download for free"``, ``"discogs"``, ``"wikidata"``),
    ``url`` (the external URL string extracted from the nested ``url`` dict returned by musicbrainzngs —
    i.e. the value of ``url.resource``).
    """

    type: str = ""
    url: str = ""

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def extract_url_resource(cls, data: JSON) -> JSON:
        """Extract the URL string from the nested ``url`` sub-object returned by musicbrainzngs.

        musicbrainzngs returns URL relations as ``{"type": "...", "url": {"resource": "https://..."}}``.
        This validator flattens ``url.resource`` into the top-level ``url`` key.

        :param data: Raw relation dict from musicbrainzngs.
        :returns: Normalised dict with ``url`` as a plain string.
        """
        if not isinstance(data, dict):  # pragma: no cover
            return data  # pragma: no cover
        url_val = data.get("url", "")
        if isinstance(url_val, dict):
            data = dict(data)
            data["url"] = url_val.get("resource", "")
        return data


class MBPlaceRelation(BaseModel):
    """A place relation on a work, typically recording a premiere location and date.

    Important attributes: ``type`` (e.g. ``"premiere"``), ``begin`` (ISO date of the event, e.g. premiere date),
    ``end``, ``ended``, ``place`` (:class:`MBPlace`).
    """

    type: str = ""
    direction: str = ""
    begin: str = ""
    end: str = ""
    ended: bool = False
    place: MBPlace = Field(default_factory=MBPlace)

    model_config = {"populate_by_name": True}

    @field_validator("ended", mode="before")
    @classmethod
    def coerce_ended(cls, v: str | bool | None) -> bool:
        """Coerce ``ended`` to bool.

        :param v: Raw value.
        :returns: Boolean.
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"


class MBLabelRelation(BaseModel):
    """A label relation on a work, typically a publishing credit with a date.

    Important attributes: ``type`` (e.g. ``"publishing"``), ``begin`` (ISO date the publishing started — CE source
    for ``CWP_PUBLISHED_DATES``), ``end``, ``ended``, ``label`` (:class:`MBLabel`).
    """

    type: str = ""
    direction: str = ""
    begin: str = ""
    end: str = ""
    ended: bool = False
    label: MBLabel = Field(default_factory=MBLabel)

    model_config = {"populate_by_name": True}

    @field_validator("ended", mode="before")
    @classmethod
    def coerce_ended(cls, v: str | bool | None) -> bool:
        """Coerce ``ended`` to bool.

        :param v: Raw value.
        :returns: Boolean.
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"


class MBSeriesRelation(BaseModel):
    """A series relation on a release, indicating membership in a named release series.

    Important attributes: ``type`` (e.g. ``"part of"``), ``ordering_key`` (integer position within the series),
    ``series`` (:class:`MBSeries`).
    """

    type: str = ""
    direction: str = ""
    ordering_key: int = Field(default=0, alias="ordering-key")
    series: MBSeries = Field(default_factory=MBSeries)

    model_config = {"populate_by_name": True}

    @field_validator("ordering_key", mode="before")
    @classmethod
    def coerce_ordering_key(cls, v: str | int | None) -> int:
        """Coerce ``ordering-key`` to int.

        :param v: Raw value.
        :returns: Integer, defaulting to 0.
        """
        if v is None:
            return 0
        return int(v)


class MBCoverArtArchive(BaseModel):
    """Cover art availability summary from the Cover Art Archive, embedded in a release response.

    Useful for pre-checking whether CAA images exist before attempting to fetch them.

    Important attributes: ``artwork`` (any image present), ``front``, ``back``, ``count``, ``darkened`` (DMCA takedown).
    """

    artwork: bool = False
    front: bool = False
    back: bool = False
    count: int = 0
    darkened: bool = False

    model_config = {"populate_by_name": True}

    @field_validator("artwork", "front", "back", "darkened", mode="before")
    @classmethod
    def coerce_bool(cls, v: str | bool | None) -> bool:
        """Coerce string ``"true"``/``"false"`` from musicbrainzngs XML to ``bool``.

        :param v: Raw value.
        :returns: Boolean.
        """
        if v is None:  # pragma: no cover
            return False  # pragma: no cover
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"

    @field_validator("count", mode="before")
    @classmethod
    def coerce_count(cls, v: str | int | None) -> int:
        """Coerce ``count`` from string to int.

        :param v: Raw value.
        :returns: Integer count, defaulting to 0.
        """
        if v is None:
            return 0
        return int(v)


class MBReleaseEvent(BaseModel):
    """A single release event (date + country) from a release's ``release-event-list``.

    The ``country`` field is extracted from the nested ``area`` object's ``iso-3166-1-code-list`` via a
    ``model_validator`` so callers always see a plain string (e.g. ``"DE"``).

    Important attributes: ``date`` (ISO date string), ``country`` (ISO 3166-1 alpha-2 code).
    """

    date: str = ""
    country: str = ""

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def extract_country_from_area(cls, data: JSON) -> JSON:
        """Extract the ISO 3166-1 alpha-2 country code from the nested ``area`` dict.

        musicbrainzngs returns release events as ``{"date": "...", "area": {"name": "...",
        "iso-3166-1-code-list": ["DE"]}}``.  This validator flattens the country code into the
        top-level ``country`` key so the model's ``country`` field is populated directly.

        :param data: Raw event dict from musicbrainzngs.
        :returns: Normalised dict with ``country`` at the top level.
        """
        if not isinstance(data, dict):  # pragma: no cover
            return data  # pragma: no cover
        if "country" not in data or not data["country"]:
            area = data.get("area", {})
            if isinstance(area, dict):
                codes = area.get("iso-3166-1-code-list", [])
                if isinstance(codes, list) and codes:
                    data = dict(data)
                    data["country"] = codes[0]
        return data


class MBLifeSpan(BaseModel):
    """A begin/end life-span on a work or artist.

    Important attributes: ``begin`` (ISO date string), ``end`` (ISO date string), ``ended`` (boolean).
    """

    begin: str = ""
    end: str = ""
    ended: bool = False

    model_config = {"populate_by_name": True}


class MBWork(BaseModel):
    """A MusicBrainz work entity with all fields used by the annotator.

    Important attributes: ``id`` (MBID), ``title``, ``type`` (e.g. ``"Symphony"``), ``language``, ``iswc``,
    ``disambiguation``, ``annotation``, ``key``, ``artist_relation_list``, ``work_relation_list``,
    ``place_relation_list``, ``label_relation_list``, ``url_relation_list``, ``tag_list``, ``attribute_list``,
    ``life_span``, ``alias_list``.

    ``iswc`` is the International Standard Musical Work Code (ISO 15707), a persistent identifier for the musical work
    itself (distinct from any recording's ISRC).  ``place_relation_list`` enables CE-compatible premiered-date extraction
    from ``premiere`` relations.  ``label_relation_list`` enables CE-compatible published-date extraction from
    ``publishing`` label relations.  ``url_relation_list`` captures IMSLP, Wikidata, AllMusic, and other external URLs.
    ``annotation`` is the MB free-text annotation (often contains scholarly notes on composition dates, sources, etc.).
    """

    id: str = ""
    title: str = ""
    type: str = ""
    language: str = ""
    iswc: str = ""
    disambiguation: str = ""
    annotation: str = ""
    key: str = ""
    artist_relation_list: list[MBArtistRelation] = Field(default_factory=list, alias="artist-relation-list")
    work_relation_list: list[MBWorkRelation] = Field(default_factory=list, alias="work-relation-list")
    place_relation_list: list[MBPlaceRelation] = Field(default_factory=list, alias="place-relation-list")
    label_relation_list: list[MBLabelRelation] = Field(default_factory=list, alias="label-relation-list")
    url_relation_list: list[MBUrlRelation] = Field(default_factory=list, alias="url-relation-list")
    tag_list: list[MBTag] = Field(default_factory=list, alias="tag-list")
    attribute_list: list[MBAttribute | str] = Field(default_factory=list, alias="attribute-list")
    life_span: MBLifeSpan = Field(default_factory=MBLifeSpan, alias="life-span")
    alias_list: list[MBAlias] = Field(default_factory=list, alias="alias-list")

    model_config = {"populate_by_name": True}

    @field_validator("annotation", mode="before")
    @classmethod
    def coerce_annotation(cls, v: str | dict[str, str] | None) -> str:
        """Extract annotation text from the musicbrainzngs dict representation.

        Per the MMD 2.0 schema, the MB API returns ``<annotation><text>…</text></annotation>``
        which musicbrainzngs parses as ``{"text": "…"}`` (plus optional ``"entity"`` and ``"name"``
        keys).  This validator extracts the ``"text"`` value so callers always receive a plain string.

        :param v: Raw annotation value — either a dict from musicbrainzngs, a plain string, or ``None``.
        :returns: The annotation text string, or ``""`` when absent.
        """
        if v is None:
            return ""
        if isinstance(v, dict):
            return str(v.get("text", ""))
        return str(v)

    @field_validator("attribute_list", mode="before")
    @classmethod
    def coerce_attributes(cls, v: JSON) -> list[JSON]:
        """Normalise the ``attribute-list`` field from the MB API response.

        The MB API may return ``None``, a non-list scalar, or a proper list.  Dicts within the list are coerced to
        :class:`MBAttribute` by Pydantic's union validation; plain strings are kept as-is.

        :param v: Raw value from the MB API response for ``attribute-list``.
        :returns: The validated list, or ``[]`` when ``v`` is ``None`` or not a list.
        """
        if isinstance(v, list):
            return v
        return []


# MBWorkRelation.work is typed as MBWork (forward reference resolved here).
MBWorkRelation.model_rebuild()


class MBLabelInfo(BaseModel):
    """One entry in a release's ``label-info-list``.

    Important attributes: ``label`` (:class:`MBLabel`), ``catalog_number``.
    """

    label: MBLabel = Field(default_factory=MBLabel)
    catalog_number: str = Field(default="", alias="catalog-number")

    model_config = {"populate_by_name": True}


class MBReleaseGroup(BaseModel):
    """Release-group summary embedded in a release response.

    Important attributes: ``id`` (release-group MBID), ``primary_type`` (e.g. ``"Album"``),
    ``secondary_type_list`` (e.g. ``["Compilation"]``), ``first_release_date``, ``disambiguation``.
    """

    id: str = ""
    primary_type: str = Field(default="", alias="primary-type")
    first_release_date: str = Field(default="", alias="first-release-date")
    secondary_type_list: list[str] = Field(default_factory=list, alias="secondary-type-list")
    disambiguation: str = ""

    model_config = {"populate_by_name": True}


class MBTextRepresentation(BaseModel):
    """Script and language metadata on a release.

    Important attributes: ``script`` (e.g. ``"Latn"``), ``language`` (ISO 639-3, e.g. ``"deu"``).
    """

    script: str = ""
    language: str = ""

    model_config = {"populate_by_name": True}


class MBRecordingStub(BaseModel):
    """Minimal recording reference embedded in a track entry within a release.

    Important attributes: ``id`` (recording MBID), ``title``, ``artist_credit``,
    ``isrc_list`` (ISRC codes; populated only when the ``"isrcs"`` include is passed to the
    release fetch — defaults to ``[]`` so ISRC-match logic treats absent data as inconclusive).
    """

    id: str = ""
    title: str = ""
    artist_credit: list[MBArtistCredit | str] = Field(default_factory=list, alias="artist-credit")
    isrc_list: list[str] = Field(default_factory=list, alias="isrc-list")

    model_config = {"populate_by_name": True}


class MBTrack(BaseModel):
    """One track entry within a medium's ``track-list``.

    Important attributes: ``id`` (track MBID), ``position`` (1-based integer), ``number`` (physical track label,
    e.g. ``"A1"`` for vinyl side A track 1, ``"1"`` for CD — use this for ``TRACKNUMBER`` on non-CD formats),
    ``length`` (track-specific duration in milliseconds — may differ from ``recording.length`` for partial performances),
    ``recording`` (:class:`MBRecordingStub`).
    """

    id: str = ""
    position: int = 0
    number: str = ""
    length: int = 0

    recording: MBRecordingStub = Field(default_factory=MBRecordingStub)

    model_config = {"populate_by_name": True}

    @field_validator("length", mode="before")
    @classmethod
    def coerce_length(cls, v: str | int | None) -> int:
        """Coerce ``length`` from string/None to int milliseconds.

        :param v: Raw value from the API response.
        :returns: Integer milliseconds, defaulting to 0.
        """
        if v is None:
            return 0
        return int(v)


class MBDisc(BaseModel):
    """A single disc ID entry attached to a medium, as returned by ``musicbrainzngs`` when ``includes=["discids"]``.

    Each entry records the physical CD's table-of-contents.  A medium may have more than one disc entry when
    multiple pressings of the same disc have slightly different TOC data (e.g. different lead-in offsets).

    ``musicbrainzngs`` returns per-track frame start positions under the key ``"offset-list"`` and the lead-out
    sector address as a string under ``"sectors"``.

    Important attributes: ``offsets`` (per-track CD frame start positions, matching the ``disc_id`` offsets in
    ``00 - disc info.yaml``), ``sectors`` (lead-out frame address).
    """

    offsets: list[int] = Field(default_factory=list, alias="offset-list")
    sectors: int = 0

    model_config = {"populate_by_name": True}

    @field_validator("sectors", mode="before")
    @classmethod
    def coerce_sectors(cls, v: str | int | None) -> int:
        """Coerce ``sectors`` from string/None to int.

        ``musicbrainzngs`` returns this field as a string from the XML response.

        :param v: Raw value from the API response.
        :returns: Integer sector count, defaulting to 0.
        """
        if v is None:
            return 0
        return int(v)


class MBMedium(BaseModel):
    """One disc (medium) in a release.

    Important attributes: ``position`` (1-based disc number), ``format`` (e.g. ``"CD"``, ``"Vinyl"``,
    ``"Digital Media"``), ``title`` (disc-specific subtitle, e.g. ``"Act I"`` or ``"Disc 1: Symphonies 1 & 2"``
    — maps to ``DISCSUBTITLE``), ``track_list``, ``disc_list`` (TOC entries populated when the release is fetched
    with ``includes=["discids"]``).

    ``musicbrainzngs`` returns disc entries under the key ``"disc-list"``.
    """

    position: int = 1
    format: str = ""
    title: str = ""
    track_list: list[MBTrack] = Field(default_factory=list, alias="track-list")
    disc_list: list[MBDisc] = Field(default_factory=list, alias="disc-list")

    model_config = {"populate_by_name": True}


class MBRelease(BaseModel):
    """Top-level release entity as returned by ``musicbrainzngs.get_release_by_id``.

    Important attributes: ``id`` (release MBID), ``title``, ``date``, ``status``, ``barcode``, ``country``,
    ``packaging`` (e.g. ``"Jewel Case"``), ``disambiguation``, ``asin`` (Amazon ASIN), ``artist_credit``,
    ``release_group``, ``label_info_list``, ``medium_list``, ``text_representation``,
    ``cover_art_archive`` (:class:`MBCoverArtArchive`), ``release_event_list`` (all release date/country events),
    ``url_relation_list`` (Discogs, Amazon, etc.), ``series_relation_list`` (box-set series membership).
    """

    id: str = ""
    title: str = ""
    date: str = ""
    status: str = ""
    barcode: str = ""
    country: str = ""
    packaging: str = ""
    disambiguation: str = ""
    asin: str = ""
    artist_credit: list[MBArtistCredit | str] = Field(default_factory=list, alias="artist-credit")
    release_group: MBReleaseGroup = Field(default_factory=MBReleaseGroup, alias="release-group")
    label_info_list: list[MBLabelInfo] = Field(default_factory=list, alias="label-info-list")
    medium_list: list[MBMedium] = Field(default_factory=list, alias="medium-list")
    text_representation: MBTextRepresentation = Field(default_factory=MBTextRepresentation, alias="text-representation")
    cover_art_archive: MBCoverArtArchive = Field(default_factory=MBCoverArtArchive, alias="cover-art-archive")
    release_event_list: list[MBReleaseEvent] = Field(default_factory=list, alias="release-event-list")
    url_relation_list: list[MBUrlRelation] = Field(default_factory=list, alias="url-relation-list")
    series_relation_list: list[MBSeriesRelation] = Field(default_factory=list, alias="series-relation-list")

    model_config = {"populate_by_name": True}


class MBRecording(BaseModel):
    """Recording entity with artist and work relationships, as returned by ``musicbrainzngs.get_recording_by_id``.

    Important attributes: ``id`` (recording MBID), ``title``, ``first_release_date``, ``disambiguation``,
    ``video`` (``True`` when this is a video recording rather than audio-only), ``length`` (duration in
    milliseconds), ``isrc_list`` (list of ISRC codes for this recording), ``artist_credit``,
    ``artist_relation_list``, ``work_relation_list``.

    ``first_release_date`` is the year (or full date) this specific audio was first commercially released.
    It is populated by the ``_patched_parse_recording`` workaround in ``_mb_api.py``, which recovers
    the ``first-release-date`` field that the musicbrainzngs XML parser currently discards.  It is distinct
    from ``release_group.first_release_date`` (album publication year) — for reissues of older recordings it
    will be earlier.

    ``isrc_list`` is only populated when the ``"isrcs"`` include is passed to ``get_recording_by_id``.  ISRCs
    are not available via the release-level ``"isrcs"`` include for embedded recordings.
    """

    id: str = ""
    title: str = ""
    first_release_date: str = Field(default="", alias="first-release-date")
    disambiguation: str = ""
    video: bool = False
    length: int = 0
    isrc_list: list[str] = Field(default_factory=list, alias="isrc-list")
    artist_credit: list[MBArtistCredit | str] = Field(default_factory=list, alias="artist-credit")
    artist_relation_list: list[MBArtistRelation] = Field(default_factory=list, alias="artist-relation-list")
    work_relation_list: list[MBWorkRelation] = Field(default_factory=list, alias="work-relation-list")

    model_config = {"populate_by_name": True}

    @field_validator("video", mode="before")
    @classmethod
    def coerce_video(cls, v: str | bool | None) -> bool:
        """Coerce the ``video`` flag from string/bool/None to a plain ``bool``.

        :param v: Raw value from the API response.
        :returns: Boolean.
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        return str(v).lower() == "true"

    @field_validator("length", mode="before")
    @classmethod
    def coerce_length(cls, v: str | int | None) -> int:
        """Coerce ``length`` from string/None to int milliseconds.

        :param v: Raw value from the API response.
        :returns: Integer milliseconds, defaulting to 0.
        """
        if v is None:
            return 0
        return int(v)


# ---------------------------------------------------------------------------
# Classical Extras / annotator internal models
# ---------------------------------------------------------------------------


class ArtistEntry(BaseModel):
    """A resolved artist with display name, sort name, MBID, and optional instrument label.

    Used as the element type in all :class:`RoleBuckets` and :class:`CeaPerformers` lists.

    Important attributes: ``name``, ``sort``, ``mbid``, ``instrument`` (empty string when the artist has no instrument role).
    """

    name: str
    sort: str
    mbid: str = ""
    instrument: str = ""


class RoleBuckets(BaseModel):
    """Collected artist roles extracted from a work's ``artist-relation-list``.

    Each list holds deduplicated :class:`ArtistEntry` objects for that role.  Deduplication is by MBID so the same person
    credited at multiple levels of a work hierarchy (movement → symphonic poem → collection) appears only once.

    CE distinguishes ``writers`` (the MB ``"writer"`` relation type, a generic creative attribution) from ``composers``
    (the MB ``"composer"`` relation type).  Both populate the standard ``COMPOSER`` host tag, but CE exposes them separately
    as ``CWP_WRITERS`` / ``CWP_COMPOSERS`` so library software can display the distinction.

    Beyond CE, music-annotator further separates ``composers`` (plain ``"composer"`` relation, no attributes) from
    ``additional_composers`` (``"composer"`` relation carrying the ``"additional"`` or ``"assistant"`` MB attribute).
    This distinction is not defined by CE but allows the primary composer to be identified unambiguously when MB marks
    a completion or ghost-writer contribution as subsidiary (e.g. Süssmayr on the Mozart Requiem).

    Important attributes: ``composers``, ``additional_composers``, ``writers``, ``lyricists``, ``librettists``,
    ``translators``, ``arrangers``, ``orchestrators``, ``reconstructors``, ``revisors``.
    """

    composers: list[ArtistEntry] = Field(default_factory=list)
    additional_composers: list[ArtistEntry] = Field(default_factory=list)
    writers: list[ArtistEntry] = Field(default_factory=list)
    lyricists: list[ArtistEntry] = Field(default_factory=list)
    librettists: list[ArtistEntry] = Field(default_factory=list)
    translators: list[ArtistEntry] = Field(default_factory=list)
    arrangers: list[ArtistEntry] = Field(default_factory=list)
    orchestrators: list[ArtistEntry] = Field(default_factory=list)
    reconstructors: list[ArtistEntry] = Field(default_factory=list)
    revisors: list[ArtistEntry] = Field(default_factory=list)
    dedicatees: list[ArtistEntry] = Field(default_factory=list)
    choreographers: list[ArtistEntry] = Field(default_factory=list)

    def seen_ids(self, role: str) -> set[str]:
        """Return the set of MBIDs already present in the named role list.

        :param role: The role name, which must be a field name on this model (e.g. ``"composers"``).
        :returns: A set of non-empty MBID strings for all entries currently in the role list.
        :raises AttributeError: If ``role`` does not name a field on this model.
        """
        return {e.mbid for e in getattr(self, role) if e.mbid}

    def add_unique(self, role: str, entry: ArtistEntry) -> None:
        """Append ``entry`` to the named role list only if its MBID is not yet present.

        If ``entry.mbid`` is empty the entry is always appended because deduplication by MBID is not possible.

        :param role: The role name, which must be a field name on this model.
        :param entry: The :class:`ArtistEntry` to potentially append.
        :raises AttributeError: If ``role`` does not name a field on this model.
        """
        bucket: list[ArtistEntry] = getattr(self, role)
        if entry.mbid and entry.mbid in self.seen_ids(role):
            return
        bucket.append(entry)


class CeaPerformers(BaseModel):
    """CE ``_cea_*`` performer classifications extracted from recording-level artist relations.

    Important attributes: ``conductors``, ``chorusmasters``, ``leaders``, ``arrangers``, ``orchestrators``, ``composers``,
    ``producers``, ``engineers``, ``ensembles``, ``vocalists``, ``instrumentalists``, ``other_soloists``.
    """

    conductors: list[ArtistEntry] = Field(default_factory=list)
    chorusmasters: list[ArtistEntry] = Field(default_factory=list)
    leaders: list[ArtistEntry] = Field(default_factory=list)
    arrangers: list[ArtistEntry] = Field(default_factory=list)
    orchestrators: list[ArtistEntry] = Field(default_factory=list)
    composers: list[ArtistEntry] = Field(default_factory=list)
    producers: list[ArtistEntry] = Field(default_factory=list)
    engineers: list[ArtistEntry] = Field(default_factory=list)
    ensembles: list[ArtistEntry] = Field(default_factory=list)
    vocalists: list[ArtistEntry] = Field(default_factory=list)
    instrumentalists: list[ArtistEntry] = Field(default_factory=list)
    other_soloists: list[ArtistEntry] = Field(default_factory=list)

    @property
    def all_soloists(self) -> list[ArtistEntry]:
        """Return all non-ensemble, non-conductor performing artists.

        :returns: Concatenated list of ``vocalists``, ``instrumentalists``, and ``other_soloists``.
        """
        return self.vocalists + self.instrumentalists + self.other_soloists


class WorkDates(BaseModel):
    """Composed, published, and premiered dates extracted from work attributes.

    Important attributes: ``composed``, ``published``, ``premiered`` — all strings, defaulting to ``""``.
    """

    composed: str = ""
    published: str = ""
    premiered: str = ""


class WorkHierarchyLevel(BaseModel):
    """One level in a Classical Extras work hierarchy.

    Level 0 is the recording's direct (bottom) work; higher indices are parent works toward the root.  These map to the
    ``cwp_work_0`` … ``cwp_work_N`` tag convention.

    ``ordering_key`` is the MB ``ordering-key`` integer from the ``parts/backward`` relation connecting this level to its
    parent.  It gives the position of this work among its siblings (e.g. Act I = 1, Act II = 2) and is used as the ``nn``
    prefix on intermediate directories and leaf filenames in :func:`~music_annotator._tags.build_dest_path`.  It is ``0``
    when MB has not populated the field; callers fall back to 1-based ordinal position in that case.

    Important attributes: ``index``, ``work_id``, ``work_title``, ``part_title`` (stripped movement/part name for
    ``cwp_part_N``), ``ordering_key``.
    """

    index: int
    work_id: str
    work_title: str
    part_title: str = ""  # stripped movement/part name (cwp_part_N)
    ordering_key: int = 0  # MB ordering-key from the parts/backward relation to this level's parent
    work_en: str = ""  # English "Work name" alias for this level (cwp_work_N_en)
    work_alt: str = ""  # semicolon-joined unlocaled aliases for this level (cwp_work_N_alt)


class CwpTags(BaseModel):
    """All Classical Extras ``_cwp_*`` tag values for one track.

    This model is constructed after the full work hierarchy is resolved and movement numbers are computed across the release.

    ``work_part_levels`` mirrors the CE ``_cwp_work_part_levels`` variable: the maximum hierarchy depth among all tracks
    sharing the same top-level work on this release (equivalently: the depth of the top-level work's subtree).  In
    music-annotator, which processes one medium at a time, this equals ``part_levels`` because no cross-disc state is
    accumulated; it is stored as a separate field so the tag is explicitly present and matches CE output.

    Important attributes: ``work_top``, ``workid_top``, ``work_top_en``, ``work_top_alt``, ``part_levels``,
    ``work_part_levels``, ``part``, ``work``, ``groupheading``, ``inter_work``, ``movt_num``, ``movt_tot``,
    ``single_work_album``, ``levels`` (per-level :class:`WorkHierarchyLevel` list), plus all artist role
    and date string fields.
    """

    work_top: str = ""
    workid_top: str = ""
    work_top_en: str = ""  # English "Work name" alias for the root work (CWP_WORK_TOP_EN)
    work_top_alt: str = ""  # semicolon-joined unlocaled aliases for the root work (CWP_WORK_TOP_ALT)
    part_levels: int = 0
    work_part_levels: int = 0
    part: str = ""
    work: str = ""
    groupheading: str = ""
    inter_work: str = ""
    movt_num: int = 0
    movt_tot: int = 0
    single_work_album: bool = False

    # Per-level arrays (index = hierarchy depth)
    levels: list[WorkHierarchyLevel] = Field(default_factory=list)

    # Work-level artist roles
    composers: str = ""
    composers_sort: str = ""
    composer_lastnames: str = ""
    writers: str = ""
    writers_sort: str = ""
    arrangers: str = ""
    arrangers_sort: str = ""
    arranger_names: str = ""
    orchestrators: str = ""
    orchestrators_sort: str = ""
    reconstructors: str = ""
    reconstructors_sort: str = ""
    revisors: str = ""
    revisors_sort: str = ""
    lyricists: str = ""
    lyricists_sort: str = ""
    librettists: str = ""
    librettists_sort: str = ""
    translators: str = ""
    translators_sort: str = ""

    # Dates and key from the bottom work
    keys: str = ""
    composed_dates: str = ""
    published_dates: str = ""
    premiered_dates: str = ""
    worktype_genres: str = ""
    worktype_genres_top: str = ""
    period: str = ""


class TrackTags(BaseModel):
    """The complete flat tag mapping written to one audio file.

    All values are strings.  Fields whose names appear in the ``excluded`` set inside :meth:`to_file_dict` are internal
    helpers and are not written to disk.  Per-level ``cwp_work_N`` / ``cwp_workid_N`` / ``cwp_part_N`` tags are stored as
    Pydantic ``extra`` fields via ``model_config = {"extra": "allow"}``.

    Important attributes: all standard Picard tag fields, MusicBrainz ID fields, ``_cea_*`` fields, ``_cwp_*`` fields,
    and the internal ``cea_conductors_list`` / ``cea_ensembles_list`` /
    ``cea_album_conductors_list`` / ``cea_album_ensembles_list`` lists used for path building.
    """

    # Internal (not written to file)
    cea_conductors_list: list[ArtistEntry] = Field(default_factory=list)
    cea_ensembles_list: list[ArtistEntry] = Field(default_factory=list)
    # Album-artist-filtered subsets of the above: conductors/ensembles credited at release level.
    # Used by build_dest_path to produce a stable top-level directory that does not fork when MB
    # credits performers inconsistently across movements.  Not written to audio files.
    cea_album_conductors_list: list[ArtistEntry] = Field(default_factory=list)
    cea_album_ensembles_list: list[ArtistEntry] = Field(default_factory=list)

    # Standard Picard tags
    title: str = ""
    artist: str = ""
    artists: str = ""
    artistsort: str = ""
    albumartist: str = ""
    albumartistsort: str = ""
    album: str = ""
    tracknumber: str = ""
    totaltracks: str = ""
    discnumber: str = ""
    date: str = ""
    originaldate: str = ""
    recording_first_release_date: str = ""  # recording.first-release-date: year this audio was first released
    media: str = "CD"
    script: str = ""
    language: str = ""
    releasetype: str = ""
    releasestatus: str = ""

    # Standard Picard fields previously missing
    isrc: str = ""  # ISRC — International Standard Recording Code
    length: str = ""  # LENGTH / TLEN — track duration in milliseconds
    discsubtitle: str = ""  # DISCSUBTITLE / TSST — medium title (e.g. "Act I")
    releasecountry: str = ""  # RELEASECOUNTRY — ISO 3166-1 alpha-2 country of first release
    totaldiscs: str = ""  # TOTALDISCS — total number of discs in the release
    releasetype_secondary: str = ""  # secondary release types (e.g. "Compilation"), semicolon-joined

    # Label / catalogue
    organization: str = ""
    label: str = ""
    label_code: str = ""  # LABEL_CODE — label code (e.g. "173" for Deutsche Grammophon)
    catalognumber: str = ""
    barcode: str = ""
    asin: str = ""  # ASIN — Amazon Standard Identification Number
    packaging: str = ""  # PACKAGING — release packaging type (e.g. "Jewel Case")
    musicbrainz_labelid: str = ""  # label MBID

    # Disambiguation / comments
    comment: str = ""  # COMMENT — recording disambiguation comment
    releasedisambiguation: str = ""  # RELEASEDISAMBIGUATION — release disambiguation comment

    # Recording session date
    recording_date: str = ""  # RECORDING_DATE — session date from artist relation begin dates
    recording_date_work: str = ""  # internal path-construction helper: union of all movements'
    # session dates for the top work; not written to audio files

    # Performer credited-as companion
    cea_performers_credited: str = ""  # credited names for performers where they differ from canonical

    # External links from work URL relations
    work_imslp_url: str = ""  # CWP_WORK_IMSLP_URL — IMSLP link for the work
    work_wikidata_url: str = ""  # CWP_WORK_WIKIDATA_URL — Wikidata link for the work

    # Work identifiers
    iswc: str = ""  # ISWC — International Standard Musical Work Code
    work_disambiguation: str = ""  # CWP_WORK_DISAMBIGUATION
    work_annotation: str = ""  # CWP_WORK_ANNOTATION

    # Release series membership
    musicbrainz_series: str = ""  # MUSICBRAINZ_SERIES — release series name(s)

    # Cover art availability (informational)
    caa_front: str = ""  # CAA_FRONT — "1" if front image available in CAA
    caa_back: str = ""  # CAA_BACK — "1" if back image available in CAA

    # Cover art sidecar file references (relative filenames in the work top directory).
    # These reference the sidecar files written alongside the tracks; no player currently
    # reads them but they make the sidecar inventory machine-readable for future tooling.
    coverart_front_file: str = ""  # COVERART_FRONT_FILE
    coverart_back_file: str = ""  # COVERART_BACK_FILE
    coverart_booklet_files: str = ""  # COVERART_BOOKLET_FILES
    coverart_medium_files: str = ""  # COVERART_MEDIUM_FILES
    coverart_tray_files: str = ""  # COVERART_TRAY_FILES
    coverart_obi_files: str = ""  # COVERART_OBI_FILES
    coverart_spine_files: str = ""  # COVERART_SPINE_FILES
    coverart_track_files: str = ""  # COVERART_TRACK_FILES
    coverart_liner_files: str = ""  # COVERART_LINER_FILES
    coverart_sticker_files: str = ""  # COVERART_STICKER_FILES
    coverart_poster_files: str = ""  # COVERART_POSTER_FILES
    coverart_matrix_files: str = ""  # COVERART_MATRIX_FILES
    coverart_top_files: str = ""  # COVERART_TOP_FILES
    coverart_bottom_files: str = ""  # COVERART_BOTTOM_FILES
    coverart_panel_files: str = ""  # COVERART_PANEL_FILES
    coverart_watermark_files: str = ""  # COVERART_WATERMARK_FILES
    coverart_raw_files: str = ""  # COVERART_RAW_FILES
    coverart_other_files: str = ""  # COVERART_OTHER_FILES
    coverart_unknown_files: str = ""  # COVERART_UNKNOWN_FILES

    # Work / movement
    work: str = ""
    groupheading: str = ""
    top_work: str = ""
    part: str = ""
    movement: str = ""
    subtitle: str = ""
    movementnumber: str = ""
    movementtotal: str = ""

    # Composer / conductor / performers
    composer: str = ""
    composersort: str = ""
    conductor: str = ""
    lyricist: str = ""
    translator: str = ""
    arranger: str = ""
    chorusmaster: str = ""
    leader: str = ""

    # Performer lists
    soloists: str = ""
    ensemble: str = ""
    band: str = ""
    vocalists: str = ""
    instrumentalists: str = ""
    instrument: str = ""

    # Genre / period / key
    genre: str = "Classical"
    period: str = ""
    key: str = ""
    # Default "1" follows CE convention for classical-only fields; build_track_tags overrides this
    # explicitly from _top_level_class (STYLEGUIDE 4.7/REND-21) so the persisted value reflects the
    # actual library class rather than the model default.
    is_classical: str = "1"

    # Work dates
    work_year: str = ""
    composed_date: str = ""
    published_date: str = ""
    premiered_date: str = ""

    # Production credits
    producer: str = ""
    engineer: str = ""

    # MusicBrainz IDs
    musicbrainz_albumid: str = ""
    musicbrainz_trackid: str = ""
    musicbrainz_recordingid: str = ""
    musicbrainz_releasegroupid: str = ""
    musicbrainz_albumartistid: str = ""
    musicbrainz_artistid: str = ""
    musicbrainz_workid: str = ""
    musicbrainz_conductorid: str = ""
    musicbrainz_composerid: str = ""
    musicbrainz_releasetrackid: str = ""

    # CEA tags
    cea_recording_artist: str = ""
    cea_recording_artists: str = ""
    cea_recording_artists_sort: str = ""
    cea_mb_artists: str = ""
    cea_soloists: str = ""
    cea_soloist_names: str = ""
    cea_soloists_sort: str = ""
    cea_vocalists: str = ""
    cea_vocalist_names: str = ""
    cea_instrumentalists: str = ""
    cea_instrumentalist_names: str = ""
    cea_other_soloists: str = ""
    cea_ensembles: str = ""
    cea_ensemble_names: str = ""
    cea_ensembles_sort: str = ""
    cea_album_soloists: str = ""
    cea_album_soloists_sort: str = ""
    cea_album_conductors: str = ""
    cea_album_conductors_sort: str = ""
    cea_album_ensembles: str = ""
    cea_album_ensembles_sort: str = ""
    cea_album_composers: str = ""
    cea_album_composers_sort: str = ""
    cea_support_performers: str = ""
    cea_support_performers_sort: str = ""
    cea_conductors: str = ""
    cea_composers: str = ""
    cea_composer_lastnames: str = ""
    cea_performers: str = ""
    cea_arrangers: str = ""
    cea_orchestrators: str = ""
    cea_chorusmasters: str = ""
    cea_leaders: str = ""
    cea_instruments: str = ""
    cea_instruments_all: str = ""

    # CWP tags
    cwp_work_top: str = ""
    cwp_workid_top: str = ""
    cwp_work_top_en: str = ""  # English "Work name" alias for the root work
    cwp_work_top_alt: str = ""  # semicolon-joined unlocaled aliases for the root work
    cwp_part_levels: str = "0"
    cwp_work_part_levels: str = "0"
    cwp_part: str = ""
    cwp_work: str = ""
    cwp_groupheading: str = ""
    cwp_inter_work: str = ""
    cwp_movt_num: str = ""
    cwp_movt_tot: str = ""
    cwp_single_work_album: str = "0"
    cwp_composers: str = ""
    cwp_composers_sort: str = ""
    cwp_composer_lastnames: str = ""
    cwp_writers: str = ""
    cwp_writers_sort: str = ""
    cwp_arrangers: str = ""
    cwp_arrangers_sort: str = ""
    cwp_arranger_names: str = ""
    cwp_orchestrators: str = ""
    cwp_orchestrators_sort: str = ""
    cwp_reconstructors: str = ""
    cwp_reconstructors_sort: str = ""
    cwp_revisors: str = ""
    cwp_revisors_sort: str = ""
    cwp_lyricists: str = ""
    cwp_lyricists_sort: str = ""
    cwp_librettists: str = ""
    cwp_librettists_sort: str = ""
    cwp_translators: str = ""
    cwp_translators_sort: str = ""
    cwp_keys: str = ""
    cwp_composed_dates: str = ""
    cwp_published_dates: str = ""
    cwp_premiered_dates: str = ""
    cwp_worktype_genres: str = ""
    cwp_worktype_genres_top: str = ""
    acoustid_id: str = ""
    # --- archival identity (extensible: 4th dim slots in here) ---
    audio_hash: str = ""  # algorithm-tagged decoded-audio hash; format "<algo>:<hexdigest>"
    acoustid_fingerprint: str = Field(default="", alias="chromaprint_fp")
    """Raw Chromaprint fingerprint string stored under the Picard-aligned key ``ACOUSTID_FINGERPRINT``
    (FLAC Vorbis ``acoustid_fingerprint``; MP3 TXXX desc ``"Acoustid Fingerprint"``), renamed from the
    legacy ``CHROMAPRINT_FP``.

    **Dual-read transition:** the forward write path emits only ``ACOUSTID_FINGERPRINT``; every
    read-back helper reads both the new key and the legacy ``CHROMAPRINT_FP``, so a mixed library
    (files not yet migrated to the new key) reads correctly throughout the transition.

    **Value-source rule for** ``ACOUSTID_ID``: the companion ``acoustid_id`` field holds the AcoustID
    cluster UUID from the fingerprint ``/v2/lookup`` endpoint — Picard's ``acoustid_id`` source.  When
    no api_key is supplied or fpcalc yields no fingerprint, ``acoustid_id`` is left empty at ingest;
    it is never re-filled from the ``list_by_mbid`` endpoint (the empty-not-fallback rule).
    """
    # AccurateRip per-track flat fields (C-AR).  Field names == lowercased FLAC/MP3 tag keys.
    # Tag keys are ACCURATERIP_V1_RESULT etc. (uppercase); desc == key in _MP3_TXXX_MAP.
    # confidence serializes as decimal string, empty string when 0/absent (not "0").
    accuraterip_v1_result: str = ""
    accuraterip_v1_confidence: str = ""
    accuraterip_v1_local_crc: str = ""
    accuraterip_v1_remote_crc: str = ""
    accuraterip_v2_result: str = ""
    accuraterip_v2_confidence: str = ""
    accuraterip_v2_local_crc: str = ""
    accuraterip_v2_remote_crc: str = ""
    accuraterip_test_crc: str = ""
    accuraterip_copy_crc: str = ""
    accuraterip_status: str = ""
    # Internal flag: set to "1" when cwp_composers / cwp_composer_lastnames were populated from
    # the additional_composers fallback (i.e. no plain primary composer relation was found).  Used
    # by the cross-track composer unification pass in _pipeline.py to identify movements whose
    # composer credit came from the fallback path so the primary can be propagated to them.
    # Never written to audio files (excluded in to_file_dict).
    cwp_composers_is_fallback: str = ""

    # Per-level work/workid/part tags are stored as extra fields
    model_config = {"extra": "allow", "populate_by_name": True}

    @property
    def chromaprint_fp(self) -> str:
        """Transition read/write bridge for the legacy field name.

        Returns :attr:`acoustid_fingerprint`.  Removed when all consuming files are updated to the
        canonical name.
        """
        return self.acoustid_fingerprint

    @chromaprint_fp.setter
    def chromaprint_fp(self, value: str) -> None:
        """Transition write bridge for the legacy field name.

        Assigns ``value`` to :attr:`acoustid_fingerprint`.  Removed when all consuming files are
        updated to the canonical name.

        :param value: The fingerprint string to store.
        """
        self.acoustid_fingerprint = value

    def to_file_dict(self) -> dict[str, str]:
        """Return a ``{tag_name: value}`` mapping suitable for writing to an audio file.

        Internal fields (``cea_conductors_list``, ``cea_ensembles_list``,
        ``cea_album_conductors_list``, ``cea_album_ensembles_list``, ``recording_date_work``,
        ``cwp_composers_is_fallback``) are excluded.
        Empty values are excluded.  All
        keys are uppercased to match Vorbis Comment / ID3 conventions.  Dynamically-added per-level
        ``cwp_work_N`` / ``cwp_workid_N`` / ``cwp_part_N`` extra fields are included.

        :returns: A flat ``dict[str, str]`` of non-empty tag key/value pairs with uppercase keys, excluding internal list
            fields.
        """
        excluded = {
            "cea_conductors_list",
            "cea_ensembles_list",
            "cea_album_conductors_list",
            "cea_album_ensembles_list",
            "recording_date_work",
            "cwp_composers_is_fallback",
        }
        out: dict[str, str] = {}
        for field_name, value in self.model_dump(exclude=excluded).items():
            if isinstance(value, str) and value:
                out[field_name.upper()] = value
        # Also include dynamically-added per-level fields
        for key, value in (self.model_extra or {}).items():
            if isinstance(value, str) and value:
                out[key.upper()] = value
        return out


class MBReleaseCandidate(BaseModel):
    """A single result from a MusicBrainz release search, enriched with a human-readable summary.

    This model is produced by :func:`~music_annotator.search_releases_by_dir` and consumed by
    :func:`~music_annotator.discover` to display ranked candidates to the user for confirmation.

    Important attributes:
        ``release_id`` (MBID of the release), ``score`` (MB relevance score 0–100),
        ``title`` (release title), ``artist`` (credit phrase), ``date`` (release date),
        ``format`` (medium format, e.g. ``"CD"``), ``tracks`` (total track count),
        ``label`` (label name), ``catalog_number``, ``country``, ``status``,
        ``mb_url`` (canonical MusicBrainz URL for the release),
        ``from_journal`` (``True`` when this candidate was confirmed by a prior journal entry rather
        than from the MB search results alone; drives a compact display in
        :func:`~music_annotator._discover._format_candidate`).
    """

    release_id: str = ""
    score: int = 0
    title: str = ""
    artist: str = ""
    date: str = ""
    format: str = ""
    tracks: int = 0
    label: str = ""
    catalog_number: str = ""
    country: str = ""
    status: str = ""
    mb_url: str = ""
    from_journal: bool = False


class CoverImage(BaseModel):
    """A single cover art image with its raw bytes, MIME type, sidecar filename, and source URL.

    Used as the element type for the multi-image lists on :class:`CoverArt`.

    ``filename`` is the suggested on-disk sidecar filename (e.g. ``"booklet-1.pdf"``), set by
    :func:`~music_annotator._mb_api.fetch_cover_art` for images that are written as sidecar files
    rather than embedded in audio tracks.  It is empty for the 500px front images stored in
    ``CoverArt.front`` (those are embedded, not written to disk as sidecars).

    ``url`` is the canonical Cover Art Archive URL for this image (e.g.
    ``"https://coverartarchive.org/release/{id}/{coverid}.jpg"``).  Stored in the transaction
    journal as the ``source`` of ``action="downloaded"`` sidecar entries so that any sidecar
    can be re-downloaded from the journal alone.

    Important attributes: ``data``, ``mime``, ``filename``, ``url``.
    """

    data: bytes = b""
    mime: str = ""
    filename: str = ""  # suggested sidecar filename; empty for 500px embedded images
    url: str = ""  # canonical CAA URL for journal provenance

    model_config = {"arbitrary_types_allowed": True}


class CoverArt(BaseModel):
    """All cover art images fetched from the Cover Art Archive for a release.

    Cover art is split by purpose:

    - ``front`` — 500px JPEG front cover image(s), embedded in every audio track's PICTURE block.
    - ``front_full`` — original-resolution front cover image(s), written as ``cover.jpg`` sidecar.
    - ``back`` — back/spine image(s), written as ``back.jpg`` / ``back.pdf`` sidecar only.
    - ``booklet`` — booklet / liner-notes page(s), written as ``booklet-N.jpg`` / ``booklet-N.pdf`` sidecars.
      JPEG/PNG pages that fit within the 16 MB FLAC block limit could be embedded, but the current scheme
      writes all booklet/back/medium images exclusively as sidecar files for simplicity.
    - ``medium`` — disc-label image(s), written as ``medium-N.jpg`` sidecar only.

    Only ``front`` is embedded in audio files.  All other images are sidecar files written once into the
    work top directory.  Their filenames and CAA source URLs are stored on the :class:`CoverImage` instances
    so that :func:`~music_annotator._pipeline.run` can write them and add ``action="downloaded"`` journal
    entries with the correct provenance.

    The legacy ``data`` / ``mime`` shortcut properties expose the first front-cover image for backward
    compatibility with code that previously used ``CoverArt.data`` directly.

    Important attributes: ``front``, ``front_full``, ``back``, ``booklet``, ``medium``,
    ``tray``, ``obi``, ``spine``, ``track``, ``liner``, ``sticker``, ``poster``, ``matrix``,
    ``top``, ``bottom``, ``panel``, ``watermark``, ``raw``, ``other``, ``unknown``.
    """

    front: list[CoverImage] = []  # 500px for embedding
    front_full: list[CoverImage] = []  # original resolution for sidecar
    back: list[CoverImage] = []
    booklet: list[CoverImage] = []
    medium: list[CoverImage] = []
    tray: list[CoverImage] = []  # image behind/on the tray
    obi: list[CoverImage] = []  # obi strip (common in Japanese releases)
    spine: list[CoverImage] = []  # edge/spine of packaging
    track: list[CoverImage] = []  # per-track art (digital releases)
    liner: list[CoverImage] = []  # protective sleeve
    sticker: list[CoverImage] = []  # adhesive sticker on packaging
    poster: list[CoverImage] = []  # poster included with release
    matrix: list[CoverImage] = []  # matrix/runout area
    top: list[CoverImage] = []  # top face of box packaging
    bottom: list[CoverImage] = []  # bottom face of box packaging
    panel: list[CoverImage] = []  # panel of folded packaging
    watermark: list[CoverImage] = []  # scan with watermark added by scanner
    raw: list[CoverImage] = []  # raw/unedited scan
    other: list[CoverImage] = []  # any type not covered above
    unknown: list[CoverImage] = []  # type string not recognised by music-annotator

    @property
    def available(self) -> bool:
        """Return ``True`` if at least one image of any type is present.

        :returns: ``True`` when any of the image lists is non-empty.
        """
        return bool(
            self.front
            or self.front_full
            or self.back
            or self.booklet
            or self.medium
            or self.tray
            or self.obi
            or self.spine
            or self.track
            or self.liner
            or self.sticker
            or self.poster
            or self.matrix
            or self.top
            or self.bottom
            or self.panel
            or self.watermark
            or self.raw
            or self.other
            or self.unknown
        )

    @property
    def data(self) -> bytes:
        """Raw bytes of the first front-cover image, or ``b""`` if none is present.

        Provided for backward compatibility with call sites that previously used ``cover.data`` directly.

        :returns: ``front[0].data`` when ``front`` is non-empty, else ``b""``.
        """
        return self.front[0].data if self.front else b""

    @property
    def mime(self) -> str:
        """MIME type of the first front-cover image, or ``""`` if none is present.

        Provided for backward compatibility with call sites that previously used ``cover.mime`` directly.

        :returns: ``front[0].mime`` when ``front`` is non-empty, else ``""``.
        """
        return self.front[0].mime if self.front else ""

    model_config = {"arbitrary_types_allowed": True}


class PeriodEntry(BaseModel):
    """One entry in the Classical Extras period map, associating a period name with a year range.

    Used in :data:`~music_annotator._works.PERIOD_MAP` which maps composition years to CE period
    names (``"Baroque"``, ``"Classical"``, ``"Romantic"``, etc.).

    Important attributes: ``name`` (period label), ``start`` (inclusive start year),
    ``end`` (inclusive end year).
    """

    name: str
    start: int
    end: int


class DirHint(BaseModel):
    """A search query and optional artist hint derived from a source directory.

    Returned by :func:`~music_annotator._discover.parse_disc_info_yaml` and
    :func:`~music_annotator._discover.parse_dir_hint`.  The ``artist`` field is empty when the
    naming convention does not reliably separate artist from title (directory-name searches),
    and populated when a FreeDB ``DTITLE`` ``"artist / title"`` entry is found.

    Important attributes: ``query`` (MB search string), ``artist`` (artist hint, may be ``""``).
    """

    query: str
    artist: str = ""


class CopyPlanEntry(BaseModel):
    """One planned file-copy operation in the :func:`~music_annotator.run` pipeline.

    Built from the source–destination mapping before any filesystem operations begin, so that
    collision detection and the copy loop can both work from the same pre-computed plan.

    Important attributes: ``idx`` (0-based index into the source/tags maps), ``src_file``
    (absolute source path), ``dest_file`` (absolute destination path including extension).
    """

    idx: int
    src_file: Path
    dest_file: Path

    model_config = {"arbitrary_types_allowed": True}


class PictureEntry(BaseModel):
    """A single embedded cover-art picture block as read back from a tagged audio file.

    Used in :func:`~music_annotator._pipeline_io._verify_copy` to compare expected and actual
    embedded PICTURE / APIC data after the tag-write step.

    Important attributes: ``pic_type`` (FLAC/ID3 picture type integer, e.g. ``3`` for COVER_FRONT),
    ``data`` (raw image bytes).
    """

    pic_type: int
    data: bytes

    model_config = {"arbitrary_types_allowed": True}


class TransactionEntry(BaseModel):
    """A single entry in the :class:`TransactionLog` describing one file operation.

    Important attributes: ``timestamp`` (ISO-8601 UTC string), ``release_id`` (MusicBrainz MBID),
    ``source`` (absolute path of the input audio file), ``destination`` (absolute path of the output
    file including extension), ``action`` (one of ``"tagged"``, ``"skipped"``, ``"dry_run"``,
    ``"downloaded"``, ``"sidecar"``, ``"repathed"``, ``"regrouped"``, ``"enriched"``,
    ``"unified"``).

    The ``"repathed"`` action is written by :func:`~music_annotator.repath` when a library file
    is moved to a corrected destination under the current path-construction policy.  For
    ``"repathed"`` entries, ``source`` is the *old* on-disk path (the legacy location before the
    move) and ``destination`` is the *new* on-disk path (the corrected location after the move).
    The ``release_id`` field is empty for ``"repathed"`` entries because repath operates offline
    from embedded tags and does not perform a MusicBrainz lookup.

    The ``"regrouped"`` action is written by :func:`~music_annotator.regroup` when confirmed
    split-release files are consolidated into the canonical directory implied by their embedded
    tags.  Unlike ``"repathed"``, ``release_id`` is populated for ``"regrouped"`` entries because
    the move is release-driven: the release MBID that drove candidate selection is known and recorded
    so that future audits can re-confirm the entry without a MusicBrainz lookup.

    The ``"enriched"`` action is written by :func:`~music_annotator.enrich` when fingerprint fields
    (``audio_hash``, ``acoustid_fingerprint``, ``acoustid_id``) are retroactively backfilled into an
    already-tagged library file.  For ``"enriched"`` entries, ``source`` and ``destination`` are
    the same path (in-place tag update).  The ``audio_hash``, ``acoustid_fingerprint``, and
    ``acoustid_id`` fields carry the full triple as written.

    The ``"unified"`` action is written by :func:`~music_annotator.unify` when a fragmented release
    (one whose tracks are split across ≥2 top_dirs due to per-track ``CEA_SOLOISTS`` variation) is
    consolidated into the canonical top_dir computed by running ``build_dest_path`` over all tracks
    of the release as a single group.  For ``"unified"`` entries, ``source`` is the old on-disk path
    and ``destination`` is the new canonical path.  The ``release_id`` field is populated because
    the move is release-driven (same as ``"regrouped"``).
    """

    timestamp: str
    release_id: str
    source: str
    destination: str
    # "tagged" | "skipped" | "dry_run" | "downloaded" | "sidecar" | "repathed" | "regrouped" | "enriched" | "unified"
    action: str
    # --- archival identity (extensible: 4th dim slots in here) ---
    audio_hash: str = ""  # algorithm-tagged decoded-audio hash; format "<algo>:<hexdigest>"
    acoustid_fingerprint: str = Field(default="", alias="chromaprint_fp")
    """Raw Chromaprint fingerprint string stored under the Picard-aligned key ``ACOUSTID_FINGERPRINT``,
    renamed from the legacy ``CHROMAPRINT_FP``.

    **Dual-read transition:** the forward write path emits only ``ACOUSTID_FINGERPRINT``; every
    read-back helper reads both the new key and the legacy ``CHROMAPRINT_FP``.

    **Value-source rule for** ``ACOUSTID_ID``: the companion ``acoustid_id`` field holds the AcoustID
    cluster UUID from the fingerprint ``/v2/lookup`` endpoint — Picard's ``acoustid_id`` source.  When
    no api_key is supplied or fpcalc yields no fingerprint, ``acoustid_id`` is left empty at ingest;
    it is never re-filled from the ``list_by_mbid`` endpoint (the empty-not-fallback rule).
    """
    acoustid_id: str = ""  # AcoustID cluster UUID from the fingerprint /v2/lookup (Picard's acoustid_id source)
    # AccurateRip per-track flat fields (C-AR).  Mirrors TrackTags flat fields exactly.

    model_config = {"populate_by_name": True}

    @property
    def chromaprint_fp(self) -> str:
        """Transition read/write bridge for the legacy field name.

        Returns :attr:`acoustid_fingerprint`.  Removed when all consuming files are updated to the
        canonical name.
        """
        return self.acoustid_fingerprint

    @chromaprint_fp.setter
    def chromaprint_fp(self, value: str) -> None:
        """Transition write bridge for the legacy field name.

        Assigns ``value`` to :attr:`acoustid_fingerprint`.  Removed when all consuming files are
        updated to the canonical name.

        :param value: The fingerprint string to store.
        """
        self.acoustid_fingerprint = value

    accuraterip_v1_result: str = ""
    accuraterip_v1_confidence: str = ""
    accuraterip_v1_local_crc: str = ""
    accuraterip_v1_remote_crc: str = ""
    accuraterip_v2_result: str = ""
    accuraterip_v2_confidence: str = ""
    accuraterip_v2_local_crc: str = ""
    accuraterip_v2_remote_crc: str = ""
    accuraterip_test_crc: str = ""
    accuraterip_copy_crc: str = ""
    accuraterip_status: str = ""
    # --- provenance (W1b rebuild) ---
    origin_time: str = ""  # ISO-8601 rip/download origin time from freedb_disc_N.yaml sidecar


class TransactionLog(BaseModel):
    """A journal of file operations performed by :func:`~music_annotator.run`.

    Persisted as a JSON array at ``<dest_root>/music_annotator_journal.json``.  Each call to
    :func:`~music_annotator.write_transaction_log` appends new entries to any that already exist in
    that file, so the log accumulates across multiple runs.

    Important attributes: ``entries`` (list of :class:`TransactionEntry`).
    """

    entries: list[TransactionEntry] = Field(default_factory=list)


class ProvenanceSidecar(BaseModel):
    """Provenance fields written into ``freedb_disc_N.yaml`` or ``music_annotator_provenance.yaml``.

    These fields record the rip/download origin of a work directory and its annotation completeness
    so that the information survives journal regeneration (W1b).

    ``origin_time`` and ``origin_source`` are written once and never overwritten (idempotent).

    ``annotation_tier`` records how completely the release could be annotated (C-TIER contract).
    It defaults to ``""`` (unset), which is a *defect* state — the ingest write path must always
    set it; the audit pass flags any empty one.  The field is overwritable **only
    monotonically upward**: a re-resolve may raise the tier (e.g. ``source-tags-only`` →
    ``mb-search-resolved`` after a later MB lookup), but may never silently lower it.  Callers
    must compare :func:`annotation_tier_rank` values before writing.

    ``needs_spot_check`` is ``True`` for ``mb-search-resolved`` entries until a human has
    confirmed the match.  It is persisted here so the ``audit`` pass can enumerate the
    spot-check population without re-running the census.

    Important attributes: ``origin_time``, ``origin_source``, ``annotation_tier``,
    ``needs_spot_check``.
    """

    origin_time: str = ""
    origin_source: str = ""
    annotation_tier: AnnotationTier | str = ""
    """Annotation completeness tier (C-TIER).  ``""`` = unset (defect).  Overwritable only upward."""
    needs_spot_check: bool = False
    """``True`` for ``mb-search-resolved`` entries awaiting human confirmation."""
    accuraterip_summary: AccurateRipSummary = Field(default_factory=AccurateRipSummary)
    """Per-release AccurateRip summary (C-AR).  Monotonic-upgrade: an incoming empty summary must not overwrite a
    populated one."""
    applied_case_ids: list[str] = Field(default_factory=list)
    """Applied contested-default case-IDs; set-union append-only; order-normalised for stable YAML.

    Records the register case-IDs (``<LAYER>-<n>`` form, e.g. ``"SEL-11"``, ``"REND-14"``) of the
    contested-case neutral defaults that were applied for this release.  Merge semantics: set-union,
    append-only — ``_write_provenance_fields`` unions the incoming set with the recorded set and writes
    the sorted union; an incoming empty list never shrinks or erases the recorded set.  Serialization
    is order-normalised (sorted) for byte-stable re-writes.  Free-text is never written to tags: the
    case-IDs live only in the provenance sidecar.
    """
