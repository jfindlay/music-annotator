"""Unit tests for music_annotator (pure-logic functions, no real I/O or MB API calls)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    artist_credit_phrase,
    artist_ids,
    artist_sort_names,
    build_cwp_tags,
    build_dest_path,
    build_track_tags,
    build_work_hierarchy,
    collect_work_dates,
    collect_work_tags_and_key,
    extract_work_artist_rels,
    is_choir,
    is_ensemble,
    is_orchestra,
    last_name,
    parse_year,
    period_for_year,
    safe_name,
    strip_common_prefix,
)
from music_annotator.models import (
    JSON,
    ArtistEntry,
    MBArtistCredit,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
    RoleBuckets,
    TrackTags,
)


def _w(d: dict[str, JSON]) -> MBWork:
    """Validate a raw work dict into an MBWork model.

    :param d: Raw dict matching the musicbrainzngs work response shape.
    :returns: An :class:`~music_annotator.models.MBWork` instance.
    """
    return MBWork.model_validate(d)


def _rec(d: dict[str, JSON]) -> MBRecording:
    """Validate a raw recording dict into an MBRecording model.

    :param d: Raw dict matching the musicbrainzngs recording response shape.
    :returns: An :class:`~music_annotator.models.MBRecording` instance.
    """
    return MBRecording.model_validate(d)


def _rel(d: dict[str, JSON]) -> MBRelease:
    """Validate a raw release dict into an MBRelease model.

    :param d: Raw dict matching the musicbrainzngs release response shape.
    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(d)


def _trk(d: dict[str, JSON]) -> MBTrack:
    """Validate a raw track dict into an MBTrack model.

    :param d: Raw dict matching the musicbrainzngs track response shape.
    :returns: An :class:`~music_annotator.models.MBTrack` instance.
    """
    return MBTrack.model_validate(d)


def _ac(items: list[JSON]) -> list[MBArtistCredit | str]:
    """Validate a raw artist-credit list into typed items.

    :param items: Raw artist-credit list from musicbrainzngs response.
    :returns: A list of :class:`~music_annotator.models.MBArtistCredit` or ``str`` items.
    """
    result: list[MBArtistCredit | str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            result.append(MBArtistCredit.model_validate(item))
    return result


# ---------------------------------------------------------------------------
# is_ensemble / is_choir / is_orchestra
# ---------------------------------------------------------------------------


class TestIsEnsemble:
    """Tests for is_ensemble, is_choir, is_orchestra."""

    @pytest.mark.parametrize(
        "name",
        [
            "Berliner Philharmoniker",
            "Chicago Symphony Orchestra",
            "Vienna Philharmonic",
            "Berliner Philharmoniker",
            "Academy of St Martin in the Fields",
        ],
    )
    def test_orchestra_names_are_ensembles(self, name: str) -> None:
        """Orchestra names should be recognised as ensembles.

        :param name: Artist name to test.
        """
        assert is_ensemble(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Westminster Abbey Choir",
            "Stockholm Chamber Chorus",
            "The Sixteen Singers",
        ],
    )
    def test_choir_names_are_ensembles(self, name: str) -> None:
        """Choir names should be recognised as ensembles.

        :param name: Artist name to test.
        """
        assert is_ensemble(name)
        assert is_choir(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Herbert von Karajan",
            "Anne-Sophie Mutter",
            "Yo-Yo Ma",
        ],
    )
    def test_person_names_not_ensembles(self, name: str) -> None:
        """Person names should not be identified as ensembles.

        :param name: Artist name to test.
        """
        assert not is_ensemble(name)

    def test_is_orchestra_positive(self) -> None:
        """is_orchestra detects 'philharmonic'."""
        assert is_orchestra("Vienna Philharmonic")

    def test_is_orchestra_negative(self) -> None:
        """is_orchestra returns False for a choir name."""
        assert not is_orchestra("Westminster Choir")

    def test_is_choir_negative(self) -> None:
        """is_choir returns False for an orchestra name."""
        assert not is_choir("Berlin Philharmoniker")


# ---------------------------------------------------------------------------
# artist_credit_phrase
# ---------------------------------------------------------------------------


