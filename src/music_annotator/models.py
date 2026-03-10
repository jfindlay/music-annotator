"""Pydantic data models for MusicBrainz API responses and Classical Extras tag fields.

These models validate and structure the raw dict data returned by ``musicbrainzngs`` before it is consumed by the annotation
logic.  All fields that the MB API may omit default to empty strings or empty lists so callers never need to guard against
``KeyError``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Base types
# ---------------------------------------------------------------------------


type JSON = dict[str, JSON] | list[JSON] | str | float | int | bool | None  # pylint: disable=invalid-name


# ---------------------------------------------------------------------------
# MusicBrainz API response models
# ---------------------------------------------------------------------------


class MBArtist(BaseModel):
    """A single artist entity as returned inside an artist-credit or relation.

    Important attributes: ``id`` (MBID), ``name`` (display name), ``sort_name`` (sortable form), ``type`` (e.g. ``"Person"``).
    """

    id: str = ""
    name: str = ""
    sort_name: str = Field(default="", alias="sort-name")
    type: str = ""

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

    Important attributes: ``type`` (relation type, e.g. ``"composer"``), ``direction``, ``artist`` (:class:`MBArtist`),
    ``attribute_list`` (list of :class:`MBAttribute` or plain strings from the MB API).
    """

    type: str = ""
    direction: str = ""
    artist: MBArtist = Field(default_factory=MBArtist)
    attribute_list: list[MBAttribute | str] = Field(default_factory=list, alias="attribute-list")

    model_config = {"populate_by_name": True}


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
    (``"forward"``/``"backward"``), ``work`` (:class:`MBWorkStub`).
    """

    type: str = ""
    direction: str = ""
    work_id: str = Field(default="", alias="work-id")
    work_title: str = Field(default="", alias="work-title")
    work: MBWorkStub = Field(default_factory=MBWorkStub)

    model_config = {"populate_by_name": True}


class MBTag(BaseModel):
    """A folksonomy tag attached to a MB entity.

    Important attributes: ``name`` (tag text), ``count`` (vote count).
    """

    name: str = ""
    count: int = 0


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

    Important attributes: ``id`` (MBID), ``title``, ``type`` (e.g. ``"Symphony"``), ``language``, ``key``,
    ``artist_relation_list``, ``work_relation_list``, ``tag_list``, ``attribute_list``, ``life_span``.
    """

    id: str = ""
    title: str = ""
    type: str = ""
    language: str = ""
    key: str = ""
    artist_relation_list: list[MBArtistRelation] = Field(default_factory=list, alias="artist-relation-list")
    work_relation_list: list[MBWorkRelation] = Field(default_factory=list, alias="work-relation-list")
    tag_list: list[MBTag] = Field(default_factory=list, alias="tag-list")
    attribute_list: list[MBAttribute | str] = Field(default_factory=list, alias="attribute-list")
    life_span: MBLifeSpan = Field(default_factory=MBLifeSpan, alias="life-span")

    model_config = {"populate_by_name": True}

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


class MBLabel(BaseModel):
    """Minimal label entity used inside ``label-info-list``.

    Important attributes: ``id`` (label MBID), ``name`` (label display name).
    """

    id: str = ""
    name: str = ""

    model_config = {"populate_by_name": True}


class MBLabelInfo(BaseModel):
    """One entry in a release's ``label-info-list``.

    Important attributes: ``label`` (:class:`MBLabel`), ``catalog_number``.
    """

    label: MBLabel = Field(default_factory=MBLabel)
    catalog_number: str = Field(default="", alias="catalog-number")

    model_config = {"populate_by_name": True}


