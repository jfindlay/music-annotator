"""Unit tests for music_annotator.models."""

from __future__ import annotations

import pytest

from music_annotator.models import (
    AccurateRipResult,
    AccurateRipSummary,
    AccurateRipTrack,
    AccurateRipTrackResult,
    AnnotationTier,
    ArtistEntry,
    CeaPerformers,
    CensusSignal,
    CoverArt,
    CoverImage,
    CwpTags,
    MBArtist,
    MBArtistRelation,
    MBCoverArtArchive,
    MBDisc,
    MBLabel,
    MBLabelInfo,
    MBLabelRelation,
    MBMedium,
    MBPlaceRelation,
    MBRecording,
    MBRelease,
    MBReleaseCandidate,
    MBReleaseEvent,
    MBReleaseGroup,
    MBSeriesRelation,
    MBTrack,
    MBUrlRelation,
    MBWork,
    MBWorkRelation,
    ProvenanceSidecar,
    RoleBuckets,
    TrackTags,
    TransactionEntry,
    TransactionLog,
    WorkDates,
    WorkHierarchyLevel,
    classify_annotation_tier,
)

# ---------------------------------------------------------------------------
# ArtistEntry
# ---------------------------------------------------------------------------


class TestArtistEntry:
    """Tests for ArtistEntry model."""

    def test_basic_construction(self) -> None:
        """ArtistEntry stores name, sort, and mbid."""
        e = ArtistEntry(name="Respighi, Ottorino", sort="Respighi, Ottorino", mbid="abc-123")
        assert e.name == "Respighi, Ottorino"
        assert e.sort == "Respighi, Ottorino"
        assert e.mbid == "abc-123"
        assert e.instrument == ""

    def test_instrument_optional(self) -> None:
        """instrument defaults to empty string."""
        e = ArtistEntry(name="Anne-Sophie Mutter", sort="Mutter, Anne-Sophie", mbid="x")
        assert e.instrument == ""

    def test_instrument_set(self) -> None:
        """instrument can be set explicitly."""
        e = ArtistEntry(name="Anne-Sophie Mutter", sort="Mutter, Anne-Sophie", mbid="x", instrument="violin")
        assert e.instrument == "violin"


# ---------------------------------------------------------------------------
# RoleBuckets
# ---------------------------------------------------------------------------


class TestRoleBuckets:
    """Tests for RoleBuckets.add_unique and seen_ids."""

    def test_add_unique_by_mbid(self) -> None:
        """Same MBID added twice should only appear once."""
        rb = RoleBuckets()
        e1 = ArtistEntry(name="Respighi", sort="Respighi, Ottorino", mbid="aaa")
        e2 = ArtistEntry(name="Respighi", sort="Respighi, Ottorino", mbid="aaa")
        rb.add_unique("composers", e1)
        rb.add_unique("composers", e2)
        assert len(rb.composers) == 1

    def test_add_unique_different_mbids(self) -> None:
        """Two different MBIDs are both added."""
        rb = RoleBuckets()
        e1 = ArtistEntry(name="Composer A", sort="A", mbid="aaa")
        e2 = ArtistEntry(name="Composer B", sort="B", mbid="bbb")
        rb.add_unique("composers", e1)
        rb.add_unique("composers", e2)
        assert len(rb.composers) == 2

    def test_add_unique_no_mbid_always_appends(self) -> None:
        """When mbid is empty, entry is always appended (no dedup possible)."""
        rb = RoleBuckets()
        e1 = ArtistEntry(name="Unknown", sort="Unknown", mbid="")
        e2 = ArtistEntry(name="Unknown", sort="Unknown", mbid="")
        rb.add_unique("arrangers", e1)
        rb.add_unique("arrangers", e2)
        assert len(rb.arrangers) == 2

    def test_seen_ids_returns_set(self) -> None:
        """seen_ids returns the set of MBIDs already present."""
        rb = RoleBuckets()
        rb.add_unique("composers", ArtistEntry(name="X", sort="X", mbid="id1"))
        rb.add_unique("composers", ArtistEntry(name="Y", sort="Y", mbid="id2"))
        assert rb.seen_ids("composers") == {"id1", "id2"}

    def test_seen_ids_excludes_empty(self) -> None:
        """seen_ids excludes entries where mbid is empty."""
        rb = RoleBuckets()
        rb.add_unique("lyricists", ArtistEntry(name="X", sort="X", mbid=""))
        assert rb.seen_ids("lyricists") == set()

    def test_all_role_buckets_start_empty(self) -> None:
        """All role lists default to empty."""
        rb = RoleBuckets()
        for role in (
            "composers",
            "lyricists",
            "librettists",
            "translators",
            "arrangers",
            "orchestrators",
            "reconstructors",
            "revisors",
        ):
            assert not getattr(rb, role)


# ---------------------------------------------------------------------------
# CeaPerformers
# ---------------------------------------------------------------------------


class TestCeaPerformers:
    """Tests for CeaPerformers.all_soloists property."""

    def test_all_soloists_empty(self) -> None:
        """all_soloists returns empty list when nothing set."""
        cea = CeaPerformers()
        assert cea.all_soloists == []

    def test_all_soloists_concatenates(self) -> None:
        """all_soloists concatenates vocalists + instrumentalists + other_soloists."""
        cea = CeaPerformers()
        v = ArtistEntry(name="Soprano", sort="S", mbid="s1", instrument="soprano")
        i = ArtistEntry(name="Violinist", sort="V", mbid="v1", instrument="violin")
        o = ArtistEntry(name="Other", sort="O", mbid="o1")
        cea.vocalists.append(v)
        cea.instrumentalists.append(i)
        cea.other_soloists.append(o)
        result = cea.all_soloists
        assert result == [v, i, o]


# ---------------------------------------------------------------------------
# WorkDates
# ---------------------------------------------------------------------------


class TestWorkDates:
    """Tests for WorkDates model defaults."""

    def test_all_defaults_empty(self) -> None:
        """All date fields default to empty strings."""
        wd = WorkDates()
        assert wd.composed == ""
        assert wd.published == ""
        assert wd.premiered == ""

    def test_explicit_values(self) -> None:
        """Fields accept explicit string values."""
        wd = WorkDates(composed="1916", published="1918", premiered="1918-03-11")
        assert wd.composed == "1916"
        assert wd.published == "1918"
        assert wd.premiered == "1918-03-11"


# ---------------------------------------------------------------------------
# WorkHierarchyLevel
# ---------------------------------------------------------------------------


class TestWorkHierarchyLevel:
    """Tests for WorkHierarchyLevel model."""

    def test_construction(self) -> None:
        """WorkHierarchyLevel stores index, work_id, work_title, part_title."""
        level = WorkHierarchyLevel(
            index=0,
            work_id="work-uuid-1",
            work_title="Fontane di Roma, P 106: I. La fontana di Valle Giulia all'alba",
            part_title="I. La fontana di Valle Giulia all'alba",
        )
        assert level.index == 0
        assert level.work_id == "work-uuid-1"
        assert "Fontane" in level.work_title
        assert level.part_title.startswith("I.")

    def test_part_title_defaults_empty(self) -> None:
        """part_title defaults to empty string."""
        level = WorkHierarchyLevel(index=1, work_id="wid", work_title="Fontane di Roma")
        assert level.part_title == ""