class TestArtistCreditPhrase:
    """Tests for artist_credit_phrase."""

    def test_single_artist(self) -> None:
        """Single artist credit with no join phrase."""
        credit = _ac([{"name": "Karajan", "artist": {"name": "Herbert von Karajan"}}])
        assert artist_credit_phrase(credit) == "Karajan"

    def test_join_phrase_string(self) -> None:
        """Join phrases (strings between dicts) are included."""
        credit = _ac(
            [
                {"name": "Karajan", "artist": {"name": "Karajan"}},
                " & ",
                {"name": "Berliner Philharmoniker", "artist": {"name": "Berliner Philharmoniker"}},
            ]
        )
        assert artist_credit_phrase(credit) == "Karajan & Berliner Philharmoniker"

    def test_empty_list(self) -> None:
        """Empty credit list returns empty string."""
        assert artist_credit_phrase([]) == ""

    def test_falls_back_to_artist_name(self) -> None:
        """When no 'name' key in item, falls back to artist.name."""
        credit = _ac([{"artist": {"name": "Fallback Artist"}}])
        assert artist_credit_phrase(credit) == "Fallback Artist"


# ---------------------------------------------------------------------------
# artist_ids / artist_sort_names
# ---------------------------------------------------------------------------


class TestArtistIds:
    """Tests for artist_ids and artist_sort_names."""

    def test_artist_ids_basic(self) -> None:
        """artist_ids extracts mbids from credit list."""
        credit = _ac(
            [
                {"artist": {"id": "id1", "name": "A"}},
                " & ",
                {"artist": {"id": "id2", "name": "B"}},
            ]
        )
        assert artist_ids(credit) == ["id1", "id2"]

    def test_artist_ids_skips_strings(self) -> None:
        """artist_ids ignores string join-phrase entries."""
        credit = _ac([" feat. "])
        assert artist_ids(credit) == []

    def test_artist_sort_names_basic(self) -> None:
        """artist_sort_names extracts sort-name from credit list."""
        credit = _ac(
            [
                {"artist": {"id": "x", "name": "Karajan", "sort-name": "Karajan, Herbert von"}},
            ]
        )
        assert artist_sort_names(credit) == ["Karajan, Herbert von"]

    def test_artist_sort_names_fallback(self) -> None:
        """artist_sort_names falls back to name when sort-name absent."""
        credit = _ac(
            [
                {"artist": {"id": "x", "name": "Ensemble X"}},
            ]
        )
        assert artist_sort_names(credit) == ["Ensemble X"]

    def test_artist_sort_names_skips_dict_without_artist(self) -> None:
        """artist_sort_names skips dict entries that have no 'artist' key."""
        # A dict with only joinphrase maps to MBArtistCredit with empty artist → skipped
        credit = _ac([{"joinphrase": " & "}, {"artist": {"id": "x", "name": "Solo"}}])
        assert artist_sort_names(credit) == ["Solo"]


# ---------------------------------------------------------------------------
# last_name
# ---------------------------------------------------------------------------


class TestLastName:
    """Tests for last_name."""

    @pytest.mark.parametrize(
        ("sort_name", "expected"),
        [
            ("Respighi, Ottorino", "Respighi"),
            ("Karajan, Herbert von", "Karajan"),
            ("Madonna", "Madonna"),
            ("", ""),
            ("Bach, Johann Sebastian", "Bach"),
        ],
    )
    def test_last_name(self, sort_name: str, expected: str) -> None:
        """last_name returns the portion before the first comma.

        :param sort_name: Sort-name string to test.
        :param expected: Expected last name.
        """
        assert last_name(sort_name) == expected


# ---------------------------------------------------------------------------
# strip_common_prefix
# ---------------------------------------------------------------------------


class TestStripCommonPrefix:
    """Tests for strip_common_prefix."""

    def test_strips_parent_prefix(self) -> None:
        """Removes parent text from child when child starts with parent."""
        child = "Fontane di Roma, P 106: I. La fontana di Valle Giulia all'alba"
        parent = "Fontane di Roma, P 106"
        result = strip_common_prefix(child, parent)
        assert result == "I. La fontana di Valle Giulia all'alba"

    def test_colon_fallback(self) -> None:
        """When no prefix match, returns text after first colon."""
        child = "Symphony No. 1: I. Allegro"
        parent = "Beethoven Works"
        result = strip_common_prefix(child, parent)
        assert result == "I. Allegro"

    def test_no_match_returns_child(self) -> None:
        """When no prefix and no colon, returns child unchanged."""
        child = "Adagio"
        parent = "Fontane di Roma"
        result = strip_common_prefix(child, parent)
        assert result == "Adagio"

    def test_empty_parent_returns_child(self) -> None:
        """Empty parent returns child unchanged."""
        assert strip_common_prefix("My Title", "") == "My Title"

    def test_empty_child_returns_child(self) -> None:
        """Empty child returns empty string unchanged."""
        assert strip_common_prefix("", "Parent") == ""

    def test_case_insensitive_match(self) -> None:
        """Prefix matching is case-insensitive."""
        child = "fontane di roma: I. Movement"
        parent = "Fontane di Roma"
        result = strip_common_prefix(child, parent)
        assert result == "I. Movement"

    def test_strips_leading_punctuation(self) -> None:
        """Leading space, colon, dash after stripping prefix are removed."""
        child = "Work A: - I. First"
        parent = "Work A"
        result = strip_common_prefix(child, parent)
        assert result == "I. First"


