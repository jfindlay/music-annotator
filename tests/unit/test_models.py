"""Unit tests for music_annotator.models."""

from __future__ import annotations

import pytest

from music_annotator.models import (
    ArtistEntry,
    CeaPerformers,
    CoverArt,
    CwpTags,
    MBArtist,
    MBLabelInfo,
    MBRelease,
    MBWork,
    MBWorkRelation,
    MBWorkStub,
    RoleBuckets,
    TrackTags,
    WorkDates,
    WorkHierarchyLevel,
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


class TestCoverArt:
    """Tests for CoverArt.available property."""

    def test_available_true_with_data(self) -> None:
        """available is True when data is non-empty bytes."""
        c = CoverArt(data=b"\xff\xd8\xff\xe0", mime="image/jpeg")
        assert c.available is True

    def test_available_false_empty(self) -> None:
        """available is False when data is empty bytes (default)."""
        c = CoverArt()
        assert c.available is False

    def test_default_mime_empty(self) -> None:
        """mime defaults to empty string."""
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
    """Tests for MBWorkRelation.work typed access via MBWorkStub."""

    def test_work_id(self) -> None:
        """work.id returns the id from the nested work stub."""
        rel = MBWorkRelation.model_validate({"type": "parts", "work": {"id": "w-abc", "title": "Symphony"}})
        assert rel.work.id == "w-abc"

    def test_work_title(self) -> None:
        """work.title returns the title from the nested work stub."""
        rel = MBWorkRelation.model_validate({"type": "parts", "work": {"id": "w-abc", "title": "Symphony No. 1"}})
        assert rel.work.title == "Symphony No. 1"

    def test_work_id_empty_when_default(self) -> None:
        """work.id returns empty string when constructed with default MBWorkStub."""
        rel = MBWorkRelation(work=MBWorkStub())
        assert rel.work.id == ""

    def test_work_title_empty_when_default(self) -> None:
        """work.title returns empty string when constructed with default MBWorkStub."""
        rel = MBWorkRelation(work=MBWorkStub())
        assert rel.work.title == ""


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