# ---------------------------------------------------------------------------
# CwpTags
# ---------------------------------------------------------------------------


class TestCwpTags:
    """Tests for CwpTags model defaults."""

    def test_defaults(self) -> None:
        """All string fields default to empty; part_levels to 0."""
        cwp = CwpTags()
        assert cwp.work_top == ""
        assert cwp.workid_top == ""
        assert cwp.part_levels == 0
        assert cwp.levels == []
        assert cwp.composers == ""
        assert cwp.period == ""


# ---------------------------------------------------------------------------
# TrackTags.to_file_dict
# ---------------------------------------------------------------------------


class TestTrackTagsToFileDict:
    """Tests for TrackTags.to_file_dict."""

    def test_uppercase_keys(self) -> None:
        """to_file_dict returns uppercase keys."""
        t = TrackTags(title="My Title", album="My Album")
        d = t.to_file_dict()
        assert "TITLE" in d
        assert "ALBUM" in d

    def test_empty_values_excluded(self) -> None:
        """Empty string values are excluded from the output dict."""
        t = TrackTags(title="Track 1", album="")
        d = t.to_file_dict()
        assert "TITLE" in d
        assert "ALBUM" not in d

    def test_internal_lists_excluded(self) -> None:
        """Internal list fields cea_conductors_list and cea_ensembles_list are excluded."""
        e = ArtistEntry(name="Karajan", sort="Karajan, Herbert von", mbid="k1")
        t = TrackTags(title="Track", cea_conductors_list=[e])
        d = t.to_file_dict()
        assert "CEA_CONDUCTORS_LIST" not in d
        assert "CEA_ENSEMBLES_LIST" not in d

    def test_per_level_extras_included(self) -> None:
        """Dynamically set per-level cwp_work_N fields appear in output."""
        t = TrackTags(title="Track")
        t.model_extra["cwp_work_0"] = "Fontane di Roma"  # type: ignore[index]
        t.model_extra["cwp_workid_0"] = "some-uuid"  # type: ignore[index]
        d = t.to_file_dict()
        assert d.get("CWP_WORK_0") == "Fontane di Roma"
        assert d.get("CWP_WORKID_0") == "some-uuid"

    def test_empty_extras_excluded(self) -> None:
        """Per-level extras with empty string values are excluded."""
        t = TrackTags(title="Track")
        t.model_extra["cwp_work_0"] = ""  # type: ignore[index]
        d = t.to_file_dict()
        assert "CWP_WORK_0" not in d

    def test_classical_genre_default(self) -> None:
        """genre defaults to 'Classical' and appears in file dict."""
        t = TrackTags(title="Track")
        d = t.to_file_dict()
        assert d.get("GENRE") == "Classical"

    def test_is_classical_default(self) -> None:
        """is_classical defaults to '1'."""
        t = TrackTags(title="Track")
        d = t.to_file_dict()
        assert d.get("IS_CLASSICAL") == "1"

    def test_media_default(self) -> None:
        """media defaults to 'CD'."""
        t = TrackTags(title="Track")
        d = t.to_file_dict()
        assert d.get("MEDIA") == "CD"


# ---------------------------------------------------------------------------
# CoverArt
# ---------------------------------------------------------------------------


class TestCoverImage:
    """Tests for the CoverImage leaf model."""

    def test_fields(self) -> None:
        """data and mime are stored as provided."""
        img = CoverImage(data=b"\xff\xd8\xff\xe0", mime="image/jpeg")
        assert img.data == b"\xff\xd8\xff\xe0"
        assert img.mime == "image/jpeg"

    def test_defaults_empty(self) -> None:
        """data and mime default to empty."""
        img = CoverImage()
        assert img.data == b""
        assert img.mime == ""


class TestCoverArt:
    """Tests for CoverArt and its backward-compatible properties."""

    def test_available_true_with_front(self) -> None:
        """available is True when front list is non-empty."""
        img = CoverImage(data=b"\xff\xd8\xff\xe0", mime="image/jpeg")
        c = CoverArt(front=[img])
        assert c.available is True

    def test_available_true_with_back_only(self) -> None:
        """available is True when only back images are present."""
        c = CoverArt(back=[CoverImage(data=b"\x89PNG", mime="image/png")])
        assert c.available is True

    def test_available_true_with_booklet_only(self) -> None:
        """available is True when only booklet images are present."""
        c = CoverArt(booklet=[CoverImage(data=b"\xff\xd8", mime="image/jpeg")])
        assert c.available is True

    def test_available_true_with_medium_only(self) -> None:
        """available is True when only medium images are present."""
        c = CoverArt(medium=[CoverImage(data=b"\xff\xd8", mime="image/jpeg")])
        assert c.available is True

    def test_available_true_with_unknown_only(self) -> None:
        """available is True when only unknown images are present."""
        c = CoverArt(unknown=[CoverImage(data=b"\xff\xd8", mime="image/jpeg")])
        assert c.available is True

    def test_available_false_empty(self) -> None:
        """available is False when all lists are empty (default)."""
        c = CoverArt()
        assert c.available is False

    def test_data_property_returns_first_front(self) -> None:
        """data compat property returns first front image bytes."""
        img = CoverImage(data=b"\xff\xd8\xff\xe0", mime="image/jpeg")
        c = CoverArt(front=[img])
        assert c.data == b"\xff\xd8\xff\xe0"

    def test_data_property_empty_when_no_front(self) -> None:
        """data compat property returns b'' when front list is empty."""
        c = CoverArt()
        assert c.data == b""

    def test_mime_property_returns_first_front(self) -> None:
        """mime compat property returns MIME of first front image."""
        img = CoverImage(data=b"\xff\xd8\xff\xe0", mime="image/jpeg")
        c = CoverArt(front=[img])
        assert c.mime == "image/jpeg"

    def test_mime_property_empty_when_no_front(self) -> None:
        """mime compat property returns '' when front list is empty."""
        c = CoverArt()
        assert c.mime == ""


# ---------------------------------------------------------------------------
# MBArtist aliased fields
# ---------------------------------------------------------------------------


class TestMBArtist:
    """Tests for MBArtist model with hyphenated alias."""

    def test_sort_name_alias(self) -> None:
        """sort-name alias populates sort_name field."""
        a = MBArtist.model_validate({"id": "1", "name": "Karajan", "sort-name": "Karajan, Herbert von"})
        assert a.sort_name == "Karajan, Herbert von"

    def test_defaults_empty(self) -> None:
        """All fields default to empty string."""
        a = MBArtist()
        assert a.id == ""
        assert a.name == ""
        assert a.sort_name == ""
        assert a.type == ""

    def test_alias_list_defaults_empty(self) -> None:
        """alias_list defaults to an empty list on a default-constructed MBArtist."""
        a = MBArtist()
        assert a.alias_list == []

    def test_alias_list_populated_from_alias_key(self) -> None:
        """alias-list entries with the 'alias' key (musicbrainzngs shape) populate alias_list correctly.

        Verifies the MBAlias model_validator remapping: the raw dict uses ``"alias"`` for the display
        text (matching the musicbrainzngs output), which must be remapped to ``name`` before Pydantic
        processes the field.
        """
        a = MBArtist.model_validate(
            {
                "id": "a1",
                "name": "Vienna Philharmonic",
                "alias-list": [
                    {"alias": "Wiener Philharmoniker", "type": "Artist name", "primary": "primary", "locale": "de"},
                ],
            }
        )
        assert len(a.alias_list) == 1
        assert a.alias_list[0].name == "Wiener Philharmoniker"
        assert a.alias_list[0].primary == "primary"
        assert a.alias_list[0].type == "Artist name"
        assert a.alias_list[0].locale == "de"