# ---------------------------------------------------------------------------
# period_for_year
# ---------------------------------------------------------------------------


class TestPeriodForYear:
    """Tests for period_for_year."""

    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (None, ""),
            (1916, "20th Century"),
            # Boundary years fall in the EARLIER range because first match wins
            (1750, "Baroque"),  # Baroque ends at 1750 (inclusive), Classical starts at 1750
            (1820, "Classical"),  # Classical ends at 1820, Early Romantic starts at 1800
            (800, "Early"),  # Early ends at 800 (inclusive), Medieval starts at 800
            (1600, "Renaissance"),  # Renaissance ends at 1600, Baroque starts at 1600
            (2000, "Contemporary"),
            (3000, ""),  # outside all ranges
        ],
    )
    def test_period_for_year(self, year: int | None, expected: str) -> None:
        """Maps year to correct period name.

        :param year: Input year (or None).
        :param expected: Expected period name.
        """
        assert period_for_year(year) == expected


# ---------------------------------------------------------------------------
# parse_year
# ---------------------------------------------------------------------------


class TestParseYear:
    """Tests for parse_year."""

    @pytest.mark.parametrize(
        ("date_str", "expected"),
        [
            ("1916", 1916),
            ("1916-03-11", 1916),
            ("", None),
            ("abc", None),
            ("20th century", None),
            ("2023-12-01", 2023),
        ],
    )
    def test_parse_year(self, date_str: str, expected: int | None) -> None:
        """Extracts the first four-digit year, or None.

        :param date_str: Input date string.
        :param expected: Expected year or None.
        """
        assert parse_year(date_str) == expected


# ---------------------------------------------------------------------------
# safe_name
# ---------------------------------------------------------------------------


class TestSafeName:
    """Tests for safe_name."""

    def test_replaces_forbidden_chars(self) -> None:
        """Characters forbidden in filenames are replaced with underscores."""
        assert safe_name('Hello: "World"') == "Hello_ _World_"

    def test_truncates_to_max_len(self) -> None:
        """String is truncated to max_len characters."""
        long_str = "A" * 200
        result = safe_name(long_str, max_len=80)
        assert len(result) == 80

    def test_strips_leading_trailing_dots_and_spaces(self) -> None:
        """Leading/trailing dots and spaces are stripped."""
        assert safe_name("  ..My Title..  ") == "My Title"

    def test_normal_string_unchanged(self) -> None:
        """A normal ASCII string is returned unchanged."""
        assert safe_name("Fontane di Roma") == "Fontane di Roma"

    def test_custom_max_len(self) -> None:
        """Custom max_len is respected."""
        assert safe_name("Hello World", max_len=5) == "Hello"


# ---------------------------------------------------------------------------
# collect_work_dates
# ---------------------------------------------------------------------------


class TestCollectWorkDates:
    """Tests for collect_work_dates."""

    def test_from_attribute_list_composed(self) -> None:
        """Reads composed date from attribute-list."""
        dates = collect_work_dates(_w({"attribute-list": [{"type": "composed date", "value": "1916"}]}))
        assert dates.composed == "1916"

    def test_from_life_span_begin(self) -> None:
        """Falls back to life-span.begin for composed date."""
        dates = collect_work_dates(_w({"life-span": {"begin": "1916-03-11"}}))
        assert dates.composed == "1916"

    def test_empty_work(self) -> None:
        """Empty work dict returns all-empty WorkDates."""
        dates = collect_work_dates(_w({}))
        assert dates.composed == ""
        assert dates.published == ""
        assert dates.premiered == ""

    def test_published_date_attribute(self) -> None:
        """Reads published date from attribute-list."""
        dates = collect_work_dates(_w({"attribute-list": [{"type": "published", "value": "1918"}]}))
        assert dates.published == "1918"

    def test_skips_string_attributes(self) -> None:
        """String entries in attribute-list are skipped without error."""
        dates = collect_work_dates(_w({"attribute-list": ["some string attribute"]}))
        assert dates.composed == ""


