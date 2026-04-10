"""Unit tests for music_annotator (pure-logic functions, no real I/O or MB API calls)."""

from __future__ import annotations

import sys
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
    configure_color,
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
from music_annotator._mb_api import _extract_session_date
from music_annotator._tags import _NAME_MAX, _proposed_short, _work_aliases
from music_annotator._works import _date_range, _score_top_work, select_primary_performance_work
from music_annotator.models import (
    JSON,
    ArtistEntry,
    MBAlias,
    MBArtistCredit,
    MBArtistRelation,
    MBLabelRelation,
    MBPlaceRelation,
    MBRecording,
    MBRelease,
    MBTrack,
    MBUrlRelation,
    MBWork,
    MBWorkRelation,
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
# configure_color
# ---------------------------------------------------------------------------


class TestConfigureColor:
    """Tests for configure_color."""

    def test_disable_color_replaces_console(self) -> None:
        """configure_color(False) replaces _console with a no_color Console."""
        configure_color(enabled=False)
        _cm = sys.modules["music_annotator._console"]
        assert _cm._console.no_color  # pylint: disable=protected-access

    def test_enable_color_replaces_console(self) -> None:
        """configure_color(True) replaces _console with a color-capable Console."""
        configure_color(enabled=True)
        _cm = sys.modules["music_annotator._console"]
        assert not _cm._console.no_color  # pylint: disable=protected-access


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

    def test_trailing_dot_preserved(self) -> None:
        """Trailing dots are NOT stripped — they carry semantic meaning in titles like 'op.'."""
        assert safe_name("Sphärenklänge, op.") == "Sphärenklänge, op."

    def test_leading_dot_replaced_with_underscore(self) -> None:
        """A leading dot is replaced with underscore to prevent hidden files on POSIX."""
        assert safe_name(".hidden") == "_hidden"

    def test_multiple_leading_dots_replaced(self) -> None:
        """Multiple leading dots are each replaced with an underscore."""
        assert safe_name("...foo") == "___foo"

    def test_leading_space_stripped(self) -> None:
        """Leading spaces are stripped."""
        assert safe_name("  foo") == "foo"

    def test_trailing_space_stripped(self) -> None:
        """Trailing spaces are stripped."""
        assert safe_name("foo  ") == "foo"

    def test_dots_and_spaces_combined(self) -> None:
        """Leading spaces are stripped but leading dots become underscores (no dot stripping)."""
        assert safe_name("  ..My Title..  ") == "__My Title.."

    def test_normal_string_unchanged(self) -> None:
        """A normal ASCII string is returned unchanged."""
        assert safe_name("Fontane di Roma") == "Fontane di Roma"

    def test_no_length_cap(self) -> None:
        """Strings longer than 255 characters are not truncated — length enforcement is the caller's responsibility."""
        long_str = "A" * 300
        assert len(safe_name(long_str)) == 300


class TestProposedShort:
    """Tests for _proposed_short — structure-aware shortening to fit within _NAME_MAX bytes."""

    def test_within_limit_returned_unchanged(self) -> None:
        """A component already within the limit is returned as-is."""
        s = "Brahms - Herbert von Karajan; Berliner Philharmoniker"
        assert len(s.encode("utf-8")) <= _NAME_MAX
        assert _proposed_short(s) == s

    def test_result_always_fits(self) -> None:
        """The result of _proposed_short always fits within _NAME_MAX bytes."""
        # 300-byte component — must be shortened to fit.
        long = "A" * 300
        result = _proposed_short(long)
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_work_dir_drops_subtitle_before_date(self) -> None:
        """Work-dir strategy: subtitle after ' _ ' is dropped before the date suffix."""
        # Build a component that is too long due to a verbose subtitle.
        base = "Symphonie fantastique, op. 14"
        subtitle = " _ " + "Épisode de la vie d'un artiste en cinq parties " * 6
        date = " [rec 1974-1975]"
        component = base + subtitle + date
        assert len(component.encode("utf-8")) > _NAME_MAX, f"Test data too short: {len(component.encode('utf-8'))} bytes"
        result = _proposed_short(component)
        assert result.endswith(date)
        assert len(result.encode("utf-8")) <= _NAME_MAX
        # The result should contain the base work title, not the subtitle.
        assert base in result
        assert "Épisode" not in result

    def test_leaf_drops_subtitle_after_separator(self) -> None:
        """Leaf strategy: movement subtitle after ' _ ' is dropped, 'nn - ' prefix preserved."""
        prefix = "01 - "
        body = "Messe in C-Dur, KV 317 _Krönungsmesse_"
        subtitle = " _ " + "Kyrie_ Andante maestoso - Più andante molto " * 6
        component = prefix + body + subtitle
        assert len(component.encode("utf-8")) > _NAME_MAX, f"Test data too short: {len(component.encode('utf-8'))} bytes"
        result = _proposed_short(component)
        assert result.startswith(prefix)
        assert len(result.encode("utf-8")) <= _NAME_MAX
        assert body in result

    def test_top_dir_drops_rightmost_performer(self) -> None:
        """Top-dir strategy: performer entries are dropped from the right until it fits."""
        composer = "Bach"
        # Make the performer list long enough to exceed NAME_MAX.
        performers = "; ".join([f"Performer Number {i:02d} With A Very Long Name" for i in range(10)])
        component = f"{composer} - {performers}"
        assert len(component.encode("utf-8")) > _NAME_MAX
        result = _proposed_short(component)
        assert result.startswith(f"{composer} - ")
        assert len(result.encode("utf-8")) <= _NAME_MAX
        # At least the first performer should still be present.
        assert "Performer Number 00" in result

    def test_word_boundary_ellipsis(self) -> None:
        """Fallback strategy: truncation at last word boundary with ellipsis appended."""
        # A long string with no structural separators.
        component = "Abcdefghij " * 30  # repeating words, all ASCII, no _ or - separators
        assert len(component.encode("utf-8")) > _NAME_MAX
        result = _proposed_short(component)
        assert result.endswith("…")
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_hard_truncation_no_space(self) -> None:
        """Hard-cut fallback: no spaces in string, truncated at UTF-8 byte boundary + ellipsis."""
        component = "X" * 300  # no spaces
        result = _proposed_short(component)
        assert result.endswith("…")
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_multibyte_not_split(self) -> None:
        """Hard-cut fallback never splits a multi-byte UTF-8 sequence."""
        # "ä" is 2 bytes; fill exactly to the boundary so a naive cut would split it.
        component = "ä" * 200  # 400 bytes, all 2-byte chars
        result = _proposed_short(component)
        assert len(result.encode("utf-8")) <= _NAME_MAX
        # Must be valid UTF-8 (decodeable without errors).
        result.encode("utf-8").decode("utf-8")

    def test_work_dir_no_subtitle_separator_falls_through(self) -> None:
        """Work-dir with date suffix but no ' _ ' separator falls through to later strategies.

        Covers the ``sep_idx == -1`` branch in strategy 1 (161->167).
        """
        # Date suffix present but no ' _ ' subtitle separator in the title.
        base = "A" * 250  # very long, no ' _ '
        component = base + " [rec 1974]"
        assert len(component.encode("utf-8")) > _NAME_MAX
        result = _proposed_short(component)
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_work_dir_subtitle_drop_still_too_long_falls_through(self) -> None:
        """Work-dir where dropping subtitle still exceeds the limit falls through to later strategies.

        Covers the candidate-too-long branch in strategy 1 (163->167).
        """
        # Make base title alone exceed the limit — dropping subtitle won't help.
        base = "B" * 250 + " _ Long Subtitle"
        component = base + " [rec 1974]"
        assert len(component.encode("utf-8")) > _NAME_MAX
        result = _proposed_short(component)
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_leaf_no_subtitle_separator_falls_through(self) -> None:
        """Leaf with 'nn - ' prefix but no ' _ ' subtitle separator falls through to later strategies.

        Covers the ``sep_idx == -1`` branch in strategy 2 (172->179).
        """
        component = "01 - " + "C" * 260  # over limit, no ' _ '
        assert len(component.encode("utf-8")) > _NAME_MAX
        result = _proposed_short(component)
        assert result.startswith("01 - ") or result.endswith("…")
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_leaf_subtitle_drop_still_too_long_falls_through(self) -> None:
        """Leaf where dropping subtitle still exceeds the limit falls through to later strategies.

        Covers the candidate-too-long branch in strategy 2 (174->179).
        """
        # Body alone exceeds limit even without the subtitle (251 + 5-byte prefix = 256 bytes > 255).
        body = "D" * 251
        component = "01 - " + body + " _ subtitle"
        assert len(component.encode("utf-8")) > _NAME_MAX
        # After dropping subtitle the candidate is "01 - " + body = 256 bytes > limit → falls through.
        assert len(("01 - " + body).encode("utf-8")) > _NAME_MAX
        result = _proposed_short(component)
        assert len(result.encode("utf-8")) <= _NAME_MAX


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

    def test_writer_added(self) -> None:
        """Writer relation is added to role_buckets.writers (separate from composers)."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {"type": "writer", "artist": {"id": "w1", "name": "Librettist W", "sort-name": "W, Librettist"}},
                    ]
                }
            ),
            rb,
        )
        assert len(rb.writers) == 1
        assert rb.writers[0].name == "Librettist W"
        assert not rb.composers

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
# extract_work_artist_rels — additional/assistant composer routing
# ---------------------------------------------------------------------------


class TestExtractWorkArtistRelsAdditionalComposer:
    """Tests for the additional/assistant composer routing in extract_work_artist_rels."""

    def test_additional_composer_routed_to_additional_composers(self) -> None:
        """Composer relation with 'additional' attribute goes to additional_composers, not composers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "attribute-list": ["additional"],
                            "artist": {"id": "s1", "name": "Süssmayr", "sort-name": "Süssmayr, Franz Xaver"},
                        },
                    ]
                }
            ),
            rb,
        )
        assert len(rb.additional_composers) == 1
        assert rb.additional_composers[0].name == "Süssmayr"
        assert not rb.composers

    def test_assistant_composer_routed_to_additional_composers(self) -> None:
        """Composer relation with 'assistant' attribute goes to additional_composers, not composers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "attribute-list": ["assistant"],
                            "artist": {"id": "a1", "name": "Assistant A", "sort-name": "A, Assistant"},
                        },
                    ]
                }
            ),
            rb,
        )
        assert len(rb.additional_composers) == 1
        assert not rb.composers

    def test_plain_composer_still_goes_to_composers(self) -> None:
        """Composer relation with no attributes continues to go to composers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "attribute-list": [],
                            "artist": {"id": "m1", "name": "Mozart", "sort-name": "Mozart, Wolfgang Amadeus"},
                        },
                    ]
                }
            ),
            rb,
        )
        assert len(rb.composers) == 1
        assert rb.composers[0].name == "Mozart"
        assert not rb.additional_composers

    def test_additional_composer_deduplication(self) -> None:
        """Same additional composer MBID from two hierarchy levels is added only once."""
        rel: JSON = {
            "type": "composer",
            "attribute-list": ["additional"],
            "artist": {"id": "s1", "name": "Süssmayr", "sort-name": "Süssmayr, Franz Xaver"},
        }
        rb = RoleBuckets()
        extract_work_artist_rels(_w({"artist-relation-list": [rel]}), rb)
        extract_work_artist_rels(_w({"artist-relation-list": [rel]}), rb)
        assert len(rb.additional_composers) == 1

    def test_primary_and_additional_composer_both_present(self) -> None:
        """Primary composer goes to composers and additional goes to additional_composers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "attribute-list": [],
                            "artist": {"id": "m1", "name": "Mozart", "sort-name": "Mozart, Wolfgang Amadeus"},
                        },
                        {
                            "type": "composer",
                            "attribute-list": ["additional"],
                            "artist": {"id": "s1", "name": "Süssmayr", "sort-name": "Süssmayr, Franz Xaver"},
                        },
                    ]
                }
            ),
            rb,
        )
        assert len(rb.composers) == 1
        assert rb.composers[0].name == "Mozart"
        assert len(rb.additional_composers) == 1
        assert rb.additional_composers[0].name == "Süssmayr"


# ---------------------------------------------------------------------------
# _score_top_work
# ---------------------------------------------------------------------------


class TestScoreTopWork:
    """Tests for _score_top_work scoring logic."""

    def test_typed_no_based_on_scores_three(self) -> None:
        """Typed work with no based-on backward relation scores 3 (2 + 1)."""
        work = _w({"id": "w1", "type": "Concerto", "work-relation-list": []})
        assert _score_top_work(work) == 3

    def test_typed_with_based_on_backward_scores_two(self) -> None:
        """Typed work that has a based-on backward relation scores 2 (only type bonus)."""
        work = _w(
            {
                "id": "w1",
                "type": "Cadenza collection",
                "work-relation-list": [{"type": "based on", "direction": "backward", "work": {"id": "w2"}}],
            }
        )
        assert _score_top_work(work) == 2

    def test_untyped_no_based_on_scores_one(self) -> None:
        """Untyped work with no based-on backward relation scores 1 (only no-based-on bonus)."""
        work = _w({"id": "w1", "type": "", "work-relation-list": []})
        assert _score_top_work(work) == 1

    def test_untyped_with_based_on_backward_scores_zero(self) -> None:
        """Untyped work with a based-on backward relation scores 0."""
        work = _w(
            {
                "id": "w1",
                "type": "",
                "work-relation-list": [{"type": "based on", "direction": "backward", "work": {"id": "w2"}}],
            }
        )
        assert _score_top_work(work) == 0

    def test_based_on_forward_direction_not_penalised(self) -> None:
        """A based-on relation with forward direction does not reduce the score."""
        work = _w(
            {
                "id": "w1",
                "type": "",
                "work-relation-list": [{"type": "based on", "direction": "forward", "work": {"id": "w2"}}],
            }
        )
        assert _score_top_work(work) == 1

    def test_other_relation_type_not_penalised(self) -> None:
        """A non-based-on relation with backward direction does not affect score."""
        work = _w(
            {
                "id": "w1",
                "type": "Symphony",
                "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "w2"}}],
            }
        )
        assert _score_top_work(work) == 3


# ---------------------------------------------------------------------------
# select_primary_performance_work
# ---------------------------------------------------------------------------


class TestSelectPrimaryPerformanceWork:
    """Tests for select_primary_performance_work candidate selection."""

    def test_single_candidate_returned_without_fetch(self, mocker: MockerFixture) -> None:
        """With only one candidate, it is returned immediately and fetch_work_detail is not called.

        :param mocker: pytest-mock fixture.
        """
        mock_fetch = mocker.patch("music_annotator._works.fetch_work_detail")
        work = _w({"id": "w1", "title": "Concerto", "work-relation-list": []})
        result = select_primary_performance_work([work])
        assert result.id == "w1"
        mock_fetch.assert_not_called()

    def test_higher_scoring_candidate_selected(self, mocker: MockerFixture) -> None:
        """The candidate whose top-level work scores highest is selected.

        Simulates the Beethoven concerto vs. Kreisler cadenza scenario:
        - Cadenza work → cadenza collection (untyped, has based-on → score 0)
        - Beethoven movement → concerto root (typed=Concerto, no based-on → score 3)

        :param mocker: pytest-mock fixture.
        """
        # Cadenza: bottom work has a parts/backward parent, whose top is untyped + based-on
        cadenza_top = _w(
            {
                "id": "cad-top",
                "type": "",
                "title": "Cadenza collection",
                "work-relation-list": [{"type": "based on", "direction": "backward", "work": {"id": "beethoven-root"}}],
            }
        )
        cadenza_bottom = _w(
            {
                "id": "cad-bot",
                "title": "Cadenza for Op. 61",
                "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "cad-top"}}],
            }
        )

        # Beethoven movement: bottom work has a parts/backward parent (the concerto root)
        beethoven_root = _w(
            {
                "id": "beethoven-root",
                "type": "Concerto",
                "title": "Violin Concerto in D major, Op. 61",
                "work-relation-list": [],
            }
        )
        beethoven_movement = _w(
            {
                "id": "beethoven-mvt",
                "title": "I. Allegro ma non troppo",
                "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "beethoven-root"}}],
            }
        )

        def _fetch(work_id: str) -> MBWork:
            return {
                "cad-top": cadenza_top,
                "beethoven-root": beethoven_root,
            }[work_id]

        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch)

        result = select_primary_performance_work([cadenza_bottom, beethoven_movement])
        assert result.id == "beethoven-mvt"

    def test_tie_broken_by_first_appearance(self, mocker: MockerFixture) -> None:
        """When two candidates score equally, the first one in the list wins.

        :param mocker: pytest-mock fixture.
        """
        work_a = _w({"id": "wa", "title": "Work A", "type": "", "work-relation-list": []})
        work_b = _w({"id": "wb", "title": "Work B", "type": "", "work-relation-list": []})
        mocker.patch("music_annotator._works.fetch_work_detail")
        result = select_primary_performance_work([work_a, work_b])
        assert result.id == "wa"

    def test_cycle_detection_does_not_loop(self, mocker: MockerFixture) -> None:
        """Circular parent references do not cause infinite loops.

        :param mocker: pytest-mock fixture.
        """
        # w1 → w2 (parts/backward), w2 → w1 (parts/backward) — a cycle
        work1 = _w(
            {
                "id": "w1",
                "type": "Concerto",
                "title": "Cyclic Work",
                "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "w2"}}],
            }
        )
        work2 = _w(
            {
                "id": "w2",
                "type": "",
                "title": "Cyclic Parent",
                "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "w1"}}],
            }
        )
        mocker.patch("music_annotator._works.fetch_work_detail", return_value=work2)
        # Should terminate without error
        result = select_primary_performance_work([work1, work1])
        assert result.id == "w1"


# ---------------------------------------------------------------------------
# build_cwp_tags — additional_composers fallback
# ---------------------------------------------------------------------------


class TestBuildCwpTagsAdditionalComposerFallback:
    """Tests for additional_composers fallback in build_cwp_tags."""

    def test_only_additional_composers_used_as_fallback(self) -> None:
        """When composers is empty, additional_composers are used for cwp.composers etc."""
        rb = RoleBuckets()
        rb.additional_composers.append(ArtistEntry(name="Süssmayr", sort="Süssmayr, Franz Xaver", mbid="s1"))
        cwp = build_cwp_tags(
            [_w({"id": "w1", "title": "Requiem", "work-relation-list": [], "attribute-list": [], "tag-list": []})],
            rb,
        )
        assert cwp.composers == "Süssmayr"
        assert "Süssmayr" in cwp.composer_lastnames

    def test_primary_composers_take_precedence_over_additional(self) -> None:
        """When both composers and additional_composers are present, primary composers win."""
        rb = RoleBuckets()
        rb.composers.append(ArtistEntry(name="Mozart", sort="Mozart, Wolfgang Amadeus", mbid="m1"))
        rb.additional_composers.append(ArtistEntry(name="Süssmayr", sort="Süssmayr, Franz Xaver", mbid="s1"))
        cwp = build_cwp_tags(
            [_w({"id": "w1", "title": "Requiem", "work-relation-list": [], "attribute-list": [], "tag-list": []})],
            rb,
        )
        assert cwp.composers == "Mozart"
        assert "Süssmayr" not in cwp.composers


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
        mocker.patch("music_annotator._works.fetch_work_detail", return_value=parent)
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

    def test_writers_populated(self) -> None:
        """A RoleBuckets with writers populates cwp.writers and cwp.writers_sort."""
        rb = RoleBuckets()
        rb.add_unique("writers", ArtistEntry(name="Poet P", sort="P, Poet", mbid="p1"))
        cwp = build_cwp_tags(
            [_w({"id": "w1", "title": "Song", "work-relation-list": [], "attribute-list": [], "tag-list": []})],
            rb,
        )
        assert cwp.writers == "Poet P"
        assert cwp.writers_sort == "P, Poet"

    def test_arranger_deduplication(self) -> None:
        """Duplicate arranger names from arrangers + orchestrators appear only once in arranger_names."""
        rb = RoleBuckets()
        rb.add_unique("arrangers", ArtistEntry(name="Orch A", sort="A, Orch", mbid="o1"))
        rb.add_unique("orchestrators", ArtistEntry(name="Orch A", sort="A, Orch", mbid="o1"))
        cwp = build_cwp_tags(
            [_w({"id": "w1", "title": "Piece", "work-relation-list": [], "attribute-list": [], "tag-list": []})],
            rb,
        )
        assert cwp.arranger_names == "Orch A"


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
        mock_set = mocker.patch("music_annotator._mb_api.mb.set_useragent")
        music_annotator.init_mb("MyApp/2.0 contact@example.com")
        mock_set.assert_called_once_with("MyApp", "2.0", "contact@example.com")

    def test_parses_no_contact(self, mocker: MockerFixture) -> None:
        """init_mb handles user-agent without contact string.

        :param mocker: pytest-mock fixture.
        """
        mock_set = mocker.patch("music_annotator._mb_api.mb.set_useragent")
        music_annotator.init_mb("MyApp/1.0")
        mock_set.assert_called_once_with("MyApp", "1.0", "")

    def test_parses_no_slash(self, mocker: MockerFixture) -> None:
        """init_mb handles user-agent without any slash.

        :param mocker: pytest-mock fixture.
        """
        mock_set = mocker.patch("music_annotator._mb_api.mb.set_useragent")
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


# ---------------------------------------------------------------------------
# MBWorkRelation — ordering_key coercion
# ---------------------------------------------------------------------------


class TestMBWorkRelationOrderingKey:
    """Tests for ordering_key field on MBWorkRelation."""

    def test_ordering_key_string_coerced_to_int(self) -> None:
        """MB API returns ordering-key as a string; Pydantic coerces it to int."""
        rel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "ordering-key": "8"})
        assert rel.ordering_key == 8

    def test_ordering_key_absent_defaults_to_zero(self) -> None:
        """ordering_key defaults to 0 when the field is absent."""
        rel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward"})
        assert rel.ordering_key == 0

    def test_ordering_key_none_defaults_to_zero(self) -> None:
        """ordering_key defaults to 0 when the field is None."""
        rel = MBWorkRelation.model_validate({"type": "parts", "direction": "backward", "ordering-key": None})
        assert rel.ordering_key == 0


# ---------------------------------------------------------------------------
# build_cwp_tags — ordering_key propagated into WorkHierarchyLevel
# ---------------------------------------------------------------------------


class TestBuildCwpTagsOrderingKey:
    """Tests for ordering_key propagation in build_cwp_tags."""

    def test_ordering_key_from_backward_relation_populated(self) -> None:
        """WorkHierarchyLevel.ordering_key is set from the parts/backward relation."""
        rb = RoleBuckets()
        movement = _w(
            {
                "id": "mov",
                "title": "I. Allegro",
                "work-relation-list": [{"type": "parts", "direction": "backward", "ordering-key": "2", "work": {"id": "sym"}}],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        symphony = _w({"id": "sym", "title": "Symphony No. 5", "work-relation-list": [], "attribute-list": [], "tag-list": []})
        cwp = music_annotator.build_cwp_tags([movement, symphony], rb)
        assert cwp.levels[0].ordering_key == 2
        assert cwp.levels[1].ordering_key == 0  # root has no parent in hierarchy

    def test_ordering_key_zero_when_no_backward_relation(self) -> None:
        """WorkHierarchyLevel.ordering_key is 0 when no parts/backward relation exists."""
        rb = RoleBuckets()
        work = _w({"id": "w1", "title": "Work", "work-relation-list": [], "attribute-list": [], "tag-list": []})
        cwp = music_annotator.build_cwp_tags([work], rb)
        assert cwp.levels[0].ordering_key == 0

    def test_cwp_ordering_key_in_model_extra(self) -> None:
        """cwp_ordering_key_{i} is written to TrackTags model_extra via build_track_tags."""
        movement = _w(
            {
                "id": "mov",
                "title": "I. Allegro",
                "work-relation-list": [{"type": "parts", "direction": "backward", "ordering-key": "3", "work": {"id": "sym"}}],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        symphony = _w({"id": "sym", "title": "Symphony", "work-relation-list": [], "attribute-list": [], "tag-list": []})
        tags = build_track_tags(
            _rel(
                {
                    "id": "r1",
                    "title": "Album",
                    "date": "2000",
                    "status": "Official",
                    "barcode": "",
                    "artist-credit": [],
                    "release-group": {"id": "rg1", "primary-type": "", "first-release-date": "1970"},
                    "label-info-list": [],
                    "text-representation": {"script": "", "language": ""},
                    "medium-list": [{"position": 1, "format": "CD", "track-list": []}],
                }
            ),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro", "artist-credit": []}}),
            1,
            _rec(
                {"id": "rec1", "title": "I. Allegro", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            ),
            [movement, symphony],
        )
        file_dict = tags.to_file_dict()
        assert file_dict.get("CWP_ORDERING_KEY_0") == "3"
        assert file_dict.get("CWP_ORDERING_KEY_1") == "0"


# ---------------------------------------------------------------------------
# build_dest_path — [rec YYYY] / [rel YYYY] year suffix
# ---------------------------------------------------------------------------


class TestBuildDestPathYear:
    """Tests for the [rec YYYY] / [rel YYYY] year suffix in build_dest_path."""

    def _make_rel(self, first_release_date: str = "", date: str = "") -> MBRelease:
        """Build a minimal MBRelease with configurable dates.

        :param first_release_date: release-group first-release-date string.
        :param date: release date string.
        :returns: An MBRelease instance.
        """
        return _rel(
            {
                "id": "r1",
                "title": "Album",
                "date": date,
                "status": "Official",
                "barcode": "",
                "artist-credit": [],
                "release-group": {"id": "rg1", "primary-type": "", "first-release-date": first_release_date},
                "label-info-list": [],
                "text-representation": {"script": "", "language": ""},
                "medium-list": [{"position": 1, "format": "CD", "track-list": []}],
            }
        )

    def _make_tags(self, originaldate: str = "", date: str = "", recording_first_release_date: str = "") -> TrackTags:
        """Build minimal TrackTags with configurable date fields.

        :param originaldate: ORIGINALDATE tag value (release group publication year).
        :param date: DATE tag value (release publication date).
        :param recording_first_release_date: RECORDING_FIRST_RELEASE_DATE tag value.
        :returns: A TrackTags instance.
        """
        return TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 1",
            cwp_composer_lastnames="Beethoven",
            originaldate=originaldate,
            date=date,
            recording_first_release_date=recording_first_release_date,
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )

    def _dest(self, tags: TrackTags, fs: FakeFilesystem) -> str:
        """Run build_dest_path and return the string result.

        :param tags: TrackTags to pass to build_dest_path.
        :param fs: pyfakefs fixture.
        :returns: String representation of the resulting path.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        return str(
            build_dest_path(
                dest_root,
                self._make_rel(),
                _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
                tags,
            )
        )

    def test_recording_first_release_date_labelled_rel(self, fs: FakeFilesystem) -> None:
        """RECORDING_FIRST_RELEASE_DATE is labelled [rel] — it is a publication year, not a session date.

        :param fs: pyfakefs fixture.
        """
        assert "[rel 1963]" in self._dest(self._make_tags(recording_first_release_date="1963"), fs)

    def test_recording_first_release_date_full_date_truncated(self, fs: FakeFilesystem) -> None:
        """RECORDING_FIRST_RELEASE_DATE full date string is truncated to 4-digit year.

        :param fs: pyfakefs fixture.
        """
        assert "[rel 1990]" in self._dest(self._make_tags(recording_first_release_date="1990-04-03"), fs)

    def test_recording_first_release_date_preferred_over_originaldate(self, fs: FakeFilesystem) -> None:
        """RECORDING_FIRST_RELEASE_DATE takes priority over ORIGINALDATE; both produce [rel].

        :param fs: pyfakefs fixture.
        """
        result = self._dest(self._make_tags(recording_first_release_date="1963", originaldate="2003"), fs)
        assert "[rel 1963]" in result
        assert "[rel 2003]" not in result

    def test_rel_year_from_originaldate_when_no_rec_date(self, fs: FakeFilesystem) -> None:
        """[rel YYYY] suffix uses ORIGINALDATE when RECORDING_FIRST_RELEASE_DATE is absent.

        :param fs: pyfakefs fixture.
        """
        assert "[rel 1963]" in self._dest(self._make_tags(originaldate="1963-05-01"), fs)

    def test_rel_year_from_date_when_originaldate_absent(self, fs: FakeFilesystem) -> None:
        """[rel YYYY] falls back to DATE when ORIGINALDATE is also absent.

        :param fs: pyfakefs fixture.
        """
        assert "[rel 2003]" in self._dest(self._make_tags(date="2003"), fs)

    def test_no_year_suffix_when_all_dates_absent(self, fs: FakeFilesystem) -> None:
        """No year suffix when no date fields are present.

        :param fs: pyfakefs fixture.
        """
        assert "[" not in self._dest(self._make_tags(), fs)

    def test_ordering_key_used_in_2level_hierarchy(self, fs: FakeFilesystem) -> None:
        """CWP_ORDERING_KEY_0 sets the track prefix for 2-level hierarchies (no intermediate dirs).

        Disc 2 of a multi-disc work has ordering-key=13; the file should be prefixed '13 -',
        not '01 -' from the disc-local MOVEMENTNUMBER.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Credo in unum Deum",
            movementnumber="1",
            movementtotal="15",
            cwp_work_top="h-Moll-Messe, BWV 232",
            cwp_composer_lastnames="Bach",
            cwp_part_levels="1",
            originaldate="1974",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_ordering_key_0"] = "13"  # type: ignore[index]
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("13 -")

    def test_2level_falls_back_to_movementnumber_when_ordering_key_zero(self, fs: FakeFilesystem) -> None:
        """Without ordering-key, MOVEMENTNUMBER is used for the 2-level file prefix.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags(originaldate="1974")
        tags.movementnumber = "3"
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("03 -")

    def test_mbid_no_longer_in_path(self, fs: FakeFilesystem) -> None:
        """Full MBID UUID is not present in the path (replaced by [rec/rel YYYY]).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Work",
            cwp_workid_top="abc123de-f456-7890-abcd-ef1234567890",
            cwp_composer_lastnames="Composer",
            originaldate="1970",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = build_dest_path(
            dest_root, self._make_rel(), _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}), tags
        )
        assert "abc123de" not in str(result)
        assert "[rel 1970]" in str(result)


# ---------------------------------------------------------------------------
# _date_range helper
# ---------------------------------------------------------------------------


class TestDateRange:
    """Tests for _date_range."""

    def test_single_year_when_no_end(self) -> None:
        """Returns single year when end is empty."""

        assert _date_range("1822", "") == "1822"

    def test_single_year_when_same_year(self) -> None:
        """Returns single year when begin and end are in the same year."""

        assert _date_range("1824-03-01", "1824-09-30") == "1824"

    def test_year_range_when_different_years(self) -> None:
        """Returns YYYY-YYYY when begin and end span different years."""

        assert _date_range("1822", "1824") == "1822-1824"

    def test_full_dates_truncated_to_years(self) -> None:
        """Full ISO dates are truncated to 4-digit years in the output."""

        assert _date_range("1983-12-20", "1984-01-05") == "1983-1984"

    def test_empty_begin_returns_empty(self) -> None:
        """Returns empty string when begin is empty."""

        assert _date_range("", "1824") == ""


# ---------------------------------------------------------------------------
# build_dest_path — [rec YYYY-YYYY] multi-year range
# ---------------------------------------------------------------------------


class TestBuildDestPathRecRange:
    """Tests for [rec YYYY-YYYY] multi-year range in build_dest_path."""

    def _dest(self, recording_date: str, fs: FakeFilesystem) -> str:
        """Run build_dest_path with a given RECORDING_DATE and return the path string.

        :param recording_date: Value for the RECORDING_DATE tag.
        :param fs: pyfakefs fixture.
        :returns: String representation of the result path.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Symphony",
            cwp_composer_lastnames="Beethoven",
            recording_date=recording_date,
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        return str(
            build_dest_path(
                dest_root,
                _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
                _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
                tags,
            )
        )

    def test_multi_year_range_produces_rec_yyyy_yyyy(self, fs: FakeFilesystem) -> None:
        """RECORDING_DATE spanning two years produces [rec YYYY-YYYY].

        :param fs: pyfakefs fixture.
        """
        result = self._dest("1983-12-20/1984-01-05", fs)
        assert "[rec 1983-1984]" in result

    def test_same_year_range_produces_rec_yyyy(self, fs: FakeFilesystem) -> None:
        """RECORDING_DATE where begin and end are in the same year produces [rec YYYY].

        :param fs: pyfakefs fixture.
        """
        result = self._dest("1984-01-27/1984-02-21", fs)
        assert "[rec 1984]" in result
        assert "-1984" not in result  # no range suffix

    def test_single_date_no_slash_produces_rec_yyyy(self, fs: FakeFilesystem) -> None:
        """RECORDING_DATE without a slash (single begin date) produces [rec YYYY].

        :param fs: pyfakefs fixture.
        """
        result = self._dest("1984-01-27", fs)
        assert "[rec 1984]" in result

    def test_slash_with_empty_end_year_produces_rec_yyyy(self, fs: FakeFilesystem) -> None:
        """RECORDING_DATE with slash but missing end year produces [rec YYYY].

        :param fs: pyfakefs fixture.
        """
        result = self._dest("1984/", fs)
        assert "[rec 1984]" in result

    def test_slash_with_invalid_begin_year_produces_no_label(self, fs: FakeFilesystem) -> None:
        """RECORDING_DATE with slash but unparseable begin year produces no [rec] label.

        :param fs: pyfakefs fixture.
        """
        result = self._dest("/1984", fs)
        assert "[rec" not in result

    def test_rec_takes_precedence_over_rel(self, fs: FakeFilesystem) -> None:
        """[rec] label takes precedence over [rel] when RECORDING_DATE is present.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Symphony",
            cwp_composer_lastnames="Beethoven",
            recording_date="1983-12-20/1984-01-05",
            originaldate="1986",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = str(
            build_dest_path(
                dest_root,
                _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
                _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
                tags,
            )
        )
        assert "[rec 1983-1984]" in result
        assert "[rel" not in result


# ---------------------------------------------------------------------------
# build_dest_path — intermediate directories for 3-level hierarchy
# ---------------------------------------------------------------------------


class TestBuildDestPathIntermediateDirs:
    """Tests for intermediate directory generation when part_levels >= 2."""

    def _make_tags_3level(
        self,
        act_part: str = "Atto I",
        act_ordering_key: str = "2",
        leaf_ordering_key: str = "4",
        movementnumber: str = "17",
    ) -> TrackTags:
        """Build TrackTags simulating a 3-level opera hierarchy.

        Level 0 = aria (leaf), level 1 = act (intermediate), level 2 = opera (root/top).

        :param act_part: Stripped part title for the act (level 1).
        :param act_ordering_key: MB ordering-key for the act within the opera.
        :param leaf_ordering_key: MB ordering-key for the aria within the act.
        :param movementnumber: Global MOVEMENTNUMBER tag (composer's numbering).
        :returns: A TrackTags instance with cwp_part_levels=2 and all per-level extras set.
        """
        tags = TrackTags(
            title="No. 17 - Esultate!",
            movementnumber=movementnumber,
            movementtotal="52",
            cwp_work_top="Otello",
            cwp_composer_lastnames="Verdi",
            originaldate="1978",
            cwp_part_levels="2",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_part_0"] = "Esultate!"  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_0"] = leaf_ordering_key  # type: ignore[index]
        tags.model_extra["cwp_part_1"] = act_part  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_1"] = act_ordering_key  # type: ignore[index]
        tags.model_extra["cwp_work_1"] = f"Otello: {act_part}"  # type: ignore[index]
        return tags

    def _make_rel(self) -> MBRelease:
        """Build a minimal release.

        :returns: An MBRelease instance.
        """
        return _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []})

    def test_intermediate_dir_created_for_act(self, fs: FakeFilesystem) -> None:
        """3-level hierarchy produces an intermediate act directory.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level()
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "Esultate!"}}),
            tags,
        )
        parts = result.parts
        # parts: ['/', 'lib', 'Verdi - Unknown Performers', 'Otello [1978]', '02 - Atto I', '04 - Esultate!']
        assert any("Atto I" in p for p in parts)
        assert any(p.startswith("02") for p in parts)  # act ordering-key=2

    def test_leaf_nn_from_ordering_key(self, fs: FakeFilesystem) -> None:
        """Leaf filename nn uses ordering-key of the aria within its act.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(leaf_ordering_key="4")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("04")

    def test_leaf_nn_falls_back_to_movementnumber_when_ordering_key_zero(self, fs: FakeFilesystem) -> None:
        """Leaf nn falls back to MOVEMENTNUMBER when ordering-key is 0.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(leaf_ordering_key="0", movementnumber="17")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("17")

    def test_intermediate_nn_falls_back_to_ordinal_when_ordering_key_zero(self, fs: FakeFilesystem) -> None:
        """Intermediate directory nn falls back to 1-based ordinal when ordering-key is 0.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(act_ordering_key="0")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        parts = result.parts
        # ordinal fallback: level 1 → ordinal=1 → "01 - Atto I"
        assert any(p.startswith("01") and "Atto I" in p for p in parts)

    def test_global_movementnumber_in_title_not_directory_prefix(self, fs: FakeFilesystem) -> None:
        """MOVEMENTNUMBER appears in the title portion of the filename, not as the only prefix.

        The leaf nn prefix comes from ordering-key; the composer's global number
        appears in the TITLE tag and therefore in the track title portion.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(leaf_ordering_key="4", movementnumber="17")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        # File starts with "04 - " (ordering-key), title contains "No. 17"
        assert result.name.startswith("04")
        assert "No. 17" in result.name

    def test_4level_hierarchy_two_intermediate_dirs(self, fs: FakeFilesystem) -> None:
        """4-level hierarchy (e.g. opera → act → scene → number) produces two intermediate dirs.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Aria",
            movementnumber="5",
            movementtotal="40",
            cwp_work_top="Opera",
            cwp_composer_lastnames="Composer",
            originaldate="1985",
            cwp_part_levels="3",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_part_0"] = "Aria"  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_0"] = "3"  # type: ignore[index]
        tags.model_extra["cwp_part_1"] = "Scene I"  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_1"] = "1"  # type: ignore[index]
        tags.model_extra["cwp_part_2"] = "Act I"  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_2"] = "2"  # type: ignore[index]
        result = build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "Aria"}}),
            tags,
        )
        parts = result.parts
        assert any("Act I" in p for p in parts)
        assert any("Scene I" in p for p in parts)
        assert result.name.startswith("03")


# ---------------------------------------------------------------------------
# MBAlias model
# ---------------------------------------------------------------------------


class TestMBAlias:
    """Tests for the MBAlias model."""

    def test_parses_full_alias(self) -> None:
        """MBAlias parses all fields from a raw MB API dict."""
        alias = MBAlias.model_validate(
            {
                "name": "Serenade for Strings",
                "sort-name": "Serenade for Strings",
                "locale": "en",
                "type": "Work name",
                "primary": "primary",
            }
        )
        assert alias.name == "Serenade for Strings"
        assert alias.sort_name == "Serenade for Strings"
        assert alias.locale == "en"
        assert alias.type == "Work name"
        assert alias.primary == "primary"

    def test_defaults_when_fields_absent(self) -> None:
        """MBAlias defaults all optional fields to empty/None when absent."""
        alias = MBAlias.model_validate({"name": "Alt title"})
        assert alias.locale is None
        assert alias.type == ""
        assert alias.primary is None
        assert alias.sort_name == ""

    def test_locale_none_when_not_set(self) -> None:
        """locale is None (not empty string) when absent from the MB response."""
        alias = MBAlias.model_validate({})
        assert alias.locale is None

    def test_mbwork_alias_list_populated(self) -> None:
        """MBWork.alias_list is populated from alias-list in MB API response."""
        work = _w(
            {
                "id": "w1",
                "title": "Серенада для струнного оркестра",
                "alias-list": [
                    {
                        "name": "Serenade for Strings in C major, op. 48",
                        "locale": "en",
                        "type": "Work name",
                        "primary": "primary",
                    },
                    {"name": "Sérénade pour cordes", "locale": "fr", "type": "Work name"},
                ],
            }
        )
        assert len(work.alias_list) == 2
        assert work.alias_list[0].locale == "en"
        assert work.alias_list[1].locale == "fr"

    def test_mbwork_alias_list_defaults_to_empty(self) -> None:
        """MBWork.alias_list defaults to [] when alias-list is absent."""
        work = _w({"id": "w1", "title": "Work"})
        assert work.alias_list == []


# ---------------------------------------------------------------------------
# _work_aliases helper
# ---------------------------------------------------------------------------


class TestWorkAliases:
    """Tests for the _work_aliases helper."""

    def test_english_alias_selected(self) -> None:
        """English Work name alias is returned as the first element."""
        work = _w(
            {
                "id": "w1",
                "title": "Серенада для струнного оркестра",
                "alias-list": [
                    {"name": "Serenade for Strings", "locale": "en", "type": "Work name"},
                    {"name": "Sérénade pour cordes", "locale": "fr", "type": "Work name"},
                ],
            }
        )
        english, _ = _work_aliases(work)
        assert english == "Serenade for Strings"

    def test_no_english_alias_returns_empty(self) -> None:
        """Returns empty string when no English Work name alias exists."""
        work = _w(
            {
                "id": "w1",
                "title": "Серенада",
                "alias-list": [{"name": "Sérénade", "locale": "fr", "type": "Work name"}],
            }
        )
        english, _ = _work_aliases(work)
        assert english == ""

    def test_non_work_name_type_english_not_selected(self) -> None:
        """English alias with type != 'Work name' is not selected as the English alias."""
        work = _w(
            {
                "id": "w1",
                "title": "Серенада",
                "alias-list": [{"name": "Serenade", "locale": "en", "type": "Search hint"}],
            }
        )
        english, _ = _work_aliases(work)
        assert english == ""

    def test_unlocaled_aliases_collected(self) -> None:
        """Aliases with locale=None are collected into the alt string."""
        work = _w(
            {
                "id": "w1",
                "title": "Серенада",
                "alias-list": [
                    {"name": "Serenade Op. 48"},
                    {"name": "String Serenade"},
                    {"name": "Sérénade", "locale": "fr", "type": "Work name"},
                ],
            }
        )
        _, alt = _work_aliases(work)
        assert "Serenade Op. 48" in alt
        assert "String Serenade" in alt
        assert "Sérénade" not in alt

    def test_canonical_title_excluded_from_alt(self) -> None:
        """The canonical work.title is not repeated in the alt string."""
        work = _w(
            {
                "id": "w1",
                "title": "Серенада",
                "alias-list": [{"name": "Серенада"}, {"name": "Alt form"}],
            }
        )
        _, alt = _work_aliases(work)
        assert "Серенада" not in alt
        assert "Alt form" in alt

    def test_unlocaled_aliases_deduplicated(self) -> None:
        """Duplicate unlocaled alias names appear only once in alt."""
        work = _w(
            {
                "id": "w1",
                "title": "Work",
                "alias-list": [{"name": "Dupe"}, {"name": "Dupe"}, {"name": "Other"}],
            }
        )
        _, alt = _work_aliases(work)
        assert alt.count("Dupe") == 1

    def test_empty_alias_list_returns_empty_strings(self) -> None:
        """Empty alias-list returns ('', '')."""
        work = _w({"id": "w1", "title": "Work"})
        assert _work_aliases(work) == ("", "")

    def test_all_unlocaled_aliases_stored(self) -> None:
        """All unlocaled aliases are stored, not capped at any limit."""
        aliases: list[JSON] = [{"name": f"Alt {i}"} for i in range(10)]
        work = _w({"id": "w1", "title": "Work", "alias-list": aliases})
        _, alt = _work_aliases(work)
        for i in range(10):
            assert f"Alt {i}" in alt

    def test_empty_alias_name_skipped(self) -> None:
        """Aliases with empty name are not included in alt."""
        work = _w(
            {
                "id": "w1",
                "title": "Work",
                "alias-list": [{"name": ""}, {"name": "Valid"}],
            }
        )
        _, alt = _work_aliases(work)
        assert alt == "Valid"


# ---------------------------------------------------------------------------
# build_cwp_tags — work_top_en and work_top_alt populated
# ---------------------------------------------------------------------------


class TestBuildCwpTagsAliases:
    """Tests for work_top_en and work_top_alt in build_cwp_tags."""

    def test_work_top_en_populated_from_root_work(self) -> None:
        """cwp.work_top_en is set from the English alias of the root work."""
        rb = RoleBuckets()
        movement = _w({"id": "mov", "title": "I. Allegro", "work-relation-list": [], "attribute-list": [], "tag-list": []})
        symphony = _w(
            {
                "id": "sym",
                "title": "Симфония № 5",
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
                "alias-list": [{"name": "Symphony No. 5", "locale": "en", "type": "Work name"}],
            }
        )
        cwp = build_cwp_tags([movement, symphony], rb)
        assert cwp.work_top_en == "Symphony No. 5"

    def test_work_top_en_empty_when_no_english_alias(self) -> None:
        """cwp.work_top_en is empty when root work has no English alias."""
        rb = RoleBuckets()
        work = _w({"id": "w1", "title": "Симфония", "work-relation-list": [], "attribute-list": [], "tag-list": []})
        cwp = build_cwp_tags([work], rb)
        assert cwp.work_top_en == ""

    def test_work_top_alt_populated_from_root_work(self) -> None:
        """cwp.work_top_alt contains unlocaled aliases of the root work."""
        rb = RoleBuckets()
        work = _w(
            {
                "id": "w1",
                "title": "Серенада",
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
                "alias-list": [{"name": "Serenade Op. 48"}, {"name": "String Serenade"}],
            }
        )
        cwp = build_cwp_tags([work], rb)
        assert "Serenade Op. 48" in cwp.work_top_alt
        assert "String Serenade" in cwp.work_top_alt

    def test_per_level_work_en_and_alt_in_model_extra(self) -> None:
        """cwp_work_{i}_en and cwp_work_{i}_alt appear in TrackTags.to_file_dict()."""
        movement = _w(
            {
                "id": "mov",
                "title": "I. Allegro",
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
                "alias-list": [{"name": "Movement Alt"}],
            }
        )
        symphony = _w(
            {
                "id": "sym",
                "title": "Симфония",
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
                "alias-list": [{"name": "Symphony No. 5", "locale": "en", "type": "Work name"}],
            }
        )
        tags = build_track_tags(
            _rel(
                {
                    "id": "r1",
                    "title": "Album",
                    "date": "2000",
                    "status": "Official",
                    "barcode": "",
                    "artist-credit": [],
                    "release-group": {"id": "rg1", "primary-type": "", "first-release-date": "1970"},
                    "label-info-list": [],
                    "text-representation": {"script": "", "language": ""},
                    "medium-list": [{"position": 1, "format": "CD", "track-list": []}],
                }
            ),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro", "artist-credit": []}}),
            1,
            _rec(
                {"id": "rec1", "title": "I. Allegro", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            ),
            [movement, symphony],
        )
        file_dict = tags.to_file_dict()
        # Level 1 (symphony root) has English alias
        assert file_dict.get("CWP_WORK_1_EN") == "Symphony No. 5"
        # Level 0 (movement) has unlocaled alias
        assert file_dict.get("CWP_WORK_0_ALT") == "Movement Alt"
        # Top-level fields
        assert file_dict.get("CWP_WORK_TOP_EN") == "Symphony No. 5"

    def test_cwp_work_top_en_in_to_file_dict(self) -> None:
        """CWP_WORK_TOP_EN appears in to_file_dict() output when populated."""
        tags = TrackTags(
            cwp_work_top="Симфония",
            cwp_work_top_en="Symphony No. 5",
            cwp_work_top_alt="Alt form",
            movementnumber="1",
            movementtotal="4",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        file_dict = tags.to_file_dict()
        assert file_dict.get("CWP_WORK_TOP_EN") == "Symphony No. 5"
        assert file_dict.get("CWP_WORK_TOP_ALT") == "Alt form"


# ---------------------------------------------------------------------------
# collect_work_dates — relation-based date sources
# ---------------------------------------------------------------------------


class TestCollectWorkDatesFromRelations:
    """Tests for the relation-based date extraction in collect_work_dates."""

    def test_composed_date_from_composer_relation_begin(self) -> None:
        """Composed date includes both begin and end years when they differ."""
        work = _w(
            {
                "id": "w1",
                "title": "T",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "direction": "backward",
                        "begin": "1822",
                        "end": "1824",
                        "artist": {"id": "a1", "name": "Beethoven", "sort-name": "Beethoven"},
                    }
                ],
            }
        )
        dates = music_annotator.collect_work_dates(work)
        assert dates.composed == "1822-1824"

    def test_composed_date_single_year_when_begin_equals_end(self) -> None:
        """Composed date is a single year when begin and end year are the same."""
        work = _w(
            {
                "id": "w1",
                "title": "T",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "direction": "backward",
                        "begin": "1824-03-01",
                        "end": "1824-09-30",
                        "artist": {"id": "a1", "name": "B", "sort-name": "B"},
                    }
                ],
            }
        )
        dates = music_annotator.collect_work_dates(work)
        assert dates.composed == "1824"

    def test_composed_date_begin_only_when_no_end(self) -> None:
        """Composed date is the begin year only when no end date is present."""
        work = _w(
            {
                "id": "w1",
                "title": "T",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "direction": "backward",
                        "begin": "1822",
                        "artist": {"id": "a1", "name": "B", "sort-name": "B"},
                    }
                ],
            }
        )
        dates = music_annotator.collect_work_dates(work)
        assert dates.composed == "1822"

    def test_composed_date_attribute_takes_precedence_over_relation(self) -> None:
        """Attribute-list composed date takes precedence over relation begin."""
        work = _w(
            {
                "id": "w1",
                "title": "T",
                "attribute-list": [{"type": "Composed", "value": "1800"}],
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "direction": "backward",
                        "begin": "1822",
                        "artist": {"id": "a1", "name": "B", "sort-name": "B"},
                    }
                ],
            }
        )
        dates = music_annotator.collect_work_dates(work)
        assert dates.composed == "1800"

    def test_published_date_from_label_relation_begin(self) -> None:
        """Published date extracted from publishing label relation begin."""

        work = _w({"id": "w1", "title": "T"})
        work.label_relation_list = [
            MBLabelRelation.model_validate(
                {"type": "publishing", "direction": "backward", "begin": "1827", "label": {"id": "l1", "name": "Breitkopf"}}
            )
        ]
        dates = music_annotator.collect_work_dates(work)
        assert dates.published == "1827"

    def test_premiered_date_from_place_relation_begin(self) -> None:
        """Premiered date extracted from premiere place relation begin (single-day event: no range)."""
        work = _w({"id": "w1", "title": "T"})
        work.place_relation_list = [
            MBPlaceRelation.model_validate(
                {"type": "premiere", "direction": "backward", "begin": "1824-05-07", "place": {"id": "p1", "name": "Vienna"}}
            )
        ]
        dates = music_annotator.collect_work_dates(work)
        assert dates.premiered == "1824"

    def test_published_date_range_from_label_relation(self) -> None:
        """Published date captures begin–end range from label publishing relation."""
        work = _w({"id": "w1", "title": "T"})
        work.label_relation_list = [
            MBLabelRelation.model_validate(
                {
                    "type": "publishing",
                    "direction": "backward",
                    "begin": "1827",
                    "end": "1828",
                    "label": {"id": "l1", "name": "Breitkopf"},
                }
            )
        ]
        dates = music_annotator.collect_work_dates(work)
        assert dates.published == "1827-1828"

    def test_non_publishing_label_relation_ignored(self) -> None:
        """Label relation with type != 'publishing' does not set published date."""

        work = _w({"id": "w1", "title": "T"})
        work.label_relation_list = [
            MBLabelRelation.model_validate(
                {"type": "other", "direction": "backward", "begin": "1827", "label": {"id": "l1", "name": "L"}}
            )
        ]
        dates = music_annotator.collect_work_dates(work)
        assert dates.published == ""

    def test_non_premiere_place_relation_ignored(self) -> None:
        """Place relation with type != 'premiere' does not set premiered date."""

        work = _w({"id": "w1", "title": "T"})
        work.place_relation_list = [
            MBPlaceRelation.model_validate(
                {"type": "concert", "direction": "backward", "begin": "1824", "place": {"id": "p1", "name": "P"}}
            )
        ]
        dates = music_annotator.collect_work_dates(work)
        assert dates.premiered == ""


# ---------------------------------------------------------------------------
# collect_work_urls
# ---------------------------------------------------------------------------


class TestCollectWorkUrls:
    """Tests for collect_work_urls."""

    def test_imslp_url_extracted(self) -> None:
        """IMSLP URL (type='download for free') is extracted."""

        work = _w({"id": "w1", "title": "T"})
        work.url_relation_list = [
            MBUrlRelation.model_validate({"type": "download for free", "url": "https://imslp.org/wiki/Symphony_No.9"})
        ]
        urls = music_annotator.collect_work_urls(work)
        assert urls.get("download for free") == "https://imslp.org/wiki/Symphony_No.9"

    def test_wikidata_url_extracted(self) -> None:
        """Wikidata URL is extracted."""

        work = _w({"id": "w1", "title": "T"})
        work.url_relation_list = [
            MBUrlRelation.model_validate({"type": "wikidata", "url": "https://www.wikidata.org/wiki/Q11989"})
        ]
        urls = music_annotator.collect_work_urls(work)
        assert urls.get("wikidata") == "https://www.wikidata.org/wiki/Q11989"

    def test_non_notable_type_excluded(self) -> None:
        """URL relations with non-notable types are not included."""

        work = _w({"id": "w1", "title": "T"})
        work.url_relation_list = [MBUrlRelation.model_validate({"type": "other databases", "url": "https://example.com"})]
        urls = music_annotator.collect_work_urls(work)
        assert not urls

    def test_empty_url_list_returns_empty_dict(self) -> None:
        """Empty url_relation_list returns empty dict."""
        work = _w({"id": "w1", "title": "T"})
        assert music_annotator.collect_work_urls(work) == {}


# ---------------------------------------------------------------------------
# _extract_session_date
# ---------------------------------------------------------------------------


class TestExtractSessionDate:
    """Tests for _extract_session_date — returns (begin, end) tuple."""

    def test_begin_and_end_returned(self) -> None:
        """Both begin and end are returned when present."""
        rels = [
            MBArtistRelation.model_validate(
                {
                    "type": "conductor",
                    "direction": "backward",
                    "begin": "1984-01-27",
                    "end": "1984-02-21",
                    "artist": {"id": "a1", "name": "K", "sort-name": "K"},
                }
            )
        ]
        assert _extract_session_date(rels) == ("1984-01-27", "1984-02-21")

    def test_minimum_begin_maximum_end_for_multiple(self) -> None:
        """Min begin and max end are returned across multiple session relations."""
        rels = [
            MBArtistRelation.model_validate(
                {
                    "type": "conductor",
                    "direction": "backward",
                    "begin": "1984-02-01",
                    "end": "1984-02-10",
                    "artist": {"id": "a1", "name": "K", "sort-name": "K"},
                }
            ),
            MBArtistRelation.model_validate(
                {
                    "type": "balance",
                    "direction": "backward",
                    "begin": "1984-01-27",
                    "end": "1984-02-21",
                    "artist": {"id": "a2", "name": "H", "sort-name": "H"},
                }
            ),
        ]
        assert _extract_session_date(rels) == ("1984-01-27", "1984-02-21")

    def test_no_end_returns_empty_end(self) -> None:
        """When no end dates exist, the end component is empty string."""
        rels = [
            MBArtistRelation.model_validate(
                {
                    "type": "conductor",
                    "direction": "backward",
                    "begin": "1984-01-27",
                    "artist": {"id": "a1", "name": "K", "sort-name": "K"},
                }
            )
        ]
        assert _extract_session_date(rels) == ("1984-01-27", "")

    def test_non_session_type_excluded(self) -> None:
        """Non-session relation types do not contribute to session dates."""
        rels = [
            MBArtistRelation.model_validate(
                {
                    "type": "composer",
                    "direction": "backward",
                    "begin": "1800",
                    "end": "1827",
                    "artist": {"id": "a1", "name": "B", "sort-name": "B"},
                }
            )
        ]
        assert _extract_session_date(rels) == ("", "")

    def test_empty_list_returns_empty_tuple(self) -> None:
        """Empty relation list returns ('', '')."""
        assert _extract_session_date([]) == ("", "")

    def test_multi_year_range(self) -> None:
        """Sessions spanning a calendar year boundary return correct begin and end."""
        rels = [
            MBArtistRelation.model_validate(
                {
                    "type": "conductor",
                    "direction": "backward",
                    "begin": "1983-12-20",
                    "end": "1984-01-05",
                    "artist": {"id": "a1", "name": "K", "sort-name": "K"},
                }
            )
        ]
        assert _extract_session_date(rels) == ("1983-12-20", "1984-01-05")


# ---------------------------------------------------------------------------
# extract_work_artist_rels — adapter, dedication, choreographer
# ---------------------------------------------------------------------------


class TestExtractWorkArtistRelsNewTypes:
    """Tests for adapter/dedication/choreographer relation handling."""

    def test_adapter_routed_to_arrangers(self) -> None:
        """'adapter' relation is routed to role_buckets.arrangers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {"type": "adapter", "direction": "backward", "artist": {"id": "a1", "name": "A", "sort-name": "A"}}
                    ]
                }
            ),
            rb,
        )
        assert len(rb.arrangers) == 1
        assert rb.arrangers[0].mbid == "a1"

    def test_dedication_routed_to_dedicatees(self) -> None:
        """'dedication' relation is routed to role_buckets.dedicatees."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {"type": "dedication", "direction": "backward", "artist": {"id": "d1", "name": "D", "sort-name": "D"}}
                    ]
                }
            ),
            rb,
        )
        assert len(rb.dedicatees) == 1

    def test_choreographer_routed_to_choreographers(self) -> None:
        """'choreographer' relation is routed to role_buckets.choreographers."""
        rb = RoleBuckets()
        extract_work_artist_rels(
            _w(
                {
                    "artist-relation-list": [
                        {
                            "type": "choreographer",
                            "direction": "backward",
                            "artist": {"id": "c1", "name": "C", "sort-name": "C"},
                        }
                    ]
                }
            ),
            rb,
        )
        assert len(rb.choreographers) == 1