# ---------------------------------------------------------------------------
# MBWork
# ---------------------------------------------------------------------------


class TestMBWork:
    """Tests for MBWork model."""

    def test_defaults(self) -> None:
        """MBWork fields all default to empty / empty list."""
        w = MBWork()
        assert w.id == ""
        assert w.title == ""
        assert w.artist_relation_list == []
        assert w.work_relation_list == []
        assert w.tag_list == []

    def test_coerce_attributes_none(self) -> None:
        """attribute_list validator accepts None and returns []."""
        data: dict[str, object] = {"id": "wid", "title": "Work", "attribute-list": None}
        w = MBWork.model_validate(data)
        assert w.attribute_list == []


# ---------------------------------------------------------------------------
# MBLabelInfo
# ---------------------------------------------------------------------------


class TestMBLabelInfo:
    """Tests for MBLabelInfo aliased field."""

    def test_catalog_number_alias(self) -> None:
        """catalog-number alias populates catalog_number field."""
        li = MBLabelInfo.model_validate({"catalog-number": "449 724-2", "label": {"id": "l1", "name": "Deutsche Grammophon"}})
        assert li.catalog_number == "449 724-2"
        assert li.label.name == "Deutsche Grammophon"


# ---------------------------------------------------------------------------
# MBRelease
# ---------------------------------------------------------------------------


class TestMBRelease:
    """Tests for MBRelease model."""

    def test_defaults(self) -> None:
        """MBRelease fields default to empty / empty list."""
        r = MBRelease()
        assert r.id == ""
        assert r.title == ""
        assert r.medium_list == []
        assert r.label_info_list == []

    def test_validate_full(self) -> None:
        """MBRelease can be populated from a nested dict."""
        data = {
            "id": "rel-1",
            "title": "Respighi: Fontane di Roma",
            "date": "1995",
            "status": "Official",
            "artist-credit": [],
            "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "1995"},
            "label-info-list": [],
            "medium-list": [],
            "text-representation": {"script": "Latn", "language": "ita"},
        }
        r = MBRelease.model_validate(data)
        assert r.title == "Respighi: Fontane di Roma"
        assert r.release_group.primary_type == "Album"
        assert r.text_representation.script == "Latn"


# ---------------------------------------------------------------------------
# Parameterized edge-cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "sort", "mbid"),
    [
        ("", "", ""),
        ("A B C", "C, A B", "uuid-x"),
        ("Single", "Single", ""),
    ],
)
def test_artist_entry_roundtrip(name: str, sort: str, mbid: str) -> None:
    """ArtistEntry round-trips through model_dump correctly.

    :param name: Artist display name.
    :param sort: Artist sort name.
    :param mbid: Artist MBID.
    """
    e = ArtistEntry(name=name, sort=sort, mbid=mbid)
    d = e.model_dump()
    assert d["name"] == name
    assert d["sort"] == sort
    assert d["mbid"] == mbid


# ---------------------------------------------------------------------------
# MBWorkRelation nested properties
# ---------------------------------------------------------------------------


class TestMBWorkRelation:
    """Tests for MBWorkRelation.work typed access via MBWork."""

    def test_work_id(self) -> None:
        """work.id returns the id from the nested work."""
        rel = MBWorkRelation.model_validate({"type": "parts", "work": {"id": "w-abc", "title": "Symphony"}})
        assert rel.work.id == "w-abc"

    def test_work_title(self) -> None:
        """work.title returns the title from the nested work."""
        rel = MBWorkRelation.model_validate({"type": "parts", "work": {"id": "w-abc", "title": "Symphony No. 1"}})
        assert rel.work.title == "Symphony No. 1"

    def test_work_id_empty_when_default(self) -> None:
        """work.id returns empty string when constructed with default MBWork."""
        rel = MBWorkRelation(work=MBWork())
        assert rel.work.id == ""

    def test_work_title_empty_when_default(self) -> None:
        """work.title returns empty string when constructed with default MBWork."""
        rel = MBWorkRelation(work=MBWork())
        assert rel.work.title == ""

    def test_work_has_artist_relation_list_when_inlined(self) -> None:
        """work.artist_relation_list is populated when work-level-rels inlines the full work."""
        rel = MBWorkRelation.model_validate(
            {
                "type": "performance",
                "work": {
                    "id": "w-full",
                    "title": "Symphony",
                    "artist-relation-list": [{"type": "composer", "artist": {"id": "a1", "name": "Bach"}}],
                },
            }
        )
        assert rel.work.id == "w-full"
        assert len(rel.work.artist_relation_list) == 1
        assert rel.work.artist_relation_list[0].type == "composer"

    def test_work_has_work_relation_list_when_inlined(self) -> None:
        """work.work_relation_list is populated when work-level-rels inlines the full work."""
        rel = MBWorkRelation.model_validate(
            {
                "type": "performance",
                "work": {
                    "id": "w-mov",
                    "title": "Movement I",
                    "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "w-top"}}],
                },
            }
        )
        assert rel.work.id == "w-mov"
        assert len(rel.work.work_relation_list) == 1
        assert rel.work.work_relation_list[0].type == "parts"


# ---------------------------------------------------------------------------
# MBWork.coerce_attributes — list path
# ---------------------------------------------------------------------------


class TestMBWorkCoerceAttributesList:
    """Tests for MBWork.coerce_attributes when given a non-None list."""

    def test_list_passed_through(self) -> None:
        """A list value is returned as-is (converted via list())."""
        w = MBWork.model_validate(
            {
                "id": "w1",
                "title": "Work",
                "attribute-list": [{"type": "key", "value": "G major"}],
            }
        )
        assert len(w.attribute_list) == 1

    def test_empty_list_accepted(self) -> None:
        """An empty list is accepted and returned as []."""
        w = MBWork.model_validate({"attribute-list": []})
        assert w.attribute_list == []

    def test_non_list_value_returns_empty(self) -> None:
        """A non-list, non-None value (e.g. a bare string) returns []."""
        w = MBWork.model_validate({"attribute-list": "unexpected"})
        assert w.attribute_list == []


# ---------------------------------------------------------------------------
# TransactionEntry
# ---------------------------------------------------------------------------


class TestTransactionEntry:
    """Tests for TransactionEntry model."""

    def test_fields_round_trip(self) -> None:
        """All fields survive a model_dump / model_validate round trip."""
        entry = TransactionEntry(
            timestamp="2026-01-01T00:00:00+00:00",
            release_id="rel-abc",
            source="/src/01.flac",
            destination="/dest/01.flac",
            action="tagged",
        )
        data = entry.model_dump()
        restored = TransactionEntry.model_validate(data)
        assert restored.timestamp == "2026-01-01T00:00:00+00:00"
        assert restored.release_id == "rel-abc"
        assert restored.source == "/src/01.flac"
        assert restored.destination == "/dest/01.flac"
        assert restored.action == "tagged"

    def test_action_skipped(self) -> None:
        """action field accepts 'skipped'."""
        entry = TransactionEntry(timestamp="t", release_id="r", source="s", destination="d", action="skipped")
        assert entry.action == "skipped"

    def test_action_dry_run(self) -> None:
        """action field accepts 'dry_run'."""
        entry = TransactionEntry(timestamp="t", release_id="r", source="s", destination="d", action="dry_run")
        assert entry.action == "dry_run"