# ---------------------------------------------------------------------------
# collect_work_tags_and_key
# ---------------------------------------------------------------------------


class TestCollectWorkTagsAndKey:
    """Tests for collect_work_tags_and_key."""

    def test_returns_tag_names(self) -> None:
        """Returns list of tag names from tag-list."""
        tags, _ = collect_work_tags_and_key(
            _w(
                {
                    "tag-list": [{"name": "orchestral"}, {"name": "impressionism"}],
                }
            )
        )
        assert "orchestral" in tags
        assert "impressionism" in tags

    def test_key_from_work_field(self) -> None:
        """Returns key from top-level key field."""
        _, key = collect_work_tags_and_key(_w({"key": "G minor"}))
        assert key == "G minor"

    def test_key_from_attribute_list(self) -> None:
        """Returns key from attribute-list when top-level key absent."""
        _, key = collect_work_tags_and_key(
            _w(
                {
                    "attribute-list": [{"type": "key signature", "value": "D major"}],
                }
            )
        )
        assert key == "D major"

    def test_empty_work(self) -> None:
        """Empty work returns empty tags list and empty key."""
        tags, key = collect_work_tags_and_key(_w({}))
        assert tags == []
        assert key == ""


# ---------------------------------------------------------------------------
# extract_work_artist_rels
# ---------------------------------------------------------------------------