class MBReleaseGroup(BaseModel):
    """Release-group summary embedded in a release response.

    Important attributes: ``id`` (release-group MBID), ``primary_type`` (e.g. ``"Album"``), ``first_release_date``.
    """

    id: str = ""
    primary_type: str = Field(default="", alias="primary-type")
    first_release_date: str = Field(default="", alias="first-release-date")

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

    Important attributes: ``id`` (recording MBID), ``title``, ``artist_credit``.
    """

    id: str = ""
    title: str = ""
    artist_credit: list[MBArtistCredit | str] = Field(default_factory=list, alias="artist-credit")

    model_config = {"populate_by_name": True}


class MBTrack(BaseModel):
    """One track entry within a medium's ``track-list``.

    Important attributes: ``id`` (track MBID), ``position`` (1-based integer), ``recording`` (:class:`MBRecordingStub`).
    """

    id: str = ""
    position: int = 0
    recording: MBRecordingStub = Field(default_factory=MBRecordingStub)

    model_config = {"populate_by_name": True}


class MBMedium(BaseModel):
    """One disc (medium) in a release.

    Important attributes: ``position`` (1-based disc number), ``format`` (e.g. ``"CD"``), ``track_list``.
    """

    position: int = 1
    format: str = ""
    track_list: list[MBTrack] = Field(default_factory=list, alias="track-list")

    model_config = {"populate_by_name": True}


class MBRelease(BaseModel):
    """Top-level release entity as returned by ``musicbrainzngs.get_release_by_id``.

    Important attributes: ``id`` (release MBID), ``title``, ``date``, ``status``, ``barcode``, ``artist_credit``,
    ``release_group``, ``label_info_list``, ``medium_list``, ``text_representation``.
    """

    id: str = ""
    title: str = ""
    date: str = ""
    status: str = ""
    barcode: str = ""
    artist_credit: list[MBArtistCredit | str] = Field(default_factory=list, alias="artist-credit")
    release_group: MBReleaseGroup = Field(default_factory=MBReleaseGroup, alias="release-group")
    label_info_list: list[MBLabelInfo] = Field(default_factory=list, alias="label-info-list")
    medium_list: list[MBMedium] = Field(default_factory=list, alias="medium-list")
    text_representation: MBTextRepresentation = Field(default_factory=MBTextRepresentation, alias="text-representation")

    model_config = {"populate_by_name": True}


class MBRecording(BaseModel):
    """Recording entity with artist and work relationships, as returned by ``musicbrainzngs.get_recording_by_id``.

    Important attributes: ``id`` (recording MBID), ``title``, ``artist_credit``, ``artist_relation_list``,
    ``work_relation_list``.
    """

    id: str = ""
    title: str = ""
    artist_credit: list[MBArtistCredit | str] = Field(default_factory=list, alias="artist-credit")
    artist_relation_list: list[MBArtistRelation] = Field(default_factory=list, alias="artist-relation-list")
    work_relation_list: list[MBWorkRelation] = Field(default_factory=list, alias="work-relation-list")

    model_config = {"populate_by_name": True}


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

    Important attributes: ``composers``, ``lyricists``, ``librettists``, ``translators``, ``arrangers``, ``orchestrators``,
    ``reconstructors``, ``revisors``.
    """

    composers: list[ArtistEntry] = Field(default_factory=list)
    lyricists: list[ArtistEntry] = Field(default_factory=list)
    librettists: list[ArtistEntry] = Field(default_factory=list)
    translators: list[ArtistEntry] = Field(default_factory=list)
    arrangers: list[ArtistEntry] = Field(default_factory=list)
    orchestrators: list[ArtistEntry] = Field(default_factory=list)
    reconstructors: list[ArtistEntry] = Field(default_factory=list)
    revisors: list[ArtistEntry] = Field(default_factory=list)

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

    Important attributes: ``index``, ``work_id``, ``work_title``, ``part_title`` (stripped movement/part name for
    ``cwp_part_N``).
    """

    index: int
    work_id: str
    work_title: str
    part_title: str = ""  # stripped movement/part name (cwp_part_N)


class CwpTags(BaseModel):
    """All Classical Extras ``_cwp_*`` tag values for one track.

    This model is constructed after the full work hierarchy is resolved and movement numbers are computed across the release.

    Important attributes: ``work_top``, ``workid_top``, ``part_levels``, ``part``, ``work``, ``groupheading``,
    ``inter_work``, ``movt_num``, ``movt_tot``, ``single_work_album``, ``levels`` (per-level :class:`WorkHierarchyLevel`
    list), plus all artist role and date string fields.
    """

    work_top: str = ""
    workid_top: str = ""
    part_levels: int = 0
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
    arrangers: str = ""
    arrangers_sort: str = ""
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
    period: str = ""


class TrackTags(BaseModel):
    """The complete flat tag mapping written to one audio file.

    All values are strings.  Fields whose names appear in the ``excluded`` set inside :meth:`to_file_dict` are internal
    helpers and are not written to disk.  Per-level ``cwp_work_N`` / ``cwp_workid_N`` / ``cwp_part_N`` tags are stored as
    Pydantic ``extra`` fields via ``model_config = {"extra": "allow"}``.

    Important attributes: all standard Picard tag fields, MusicBrainz ID fields, ``_cea_*`` fields, ``_cwp_*`` fields,
    and the internal ``cea_conductors_list`` / ``cea_ensembles_list`` lists used for path building.
    """

    # Internal (not written to file)
    cea_conductors_list: list[ArtistEntry] = Field(default_factory=list)
    cea_ensembles_list: list[ArtistEntry] = Field(default_factory=list)

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
    media: str = "CD"
    script: str = ""
    language: str = ""
    releasetype: str = ""
    releasestatus: str = ""

    # Label / catalogue
    organization: str = ""
    label: str = ""
    catalognumber: str = ""
    barcode: str = ""

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
    cea_soloists: str = ""
    cea_soloist_names: str = ""
    cea_vocalists: str = ""
    cea_instrumentalists: str = ""
    cea_other_soloists: str = ""
    cea_ensembles: str = ""
    cea_ensemble_names: str = ""
    cea_conductors: str = ""
    cea_composers: str = ""
    cea_composer_lastnames: str = ""
    cea_performers: str = ""
    cea_arrangers: str = ""
    cea_orchestrators: str = ""
    cea_chorusmasters: str = ""
    cea_leaders: str = ""
    cea_instruments: str = ""

    # CWP tags
    cwp_work_top: str = ""
    cwp_workid_top: str = ""
    cwp_part_levels: str = "0"
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
    cwp_arrangers: str = ""
    cwp_arrangers_sort: str = ""
    cwp_orchestrators: str = ""
    cwp_orchestrators_sort: str = ""
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

    # Per-level work/workid/part tags are stored as extra fields
    model_config = {"extra": "allow"}

    def to_file_dict(self) -> dict[str, str]:
        """Return a ``{tag_name: value}`` mapping suitable for writing to an audio file.

        Internal fields (``cea_conductors_list``, ``cea_ensembles_list``) are excluded.  Empty values are excluded.  All
        keys are uppercased to match Vorbis Comment / ID3 conventions.  Dynamically-added per-level
        ``cwp_work_N`` / ``cwp_workid_N`` / ``cwp_part_N`` extra fields are included.

        :returns: A flat ``dict[str, str]`` of non-empty tag key/value pairs with uppercase keys, excluding internal list
            fields.
        """
        excluded = {"cea_conductors_list", "cea_ensembles_list"}
        out: dict[str, str] = {}
        for field_name, value in self.model_dump(exclude=excluded).items():
            if isinstance(value, str) and value:
                out[field_name.upper()] = value
        # Also include dynamically-added per-level fields
        for key, value in (self.model_extra or {}).items():
            if isinstance(value, str) and value:
                out[key.upper()] = value
        return out


class CoverArt(BaseModel):
    """Cover art image bytes and inferred MIME type.

    Important attributes: ``data`` (raw image bytes, ``b""`` when unavailable), ``mime`` (MIME type string).
    """

    data: bytes = b""
    mime: str = ""

    @property
    def available(self) -> bool:
        """Return ``True`` if image data is present.

        :returns: ``True`` when ``data`` is non-empty.
        """
        return len(self.data) > 0

    model_config = {"arbitrary_types_allowed": True}