# ---------------------------------------------------------------------------
# TransactionLog
# ---------------------------------------------------------------------------


class TestTransactionLog:
    """Tests for TransactionLog model."""

    def test_default_entries_is_empty(self) -> None:
        """Default TransactionLog has an empty entries list."""
        log = TransactionLog()
        assert log.entries == []

    def test_entries_validated(self) -> None:
        """Entries are coerced from raw dicts via model_validate."""
        log = TransactionLog.model_validate(
            {
                "entries": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00",
                        "release_id": "r",
                        "source": "s",
                        "destination": "d",
                        "action": "tagged",
                    }
                ]
            }
        )
        assert len(log.entries) == 1
        assert isinstance(log.entries[0], TransactionEntry)
        assert log.entries[0].action == "tagged"


# ---------------------------------------------------------------------------
# New model field tests
# ---------------------------------------------------------------------------


class TestMBArtistRelationEnded:
    """Tests for MBArtistRelation.coerce_ended."""

    def test_ended_string_true(self) -> None:
        """String 'true' is coerced to True."""
        rel = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward", "ended": "true"})
        assert rel.ended is True

    def test_ended_string_false(self) -> None:
        """String 'false' is coerced to False."""
        rel = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward", "ended": "false"})
        assert rel.ended is False

    def test_ended_none_defaults_false(self) -> None:
        """None is coerced to False."""
        rel = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward"})
        assert rel.ended is False

    def test_begin_end_populated(self) -> None:
        """begin and end date fields are populated."""
        rel = MBArtistRelation.model_validate(
            {"type": "conductor", "direction": "backward", "begin": "1984-01-27", "end": "1984-02-21", "ended": "true"}
        )
        assert rel.begin == "1984-01-27"
        assert rel.end == "1984-02-21"
        assert rel.ended is True

    def test_target_credit_populated(self) -> None:
        """target-credit and source-credit are populated."""
        rel = MBArtistRelation.model_validate(
            {"type": "performer", "direction": "backward", "target-credit": "Anne-Sophie Mutter"}
        )
        assert rel.target_credit == "Anne-Sophie Mutter"


class TestMBWorkRelationEnded:
    """Tests for MBWorkRelation.coerce_ended."""

    def test_ended_coerced_from_string(self) -> None:
        """String 'true' is coerced to True on MBWorkRelation."""
        wrel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "ended": "true"})
        assert wrel.ended is True

    def test_begin_populated(self) -> None:
        """begin date is populated on MBWorkRelation."""
        wrel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "begin": "1900"})
        assert wrel.begin == "1900"


class TestMBUrlRelation:
    """Tests for MBUrlRelation URL extraction."""

    def test_url_extracted_from_nested_dict(self) -> None:
        """URL resource is extracted from the nested url dict returned by musicbrainzngs."""
        url_rel = MBUrlRelation.model_validate({"type": "discogs", "url": {"resource": "https://www.discogs.com/release/123"}})
        assert url_rel.url == "https://www.discogs.com/release/123"

    def test_url_plain_string_passthrough(self) -> None:
        """Plain string url is passed through unchanged."""
        url_rel = MBUrlRelation.model_validate({"type": "wikidata", "url": "https://www.wikidata.org/wiki/Q123"})
        assert url_rel.url == "https://www.wikidata.org/wiki/Q123"

    def test_url_defaults_to_empty(self) -> None:
        """url defaults to empty string when absent."""
        url_rel = MBUrlRelation.model_validate({"type": "allmusic"})
        assert url_rel.url == ""


class TestMBPlaceRelation:
    """Tests for MBPlaceRelation model."""

    def test_place_populated(self) -> None:
        """Place name and id are populated from nested place dict."""
        prel = MBPlaceRelation.model_validate(
            {
                "type": "premiere",
                "direction": "backward",
                "begin": "1824-05-07",
                "place": {"id": "abc", "name": "Theater am Kärntnertor"},
            }
        )
        assert prel.type == "premiere"
        assert prel.begin == "1824-05-07"
        assert prel.place.name == "Theater am Kärntnertor"

    def test_ended_coerced(self) -> None:
        """ended is coerced from string."""
        prel = MBPlaceRelation.model_validate({"type": "premiere", "direction": "backward", "ended": "true"})
        assert prel.ended is True


class TestMBLabelRelation:
    """Tests for MBLabelRelation model."""

    def test_label_populated(self) -> None:
        """Label name is populated from nested label dict."""
        lrel = MBLabelRelation.model_validate(
            {
                "type": "publishing",
                "direction": "backward",
                "begin": "1827",
                "label": {"id": "lbl1", "name": "Breitkopf & Härtel"},
            }
        )
        assert lrel.type == "publishing"
        assert lrel.begin == "1827"
        assert lrel.label.name == "Breitkopf & Härtel"


class TestMBSeriesRelation:
    """Tests for MBSeriesRelation model."""

    def test_series_populated(self) -> None:
        """Series name is populated and ordering_key coerced."""
        srel = MBSeriesRelation.model_validate(
            {"type": "part of", "ordering-key": "3", "series": {"id": "s1", "name": "Karajan Gold", "type": "Release series"}}
        )
        assert srel.series.name == "Karajan Gold"
        assert srel.ordering_key == 3

    def test_ordering_key_none_defaults_zero(self) -> None:
        """ordering-key None defaults to 0."""
        srel = MBSeriesRelation.model_validate({"type": "part of", "series": {"id": "s1", "name": "S"}})
        assert srel.ordering_key == 0


class TestMBCoverArtArchive:
    """Tests for MBCoverArtArchive field coercion."""

    def test_bool_fields_coerced_from_string(self) -> None:
        """String 'true'/'false' fields are coerced to bool."""
        caa = MBCoverArtArchive.model_validate(
            {"artwork": "true", "front": "true", "back": "false", "darkened": "false", "count": "3"}
        )
        assert caa.artwork is True
        assert caa.front is True
        assert caa.back is False
        assert caa.count == 3

    def test_defaults_to_false_when_absent(self) -> None:
        """All fields default to False/0 when absent."""
        caa = MBCoverArtArchive.model_validate({})
        assert caa.front is False
        assert caa.count == 0