class TestExtractWorkArtistRels:
    """Tests for extract_work_artist_rels."""

    def test_composer_added(self) -> None:
        """Composer relation is added to role_buckets.composers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {"type": "composer", "artist": {"id": "c1", "name": "Respighi", "sort-name": "Respighi, Ottorino"}},
                    ]
                }
            ),
            rb,
        )
        assert len(rb.composers) == 1
        assert rb.composers[0].name == "Respighi"

    def test_arranger_added(self) -> None:
        """Arranger relation is added to role_buckets.arrangers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {"type": "arranger", "artist": {"id": "a1", "name": "Arranger A", "sort-name": "A, Arranger"}},
                    ]
                }
            ),
            rb,
        )
        assert len(rb.arrangers) == 1

    def test_deduplication_across_calls(self) -> None:
        """Same composer MBID from two levels is added only once."""
        rel: JSON = {"type": "composer", "artist": {"id": "c1", "name": "Respighi", "sort-name": "Respighi, Ottorino"}}
        rb = RoleBuckets()
        extract_work_artist_rels(_w({"artist-relation-list": [rel]}), rb)
        extract_work_artist_rels(_w({"artist-relation-list": [rel]}), rb)
        assert len(rb.composers) == 1

    def test_unknown_type_ignored(self) -> None:
        """Unknown relation type does not add to any bucket."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {"type": "some-unknown-role", "artist": {"id": "x1", "name": "X", "sort-name": "X"}},
                    ]
                }
            ),
            rb,
        )
        for role in ("composers", "lyricists", "arrangers", "orchestrators"):
            assert not getattr(rb, role)


# ---------------------------------------------------------------------------
# build_work_hierarchy (with mocked fetch_work_detail)
# ---------------------------------------------------------------------------


class TestBuildWorkHierarchy:
    """Tests for build_work_hierarchy with mocked fetch_work_detail."""

    def test_single_level(self) -> None:
        """A work with no parents returns a single-element list."""
        result = build_work_hierarchy(_w({"id": "w1", "title": "Adagio", "work-relation-list": []}))
        assert len(result) == 1
        assert result[0].id == "w1"

    def test_two_levels(self, mocker: MockerFixture) -> None:
        """A work with one parent returns a two-element list.

        :param mocker: pytest-mock fixture.
        """
        parent = _w({"id": "w2", "title": "Symphony No. 1", "work-relation-list": []})
        child = _w(
            {
                "id": "w1",
                "title": "I. Allegro",
                "work-relation-list": [
                    {"direction": "backward", "type": "parts", "work": {"id": "w2", "title": "Symphony No. 1"}},
                ],
            }
        )
        mocker.patch("music_annotator.fetch_work_detail", return_value=parent)
        result = build_work_hierarchy(child)
        assert len(result) == 2
        assert result[0].id == "w1"
        assert result[1].id == "w2"

    def test_cycle_protection(self) -> None:
        """Circular parent references are detected and do not cause infinite recursion."""
        work = _w(
            {
                "id": "w1",
                "title": "Cyclic Work",
                "work-relation-list": [
                    {"direction": "backward", "type": "parts", "work": {"id": "w1", "title": "Cyclic Work"}},
                ],
            }
        )
        # Should not raise; visited set prevents re-entry
        result = build_work_hierarchy(work)
        assert result[0].id == "w1"


# ---------------------------------------------------------------------------
# build_cwp_tags
# ---------------------------------------------------------------------------


class TestBuildCwpTags:
    """Tests for build_cwp_tags."""

    def test_empty_hierarchy(self) -> None:
        """Empty hierarchy returns default CwpTags."""
        rb = RoleBuckets()
        cwp = build_cwp_tags([], rb)
        assert cwp.work_top == ""
        assert cwp.levels == []

    def test_single_level_work(self) -> None:
        """Single-level work populates work and groupheading."""
        rb = RoleBuckets()
        rb.add_unique("composers", ArtistEntry(name="Respighi", sort="Respighi, Ottorino", mbid="r1"))
        cwp = build_cwp_tags(
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Fontane di Roma",
                        "type": "Symphonic poem",
                        "work-relation-list": [],
                        "attribute-list": [{"type": "composed date", "value": "1916"}],
                        "tag-list": [],
                    }
                )
            ],
            rb,
        )
        assert cwp.work_top == "Fontane di Roma"
        assert cwp.workid_top == "w1"
        assert cwp.part_levels == 0
        assert cwp.composed_dates == "1916"
        assert cwp.period == "20th Century"
        assert len(cwp.levels) == 1
        assert cwp.composers == "Respighi"
        assert cwp.composer_lastnames == "Respighi"

    def test_two_level_work(self) -> None:
        """Two-level work populates work, groupheading, and part."""
        rb = RoleBuckets()
        cwp = build_cwp_tags(
            [
                _w(
                    {
                        "id": "m1",
                        "title": "Fontane di Roma: I. Valle Giulia all'alba",
                        "type": "",
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                ),
                _w(
                    {
                        "id": "p1",
                        "title": "Fontane di Roma",
                        "type": "Symphonic poem",
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                ),
            ],
            rb,
        )
        assert cwp.work_top == "Fontane di Roma"
        assert cwp.part_levels == 1
        assert cwp.part != ""  # stripped part name should be non-empty

    def test_three_level_inter_work(self) -> None:
        """Three-level hierarchy populates inter_work from the middle level."""
        rb = RoleBuckets()
        cwp = build_cwp_tags(
            [
                _w({"id": "l0", "title": "Suite: I. Allegro", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
                _w({"id": "l1", "title": "Suite Movements", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
                _w({"id": "l2", "title": "Suite", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
            ],
            rb,
        )
        assert cwp.part_levels == 2
        assert cwp.inter_work != ""


# ---------------------------------------------------------------------------
# build_dest_path
# ---------------------------------------------------------------------------


class TestBuildDestPath:
    """Tests for build_dest_path."""

    def _make_tags(self, **kwargs: str) -> TrackTags:
        """Create a minimal TrackTags with required movement fields.

        :param kwargs: Additional keyword arguments to pass to TrackTags.
        :returns: A TrackTags instance with movementnumber and movementtotal set.
        """
        return TrackTags(
            title=kwargs.get("title", "I. Allegro"),
            movementnumber=kwargs.get("movementnumber", "1"),
            movementtotal=kwargs.get("movementtotal", "4"),
            cwp_work_top=kwargs.get("cwp_work_top", "Symphony No. 1"),
            cwp_workid_top=kwargs.get("cwp_workid_top", "work-uuid-1"),
            cwp_composer_lastnames=kwargs.get("cwp_composer_lastnames", "Beethoven"),
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )

    def test_returns_path_under_dest_root(self, fs: FakeFilesystem) -> None:
        """Returned path is under dest_root.

        :param fs: pyfakefs filesystem fixture.
        """
        dest_root = Path("/music_lib")
        fs.create_dir(str(dest_root))
        result = build_dest_path(
            dest_root,
            _rel({"id": "rel-1", "title": "Beethoven: Symphonies", "artist-credit": [], "medium-list": []}),
            _trk({"id": "trk-1", "position": 1, "recording": {"id": "rec-1", "title": "I. Allegro"}}),
            self._make_tags(),
        )
        assert str(result).startswith("/music_lib")

    def test_path_contains_work_title(self, fs: FakeFilesystem) -> None:
        """Path contains a component derived from the work title.

        :param fs: pyfakefs filesystem fixture.
        """
        dest_root = Path("/music_lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags(cwp_work_top="Fontane di Roma", cwp_workid_top="work-uuid-1")
        result = build_dest_path(
            dest_root,
            _rel({"id": "rel-1", "title": "Album", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "I. Allegro"}}),
            tags,
        )
        assert "Fontane di Roma" in str(result)

    def test_path_contains_movement_number_prefix(self, fs: FakeFilesystem) -> None:
        """Filename starts with zero-padded movement number.

        :param fs: pyfakefs filesystem fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags(movementnumber="3", movementtotal="4", title="III. Scherzo")
        result = build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Album", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 3, "recording": {"id": "rec1", "title": "III. Scherzo"}}),
            tags,
        )
        assert result.name.startswith("03")

    def test_three_digit_prefix_for_large_work(self, fs: FakeFilesystem) -> None:
        """Movement number prefix is 3 digits when total > 99.

        :param fs: pyfakefs filesystem fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags(movementnumber="5", movementtotal="120", title="Track 5")
        result = build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Album", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 5, "recording": {"id": "rec1", "title": "Track 5"}}),
            tags,
        )
        assert result.name.startswith("005")


# ---------------------------------------------------------------------------
# init_mb
# ---------------------------------------------------------------------------


class TestInitMb:
    """Tests for init_mb user-agent parsing."""

    def test_parses_app_version_contact(self, mocker: MockerFixture) -> None:
        """init_mb calls mb.set_useragent with app, version, contact.

        :param mocker: pytest-mock fixture.
        """
        mock_set = mocker.patch("music_annotator.mb.set_useragent")
        music_annotator.init_mb("MyApp/2.0 contact@example.com")
        mock_set.assert_called_once_with("MyApp", "2.0", "contact@example.com")

    def test_parses_no_contact(self, mocker: MockerFixture) -> None:
        """init_mb handles user-agent without contact string.

        :param mocker: pytest-mock fixture.
        """
        mock_set = mocker.patch("music_annotator.mb.set_useragent")
        music_annotator.init_mb("MyApp/1.0")
        mock_set.assert_called_once_with("MyApp", "1.0", "")

    def test_parses_no_slash(self, mocker: MockerFixture) -> None:
        """init_mb handles user-agent without any slash.

        :param mocker: pytest-mock fixture.
        """
        mock_set = mocker.patch("music_annotator.mb.set_useragent")
        music_annotator.init_mb("MyApp")
        mock_set.assert_called_once_with("MyApp", "1.0", "")


# ---------------------------------------------------------------------------
# collect_work_dates — premiered branch
# ---------------------------------------------------------------------------


class TestCollectWorkDatesPremiered:
    """Tests for the premiered attribute branch of collect_work_dates."""

    def test_premiered_date_attribute(self) -> None:
        """Reads premiered date from attribute-list when type contains 'premiered'."""
        dates = music_annotator.collect_work_dates(
            _w(
                {
                    "attribute-list": [{"type": "premiered date", "value": "1920"}],
                }
            )
        )
        assert dates.premiered == "1920"

    def test_premiere_date_attribute(self) -> None:
        """Reads premiered date from attribute-list when type contains 'premiere'."""
        dates = music_annotator.collect_work_dates(
            _w(
                {
                    "attribute-list": [{"type": "premiere", "value": "1921"}],
                }
            )
        )
        assert dates.premiered == "1921"


# ---------------------------------------------------------------------------
# collect_work_tags_and_key — string attribute branch
# ---------------------------------------------------------------------------


class TestCollectWorkTagsStringAttr:
    """Tests for string attribute handling in collect_work_tags_and_key."""

    def test_string_attribute_skipped(self) -> None:
        """String entries in attribute-list are skipped without affecting key."""
        _, key = music_annotator.collect_work_tags_and_key(_w({"attribute-list": ["some-raw-string-flag"]}))
        assert key == ""


# ---------------------------------------------------------------------------
# artist_credit_phrase — non-str/non-dict item (no match branch)
# ---------------------------------------------------------------------------


class TestArtistCreditPhraseNoMatch:
    """Tests for artist_credit_phrase with only dict/str items (no non-str/non-dict)."""

    def test_non_credit_items_skipped_by_ac_helper(self) -> None:
        """_ac() skips non-str, non-dict items; only dicts become MBArtistCredit."""
        # _ac only processes str and dict items — int 42 is skipped at the _ac() level
        credit = _ac([{"name": "Karajan"}, {"name": "Mutter"}])
        result = music_annotator.artist_credit_phrase(credit)
        assert result == "KarajanMutter"


# ---------------------------------------------------------------------------
# build_work_hierarchy — visited cycle (explicit visited set)
# ---------------------------------------------------------------------------


class TestBuildWorkHierarchyCycle:
    """Tests for build_work_hierarchy cycle detection via explicit visited set."""

    def test_already_visited_returns_single(self) -> None:
        """Work already in visited returns single-element list immediately."""
        work = _w({"id": "w1", "title": "Work", "work-relation-list": []})
        visited: set[str] = {"w1"}
        result = music_annotator.build_work_hierarchy(work, visited)
        assert len(result) == 1
        assert result[0].id == "w1"


# ---------------------------------------------------------------------------
# build_cwp_tags — two-level with empty bottom_part
# ---------------------------------------------------------------------------


class TestBuildCwpTagsTwoLevelEmptyPart:
    """Tests for build_cwp_tags with two levels and empty bottom part."""

    def test_two_level_bottom_part_empty(self) -> None:
        """Two-level hierarchy where movement has empty title (no stripped part)."""
        rb = RoleBuckets()
        cwp = music_annotator.build_cwp_tags(
            [
                _w({"id": "m1", "title": "", "type": "", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
                _w(
                    {
                        "id": "p1",
                        "title": "Symphony",
                        "type": "",
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                ),
            ],
            rb,
        )
        # bottom_part is "" → if bottom_part: is False → not appended to gh_parts
        assert cwp.work_top == "Symphony"
        assert cwp.part == ""


# ---------------------------------------------------------------------------
# build_dest_path — composer dedup with empty part; no-Person artist-credit; no performers
# ---------------------------------------------------------------------------


class TestBuildDestPathEdgeCases:
    """Edge-case tests for build_dest_path."""

    def _make_tags_no_composer(self, **kwargs: str) -> TrackTags:
        """Build TrackTags with no composer last names and no conductors/ensembles.

        :param kwargs: Additional keyword arguments for TrackTags.
        :returns: A TrackTags instance.
        """
        return TrackTags(
            title=kwargs.get("title", "I. Allegro"),
            movementnumber=kwargs.get("movementnumber", "1"),
            movementtotal=kwargs.get("movementtotal", "4"),
            cwp_work_top=kwargs.get("cwp_work_top", "Symphony"),
            cwp_workid_top=kwargs.get("cwp_workid_top", "w1"),
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )

    def test_rec_title_fallback_when_no_title(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses _rec_title fallback when tags.title is empty.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # TrackTags with no title → file_dict["TITLE"] is absent → _rec_title called
        tags = TrackTags(
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Work",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Composer",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Album", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Adagio"}}),
            tags,
        )
        assert "Adagio" in result.name

    def test_no_person_in_artist_credit_uses_unknown_composer(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses 'Unknown Composer' when artist-credit has no Person type.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # artist-credit has a string join phrase (not a dict) → no Person found
        tags = self._make_tags_no_composer(title="Track")
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Album", "artist-credit": [" & "], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Track"}}),
            tags,
        )
        assert "Unknown Composer" in str(result)

    def test_dict_artist_credit_without_person_type(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses 'Unknown Composer' when dict artist lacks 'Person' type.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # artist has type "Group" → not "Person" → composer remains ""
        tags = self._make_tags_no_composer(title="Track")
        result = music_annotator.build_dest_path(
            dest_root,
            _rel(
                {
                    "id": "r1",
                    "title": "Album",
                    "artist-credit": [{"artist": {"type": "Group", "name": "Some Ensemble", "sort-name": "Ensemble, Some"}}],
                    "medium-list": [],
                }
            ),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Track"}}),
            tags,
        )
        assert "Unknown Composer" in str(result)

    def test_duplicate_composer_lastnames_deduplicated(self, fs: FakeFilesystem) -> None:
        """Duplicate entries in CWP_COMPOSER_LASTNAMES are deduplicated.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # cwp_composer_lastnames with duplicate: "Bach; Bach"
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Work",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Bach; Bach",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "T"}}),
            tags,
        )
        # "Bach; Bach" → dedup → "Bach" (appears once)
        top_dir = result.parts[2]  # dest_root / top_dir / work_dir / filename
        assert top_dir.count("Bach") == 1

    def test_no_conductors_or_ensembles_uses_fallback_performer(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses CEA_ENSEMBLE_NAMES fallback when conductors/ensembles empty.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # No conductors or ensembles → else branch → falls back to ARTIST
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Work",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Composer",
            artist="Solo Artist",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "T"}}),
            tags,
        )
        assert "Solo Artist" in str(result)

    def test_conductors_present_used_as_performers(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses conductor name as performers when conductor is present.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        conductor = ArtistEntry(name="Karajan", sort="Karajan, H", mbid="k1")
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Work",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Bach",
            cea_conductors_list=[conductor],
            cea_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "T"}}),
            tags,
        )
        # Conductor name should appear in performers component
        assert "Karajan" in str(result)


# ---------------------------------------------------------------------------
# build_work_hierarchy — non-backward/parts relation (598->597 branch)
# ---------------------------------------------------------------------------


class TestBuildWorkHierarchyNonPartsRel:
    """Tests for the case where a work has a relation that is not backward/parts."""

    def test_non_parts_relation_ignored(self) -> None:
        """Work with a forward (non-backward) relation returns single-element list."""
        work = _w(
            {
                "id": "w1",
                "title": "Work",
                "work-relation-list": [
                    # direction is "forward" → the if-check is False
                    {"direction": "forward", "type": "parts", "work": {"id": "p1"}},
                ],
            }
        )
        result = music_annotator.build_work_hierarchy(work)
        assert len(result) == 1
        assert result[0].id == "w1"

    def test_non_performance_type_relation_ignored(self) -> None:
        """Work with backward but wrong type relation returns single-element list."""
        work = _w(
            {
                "id": "w1",
                "title": "Work",
                "work-relation-list": [
                    {"direction": "backward", "type": "arranged from", "work": {"id": "p1"}},
                ],
            }
        )
        result = music_annotator.build_work_hierarchy(work)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# collect_work_dates — attribute type not matching any case (729->719 branch)
# ---------------------------------------------------------------------------


class TestCollectWorkDatesUnknownAttr:
    """Tests for the unrecognized attribute type branch in collect_work_dates."""

    def test_unknown_type_attribute_skipped(self) -> None:
        """Attribute with unrecognized type does not set any date field."""
        dates = music_annotator.collect_work_dates(
            _w(
                {
                    "attribute-list": [{"type": "catalogue number", "value": "Op. 67"}],
                }
            )
        )
        assert dates.composed == ""
        assert dates.published == ""
        assert dates.premiered == ""

    def test_premiered_then_unknown_attr(self) -> None:
        """Premiered attr followed by unknown attr: premiered set, loop continues."""
        dates = music_annotator.collect_work_dates(
            _w(
                {
                    "attribute-list": [
                        {"type": "premiere", "value": "1920"},
                        {"type": "catalogue number", "value": "Op. 67"},
                    ],
                }
            )
        )
        assert dates.premiered == "1920"