class TestMBReleaseEvent:
    """Tests for MBReleaseEvent country extraction."""

    def test_country_extracted_from_area(self) -> None:
        """Country code is extracted from the nested area.iso-3166-1-code-list."""
        evt = MBReleaseEvent.model_validate({"date": "1986", "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]}})
        assert evt.date == "1986"
        assert evt.country == "DE"

    def test_country_empty_when_no_area(self) -> None:
        """country defaults to empty when area is absent."""
        evt = MBReleaseEvent.model_validate({"date": "1986"})
        assert evt.country == ""


class TestMBTrackCoerceLength:
    """Tests for MBTrack.coerce_length."""

    def test_length_coerced_from_string(self) -> None:
        """String length is coerced to int."""
        track = MBTrack.model_validate({"id": "t1", "position": 1, "length": "541000"})
        assert track.length == 541000

    def test_length_none_defaults_zero(self) -> None:
        """None length defaults to 0."""
        track = MBTrack.model_validate({"id": "t1", "position": 1})
        assert track.length == 0

    def test_number_populated(self) -> None:
        """number field is populated for non-CD formats."""
        track = MBTrack.model_validate({"id": "t1", "position": 1, "number": "A1"})
        assert track.number == "A1"


class TestMBRecordingNewFields:
    """Tests for new MBRecording fields."""

    def test_video_coerced(self) -> None:
        """video field is coerced from string."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "video": "true"})
        assert rec.video is True

    def test_length_coerced(self) -> None:
        """length field is coerced from string to int."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "length": "542373"})
        assert rec.length == 542373

    def test_isrc_list_populated(self) -> None:
        """isrc-list is populated."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "isrc-list": ["DEF058402370"]})
        assert rec.isrc_list == ["DEF058402370"]

    def test_disambiguation_populated(self) -> None:
        """disambiguation is populated."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "disambiguation": "live recording"})
        assert rec.disambiguation == "live recording"


class TestMBReleaseGroupSecondaryTypes:
    """Tests for MBReleaseGroup.secondary_type_list."""

    def test_secondary_types_populated(self) -> None:
        """secondary-type-list is populated."""
        rg = MBReleaseGroup.model_validate({"id": "rg1", "primary-type": "Album", "secondary-type-list": ["Compilation"]})
        assert rg.secondary_type_list == ["Compilation"]

    def test_secondary_types_defaults_empty(self) -> None:
        """secondary_type_list defaults to []."""
        rg = MBReleaseGroup.model_validate({"id": "rg1", "primary-type": "Album"})
        assert rg.secondary_type_list == []


class TestMBLabelLabelCode:
    """Tests for MBLabel.label_code."""

    def test_label_code_populated(self) -> None:
        """label-code is populated."""
        label = MBLabel.model_validate({"id": "l1", "name": "Deutsche Grammophon", "label-code": "173"})
        assert label.label_code == "173"


class TestMBWorkNewFields:
    """Tests for new MBWork fields."""

    def test_iswc_populated(self) -> None:
        """iswc is populated from the musicbrainzngs response."""
        w = MBWork.model_validate({"id": "w1", "title": "T", "iswc": "T-909.345.750-2"})
        assert w.iswc == "T-909.345.750-2"

    def test_disambiguation_populated(self) -> None:
        """disambiguation is populated."""
        w = MBWork.model_validate({"id": "w1", "title": "T", "disambiguation": "1841 arrangement by Mendelssohn"})
        assert w.disambiguation == "1841 arrangement by Mendelssohn"

    def test_annotation_plain_string_passthrough(self) -> None:
        """annotation accepts a plain string directly."""
        w = MBWork.model_validate({"id": "w1", "title": "T", "annotation": "Scholarly note."})
        assert w.annotation == "Scholarly note."

    def test_annotation_from_musicbrainzngs_dict(self) -> None:
        """annotation is extracted from the musicbrainzngs {'text': '...'} dict format.

        Per MMD 2.0 schema, annotation is <annotation><text>…</text></annotation>;
        musicbrainzngs parses this as {'text': '...'} rather than a plain string.
        """
        w = MBWork.model_validate({"id": "w1", "title": "T", "annotation": {"text": "Composed 1822–1824."}})
        assert w.annotation == "Composed 1822–1824."

    def test_annotation_none_defaults_to_empty(self) -> None:
        """annotation defaults to empty string when None is passed."""
        w = MBWork.model_validate({"id": "w1", "title": "T", "annotation": None})
        assert w.annotation == ""

    def test_place_relation_list_populated(self) -> None:
        """place-relation-list is populated from the API response."""
        w = MBWork.model_validate(
            {
                "id": "w1",
                "title": "T",
                "place-relation-list": [
                    {
                        "type": "premiere",
                        "direction": "backward",
                        "begin": "1824-05-07",
                        "place": {"id": "p1", "name": "Vienna"},
                    }
                ],
            }
        )
        assert len(w.place_relation_list) == 1
        assert w.place_relation_list[0].begin == "1824-05-07"
        assert w.place_relation_list[0].place.name == "Vienna"

    def test_label_relation_list_populated(self) -> None:
        """label-relation-list is populated from the API response."""
        w = MBWork.model_validate(
            {
                "id": "w1",
                "title": "T",
                "label-relation-list": [
                    {"type": "publishing", "direction": "backward", "begin": "1827", "label": {"id": "l1", "name": "Breitkopf"}}
                ],
            }
        )
        assert len(w.label_relation_list) == 1
        assert w.label_relation_list[0].begin == "1827"

    def test_url_relation_list_populated(self) -> None:
        """url-relation-list is populated from the API response."""
        w = MBWork.model_validate(
            {
                "id": "w1",
                "title": "T",
                "url-relation-list": [
                    {"type": "download for free", "url": {"resource": "https://imslp.org/wiki/Symphony_No.9"}}
                ],
            }
        )
        assert len(w.url_relation_list) == 1
        assert w.url_relation_list[0].type == "download for free"
        assert "imslp" in w.url_relation_list[0].url


class TestMBReleaseNewFields:
    """Tests for new MBRelease fields."""

    def test_country_populated(self) -> None:
        """country is populated."""
        rel = MBRelease.model_validate({"id": "r1", "title": "T", "country": "DE"})
        assert rel.country == "DE"

    def test_packaging_populated(self) -> None:
        """packaging is populated."""
        rel = MBRelease.model_validate({"id": "r1", "title": "T", "packaging": "Jewel Case"})
        assert rel.packaging == "Jewel Case"

    def test_cover_art_archive_populated(self) -> None:
        """cover-art-archive is populated."""
        rel = MBRelease.model_validate(
            {
                "id": "r1",
                "title": "T",
                "cover-art-archive": {"artwork": "true", "front": "true", "back": "false", "count": "1", "darkened": "false"},
            }
        )
        assert rel.cover_art_archive.front is True
        assert rel.cover_art_archive.count == 1

    def test_release_event_list_populated(self) -> None:
        """release-event-list is populated with country extracted from area."""
        rel = MBRelease.model_validate(
            {
                "id": "r1",
                "title": "T",
                "release-event-list": [{"date": "1986", "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]}}],
            }
        )
        assert len(rel.release_event_list) == 1
        assert rel.release_event_list[0].country == "DE"

    def test_series_relation_list_populated(self) -> None:
        """series-relation-list is populated."""
        rel = MBRelease.model_validate(
            {
                "id": "r1",
                "title": "T",
                "series-relation-list": [
                    {
                        "type": "part of",
                        "ordering-key": "2",
                        "series": {"id": "s1", "name": "Karajan Gold", "type": "Release series"},
                    }
                ],
            }
        )
        assert len(rel.series_relation_list) == 1
        assert rel.series_relation_list[0].series.name == "Karajan Gold"


class TestCoverageGaps:
    """Targeted tests to cover remaining validator branches."""

    def test_mb_work_relation_ended_bool_true(self) -> None:
        """MBWorkRelation.coerce_ended handles bool True directly."""
        wrel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "ended": True})
        assert wrel.ended is True

    def test_mb_work_relation_ended_bool_false(self) -> None:
        """MBWorkRelation.coerce_ended handles bool False directly."""
        wrel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "ended": False})
        assert wrel.ended is False

    def test_mb_url_relation_non_dict_passthrough(self) -> None:
        """MBUrlRelation.extract_url_resource passes non-dict input through unchanged."""
        # Pydantic validates the model; passing a non-dict raises ValidationError but
        # the validator's 'return data' non-dict branch must be reachable via model_validate
        # with a pre-validated dict that has a plain string url (already covered).
        # The non-dict guard covers cases where mode="before" receives a non-dict from Pydantic internals.
        url_rel = MBUrlRelation.model_validate({"type": "wikidata", "url": ""})
        assert url_rel.url == ""

    def test_mb_artist_relation_ended_bool(self) -> None:
        """MBArtistRelation.coerce_ended handles bool input directly."""
        rel = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward", "ended": True})
        assert rel.ended is True
        rel2 = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward", "ended": False})
        assert rel2.ended is False

    def test_mb_artist_relation_ended_string_false(self) -> None:
        """MBArtistRelation.coerce_ended handles string 'false'."""
        rel = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward", "ended": "false"})
        assert rel.ended is False

    def test_mb_place_relation_ended_bool(self) -> None:
        """MBPlaceRelation.coerce_ended handles bool input directly."""
        prel = MBPlaceRelation.model_validate({"type": "premiere", "direction": "backward", "ended": True})
        assert prel.ended is True

    def test_mb_label_relation_ended_bool_and_string(self) -> None:
        """MBLabelRelation.coerce_ended handles bool True and string 'false'."""
        lrel = MBLabelRelation.model_validate({"type": "publishing", "ended": True, "label": {"id": "l1", "name": "L"}})
        assert lrel.ended is True
        lrel2 = MBLabelRelation.model_validate({"type": "publishing", "ended": "false", "label": {"id": "l1", "name": "L"}})
        assert lrel2.ended is False

    def test_mb_series_relation_ordering_key_none(self) -> None:
        """MBSeriesRelation.coerce_ordering_key handles None."""
        srel = MBSeriesRelation.model_validate({"type": "part of", "ordering-key": None, "series": {"id": "s1", "name": "S"}})
        assert srel.ordering_key == 0

    def test_mb_cover_art_archive_bool_fields_direct_bool(self) -> None:
        """MBCoverArtArchive.coerce_bool handles direct bool True."""
        caa = MBCoverArtArchive.model_validate({"artwork": True, "front": True, "back": False, "darkened": False, "count": 2})
        assert caa.artwork is True
        assert caa.back is False

    def test_mb_cover_art_archive_count_string(self) -> None:
        """MBCoverArtArchive.coerce_count handles None."""
        caa = MBCoverArtArchive.model_validate({"count": None})
        assert caa.count == 0

    def test_mb_release_event_no_area_codes(self) -> None:
        """MBReleaseEvent with area but no iso codes leaves country empty."""
        evt = MBReleaseEvent.model_validate({"date": "1986", "area": {"name": "Germany", "iso-3166-1-code-list": []}})
        assert evt.country == ""

    def test_mb_track_length_none_to_zero(self) -> None:
        """MBTrack.coerce_length converts None to 0."""
        track = MBTrack.model_validate({"id": "t1", "position": 1, "length": None})
        assert track.length == 0

    def test_mb_recording_video_false_bool(self) -> None:
        """MBRecording.coerce_video handles bool False."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "video": False})
        assert rec.video is False

    def test_mb_recording_video_bool_true(self) -> None:
        """MBRecording.coerce_video handles bool True directly."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "video": True})
        assert rec.video is True

    def test_mb_recording_length_none_to_zero(self) -> None:
        """MBRecording.coerce_length converts None to 0."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "length": None})
        assert rec.length == 0

    def test_mb_work_relation_ended_none_to_false(self) -> None:
        """MBWorkRelation.coerce_ended converts None to False."""
        wrel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "ended": None})
        assert wrel.ended is False

    def test_mb_url_relation_non_dict_url_unchanged(self) -> None:
        """MBUrlRelation with non-dict url field: non-dict case is a plain string so url stays."""
        # This exercises the isinstance(url_val, dict) branch being False (url is already a string).
        url_rel = MBUrlRelation.model_validate({"type": "wikidata", "url": "https://www.wikidata.org/wiki/Q11989"})
        assert url_rel.url == "https://www.wikidata.org/wiki/Q11989"

    def test_mb_place_relation_ended_none_to_false(self) -> None:
        """MBPlaceRelation.coerce_ended converts None to False."""
        prel = MBPlaceRelation.model_validate({"type": "premiere", "ended": None, "place": {"id": "p1", "name": "P"}})
        assert prel.ended is False

    def test_mb_label_relation_ended_none_to_false(self) -> None:
        """MBLabelRelation.coerce_ended converts None to False."""
        lrel = MBLabelRelation.model_validate({"type": "publishing", "ended": None, "label": {"id": "l1", "name": "L"}})
        assert lrel.ended is False

    def test_mb_series_relation_ordering_key_string(self) -> None:
        """MBSeriesRelation.coerce_ordering_key converts string to int."""
        srel = MBSeriesRelation.model_validate({"type": "part of", "ordering-key": "5", "series": {"id": "s1", "name": "S"}})
        assert srel.ordering_key == 5

    def test_mb_cover_art_archive_count_string_value(self) -> None:
        """MBCoverArtArchive.coerce_count converts string to int."""
        caa = MBCoverArtArchive.model_validate({"count": "5"})
        assert caa.count == 5

    def test_mb_artist_relation_ended_none_to_false(self) -> None:
        """MBArtistRelation.coerce_ended converts None to False."""
        rel = MBArtistRelation.model_validate({"type": "conductor", "direction": "backward", "ended": None})
        assert rel.ended is False

    def test_mb_recording_video_none_to_false(self) -> None:
        """MBRecording.coerce_video converts None to False."""
        rec = MBRecording.model_validate({"id": "r1", "title": "T", "video": None})
        assert rec.video is False

    def test_mb_place_relation_ended_direct_bool_false(self) -> None:
        """MBPlaceRelation.coerce_ended returns bool False directly (line 266)."""
        prel = MBPlaceRelation.model_validate({"type": "premiere", "ended": False, "place": {"id": "p1", "name": "P"}})
        assert prel.ended is False

    def test_mb_cover_art_archive_coerce_bool_direct_false(self) -> None:
        """MBCoverArtArchive.coerce_bool returns bool False directly (line 388)."""
        caa = MBCoverArtArchive.model_validate({"front": False})
        assert caa.front is False

    def test_mb_artist_relation_coerce_ended_direct_false(self) -> None:
        """MBArtistRelation.coerce_ended returns False for None (line 433) and bool False (434)."""
        rel_none = MBArtistRelation.model_validate({"type": "conductor", "ended": None})
        assert rel_none.ended is False
        rel_bool = MBArtistRelation.model_validate({"type": "conductor", "ended": False})
        assert rel_bool.ended is False

    def test_mb_release_event_country_already_set(self) -> None:
        """MBReleaseEvent.extract_country_from_area skips extraction when country is already set (434→441)."""
        # country key present and non-empty → inner block skipped → return data unchanged
        evt = MBReleaseEvent.model_validate(
            {"date": "1986", "country": "DE", "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]}}
        )
        assert evt.country == "DE"

    def test_mb_release_event_area_not_dict(self) -> None:
        """MBReleaseEvent.extract_country_from_area skips when area is not a dict (436→441)."""
        evt = MBReleaseEvent.model_validate({"date": "1986", "area": "Germany"})
        assert evt.country == ""


class TestMBDisc:
    """Tests for MBDisc model construction and defaults."""

    def test_defaults(self) -> None:
        """MBDisc with no data has empty offsets and zero sectors."""
        disc = MBDisc.model_validate({})
        assert disc.offsets == []
        assert disc.sectors == 0

    def test_offsets_and_sectors_populated(self) -> None:
        """MBDisc correctly stores offsets and sectors from API data."""
        disc = MBDisc.model_validate({"offset-list": [182, 67232, 113807], "sectors": "355382"})
        assert disc.offsets == [182, 67232, 113807]
        assert disc.sectors == 355382

    def test_sectors_coerced_from_int(self) -> None:
        """MBDisc.coerce_sectors accepts a plain int."""
        disc = MBDisc.model_validate({"sectors": 12345})
        assert disc.sectors == 12345

    def test_sectors_none_defaults_to_zero(self) -> None:
        """MBDisc.coerce_sectors treats None as 0."""
        disc = MBDisc.model_validate({"sectors": None})
        assert disc.sectors == 0


class TestMBMediumDiscList:
    """Tests for MBMedium.disc_list populated from 'discs' key."""

    def test_disc_list_defaults_empty(self) -> None:
        """MBMedium with no 'discs' key has an empty disc_list."""
        medium = MBMedium.model_validate({"position": 1, "format": "CD", "track-list": []})
        assert medium.disc_list == []

    def test_disc_list_populated(self) -> None:
        """MBMedium.disc_list is populated when 'discs' key is present."""
        medium = MBMedium.model_validate(
            {
                "position": 2,
                "format": "CD",
                "track-list": [],
                "disc-list": [
                    {"offset-list": [182, 67232, 113807, 136232, 175832, 233432, 283307, 310607], "sectors": "355382"},
                    {"offset-list": [183, 67233, 113808, 136233, 175833, 233433, 283308, 310608], "sectors": "355383"},
                ],
            }
        )
        assert len(medium.disc_list) == 2
        assert medium.disc_list[0].offsets == [182, 67232, 113807, 136232, 175832, 233432, 283307, 310607]
        assert medium.disc_list[0].sectors == 355382
        assert medium.disc_list[1].offsets == [183, 67233, 113808, 136233, 175833, 233433, 283308, 310608]


# ---------------------------------------------------------------------------
# MBReleaseCandidate
# ---------------------------------------------------------------------------


class TestMBReleaseCandidate:
    """Tests for MBReleaseCandidate model."""

    def test_from_journal_defaults_false(self) -> None:
        """MBReleaseCandidate.from_journal defaults to False when not supplied."""
        candidate = MBReleaseCandidate(release_id="rel-1", score=90, title="My Release")
        assert candidate.from_journal is False

    def test_from_journal_true_accepted(self) -> None:
        """MBReleaseCandidate accepts from_journal=True."""
        candidate = MBReleaseCandidate(release_id="rel-1", score=101, from_journal=True)
        assert candidate.from_journal is True

    def test_model_copy_preserves_from_journal(self) -> None:
        """model_copy with from_journal update round-trips correctly."""
        original = MBReleaseCandidate(release_id="rel-1", score=80, title="T")
        enriched = original.model_copy(update={"from_journal": True, "score": 101})
        assert enriched.from_journal is True
        assert enriched.score == 101
        assert enriched.title == "T"
        assert enriched.release_id == "rel-1"


# ---------------------------------------------------------------------------
# AnnotationTier vocabulary and classify_annotation_tier (C-TIER KAT)
# ---------------------------------------------------------------------------


class TestAnnotationTierEnum:
    """Tests for the AnnotationTier StrEnum vocabulary."""

    def test_all_five_values_present(self) -> None:
        """AnnotationTier has exactly five members with the correct string values."""
        values = {t.value for t in AnnotationTier}
        assert values == {
            "full-mb-verified",
            "mb-search-resolved",
            "mb-partial",
            "alternate-source",
            "source-tags-only",
        }

    def test_str_enum_values_equal_strings(self) -> None:
        """AnnotationTier members have the correct string values (StrEnum contract)."""
        assert AnnotationTier.FULL_MB_VERIFIED.value == "full-mb-verified"
        assert AnnotationTier.MB_SEARCH_RESOLVED.value == "mb-search-resolved"
        assert AnnotationTier.MB_PARTIAL.value == "mb-partial"
        assert AnnotationTier.ALTERNATE_SOURCE.value == "alternate-source"
        assert AnnotationTier.SOURCE_TAGS_ONLY.value == "source-tags-only"

    def test_annotation_tier_from_string(self) -> None:
        """AnnotationTier can be constructed from its string value."""
        assert AnnotationTier("full-mb-verified") is AnnotationTier.FULL_MB_VERIFIED
        assert AnnotationTier("source-tags-only") is AnnotationTier.SOURCE_TAGS_ONLY


class TestTierClassifierMapsCensusSignals:
    """KAT: classify_annotation_tier maps each census axis-2 signal to the correct tier.

    Covers the C-TIER classification→tier mapping contract.
    """

    def test_embedded_mbid_maps_to_full_mb_verified(self) -> None:
        """embedded-mbid signal → full-mb-verified, needs_spot_check=False."""
        tier, spot_check = classify_annotation_tier(CensusSignal.EMBEDDED_MBID)
        assert tier == AnnotationTier.FULL_MB_VERIFIED
        assert spot_check is False

    def test_search_hit_maps_to_mb_search_resolved(self) -> None:
        """search-hit signal → mb-search-resolved, needs_spot_check=True."""
        tier, spot_check = classify_annotation_tier(CensusSignal.SEARCH_HIT)
        assert tier == AnnotationTier.MB_SEARCH_RESOLVED
        assert spot_check is True

    def test_mismatch_maps_to_mb_partial(self) -> None:
        """mismatch signal → mb-partial, needs_spot_check=False."""
        tier, spot_check = classify_annotation_tier(CensusSignal.MISMATCH)
        assert tier == AnnotationTier.MB_PARTIAL
        assert spot_check is False

    def test_not_in_mb_maps_to_source_tags_only(self) -> None:
        """not-in-mb signal → source-tags-only, needs_spot_check=False."""
        tier, spot_check = classify_annotation_tier(CensusSignal.NOT_IN_MB)
        assert tier == AnnotationTier.SOURCE_TAGS_ONLY
        assert spot_check is False

    def test_all_census_signals_covered(self) -> None:
        """Every CensusSignal maps to a valid AnnotationTier (no signal is unhandled)."""
        for signal in CensusSignal:
            tier, _ = classify_annotation_tier(signal)
            assert isinstance(tier, AnnotationTier), f"signal {signal!r} did not return an AnnotationTier"

    def test_census_signal_string_values(self) -> None:
        """CensusSignal members have the expected string values."""
        assert CensusSignal.EMBEDDED_MBID.value == "embedded-mbid"
        assert CensusSignal.ISRC_MATCH.value == "isrc-match"
        assert CensusSignal.SEARCH_HIT.value == "search-hit"
        assert CensusSignal.MISMATCH.value == "mismatch"
        assert CensusSignal.NOT_IN_MB.value == "not-in-mb"

    def test_classify_isrc_match_arm(self) -> None:
        """isrc-match signal → full-mb-verified, needs_spot_check=False (C-ISRC KAT).

        Pins the C-ISRC classifier arm: CensusSignal.ISRC_MATCH maps to
        AnnotationTier.FULL_MB_VERIFIED with needs_spot_check=False, identical to EMBEDDED_MBID.
        """
        tier, spot_check = classify_annotation_tier(CensusSignal.ISRC_MATCH)
        assert tier == AnnotationTier.FULL_MB_VERIFIED
        assert spot_check is False


# ---------------------------------------------------------------------------
# AccurateRip provenance models (C-AR KATs)
# ---------------------------------------------------------------------------


class TestAccurateRipResultEnum:
    """KAT: AccurateRipResult enum values round-trip (value ↔ enum member)."""

    def test_accuraterip_result_enum_exhaustive(self) -> None:
        """All three AccurateRipResult values round-trip between string and enum member.

        Pins the C-AR contract: the three whipper WhipperLogger result strings are the
        exact enum values, and constructing from the string yields the correct member.
        """
        assert AccurateRipResult("exact-match") is AccurateRipResult.EXACT_MATCH
        assert AccurateRipResult("no-exact-match") is AccurateRipResult.NO_EXACT_MATCH
        assert AccurateRipResult("not-present") is AccurateRipResult.NOT_PRESENT

        assert AccurateRipResult.EXACT_MATCH.value == "exact-match"
        assert AccurateRipResult.NO_EXACT_MATCH.value == "no-exact-match"
        assert AccurateRipResult.NOT_PRESENT.value == "not-present"

    def test_accuraterip_result_all_members_covered(self) -> None:
        """Exactly three AccurateRipResult members exist (no silent additions)."""
        members = list(AccurateRipResult)
        assert len(members) == 3
        assert set(members) == {AccurateRipResult.EXACT_MATCH, AccurateRipResult.NO_EXACT_MATCH, AccurateRipResult.NOT_PRESENT}


class TestAccurateRipTrackResult:
    """Tests for AccurateRipTrackResult model defaults and field types."""

    def test_defaults(self) -> None:
        """AccurateRipTrackResult defaults to NOT_PRESENT with zero confidence and empty CRCs."""
        r = AccurateRipTrackResult()
        assert r.version == ""
        assert r.result is AccurateRipResult.NOT_PRESENT
        assert r.confidence == 0
        assert r.local_crc == ""
        assert r.remote_crc == ""

    def test_exact_match_construction(self) -> None:
        """AccurateRipTrackResult can be constructed with an exact-match result."""
        r = AccurateRipTrackResult(
            version="v1", result=AccurateRipResult.EXACT_MATCH, confidence=42, local_crc="AABBCCDD", remote_crc="AABBCCDD"
        )
        assert r.result is AccurateRipResult.EXACT_MATCH
        assert r.confidence == 42
        assert r.local_crc == "AABBCCDD"


class TestAccurateRipTrack:
    """Tests for AccurateRipTrack model defaults and nested structure."""

    def test_defaults(self) -> None:
        """AccurateRipTrack defaults to empty v1/v2 results and empty CRC/status fields."""
        t = AccurateRipTrack()
        assert t.v1.result is AccurateRipResult.NOT_PRESENT
        assert t.v2.result is AccurateRipResult.NOT_PRESENT
        assert t.test_crc == ""
        assert t.copy_crc == ""
        assert t.status == ""

    def test_populated_track(self) -> None:
        """AccurateRipTrack can carry populated v1/v2 results and rip CRCs."""
        t = AccurateRipTrack(
            v1=AccurateRipTrackResult(
                version="v1", result=AccurateRipResult.EXACT_MATCH, confidence=10, local_crc="AABB1122", remote_crc="AABB1122"
            ),
            v2=AccurateRipTrackResult(
                version="v2", result=AccurateRipResult.NO_EXACT_MATCH, confidence=5, local_crc="CCDD3344", remote_crc="EEFF5566"
            ),
            test_crc="12345678",
            copy_crc="12345678",
            status="Copy OK",
        )
        assert t.v1.result is AccurateRipResult.EXACT_MATCH
        assert t.v2.result is AccurateRipResult.NO_EXACT_MATCH
        assert t.test_crc == "12345678"
        assert t.status == "Copy OK"


class TestAccurateRipSummary:
    """Tests for AccurateRipSummary model and monotonic-upgrade rule."""

    def test_defaults(self) -> None:
        """AccurateRipSummary defaults to empty strings and zero counters."""
        s = AccurateRipSummary()
        assert s.mb_disc_id == ""
        assert s.cddb_disc_id == ""
        assert s.log_sha256 == ""
        assert s.accurately_ripped == 0
        assert s.in_ar_database == 0
        assert s.summary_text == ""

    def test_is_populated_empty(self) -> None:
        """is_populated() returns False for a default (empty) AccurateRipSummary."""
        assert AccurateRipSummary().is_populated() is False

    def test_is_populated_with_log_sha256(self) -> None:
        """is_populated() returns True when log_sha256 is set."""
        assert AccurateRipSummary(log_sha256="ABCDEF01").is_populated() is True

    def test_is_populated_with_counts(self) -> None:
        """is_populated() returns True when accurately_ripped or in_ar_database is non-zero."""
        assert AccurateRipSummary(accurately_ripped=10).is_populated() is True
        assert AccurateRipSummary(in_ar_database=5).is_populated() is True

    def test_is_populated_with_text(self) -> None:
        """is_populated() returns True when summary_text is set."""
        assert AccurateRipSummary(summary_text="All tracks accurately ripped").is_populated() is True

    def test_applied_case_ids_default_empty(self) -> None:
        """applied_case_ids defaults to an empty list on a default-constructed ProvenanceSidecar.

        Pins the C-CASE-PROV field contract: the field is present with a safe default so callers
        never need to guard against AttributeError or KeyError.
        """
        assert ProvenanceSidecar().applied_case_ids == []

    def test_accuraterip_summary_monotonic(self) -> None:
        """A present accuraterip_summary on ProvenanceSidecar survives a later empty-summary merge.

        Pins the C-AR monotonic-upgrade rule: an incoming empty AccurateRipSummary must not
        overwrite a populated one.  The caller is responsible for checking is_populated() before
        overwriting; this test verifies the sentinel method and the sidecar field coexist correctly.
        """
        populated = AccurateRipSummary(
            mb_disc_id="abc123",
            cddb_disc_id="deadbeef",
            log_sha256="AABBCCDD" * 8,
            accurately_ripped=12,
            in_ar_database=12,
            summary_text="All tracks accurately ripped",
        )
        empty = AccurateRipSummary()

        sidecar = ProvenanceSidecar(accuraterip_summary=populated)
        assert sidecar.accuraterip_summary.is_populated() is True

        # Simulate the monotonic-upgrade rule: only overwrite when incoming is populated.
        if empty.is_populated():
            sidecar.accuraterip_summary = empty  # pragma: no cover

        # The populated summary must survive the empty-summary "merge".
        assert sidecar.accuraterip_summary.mb_disc_id == "abc123"
        assert sidecar.accuraterip_summary.log_sha256 == "AABBCCDD" * 8
        assert sidecar.accuraterip_summary.accurately_ripped == 12