# ---------------------------------------------------------------------------
# build_cwp_tags — three-level with empty middle part (911->909 branch)
# ---------------------------------------------------------------------------


class TestBuildCwpTagsThreeLevelEmptyMiddle:
    """Tests for the inter-parts loop with an empty middle part."""

    def test_three_level_empty_middle_part(self) -> None:
        """Three-level hierarchy where middle work has empty title → inter_part is empty."""
        rb = RoleBuckets()
        cwp = music_annotator.build_cwp_tags(
            [
                _w({"id": "l0", "title": "Suite: I. Allegro", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
                _w({"id": "l1", "title": "", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
                _w({"id": "l2", "title": "Suite", "work-relation-list": [], "attribute-list": [], "tag-list": []}),
            ],
            rb,
        )
        # Middle part is empty → groupheading has no middle component
        assert cwp.work_top == "Suite"
        assert "::" not in cwp.inter_work  # inter_work computed but empty middle not added


# ---------------------------------------------------------------------------
# build_track_tags — work-relation non-performance type (1017->1016 branch)
# ---------------------------------------------------------------------------


class TestBuildTrackTagsNonPerformanceRel:
    """Tests for work-relation-list entry with type != 'performance'."""

    def test_non_performance_work_rel_not_used(self) -> None:
        """Recording with non-performance work relation has empty musicbrainz_workid."""
        tags = build_track_tags(
            _rel(
                {
                    "id": "r1",
                    "title": "Album",
                    "date": "2000",
                    "status": "Official",
                    "barcode": "",
                    "artist-credit": [],
                    "release-group": {"id": "rg1", "primary-type": "", "first-release-date": ""},
                    "label-info-list": [],
                    "text-representation": {"script": "", "language": ""},
                    "medium-list": [{"position": 1, "format": "CD", "track-list": []}],
                }
            ),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "T", "artist-credit": []}}),
            1,
            _rec(
                {
                    "id": "rec1",
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {"type": "arrangement", "work": {"id": "w1", "title": "W"}},
                    ],
                }
            ),
            [],
        )
        assert tags.musicbrainz_workid == ""
