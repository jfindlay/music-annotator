"""Unit tests for music_annotator (pure-logic functions, no real I/O or MB API calls)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    CANNOT_RECOMPUTE,
    JOURNAL_FILENAME,
    PROVENANCE_FILENAME,
    artist_credit_phrase,
    artist_ids,
    artist_sort_names,
    build_cwp_tags,
    build_dest_path,
    build_track_tags,
    build_work_hierarchy,
    canonical_artist_form,
    collect_work_dates,
    collect_work_tags_and_key,
    configure_color,
    extract_work_artist_rels,
    is_catalogue_colon_corrupt,
    is_choir,
    is_ensemble,
    is_orchestra,
    last_name,
    parse_year,
    period_for_year,
    rederive_part_label,
    safe_name,
    strip_common_prefix,
)
from music_annotator._audit import _audit_tier_pass, _make_audit_counts
from music_annotator._mb_api import _extract_session_date
from music_annotator._pipeline_io import _write_provenance_fields
from music_annotator._tags import _NAME_MAX, _proposed_short, _top_dir_component, _work_aliases
from music_annotator._works import (
    _date_range,
    _old_bare_colon_split,
    _score_top_work,
    select_primary_performance_work,
    work_group_modal_depth,
)
from music_annotator.models import (
    JSON,
    AccurateRipSummary,
    AnnotationTier,
    ArtistEntry,
    MBAlias,
    MBArtist,
    MBArtistRelation,
    MBLabelRelation,
    MBPlaceRelation,
    MBRelease,
    MBTrack,
    MBUrlRelation,
    MBWork,
    MBWorkRelation,
    ProvenanceSidecar,
    RoleBuckets,
    TrackTags,
    TransactionEntry,
)
from tests.conftest import _ac, _rec, _rel, _trk, _w

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

    def test_catalogue_colon_not_a_separator(self) -> None:
        """A colon inside a catalogue number (no trailing space) does not trigger a split.

        Regression for the Haydn Hoboken case: ``"…, Hob. III:31"`` with a non-matching parent must
        return the full child title, not the spurious bare label ``"31"`` produced by a bare-colon split.
        """
        child = "String Quartet in E-flat major, op. 20 no. 1, Hob. III:31"
        parent = "String Quartets, op. 20"
        result = strip_common_prefix(child, parent)
        assert result == child

    def test_colon_space_separator_still_splits(self) -> None:
        """A genuine ``": "`` title-vs-movement separator still splits when the prefix does not match."""
        assert strip_common_prefix("RV 249: I. Allegro", "Some Parent") == "I. Allegro"

    def test_first_colon_space_wins_over_later_catalogue_colon(self) -> None:
        """When both a ``": "`` separator and a later catalogue colon are present, split on the separator.

        Handel ``"HWV 350: 16: (Minuet)"`` (with a non-matching parent) splits on the first ``": "``,
        yielding the movement label rather than mangling on the trailing catalogue colon.
        """
        result = strip_common_prefix("Suite in G major, HWV 350: 16: (Minuet)", "Nonmatching")
        assert result == "16: (Minuet)"


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


class TestProposedShortSuffixAware:
    """Tests for the audio_suffix parameter of _proposed_short — suffix-safe truncation.

    Verifies that truncation always reserves the suffix's bytes so that stem+suffix ≤ _NAME_MAX,
    and that the correct audio extension is preserved in the result.
    """

    def test_title_within_limit_but_title_plus_suffix_over(self) -> None:
        """Stem within _NAME_MAX but stem+suffix over → shortened, ends with suffix, ≤ _NAME_MAX.

        This is the primary bug case: a title that fits on its own but whose leaf (title+".flac")
        exceeds the limit.  Without suffix awareness the ellipsis strategies cut into the suffix.
        """
        # Build a stem that fits within _NAME_MAX on its own but not with ".flac" appended.
        # ".flac" is 5 bytes; stem must be > _NAME_MAX - 5 = 250 bytes and ≤ _NAME_MAX = 255 bytes.
        stem = "01 - " + "A" * 247  # 252 bytes — fits alone, but 252 + 5 = 257 > 255
        assert len(stem.encode("utf-8")) <= _NAME_MAX, "stem must fit alone for this test to be meaningful"
        leaf = stem + ".flac"
        assert len(leaf.encode("utf-8")) > _NAME_MAX, "leaf must exceed limit"
        result = _proposed_short(leaf, audio_suffix=".flac")
        assert result.endswith(".flac"), f"result must end with .flac, got {result!r}"
        assert len(result.encode("utf-8")) <= _NAME_MAX, (
            f"result must fit within _NAME_MAX, got {len(result.encode('utf-8'))} bytes"
        )

    def test_title_already_over_limit(self) -> None:
        """Stem already over _NAME_MAX → shortened, ends with suffix, ≤ _NAME_MAX."""
        # Stem alone exceeds _NAME_MAX; adding ".flac" makes it even longer.
        stem = "01 - " + "B" * 260  # 265 bytes > 255
        assert len(stem.encode("utf-8")) > _NAME_MAX
        leaf = stem + ".flac"
        result = _proposed_short(leaf, audio_suffix=".flac")
        assert result.endswith(".flac"), f"result must end with .flac, got {result!r}"
        assert len(result.encode("utf-8")) <= _NAME_MAX, (
            f"result must fit within _NAME_MAX, got {len(result.encode('utf-8'))} bytes"
        )

    def test_short_leaf_unchanged(self) -> None:
        """A leaf already within _NAME_MAX is returned unchanged — no gratuitous ellipsis."""
        leaf = "01 - Sonata in C major.flac"
        assert len(leaf.encode("utf-8")) <= _NAME_MAX
        result = _proposed_short(leaf, audio_suffix=".flac")
        assert result == leaf, f"short leaf must be returned unchanged, got {result!r}"

    def test_trailing_dot_in_stem_not_mistaken_for_extension(self) -> None:
        """A trailing dot in the work title (e.g. 'op.') is not mistaken for the audio extension.

        Path.suffix on '01 - Sonata op. 23.flac' would return '. 23' (wrong); the fix uses the
        known audio extension directly so 'op.' is preserved as part of the stem.
        """
        # Build a leaf whose stem ends in "op. 23" — the ". 23" must NOT be treated as the suffix.
        # Make it long enough to require truncation: "01 - " (5) + "Sonata " * 36 (252) + "op. 23" (6) = 263 bytes.
        stem = "01 - " + "Sonata " * 36 + "op. 23"  # ends in "op. 23", well over 255 bytes
        assert len(stem.encode("utf-8")) > _NAME_MAX
        leaf = stem + ".flac"
        result = _proposed_short(leaf, audio_suffix=".flac")
        assert result.endswith(".flac"), f"result must end with .flac, not with '. 23', got {result!r}"
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_mp3_suffix_preserved(self) -> None:
        """The .mp3 suffix is preserved just as .flac is."""
        stem = "01 - " + "C" * 260  # over limit
        leaf = stem + ".mp3"
        result = _proposed_short(leaf, audio_suffix=".mp3")
        assert result.endswith(".mp3"), f"result must end with .mp3, got {result!r}"
        assert len(result.encode("utf-8")) <= _NAME_MAX

    def test_no_suffix_behaves_as_before(self) -> None:
        """Calling _proposed_short without audio_suffix (default '') behaves as the original function."""
        component = "X" * 300  # no spaces, no suffix
        result = _proposed_short(component)
        assert result.endswith("…")
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

        Sets ``cwp_worktype_genres_top="Classical"`` so that ``IS_CLASSICAL`` is set to ``"1"``
        (the CE-classical predicate requires both ``cwp_work_top`` non-empty and
        ``cwp_worktype_genres_top`` containing ``"Classical"``).

        :param kwargs: Additional keyword arguments for TrackTags.
        :returns: A TrackTags instance.
        """
        return TrackTags(
            title=kwargs.get("title", "I. Allegro"),
            movementnumber=kwargs.get("movementnumber", "1"),
            movementtotal=kwargs.get("movementtotal", "4"),
            cwp_work_top=kwargs.get("cwp_work_top", "Symphony"),
            cwp_workid_top=kwargs.get("cwp_workid_top", "w1"),
            cwp_worktype_genres_top=kwargs.get("cwp_worktype_genres_top", "Classical"),
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

    def test_no_composer_in_tags_uses_recital_shape(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses performer-led shape (albumartist alone) when CWP/CEA composer tags are empty.

        When cwp_composer_lastnames and cea_composer_lastnames are both empty, the performer-led
        branch of :func:`~music_annotator._tags._top_dir_component` fires and uses albumartist as
        the top_dir.  The release.artist_credit is not consulted (the performer-led branch
        short-circuits it).  The album name is excluded from the topmost path component.

        When albumartist is also empty, falls back to album title alone (or "Unknown Album").

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # No composer in tags, no albumartist → performer-led branch → "Unknown Album" top_dir.
        tags = self._make_tags_no_composer(title="Track")
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Album", "artist-credit": [" & "], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Track"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        # Performer-led branch: top_dir is albumartist-based (or "Unknown Album" when empty).
        # C-UNIVERSAL: no class prefix — parts[0] is the top_dir directly.
        assert rel.parts[0] == "Unknown Album", (
            f"Expected top_dir 'Unknown Album' (no albumartist, no class prefix), got {rel.parts[0]!r}"
        )

    def test_no_composer_in_tags_with_albumartist_uses_performer_first(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses albumartist alone when CWP/CEA composer tags are empty but albumartist is set.

        The performer-led branch uses albumartist as the primary attribution.  The album name is
        excluded from the topmost path component (album identity belongs to the playlist lens).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_no_composer(title="Track")
        tags.albumartist = "Mitsuko Uchida"
        tags.album = "Schubert Sonatas"
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Schubert Sonatas", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Track"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        # C-UNIVERSAL: no class prefix — parts[0] is the top_dir directly.
        assert "Mitsuko Uchida" in rel.parts[0], (
            f"Expected albumartist 'Mitsuko Uchida' in top_dir (parts[0]), got {rel.parts[0]!r}"
        )

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
            cwp_worktype_genres_top="Classical",
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
        # result.parts: ["/", "lib", top_dir, work_dir, filename]  (no class prefix — C-UNIVERSAL)
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
            cwp_worktype_genres_top="Classical",
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
        conductor = ArtistEntry(name="Karajan", sort="Karajan, H", mbid="")
        tags = TrackTags(
            title="T",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Work",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Bach",
            cwp_worktype_genres_top="Classical",
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

    def test_album_conductor_used_over_track_only_conductor(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses album-level conductor, not track-only conductor.

        When the release artist credit names the conductor, the album-level list is non-empty and
        that name is used for the directory.  A track-only conductor (not in release.artist_credit)
        is ignored for the path, even if it is in cea_conductors_list.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        album_conductor = ArtistEntry(name="Marriner", sort="Marriner, N", mbid="")
        track_only_conductor = ArtistEntry(name="TrackOnly", sort="TrackOnly, X", mbid="")
        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Mozart",
            cwp_worktype_genres_top="Classical",
            cea_conductors_list=[album_conductor, track_only_conductor],
            cea_ensembles_list=[],
            cea_album_conductors_list=[album_conductor],
            cea_album_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        assert "Marriner" in str(result)
        assert "TrackOnly" not in str(result)

    def test_album_ensemble_used_over_track_only_ensemble(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses album-level ensemble, not track-only ensemble.

        Same contract as the conductor variant above but for ensembles.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        album_ensemble = ArtistEntry(name="ASMiF", sort="ASMiF", mbid="")
        track_only_ensemble = ArtistEntry(name="ASMiF Chamber Ensemble", sort="ASMiF Chamber Ensemble", mbid="")
        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Mozart",
            cwp_worktype_genres_top="Classical",
            cea_conductors_list=[],
            cea_ensembles_list=[album_ensemble, track_only_ensemble],
            cea_album_conductors_list=[],
            cea_album_ensembles_list=[album_ensemble],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        assert "ASMiF" in str(result)
        # Track-only named subgroup must not appear in the directory path.
        assert "Chamber Ensemble" not in str(result)

    def test_empty_album_lists_fall_back_to_all_conductors_ensembles(self, fs: FakeFilesystem) -> None:
        """build_dest_path falls back to per-track union when album-level lists are empty.

        When the release artist credit has no conductors or ensembles (e.g. composer-only release),
        the path must still include the track-level performers.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        conductor = ArtistEntry(name="Gardiner", sort="Gardiner, J", mbid="")
        tags = TrackTags(
            title="Kyrie",
            movementnumber="1",
            movementtotal="6",
            cwp_work_top="Mass in B minor",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Bach",
            cwp_worktype_genres_top="Classical",
            cea_conductors_list=[conductor],
            cea_ensembles_list=[],
            cea_album_conductors_list=[],
            cea_album_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Kyrie"}}),
            tags,
        )
        assert "Gardiner" in str(result)

    def test_album_conductor_and_ensemble_both_appear(self, fs: FakeFilesystem) -> None:
        """build_dest_path joins album conductor and ensemble with '; '.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        conductor = ArtistEntry(name="Karajan", sort="Karajan, H", mbid="")
        ensemble = ArtistEntry(name="BPO", sort="BPO", mbid="")
        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            cea_conductors_list=[conductor],
            cea_ensembles_list=[ensemble],
            cea_album_conductors_list=[conductor],
            cea_album_ensembles_list=[ensemble],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        # result.parts: ["/", "lib", top_dir, work_dir, filename]  (no class prefix — C-UNIVERSAL)
        top_dir = result.parts[2]
        assert "Karajan" in top_dir
        assert "BPO" in top_dir

    def test_album_tag_used_as_work_dir_when_no_work(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses ALBUM as the work directory when CWP_WORK_TOP and WORK are absent.

        Non-classical releases have no CWP work hierarchy and no WORK tag.  Without this fallback,
        work_dir would be ``""`` and Path would collapse it, reducing the relative path to two
        components and corrupting work_top_dir derivation in the copy pipeline.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Summer Love",
            album="Liz Rhodes",
            movementnumber="1",
            movementtotal="10",
            artist="Rhodes, Liz",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Liz Rhodes", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Summer Love"}}),
            tags,
        )
        # Must have at least 5 parts: /, lib, top_dir, work_dir, leaf (no class prefix — C-UNIVERSAL)
        assert len(result.parts) >= 5, "work_dir must not collapse when ALBUM is the only work tag"
        assert "Liz Rhodes" in result.parts[3]

    def test_unknown_album_fallback_when_all_work_tags_absent(self, fs: FakeFilesystem) -> None:
        """build_dest_path uses 'Unknown Album' when CWP_WORK_TOP, WORK, and ALBUM are all absent.

        Ensures the path always has a non-empty work-dir component even for the most bare-bones
        tags, preventing the two-component path collapse that causes ENOTDIR in the copy pipeline.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Track One",
            movementnumber="1",
            movementtotal="1",
            artist="Some Artist",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Album", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Track One"}}),
            tags,
        )
        assert len(result.parts) >= 4, "work_dir must not collapse when all work tags are absent"
        assert "Unknown Album" in result.parts[3]


# ---------------------------------------------------------------------------
# build_dest_path — C-NOSOLO freeze witness (KAT)
# ---------------------------------------------------------------------------


class TestBuildDestPathConcertoNoSoloist:
    """KAT (C-NOSOLO): build_dest_path never injects a soloist into any path component.

    STYLEGUIDE 4.5 / SEL-11: the soloist is never a path component, however principal.
    Two assertions freeze C-NOSOLO:

    1. A Concerto work whose recording carries a named soloist (Mutter) — the soloist name
       is absent from every path component.
    2. A multi-disc concerto with different soloists per disc — all movements still land under
       the *same* top directory, driven purely by the conductor/ensemble component, proving
       the deletion did not regress cross-medium grouping.
    """

    def _make_concerto_tags(
        self,
        *,
        conductor_name: str = "Karajan",
        soloist_name: str = "Mutter",
        cwp_movt_num: str = "1",
    ) -> TrackTags:
        """Build a TrackTags instance for a Classical Concerto movement with a named soloist.

        The soloist is present in ``cea_soloists`` (per-track) and ``cea_album_soloists``
        (album-level) to mirror what the pipeline would produce.  C-NOSOLO asserts neither
        field ever reaches the path.

        :param conductor_name: Name to use for the album-level conductor list entry.
        :param soloist_name: Soloist name to embed in ``cea_soloists`` / ``cea_album_soloists``.
        :param cwp_movt_num: Movement number string (used as the leaf ``nn`` prefix).
        :returns: A populated :class:`~music_annotator.models.TrackTags` instance.
        """
        conductor = ArtistEntry(name=conductor_name, sort=f"{conductor_name}, X", mbid="")
        return TrackTags(
            title="I. Allegro",
            movementnumber=cwp_movt_num,
            movementtotal="3",
            cwp_work_top="Violin Concerto in D major",
            cwp_workid_top="w-conc-1",
            cwp_composer_lastnames="Brahms",
            cwp_worktype_genres_top="Classical",
            cwp_movt_num=cwp_movt_num,
            cea_soloists=soloist_name,
            cea_album_soloists=soloist_name,
            cea_conductors_list=[conductor],
            cea_ensembles_list=[],
            cea_album_conductors_list=[conductor],
            cea_album_ensembles_list=[],
        )

    def test_concerto_soloist_absent_from_all_path_components(self, fs: FakeFilesystem) -> None:
        """KAT (C-NOSOLO): soloist name is absent from every path component for a Concerto work.

        A Concerto recording with soloist "Mutter" must produce a path that contains "Mutter"
        in no component — not the class dir, not the top_dir, not the work_dir, not the filename.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_concerto_tags(conductor_name="Karajan", soloist_name="Mutter")
        result = build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        path_str = str(result)
        assert "Mutter" not in path_str, f"C-NOSOLO violated: soloist 'Mutter' found in path '{path_str}'"
        # Conductor must still be present — the performers component is conductors → ensembles.
        rel = result.relative_to(dest_root)
        top_dir = rel.parts[0]  # C-UNIVERSAL: parts[0] = top_dir (no class prefix)
        assert "Karajan" in top_dir, f"Expected conductor 'Karajan' in top_dir '{top_dir}' (performers component intact)"

    def test_multi_disc_concerto_same_top_dir_without_soloist(self, fs: FakeFilesystem) -> None:
        """KAT S1b (C-NOSOLO): multi-disc concerto movements share one top directory via conductor/ensemble.

        Two movements from different discs, each with a different soloist (Mutter on disc 1,
        Perlman on disc 2), must produce paths that share the same top-level directory.  The
        grouping is driven purely by the conductor/ensemble component (Karajan), not by any
        soloist union — proving the deletion did not regress cross-medium grouping.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # Both movements share the same conductor (Karajan) → same performers component → same top_dir.
        tags_d1 = self._make_concerto_tags(conductor_name="Karajan", soloist_name="Mutter", cwp_movt_num="1")
        tags_d2 = self._make_concerto_tags(conductor_name="Karajan", soloist_name="Perlman", cwp_movt_num="2")

        result_d1 = build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags_d1,
        )
        result_d2 = build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t2", "position": 1, "recording": {"id": "rec2", "title": "II. Adagio"}}),
            tags_d2,
        )

        # Both paths must be under the same top_dir (C-UNIVERSAL: no class prefix).
        rel_d1 = result_d1.relative_to(dest_root)
        rel_d2 = result_d2.relative_to(dest_root)
        top_dir_d1 = rel_d1.parts[0]  # C-UNIVERSAL: parts[0] = top_dir (no class prefix)
        top_dir_d2 = rel_d2.parts[0]
        assert top_dir_d1 == top_dir_d2, (
            f"C-NOSOLO cross-medium grouping regressed: disc-1 top_dir '{top_dir_d1}' != "
            f"disc-2 top_dir '{top_dir_d2}'.  Both should be 'Brahms - Karajan [...]'."
        )
        # Neither soloist name appears in any path component.
        for soloist in ("Mutter", "Perlman"):
            assert soloist not in str(result_d1), f"C-NOSOLO violated: '{soloist}' in disc-1 path '{result_d1}'"
            assert soloist not in str(result_d2), f"C-NOSOLO violated: '{soloist}' in disc-2 path '{result_d2}'"


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

    def test_recording_date_work_overrides_per_track_recording_date(self, fs: FakeFilesystem) -> None:
        """recording_date_work takes priority over per-track recording_date for the directory label.

        When recording_date_work is set (by run() to the union range across all movements of the
        work), build_dest_path must use it so that movements with different individual session dates
        still land in the same destination directory.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # Per-track date says 1981; work-level union says 1981-1984.  The directory should
        # reflect the union range, not the per-track value.
        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 5",
            cwp_composer_lastnames="Beethoven",
            recording_date="1981-10-01",
            recording_date_work="1981-01-15/1984-02-20",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = str(
            build_dest_path(
                dest_root,
                self._make_rel(),
                _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
                tags,
            )
        )
        assert "[rec 1981-1984]" in result
        assert "[rec 1981]" not in result.replace("[rec 1981-1984]", "")

    def test_no_year_suffix_when_all_dates_absent(self, fs: FakeFilesystem) -> None:
        """No year suffix when no date fields are present.

        :param fs: pyfakefs fixture.
        """
        assert "[" not in self._dest(self._make_tags(), fs)

    def test_cwp_movt_num_used_in_2level_hierarchy(self, fs: FakeFilesystem) -> None:
        """CWP_MOVT_NUM sets the track prefix for 2-level hierarchies (no intermediate dirs).

        Disc 2 of a multi-disc work has cwp_movt_num=13 (the per-group index, disc-spanning);
        the file should be prefixed '13 -', not '01 -' from the disc-local MOVEMENTNUMBER.

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
            cwp_movt_num="13",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("13 -")

    def test_2level_cwp_movt_num_is_primary_leaf_authority(self, fs: FakeFilesystem) -> None:
        """CWP_MOVT_NUM is the primary leaf authority for 2-level hierarchies.

        When cwp_movt_num is set it drives the file prefix regardless of MOVEMENTNUMBER or
        any ordering-key value.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags(originaldate="1974")
        tags.cwp_movt_num = "3"
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("03 -")

    def test_2level_falls_back_to_global_track_idx_when_cwp_movt_num_absent(self, fs: FakeFilesystem) -> None:
        """Without CWP_MOVT_NUM, global_track_idx drives the 2-level file prefix.

        This is the multi-disc fallback: track.position resets to 1 for each disc, so using it
        as the leaf nn would produce collisions across discs.  global_track_idx is the 1-based
        running index across all source files in the session and is always globally unique.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # No cwp_movt_num → global_track_idx must win.
        tags = self._make_tags(originaldate="1974")
        tags.cwp_movt_num = ""  # absent
        tags.movementnumber = ""  # also absent — confirms MOVEMENTNUMBER is not a leaf source
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
            global_track_idx=17,
        )
        assert result.name.startswith("17 -")

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
        cwp_movt_num: str = "4",
        movementnumber: str = "17",
    ) -> TrackTags:
        """Build TrackTags simulating a 3-level opera hierarchy.

        Level 0 = aria (leaf), level 1 = act (intermediate), level 2 = opera (root/top).

        :param act_part: Stripped part title for the act (level 1).
        :param act_ordering_key: MB ordering-key for the act within the opera (intermediate level).
        :param cwp_movt_num: Per-group track index driving the leaf nn (C-L0 authority).
        :param movementnumber: Global MOVEMENTNUMBER tag (composer's numbering, in title only).
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
            cwp_movt_num=cwp_movt_num,
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_part_0"] = "Esultate!"  # type: ignore[index]
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

    def test_leaf_nn_from_cwp_movt_num(self, fs: FakeFilesystem) -> None:
        """Leaf filename nn uses CWP_MOVT_NUM (the per-group track index).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(cwp_movt_num="4")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("04")

    def test_leaf_nn_cwp_movt_num_is_primary_authority_in_3level(self, fs: FakeFilesystem) -> None:
        """CWP_MOVT_NUM is the primary leaf authority in 3-level hierarchies.

        When cwp_movt_num is set it drives the file prefix; MOVEMENTNUMBER (the composer's global
        numbering) is not used as a leaf source — it appears only in the title portion.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(cwp_movt_num="17", movementnumber="17")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        assert result.name.startswith("17")

    def test_leaf_nn_falls_back_to_global_track_idx_when_cwp_movt_num_absent(self, fs: FakeFilesystem) -> None:
        """Leaf nn in 3-level hierarchy falls back to global_track_idx when CWP_MOVT_NUM is absent.

        For multi-disc works where CWP_MOVT_NUM is not set, track.position resets per disc
        and is an unreliable fallback.  global_track_idx provides a globally unique running index.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        # cwp_movt_num="" (absent) and movementnumber="" — confirms neither drives the leaf
        tags = self._make_tags_3level(cwp_movt_num="", movementnumber="")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
            global_track_idx=23,
        )
        assert result.name.startswith("23")

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
        """MOVEMENTNUMBER appears in the title portion of the filename, not as the leaf prefix.

        The leaf nn prefix comes from CWP_MOVT_NUM (the per-group track index); the composer's
        global movement number appears in the TITLE tag and therefore in the track title portion.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = self._make_tags_3level(cwp_movt_num="4", movementnumber="17")
        result = build_dest_path(
            dest_root,
            self._make_rel(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "T"}}),
            tags,
        )
        # File starts with "04 - " (cwp_movt_num=4), title contains "No. 17"
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
            cwp_movt_num="3",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_part_0"] = "Aria"  # type: ignore[index]
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
# build_dest_path — leaf sequential numbering (KAT for C-L0)
# ---------------------------------------------------------------------------


class TestBuildDestPathLeafSequential:
    """KAT: leaf nn is sequential, gap-free, collision-free — driven by CWP_MOVT_NUM (C-L0)."""

    def _make_rel(self) -> MBRelease:
        """Build a minimal release.

        :returns: An MBRelease instance.
        """
        return _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []})

    def _make_tags(self, cwp_movt_num: str, cwp_movt_tot: str = "3") -> TrackTags:
        """Build TrackTags for a 2-level work (symphony-with-movements shape).

        :param cwp_movt_num: Per-group track index (the C-L0 leaf authority).
        :param cwp_movt_tot: Total tracks in the group (for MOVEMENTTOTAL/width).
        :returns: A TrackTags instance with cwp_part_levels=1 (2-level hierarchy).
        """
        return TrackTags(
            title="Movement",
            movementnumber=cwp_movt_num,
            movementtotal=cwp_movt_tot,
            cwp_work_top="Symphony No. 9",
            cwp_composer_lastnames="Mahler",
            originaldate="1998",
            cwp_part_levels="1",
            cwp_movt_num=cwp_movt_num,
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )

    def test_split_movement_leaf_sequential(self, fs: FakeFilesystem) -> None:
        """Leaf nn is sequential and collision-free even when recordings share one MB bottom work.

        Scenario A (the bug case): movement I of a symphony has >=3 sub-section recordings that
        share one MB bottom work — i.e. all would have the same CWP_ORDERING_KEY_0.  The leaf nn
        must come from CWP_MOVT_NUM (the per-group index), giving 01, 02, 03 with no repeats and
        no .dd suffix.

        Scenario B (Bach-Mass regression): distinct bottom works (one recording each) with
        cwp_movt_num = 1..N → leaves are still 01..N, gap-free.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        release = self._make_rel()
        track = _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "Movement"}})

        # --- Scenario A: three sub-section recordings sharing one MB bottom work.
        # All would have cwp_ordering_key_0=1 (constant across the bottom work).
        # CWP_MOVT_NUM = "1", "2", "3" from the per-group enumeration.
        leaves_a: list[str] = []
        for movt_num in ("1", "2", "3"):
            tags = self._make_tags(cwp_movt_num=movt_num)
            # Simulate the same bottom-work ordering key (the old bug source — all = "1").
            tags.model_extra["cwp_ordering_key_0"] = "1"  # type: ignore[index]
            result = build_dest_path(dest_root, release, track, tags)
            leaves_a.append(result.name)

        assert leaves_a == ["01 - Movement", "02 - Movement", "03 - Movement"], (
            f"Leaves collided or are out of order: {leaves_a}"
        )
        # No .dd suffix (dedup machinery must not fire for distinct cwp_movt_num values).
        assert not any(".dd" in leaf for leaf in leaves_a)

        # --- Scenario B: Bach-Mass-shaped regression — distinct bottom works, one recording each.
        # cwp_movt_num = 1..5; ordering keys happen to match (the clean case).
        leaves_b: list[str] = []
        for movt_num in ("1", "2", "3", "4", "5"):
            tags = self._make_tags(cwp_movt_num=movt_num, cwp_movt_tot="5")
            tags.model_extra["cwp_ordering_key_0"] = movt_num  # type: ignore[index]
            result = build_dest_path(dest_root, release, track, tags)
            leaves_b.append(result.name)

        assert leaves_b == [
            "01 - Movement",
            "02 - Movement",
            "03 - Movement",
            "04 - Movement",
            "05 - Movement",
        ], f"Bach-Mass regression: leaves not gap-free: {leaves_b}"


# ---------------------------------------------------------------------------
# build_dest_path — intermediate sibling numbering (KAT for C-L1)
# ---------------------------------------------------------------------------


class TestBuildDestPathIntermediateSiblingIndex:
    """KAT: intermediate directory nn is gap-free per-group sibling index (C-L1).

    Exercises the CWP_INTER_INDEX_{i} consumption path in build_dest_path and verifies
    that a non-contiguous ordering-key is overridden by the gap-free substrate index.
    """

    def _make_rel(self) -> MBRelease:
        """Build a minimal release.

        :returns: An MBRelease instance.
        """
        return _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []})

    def _make_opera_track(
        self,
        cwp_movt_num: str,
        act_part: str,
        act_workid: str,
        act_ordering_key: str,
        act_inter_index: str,
        scene_part: str = "",
        scene_workid: str = "",
        scene_ordering_key: str = "",
        scene_inter_index: str = "",
    ) -> TrackTags:
        """Build TrackTags for a 3-level opera track (leaf → act → opera).

        :param cwp_movt_num: Per-group track index (C-L0 leaf authority).
        :param act_part: Part title for the act (level 1).
        :param act_workid: MBID for the act (level 1) — used as node identity by the substrate.
        :param act_ordering_key: MB ordering-key for the act (may be non-contiguous).
        :param act_inter_index: Gap-free sibling index for the act (from the C-L1 substrate pass).
        :param scene_part: Optional part title for level 2 (4-level hierarchy).
        :param scene_workid: Optional MBID for level 2 (4-level hierarchy).
        :param scene_ordering_key: Optional ordering-key for level 2.
        :param scene_inter_index: Optional gap-free sibling index for level 2.
        :returns: A TrackTags instance with per-level extras set.
        """
        part_levels = "3" if scene_part else "2"
        tags = TrackTags(
            title="Aria",
            movementnumber=cwp_movt_num,
            movementtotal="10",
            cwp_work_top="Opera",
            cwp_composer_lastnames="Wagner",
            originaldate="1983",
            cwp_part_levels=part_levels,
            cwp_movt_num=cwp_movt_num,
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_part_0"] = "Aria"  # type: ignore[index]
        tags.model_extra["cwp_workid_0"] = f"w-aria-{cwp_movt_num}"  # type: ignore[index]
        tags.model_extra["cwp_part_1"] = act_part  # type: ignore[index]
        tags.model_extra["cwp_workid_1"] = act_workid  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_1"] = act_ordering_key  # type: ignore[index]
        tags.model_extra["cwp_inter_index_1"] = act_inter_index  # type: ignore[index]
        if scene_part:
            tags.model_extra["cwp_part_2"] = scene_part  # type: ignore[index]
            tags.model_extra["cwp_workid_2"] = scene_workid  # type: ignore[index]
            tags.model_extra["cwp_ordering_key_2"] = scene_ordering_key  # type: ignore[index]
            tags.model_extra["cwp_inter_index_2"] = scene_inter_index  # type: ignore[index]
        return tags

    def test_opera_scene_intermediate_dir_numbered(self, fs: FakeFilesystem) -> None:
        """Intermediate act dirs use gap-free CWP_INTER_INDEX_{i}, not the raw ordering-key.

        KAT for C-L1: Wagner-shaped opera where two acts have non-contiguous ordering-keys
        (e.g. 2 and 5 — as if MB assigned them with a gap).  The CWP_INTER_INDEX_1 substrate
        index provides gap-free values (1 and 2), so the rendered act directory prefixes must
        be ``01`` and ``02``, NOT ``02`` and ``05``.

        Also verifies that within each act the leaf nn comes from CWP_MOVT_NUM (C-L0), not the
        ordering-key.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        release = self._make_rel()
        trk = _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "Aria"}})

        # Act I: ordering-key=2 but sibling index=1 (gap-free).
        tags_act1 = self._make_opera_track(
            cwp_movt_num="1",
            act_part="Akt I",
            act_workid="w-act1",
            act_ordering_key="2",
            act_inter_index="1",
        )
        # Act II: ordering-key=5 but sibling index=2 (gap-free).
        tags_act2 = self._make_opera_track(
            cwp_movt_num="2",
            act_part="Akt II",
            act_workid="w-act2",
            act_ordering_key="5",
            act_inter_index="2",
        )

        path_act1 = build_dest_path(dest_root, release, trk, tags_act1)
        path_act2 = build_dest_path(dest_root, release, trk, tags_act2)

        # Assert: intermediate act prefix is from the gap-free sibling index, not the ordering-key.
        # With raw ordering-key=2/5 the prefixes would be "02" / "05".
        # With cwp_inter_index_1=1/2 the prefixes must be "01" / "02".
        act1_dir = next(p for p in path_act1.parts if "Akt I" in p)
        act2_dir = next(p for p in path_act2.parts if "Akt II" in p)
        assert act1_dir.startswith("01"), f"Act I dir should start with '01' (gap-free index=1), got '{act1_dir}'"
        assert act2_dir.startswith("02"), f"Act II dir should start with '02' (gap-free index=2), got '{act2_dir}'"
        # Leaf nn is from CWP_MOVT_NUM (C-L0) — not the ordering-key.
        assert path_act1.name.startswith("01"), f"Act I leaf should start with '01', got '{path_act1.name}'"
        assert path_act2.name.startswith("02"), f"Act II leaf should start with '02', got '{path_act2.name}'"

    def test_inter_index_absent_falls_back_to_ordering_key(self, fs: FakeFilesystem) -> None:
        """When CWP_INTER_INDEX_{i} is absent, the ordering-key is used for the intermediate nn.

        This exercises the no-group/no-hierarchy fallback path (escape hatch) introduced by C-L1.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        release = self._make_rel()
        trk = _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "Aria"}})

        # Construct a track with cwp_ordering_key_1=3 but no cwp_inter_index_1 set.
        tags = TrackTags(
            title="Aria",
            movementnumber="1",
            movementtotal="5",
            cwp_work_top="Opera",
            cwp_composer_lastnames="Verdi",
            originaldate="1980",
            cwp_part_levels="2",
            cwp_movt_num="1",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        tags.model_extra["cwp_part_0"] = "Aria"  # type: ignore[index]
        tags.model_extra["cwp_part_1"] = "Atto III"  # type: ignore[index]
        tags.model_extra["cwp_ordering_key_1"] = "3"  # type: ignore[index]
        # cwp_inter_index_1 is deliberately absent → fallback to ordering-key.

        result = build_dest_path(dest_root, release, trk, tags)
        act_dir = next(p for p in result.parts if "Atto III" in p)
        assert act_dir.startswith("03"), f"Fallback to ordering-key=3 expected '03', got '{act_dir}'"


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


# ---------------------------------------------------------------------------
# TestAuditTierPass — KATs: tier enumeration audit pass
# ---------------------------------------------------------------------------


def _write_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a journal JSON file to ``dest_root / JOURNAL_FILENAME``.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal_path.write_text(json.dumps(entries), encoding="utf-8")


class TestAuditTierPass:
    """KAT tests for the tier-enumeration audit pass.

    Covers :func:`music_annotator._audit._audit_tier_pass` directly and via the full
    :func:`music_annotator.audit` integration.  All tests use pyfakefs for filesystem isolation
    and patch the structlog ``log`` object in ``music_annotator._audit`` to assert on logged events.
    """

    # ------------------------------------------------------------------
    # KAT: test_audit_enumerates_tiers
    # ------------------------------------------------------------------

    def test_audit_enumerates_tiers(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: _audit_tier_pass counts per-tier and provisional_total correctly for a mixed library.

        Fixture library:
        - Work-A: ``full-mb-verified`` (1 track) → tier_full=1, provisional_total unchanged
        - Work-B: ``mb-search-resolved`` (1 track) → tier_search=1, provisional_total=1
        - Work-C: ``mb-partial`` (1 track) → tier_partial=1, provisional_total=2
        - Work-D: ``alternate-source`` (1 track) → tier_alt=1, provisional_total=3
        - Work-E: ``source-tags-only`` (1 track) → tier_source_only=1, provisional_total=4

        Asserts per-tier counts and provisional_total.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tiers = [
            ("Work-A [2020]", AnnotationTier.FULL_MB_VERIFIED, False),
            ("Work-B [2020]", AnnotationTier.MB_SEARCH_RESOLVED, True),
            ("Work-C [2020]", AnnotationTier.MB_PARTIAL, False),
            ("Work-D [2020]", AnnotationTier.ALTERNATE_SOURCE, False),
            ("Work-E [2020]", AnnotationTier.SOURCE_TAGS_ONLY, False),
        ]

        entries: list[dict[str, str]] = []
        for work_dir_name, tier, spot_check in tiers:
            work_top_dir = dest_root / "Composer - Performer" / work_dir_name
            work_top_dir.mkdir(parents=True, exist_ok=True)
            sidecar_path = work_top_dir / PROVENANCE_FILENAME
            _write_provenance_fields(
                sidecar_path,
                ProvenanceSidecar(
                    origin_time="2024-01-01T00:00:00+00:00",
                    origin_source="/rip/source",
                    annotation_tier=tier,
                    needs_spot_check=spot_check,
                ),
            )
            dest_file = str(work_top_dir / "01 - Track.flac")
            entries.append(
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": f"rel-{work_dir_name}",
                    "source": f"/src/{work_dir_name}/01.flac",
                    "destination": dest_file,
                    "action": "tagged",
                    "audio_hash": "",
                    "acoustid_id": "",
                }
            )

        journal_entries = [
            TransactionEntry(
                timestamp=e["timestamp"],
                release_id=e["release_id"],
                source=e["source"],
                destination=e["destination"],
                action=e["action"],
                audio_hash=e["audio_hash"],
                acoustid_id=e["acoustid_id"],
            )
            for e in entries
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_full"] == 1
        assert counts["tier_search"] == 1
        assert counts["tier_partial"] == 1
        assert counts["tier_alt"] == 1
        assert counts["tier_source_only"] == 1
        assert counts["provisional_total"] == 4

    def test_audit_enumerates_tiers_multi_track(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass applies a work_dir's tier to all its tracks (multi-track case).

        A single work_dir with ``mb-search-resolved`` and two tracks should yield
        ``tier_search=2``, ``provisional_total=2``, and ``needs_spot_check=2``.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Multi [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = work_top_dir / PROVENANCE_FILENAME
        _write_provenance_fields(
            sidecar_path,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
                needs_spot_check=True,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-multi",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-multi",
                source="/src/02.flac",
                destination=str(work_top_dir / "02 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_search"] == 2
        assert counts["provisional_total"] == 2
        assert counts["needs_spot_check"] == 2

    # ------------------------------------------------------------------
    # KAT: test_audit_flags_needs_spot_check
    # ------------------------------------------------------------------

    def test_audit_flags_needs_spot_check(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: _audit_tier_pass surfaces the mb-search-resolved population via needs_spot_check.

        A library with one ``mb-search-resolved`` work (needs_spot_check=True) and one
        ``full-mb-verified`` work (needs_spot_check=False) should yield needs_spot_check=1
        and log ``audit_tier_needs_spot_check`` exactly once.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Work-A: full-mb-verified, no spot check
        work_a = dest_root / "Composer - Performer" / "Work-A [2020]"
        work_a.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_a / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        # Work-B: mb-search-resolved, needs spot check
        work_b = dest_root / "Composer - Performer" / "Work-B [2020]"
        work_b.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_b / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
                needs_spot_check=True,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-a",
                source="/src/a/01.flac",
                destination=str(work_a / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-b",
                source="/src/b/01.flac",
                destination=str(work_b / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["needs_spot_check"] == 1
        assert counts["tier_full"] == 1
        assert counts["tier_search"] == 1

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_tier_needs_spot_check" in info_events
        assert "audit_tier_provisional" in info_events

    # ------------------------------------------------------------------
    # KAT: test_audit_enumerates_spot_check_population
    # ------------------------------------------------------------------

    def test_audit_enumerates_spot_check_population(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: _audit_tier_pass enumerates the spot-check population with AR status attached.

        Two fixtures:
        (a) ``mb-search-resolved`` work with a populated ``AccurateRipSummary`` (AR-verified):
            ``audit_tier_needs_spot_check`` must include ``ar_verified=True``,
            ``accurately_ripped=2``, ``in_ar_database=2``.
        (b) ``mb-search-resolved`` work with an empty ``AccurateRipSummary`` (no AR data):
            ``audit_tier_needs_spot_check`` must include ``ar_verified=False``,
            ``accurately_ripped=0``, ``in_ar_database=0``.

        This verifies that a rip that is AccurateRip-verified but only search-resolved is
        visibly distinguished from one with no AR data (J1 spot-check gate).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Work-A: mb-search-resolved + AR-verified (log_sha256 non-empty)
        work_a = dest_root / "Composer - Performer" / "Work-A [2020]"
        work_a.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_a / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="whipper",
                annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
                needs_spot_check=True,
                accuraterip_summary=AccurateRipSummary(
                    mb_disc_id="disc-abc",
                    log_sha256="A" * 64,
                    accurately_ripped=2,
                    in_ar_database=2,
                    summary_text="All tracks accurately ripped",
                ),
            ),
        )

        # Work-B: mb-search-resolved + no AR data (empty AccurateRipSummary)
        work_b = dest_root / "Composer - Performer" / "Work-B [2020]"
        work_b.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_b / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
                needs_spot_check=True,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-a",
                source="/src/a/01.flac",
                destination=str(work_a / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-b",
                source="/src/b/01.flac",
                destination=str(work_b / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["needs_spot_check"] == 2

        # Collect all audit_tier_needs_spot_check calls
        spot_check_calls = [c for c in mock_log.info.call_args_list if c.args[0] == "audit_tier_needs_spot_check"]
        assert len(spot_check_calls) == 2

        # Find the AR-verified call (work_a) and the no-AR call (work_b)
        ar_verified_calls = [c for c in spot_check_calls if c.kwargs.get("ar_verified") is True]
        no_ar_calls = [c for c in spot_check_calls if c.kwargs.get("ar_verified") is False]
        assert len(ar_verified_calls) == 1, "expected exactly one AR-verified spot-check call"
        assert len(no_ar_calls) == 1, "expected exactly one no-AR spot-check call"

        # AR-verified call must carry the correct counts
        ar_call = ar_verified_calls[0]
        assert ar_call.kwargs["accurately_ripped"] == 2
        assert ar_call.kwargs["in_ar_database"] == 2

        # No-AR call must carry zero counts
        no_ar_call = no_ar_calls[0]
        assert no_ar_call.kwargs["accurately_ripped"] == 0
        assert no_ar_call.kwargs["in_ar_database"] == 0

    def test_audit_tier_pass_skips_non_eligible_actions(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass skips entries with actions other than 'tagged' and 'enriched'.

        A 'copied' action entry should not contribute to any tier count.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-1",
                source="/src/01.flac",
                destination="/lib/Composer/Work [2020]/01.flac",
                action="copied",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_full"] == 0
        assert counts["provisional_total"] == 0
        assert counts["needs_spot_check"] == 0

    def test_audit_tier_pass_skips_dest_not_under_dest_root(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass silently skips entries whose destination is not under dest_root.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-1",
                source="/src/01.flac",
                destination="/other/Composer/Work [2020]/01.flac",
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_full"] == 0
        assert counts["provisional_total"] == 0

    def test_audit_tier_pass_skips_dest_with_too_few_parts(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass silently skips entries whose relative path has fewer than two parts.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-1",
                source="/src/01.flac",
                destination="/lib/only-one-part.flac",
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_full"] == 0
        assert counts["provisional_total"] == 0

    def test_audit_tier_pass_logs_unset_tier(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass logs audit_tier_unset when annotation_tier is empty in the sidecar.

        An empty annotation_tier is a defect state (lossless principle); the pass must flag it.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Unset [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        # Write a sidecar with no annotation_tier (empty string default)
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier="",
                needs_spot_check=False,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-unset",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_tier_unset" in warning_events
        assert counts["tier_full"] == 0
        assert counts["provisional_total"] == 0

    def test_audit_tier_pass_logs_unrecognised_tier(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass logs audit_tier_unset when annotation_tier is an unrecognised string.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Bad [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        # Write a sidecar with an invalid tier string directly (bypassing Pydantic validation)
        sidecar_path = work_top_dir / PROVENANCE_FILENAME
        sidecar_path.write_text(
            "origin_time: '2024-01-01T00:00:00+00:00'\norigin_source: /rip/source\nannotation_tier: not-a-valid-tier\n",
            encoding="utf-8",
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-bad",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mock_log = mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        warning_events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert "audit_tier_unset" in warning_events
        assert counts["tier_full"] == 0
        assert counts["provisional_total"] == 0

    def test_audit_tier_pass_deduplicates_destinations(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass counts each destination only once even if it appears multiple times in the journal.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Dup [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        dest_file = str(work_top_dir / "01 - Track.flac")
        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-dup",
                source="/src/01.flac",
                destination=dest_file,
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
            TransactionEntry(
                timestamp="2024-01-02T00:00:00Z",
                release_id="rel-dup",
                source="/src/01.flac",
                destination=dest_file,
                action="enriched",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_full"] == 1  # counted only once despite two journal entries

    def test_audit_tier_pass_enriched_action_eligible(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass includes 'enriched' action entries in the eligible set.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Enrich [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.SOURCE_TAGS_ONLY,
                needs_spot_check=False,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-enrich",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="enriched",
                audio_hash="flac-md5:aabb",
                acoustid_id="some-acoustid",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_source_only"] == 1
        assert counts["provisional_total"] == 1

    def test_audit_tier_pass_uses_freedb_sidecar_when_present(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_audit_tier_pass reads tier from a freedb_disc_N.yaml sidecar when one exists.

        When a ``freedb_disc_*.yaml`` file is present in the work_top_dir, the tier pass must
        read the tier from it rather than falling back to ``music_annotator_provenance.yaml``.
        This exercises the ``sidecar_path is not None`` branch in :func:`_audit_tier_pass`.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Freedb [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        # Write a freedb sidecar (not the fallback PROVENANCE_FILENAME)
        freedb_sidecar = work_top_dir / "freedb_disc_1.yaml"
        _write_provenance_fields(
            freedb_sidecar,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.MB_PARTIAL,
                needs_spot_check=False,
            ),
        )

        journal_entries = [
            TransactionEntry(
                timestamp="2024-01-01T00:00:00Z",
                release_id="rel-freedb",
                source="/src/01.flac",
                destination=str(work_top_dir / "01 - Track.flac"),
                action="tagged",
                audio_hash="",
                acoustid_id="",
            ),
        ]

        counts = _make_audit_counts()
        mocker.patch("music_annotator._audit.log")
        _audit_tier_pass(dest_root, journal_entries, counts)

        assert counts["tier_partial"] == 1
        assert counts["provisional_total"] == 1

    def test_audit_summary_includes_tier_counts(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """audit() logs audit_summary with tier counts after the tier-enumeration pass.

        Verifies that the full audit() integration includes tier_full, tier_search,
        provisional_total, and needs_spot_check in the audit_summary event.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        work_top_dir = dest_root / "Composer - Performer" / "Work-Full [2020]"
        work_top_dir.mkdir(parents=True, exist_ok=True)
        _write_provenance_fields(
            work_top_dir / PROVENANCE_FILENAME,
            ProvenanceSidecar(
                origin_time="2024-01-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
                needs_spot_check=False,
            ),
        )

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "release_id": "rel-full",
                    "source": "/src/01.flac",
                    "destination": str(work_top_dir / "01 - Track.flac"),
                    "action": "tagged",
                    "audio_hash": "",
                    "acoustid_id": "",
                }
            ],
        )

        mock_log = mocker.patch("music_annotator._audit.log")
        music_annotator.audit(dest_root=dest_root)

        info_events = [c.args[0] for c in mock_log.info.call_args_list]
        assert "audit_summary" in info_events
        summary_call = next(c for c in mock_log.info.call_args_list if c.args[0] == "audit_summary")
        assert summary_call.kwargs["tier_full"] == 1
        assert summary_call.kwargs["tier_search"] == 0
        assert summary_call.kwargs["provisional_total"] == 0
        assert summary_call.kwargs["needs_spot_check"] == 0


# ---------------------------------------------------------------------------
# C-UNIVERSAL KATs (a): prefix-less path witnesses — build_dest_path
# ---------------------------------------------------------------------------


class TestBuildDestPathPrefixLess:
    """KATs (a): prefix-less path witnesses for :func:`~music_annotator.build_dest_path` (C-UNIVERSAL).

    Verifies that the catalog path is prefix-less: the first component directly under ``dest_root``
    is the scholarship-stable first-component shape, with no top-level class directory.
    """

    def test_single_composer_classical_no_class_prefix(self, fs: FakeFilesystem) -> None:
        """Single-composer classical release → ``dest_root / "<Composer> - <Performers>" / …`` with no class component.

        The path was previously ``dest_root / "Classical" / "<Composer> - <Performers>" / …``.
        Under C-UNIVERSAL the class prefix is absent; the first component is the composer-first shape.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        conductor = ArtistEntry(name="Karajan", sort="Karajan, H", mbid="")
        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            cea_conductors_list=[conductor],
            cea_ensembles_list=[],
            cea_album_conductors_list=[conductor],
            cea_album_ensembles_list=[],
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        # parts[0] = top_dir (composer-first), parts[1] = work_dir, parts[2] = leaf — no class prefix.
        assert rel.parts[0].startswith("Beethoven"), (
            f"Expected top_dir to start with 'Beethoven' (no class prefix), got {rel.parts[0]!r}"
        )
        assert "Karajan" in rel.parts[0], f"Expected 'Karajan' in top_dir, got {rel.parts[0]!r}"
        assert "Classical" not in rel.parts, f"Expected no 'Classical' class prefix in path, got parts={rel.parts!r}"

    def test_pop_album_no_class_prefix(self, fs: FakeFilesystem) -> None:
        """Pop album → ``dest_root / "<Artist>" / …`` with no class component and no album name.

        A pop album (no linked composer) routes through the performer-led branch of
        :func:`~music_annotator._tags._top_dir_component`.  The album name belongs to the
        playlist lens and must not appear in the topmost path component.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Come Together",
            movementnumber="1",
            movementtotal="17",
            releasetype="Album",
            album="Abbey Road",
            albumartist="The Beatles",
            cwp_work_top="",
            cwp_worktype_genres_top="",
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Abbey Road", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Come Together"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        assert rel.parts[0] == "The Beatles", f"Expected top_dir 'The Beatles', got {rel.parts[0]!r}"
        assert "Abbey Road" not in rel.parts[0], (
            f"Album name must not appear in top_dir (belongs to playlist lens), got {rel.parts[0]!r}"
        )
        assert "Popular" not in rel.parts, f"Expected no 'Popular' class prefix in path, got parts={rel.parts!r}"

    def test_compilation_no_class_prefix(self, fs: FakeFilesystem) -> None:
        """Compilation → ``dest_root / "<Various Artists>" / …`` with no class component and no album name.

        A composerless compilation routes through the performer-led branch.  The album name
        belongs to the playlist lens and must not appear in the topmost path component.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Piano Concerto No. 1",
            movementnumber="1",
            movementtotal="1",
            releasetype_secondary="Compilation",
            albumartist="Various Artists",
            albumartistsort="Various Artists",
            album="Great Piano Concertos",
            cwp_work_top="",
            cwp_worktype_genres_top="",
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Great Piano Concertos", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Piano Concerto No. 1"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        assert rel.parts[0] == "Various Artists", f"Expected top_dir 'Various Artists', got {rel.parts[0]!r}"
        assert "Great Piano Concertos" not in rel.parts[0], (
            f"Album name must not appear in top_dir (belongs to playlist lens), got {rel.parts[0]!r}"
        )
        assert "Compilations" not in rel.parts, f"Expected no 'Compilations' class prefix in path, got parts={rel.parts!r}"


# ---------------------------------------------------------------------------
# C-UNIVERSAL KATs (b): first-component-rule witnesses — _top_dir_component
# ---------------------------------------------------------------------------


class TestTopDirComponent:
    """KATs (b): first-component-rule witnesses for :func:`~music_annotator._tags._top_dir_component` (C-UNIVERSAL).

    The topmost path component derives only from composer and performers — scholarship-stable data.
    The album name never appears in the topmost path component.  Free-classification parameters
    (``releasetype_secondary`` types such as "Compilation") never gate the topmost component.

    Two cases:

    1. Performer-led (no linked composer) → ``<albumartist>`` (or bare ``<album>`` when albumartist
       is empty — floor to avoid an empty top dir).
    2. Composer-bearing (dominant population) → ``None`` (caller uses ``<composer> - <performers>``).
       Applies regardless of ``releasetype_secondary``, including "Compilation".
    """

    def test_single_composer_returns_none(self) -> None:
        """Single-composer → None (caller uses <composer> - <performers> unchanged).

        The dominant population: a work with a single composer linked in MB.  _top_dir_component
        returns None to signal the caller should use the default composer-first shape.

        :returns: None.
        """
        tags = TrackTags(
            cwp_work_top="Symphony No. 9",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Beethoven",
            cea_composer_lastnames="Beethoven",
            albumartist="Herbert von Karajan",
            albumartistsort="Karajan, Herbert von",
            album="Beethoven: Symphony No. 9",
        )
        result = _top_dir_component(tags)
        assert result is None, f"Expected None for single-composer, got {result!r}"

    def test_performer_led_returns_albumartist(self) -> None:
        """Performer-led (no linked composer) → ``<albumartist>`` (album name excluded from path).

        Signal: CWP_COMPOSER_LASTNAMES and CEA_COMPOSER_LASTNAMES are both empty.
        The topmost path component derives from the album artist alone; the album name belongs to
        the playlist lens, not the directory tree.  Universal: a pop album routes here exactly as
        a classical recital does.

        :returns: None.
        """
        tags = TrackTags(
            cwp_work_top="Sonata in B minor",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            albumartist="Mitsuko Uchida",
            albumartistsort="Uchida, Mitsuko",
            album="Schubert: Piano Sonatas",
        )
        result = _top_dir_component(tags)
        assert result is not None, "Expected a non-None result for performer-led"
        assert result == "Mitsuko Uchida", f"Expected albumartist 'Mitsuko Uchida' as top_dir, got {result!r}"
        # The album name must not appear in the topmost path component.
        assert "Schubert" not in result and "Piano Sonatas" not in result, (
            f"Album name must not appear in top_dir (belongs to playlist lens), got {result!r}"
        )

    def test_performer_led_pop_album_returns_albumartist(self) -> None:
        """Pop album (no linked composer) → ``<albumartist>`` (album name excluded from path).

        Demonstrates the universal nature of the performer-led branch: a pop album routes here
        exactly as a classical recital does.  The album name belongs to the playlist lens, not
        the directory tree.

        :returns: None.
        """
        tags = TrackTags(
            releasetype="Album",
            cwp_work_top="",
            cwp_worktype_genres_top="",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            albumartist="The Beatles",
            albumartistsort="Beatles, The",
            album="Abbey Road",
        )
        result = _top_dir_component(tags)
        assert result is not None, "Expected a non-None result for pop album"
        assert result == "The Beatles", f"Expected 'The Beatles' as top_dir, got {result!r}"
        # The album name must not appear in the topmost path component.
        assert "Abbey Road" not in result, f"Album name must not appear in top_dir (belongs to playlist lens), got {result!r}"

    def test_compilation_with_composer_returns_none(self) -> None:
        """Compilation with a linked composer → ``None`` (falls through to composer-bearing case).

        The topmost path component derives only from composer and performers; the album name never
        appears in the path.  A free-classification parameter (``releasetype_secondary`` containing
        "Compilation") must not gate the topmost component when a composer is present.

        :returns: None.
        """
        tags = TrackTags(
            cwp_work_top="Piano Concerto No. 1",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Beethoven",
            cea_composer_lastnames="Beethoven",
            releasetype_secondary="Compilation",
            albumartist="Various Artists",
            albumartistsort="Various Artists",
            album="Great Piano Concertos",
        )
        result = _top_dir_component(tags)
        assert result is None, (
            f"Expected None for compilation with linked composer (caller uses <composer> - <performers>), got {result!r}"
        )

    def test_compilation_named_artist_with_composer_returns_none(self) -> None:
        """Compilation with a named albumartist and a linked composer → ``None``.

        The "Compilation" secondary type does not short-circuit when a composer is present.
        The topmost path component derives from composer + performers, not from the album name.

        :returns: None.
        """
        tags = TrackTags(
            cwp_work_top="Violin Concerto",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Brahms",
            cea_composer_lastnames="Brahms",
            releasetype_secondary="Compilation",
            albumartist="Itzhak Perlman",
            albumartistsort="Perlman, Itzhak",
            album="Perlman Plays Concertos",
        )
        result = _top_dir_component(tags)
        assert result is None, (
            f"Expected None for compilation with linked composer (caller uses <composer> - <performers>), got {result!r}"
        )

    def test_composerless_compilation_returns_albumartist(self) -> None:
        """Composerless compilation → ``<albumartist>`` (performer-led branch; album name excluded).

        Regression guard: when no composer is linked, the performer-led branch fires regardless
        of ``releasetype_secondary``.  The album artist is the primary attribution; the album name
        belongs to the playlist lens and must not appear in the topmost path component.

        :returns: None.
        """
        tags = TrackTags(
            cwp_work_top="",
            cwp_worktype_genres_top="",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            releasetype_secondary="Compilation",
            albumartist="Various Artists",
            albumartistsort="Various Artists",
            album="Now That's What I Call Music",
        )
        result = _top_dir_component(tags)
        assert result is not None, "Expected a non-None result for composerless compilation"
        assert result == "Various Artists", f"Expected 'Various Artists' as top_dir, got {result!r}"
        # The album name must not appear in the topmost path component.
        assert "Now That's What I Call Music" not in result, (
            f"Album name must not appear in top_dir (belongs to playlist lens), got {result!r}"
        )

    def test_performer_led_no_albumartist_returns_album_only(self) -> None:
        """Performer-led with no albumartist → album-only shape (no performer prefix).

        Edge case: both ALBUMARTIST and ARTIST are empty.  Falls back to album title alone.

        :returns: None.
        """
        tags = TrackTags(
            cwp_work_top="Sonata",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            albumartist="",
            artist="",
            album="Unknown Recital",
        )
        result = _top_dir_component(tags)
        assert result is not None, "Expected a non-None result for performer-led with no albumartist"
        assert result == "Unknown Recital", f"Expected 'Unknown Recital', got {result!r}"

    def test_build_dest_path_recital_uses_performer_first(self, fs: FakeFilesystem) -> None:
        """build_dest_path for a recital → ``dest_root / "<albumartist>" / …`` (album name excluded).

        Verifies the performer-led branch end-to-end through build_dest_path (no class prefix).
        The album name must not appear in the topmost path component.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Sonata in B minor",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Sonata in B minor",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="",
            cea_composer_lastnames="",
            albumartist="Mitsuko Uchida",
            albumartistsort="Uchida, Mitsuko",
            album="Schubert: Piano Sonatas",
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Schubert: Piano Sonatas", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Sonata in B minor"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        assert "Mitsuko Uchida" in rel.parts[0], (
            f"Expected albumartist 'Mitsuko Uchida' in top_dir (parts[0]), got {rel.parts[0]!r}"
        )
        assert "Classical" not in rel.parts, f"Expected no 'Classical' class prefix in path, got parts={rel.parts!r}"

    def test_build_dest_path_compilation_with_composer_uses_composer_performers(self, fs: FakeFilesystem) -> None:
        """build_dest_path for a compilation with a linked composer → ``<composer> - <performers>``.

        The topmost path component derives from composer and performers, not from the album name.
        A free-classification parameter (``releasetype_secondary`` containing "Compilation") does
        not gate the topmost component when a composer is present.  The album name must not appear
        in the topmost path component.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        tags = TrackTags(
            title="Ouvertüre",
            movementnumber="1",
            movementtotal="1",
            cwp_work_top="Ouvertüre",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Rossini",
            cea_composer_lastnames="Rossini",
            releasetype_secondary="Compilation",
            albumartist="Herbert von Karajan",
            albumartistsort="Karajan, Herbert von",
            album="Ouvertüren",
            artist="Herbert von Karajan; Berliner Philharmoniker",
            cea_conductors="Herbert von Karajan",
            cea_ensembles="Berliner Philharmoniker",
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "Ouvertüren", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Ouvertüre"}}),
            tags,
        )
        rel = result.relative_to(dest_root)
        top = rel.parts[0]
        assert "Rossini" in top, f"Expected composer 'Rossini' in top_dir (parts[0]), got {top!r}"
        assert "Ouvertüren" not in top, (
            f"Expected album name 'Ouvertüren' to be absent from top_dir (album name must not appear in path), got {top!r}"
        )
        assert "Classical" not in rel.parts, f"Expected no 'Classical' class prefix in path, got parts={rel.parts!r}"


# ---------------------------------------------------------------------------
# Box-set performers path component — C-UNIVERSAL KATs
# ---------------------------------------------------------------------------


class TestBoxSetPerformersComponent:
    """KATs: the performers path component never resolves to the release/edition title.

    For box-set recordings (e.g. "Complete Mozart Edition"), the recording's ARTIST tag carries
    the edition/collection title rather than a real performer name.  The performers path component
    must derive from the embedded CEA_CONDUCTORS / CEA_ENSEMBLES tags (the real performers), not
    from ARTIST.  When those tags are absent, the component must fall back to "Unknown Performers"
    rather than baking the edition title into the path.

    Two KATs:

    1. **CEA tags present**: a composer-bearing box-set track whose embedded CEA_CONDUCTORS /
       CEA_ENSEMBLES carry the real performer renders ``<composer> - <conductor; ensemble>`` —
       not ``<composer> - <edition title>``.
    2. **ARTIST == ALBUM (edition-title tell)**: a composer-bearing box-set track whose embedded
       tags carry ARTIST == ALBUM and no CEA_* performer keys renders without the edition title
       (composer-only or "Unknown Performers").
    """

    def test_boxset_with_cea_tags_renders_real_performer(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Box-set track with CEA_CONDUCTORS/CEA_ENSEMBLES renders <composer> - <conductor; ensemble>.

        KAT 1: the embedded CEA_CONDUCTORS and CEA_ENSEMBLES tags carry the real performers.
        build_dest_path must use those tags (via the per-track fallback) and must NOT use the
        ARTIST tag (which carries the edition title "Complete Mozart Edition").

        The path must equal "Mozart - Sir Neville Marriner; Academy of St Martin in the Fields"
        and must not contain the edition title.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # fetch_artist_aliases is called for the conductor (with a real MBID).
        # The ensemble has no MBID (per-track ensemble MBID cannot be reliably derived from
        # embedded tags — see _hydrate_performer_lists), so fetch_artist_aliases is not called
        # for it; _canonical_name falls back to entry.name directly.
        marriner = MBArtist.model_validate({"id": "marriner-mbid", "name": "Sir Neville Marriner"})
        mocker.patch("music_annotator._tags.fetch_artist_aliases", return_value=marriner)

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 40",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Mozart",
            cwp_worktype_genres_top="Classical",
            # ARTIST carries the edition title — the bug: this must NOT appear in the path.
            artist="Complete Mozart Edition",
            albumartist="Wolfgang Amadeus Mozart",
            album="Complete Mozart Edition",
            # CEA_CONDUCTORS / CEA_ENSEMBLES carry the real performers.
            cea_conductors="Sir Neville Marriner",
            cea_ensembles="Academy of St Martin in the Fields",
            # Per-track lists are hydrated from the string tags (simulating repath).
            cea_conductors_list=[ArtistEntry(name="Sir Neville Marriner", sort="Marriner, Neville", mbid="marriner-mbid")],
            cea_ensembles_list=[
                ArtistEntry(
                    name="Academy of St Martin in the Fields",
                    sort="Academy of St Martin in the Fields",
                    mbid="",
                ),
            ],
        )

        result = music_annotator.build_dest_path(
            dest_root,
            MBRelease(),
            MBTrack(),
            tags,
            global_track_idx=0,
        )
        path_str = str(result.relative_to(dest_root))

        # The top-dir must be <composer> - <real performers>, not <composer> - <edition title>.
        top = result.relative_to(dest_root).parts[0]
        assert top == "Mozart - Sir Neville Marriner; Academy of St Martin in the Fields", (
            f"Expected 'Mozart - Sir Neville Marriner; Academy of St Martin in the Fields', got {top!r}"
        )
        # The edition title must not appear anywhere in the path.
        assert "Complete Mozart Edition" not in path_str, (
            f"Edition title must not appear in path (performers component must never resolve to the edition title), "
            f"got {path_str!r}"
        )

    def test_boxset_artist_equals_album_renders_without_edition_title(self, fs: FakeFilesystem) -> None:
        """Box-set track with ARTIST == ALBUM and no CEA_* performer keys renders without the edition title.

        KAT 2: when the embedded tags carry ARTIST == ALBUM (the edition-title tell) and no
        CEA_CONDUCTORS / CEA_ENSEMBLES are present, the performers component must not contain
        the edition string.  The path renders as composer-only or "Unknown Performers" — never
        as the edition title.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 40",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Mozart",
            cwp_worktype_genres_top="Classical",
            # ARTIST == ALBUM: the edition-title tell.  No CEA_* performer tags.
            artist="Complete Mozart Edition",
            albumartist="Wolfgang Amadeus Mozart",
            album="Complete Mozart Edition",
            # No per-track performer lists (simulating repath with no CEA_* tags).
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )

        result = music_annotator.build_dest_path(
            dest_root,
            MBRelease(),
            MBTrack(),
            tags,
            global_track_idx=0,
        )
        path_str = str(result.relative_to(dest_root))

        # The edition title must not appear in the path.
        assert "Complete Mozart Edition" not in path_str, (
            f"Edition title must not appear in path (performers component must never resolve to the edition title), "
            f"got {path_str!r}"
        )
        # The top-dir must start with the composer.
        top = result.relative_to(dest_root).parts[0]
        assert top.startswith("Mozart"), f"Expected top_dir to start with composer 'Mozart', got {top!r}"


# ---------------------------------------------------------------------------
# canonical_artist_form
# ---------------------------------------------------------------------------


class TestCanonicalArtistForm:
    """Tests for canonical_artist_form — primary-alias selection per STYLEGUIDE 3.1/NORM-2.

    The resolver is total: it always returns a non-empty string when the artist has a name.
    It selects the primary-flagged MB alias (preferring substantive name-form types) and falls
    back to ``MBArtist.name`` when no primary alias exists.
    """

    def test_primary_native_latin_alias_preferred_over_display_name(self) -> None:
        """An artist with a primary-flagged native-Latin alias resolves to the alias, not the display name.

        KAT (a): "Wiener Philharmoniker" is the primary alias; "Vienna Philharmonic" is the display
        name.  The resolver must return the alias.
        """
        artist = MBArtist.model_validate(
            {
                "id": "a1",
                "name": "Vienna Philharmonic",
                "alias-list": [
                    {"alias": "Wiener Philharmoniker", "type": "Artist name", "primary": "primary", "locale": "de"},
                ],
            }
        )
        assert canonical_artist_form(artist) == "Wiener Philharmoniker"

    def test_no_alias_falls_back_to_name(self) -> None:
        """An artist with no aliases resolves to the display name.

        KAT (b): fallback proof — when alias_list is empty, artist.name is returned.
        """
        artist = MBArtist.model_validate({"id": "a2", "name": "Herbert von Karajan"})
        assert canonical_artist_form(artist) == "Herbert von Karajan"

    def test_non_primary_alias_falls_back_to_name(self) -> None:
        """An artist with only a non-primary alias resolves to the display name.

        KAT (c): primary-only proof — a non-primary alias (primary is None) must not be selected;
        the resolver falls back to artist.name.
        """
        artist = MBArtist.model_validate(
            {
                "id": "a3",
                "name": "Berlin Philharmonic",
                "alias-list": [
                    {"alias": "Berliner Philharmoniker", "type": "Artist name", "locale": "de"},
                ],
            }
        )
        assert canonical_artist_form(artist) == "Berlin Philharmonic"

    def test_typed_primary_preferred_over_untyped_primary(self) -> None:
        """When multiple primary aliases exist, a typed one (Artist name) is preferred over an untyped one."""
        artist = MBArtist.model_validate(
            {
                "id": "a4",
                "name": "Some Orchestra",
                "alias-list": [
                    {"alias": "Untyped Primary", "primary": "primary", "locale": "en"},
                    {"alias": "Typed Primary", "type": "Artist name", "primary": "primary", "locale": "de"},
                ],
            }
        )
        assert canonical_artist_form(artist) == "Typed Primary"

    def test_untyped_primary_used_when_no_typed_primary(self) -> None:
        """When only an untyped primary alias exists, it is returned (any primary beats no primary)."""
        artist = MBArtist.model_validate(
            {
                "id": "a5",
                "name": "Some Ensemble",
                "alias-list": [
                    {"alias": "Primary Alias", "primary": "primary", "locale": "en"},
                ],
            }
        )
        assert canonical_artist_form(artist) == "Primary Alias"


# ---------------------------------------------------------------------------
# build_dest_path — canonical path performer name-forms (KAT for C-CANON)
# ---------------------------------------------------------------------------


class TestBuildDestPathCanonicalPerformerForms:
    """KAT (C-CANON): build_dest_path renders canonical entity name-forms in the performers component.

    The compact path projection uses the primary-flagged MB alias (per STYLEGUIDE 3.1/NORM-2) for
    each conductor and ensemble, not the as-credited display name.  Preserved tag surfaces
    (``ARTIST``, ``ALBUMARTIST``) are unaffected — they remain as-credited (D-A7 surface split).

    Two behavioural witnesses:

    1. **Alias-present**: an ensemble whose hydrated ``MBArtist`` has a primary native-Latin alias
       — the path performers component carries the alias form, not the anglicised display name.
    2. **Alias-absent (no-regression)**: an ensemble with no aliases — the path carries the
       as-credited display name unchanged, proving the resolver does not corrupt the no-alias case.

    Both witnesses also assert that ``ARTIST`` / ``ALBUMARTIST`` in the tags are unchanged,
    freezing the D-A7 surface split.
    """

    def _make_classical_tags(
        self,
        *,
        ensemble_entry: ArtistEntry,
        artist: str = "Vienna Philharmonic",
        albumartist: str = "Vienna Philharmonic",
    ) -> TrackTags:
        """Build a minimal classical TrackTags with one album-level ensemble.

        :param ensemble_entry: The :class:`~music_annotator.models.ArtistEntry` for the ensemble.
        :param artist: Value for the ``ARTIST`` tag (preserved surface).
        :param albumartist: Value for the ``ALBUMARTIST`` tag (preserved surface).
        :returns: A populated :class:`~music_annotator.models.TrackTags` instance.
        """
        return TrackTags(
            title="I. Allegro",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Symphony No. 9",
            cwp_workid_top="w1",
            cwp_composer_lastnames="Beethoven",
            cwp_worktype_genres_top="Classical",
            artist=artist,
            albumartist=albumartist,
            cea_conductors_list=[],
            cea_ensembles_list=[ensemble_entry],
            cea_album_conductors_list=[],
            cea_album_ensembles_list=[ensemble_entry],
        )

    def test_path_carries_alias_form_when_primary_alias_present(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Path performers component carries the primary-flagged MB alias, not the display name.

        KAT (alias-present): the ensemble "Vienna Philharmonic" has a primary native-Latin alias
        "Wiener Philharmoniker".  After hydration via ``fetch_artist_aliases``, the resolver selects
        the alias.  The path must contain "Wiener Philharmoniker", not "Vienna Philharmonic".

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        hydrated = MBArtist.model_validate(
            {
                "id": "vp-1",
                "name": "Vienna Philharmonic",
                "alias-list": [
                    {"alias": "Wiener Philharmoniker", "type": "Artist name", "primary": "primary", "locale": "de"},
                ],
            }
        )
        mocker.patch("music_annotator._tags.fetch_artist_aliases", return_value=hydrated)

        ensemble_entry = ArtistEntry(name="Vienna Philharmonic", sort="Vienna Philharmonic", mbid="vp-1")
        tags = self._make_classical_tags(
            ensemble_entry=ensemble_entry,
            artist="Vienna Philharmonic",
            albumartist="Vienna Philharmonic",
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        path_str = str(result)
        # Path performers component must carry the canonical alias form.
        assert "Wiener Philharmoniker" in path_str, f"Expected canonical alias 'Wiener Philharmoniker' in path '{path_str}'"
        # The anglicised display name must not appear in the path.
        assert "Vienna Philharmonic" not in path_str, (
            f"Display name 'Vienna Philharmonic' must not appear in path '{path_str}' (alias should replace it)"
        )
        # Preserved tag surfaces are unchanged — ARTIST and ALBUMARTIST stay as-credited (D-A7).
        assert tags.artist == "Vienna Philharmonic", "ARTIST tag must remain as-credited"
        assert tags.albumartist == "Vienna Philharmonic", "ALBUMARTIST tag must remain as-credited"

    def test_path_unchanged_when_no_primary_alias(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """Path performers component is unchanged when the ensemble has no primary alias.

        KAT (alias-absent, no-regression): the ensemble "Berlin Philharmonic" has no primary alias.
        The resolver falls back to ``MBArtist.name``.  The path must carry "Berlin Philharmonic"
        unchanged, proving the canonical-form wiring does not corrupt the no-alias case.

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        hydrated = MBArtist.model_validate({"id": "bp-1", "name": "Berlin Philharmonic"})
        mocker.patch("music_annotator._tags.fetch_artist_aliases", return_value=hydrated)

        ensemble_entry = ArtistEntry(name="Berlin Philharmonic", sort="Berlin Philharmonic", mbid="bp-1")
        tags = self._make_classical_tags(
            ensemble_entry=ensemble_entry,
            artist="Berlin Philharmonic",
            albumartist="Berlin Philharmonic",
        )
        result = music_annotator.build_dest_path(
            dest_root,
            _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []}),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "I. Allegro"}}),
            tags,
        )
        path_str = str(result)
        # No alias → resolver falls back to MBArtist.name → path unchanged from as-credited form.
        assert "Berlin Philharmonic" in path_str, (
            f"Expected as-credited name 'Berlin Philharmonic' in path '{path_str}' (no-alias fallback)"
        )
        # Preserved tag surfaces are unchanged — ARTIST and ALBUMARTIST stay as-credited (D-A7).
        assert tags.artist == "Berlin Philharmonic", "ARTIST tag must remain as-credited"
        assert tags.albumartist == "Berlin Philharmonic", "ALBUMARTIST tag must remain as-credited"


# ---------------------------------------------------------------------------
# work_group_modal_depth + build_dest_path group_modal_depth clamp
# KAT for C-W3b-INT: uniform-ceiling / ragged-floor depth rule (STYLEGUIDE 4.5)
# ---------------------------------------------------------------------------


class TestWorkGroupModalDepth:
    """Tests for work_group_modal_depth and the build_dest_path group_modal_depth clamp.

    Covers the frozen corner pins of the uniform-ceiling/ragged-floor depth rule
    (STYLEGUIDE 4.5 / C-W3b): modal ties resolve to the shallower depth; PL=0 orphans
    (Shape E) are excluded from the modal computation; the clamp is down-only (never pads up);
    and the function is total (never raises, always returns a non-negative int).
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_rel() -> MBRelease:
        """Build a minimal release stub.

        :returns: An :class:`~music_annotator.models.MBRelease` instance.
        """
        return _rel({"id": "r1", "title": "A", "artist-credit": [], "medium-list": []})

    @staticmethod
    def _make_trk() -> MBTrack:
        """Build a minimal track stub.

        :returns: An :class:`~music_annotator.models.MBTrack` instance.
        """
        return _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "Movement"}})

    @staticmethod
    def _make_tags_with_levels(part_levels: int, extra_parts: dict[str, str] | None = None) -> TrackTags:
        """Build TrackTags with the given CWP_PART_LEVELS and optional extra per-level fields.

        :param part_levels: The ``CWP_PART_LEVELS`` value to set.
        :param extra_parts: Optional dict of additional model_extra fields (e.g. cwp_part_1, cwp_ordering_key_1).
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            title="Movement",
            movementnumber="1",
            movementtotal="4",
            cwp_work_top="Water Music",
            cwp_composer_lastnames="Handel",
            originaldate="1978",
            cwp_part_levels=str(part_levels),
            cwp_movt_num="1",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        if extra_parts:
            for key, val in extra_parts.items():
                tags.model_extra[key] = val  # type: ignore[index]
        return tags

    # ------------------------------------------------------------------
    # (a) clamp-down: over-resolved PL=3 movement clamps to modal depth 2
    # ------------------------------------------------------------------

    def test_clamp_down_over_resolved_movement(self, fs: FakeFilesystem) -> None:
        """Clamp-down KAT: a PL=3 over-resolved movement renders at 2 levels when modal depth is 2.

        A work-group whose modal depth is 2 but whose one movement carries PL=3 (e.g. Handel
        Water Music movement IIIa/IIIb sub-parts) must render that movement's path at 2 levels
        (the over-resolution removed), not 3.  This is the Shapes C/D case the rule targets.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # PL=3 track: 4-level hierarchy (root → act → scene → leaf).
        tags = self._make_tags_with_levels(
            3,
            extra_parts={
                "cwp_part_0": "IIIa",
                "cwp_part_1": "Scene I",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Act I",
                "cwp_ordering_key_2": "1",
            },
        )

        # Without clamp: 3 intermediate dirs + leaf = 4 levels below work_dir.
        result_unclamped = build_dest_path(
            dest_root,
            self._make_rel(),
            self._make_trk(),
            tags,
            group_modal_depth=None,
        )
        # With clamp to modal depth 2: should render at 2 levels (1 intermediate + leaf).
        result_clamped = build_dest_path(
            dest_root,
            self._make_rel(),
            self._make_trk(),
            tags,
            group_modal_depth=2,
        )

        # Unclamped path has more components than clamped path (over-resolution present).
        assert len(result_unclamped.parts) > len(result_clamped.parts), (
            "Unclamped PL=3 path must be deeper than clamped-to-2 path"
        )
        # Clamped path: part_levels becomes min(3, 2)=2, so the >=2 branch fires with 1 intermediate dir.
        # Depth below work_dir: 1 intermediate + 1 leaf = 2 components.
        # Verify the deepest sub-part (Act I level 2) is NOT present in the clamped path.
        assert "Act I" not in str(result_clamped), (
            "Clamped path must not contain the over-resolved Act I intermediate directory"
        )

    # ------------------------------------------------------------------
    # (b) ragged-floor preserved: PL=1 shallow node never padded up
    # ------------------------------------------------------------------

    def test_ragged_floor_preserved_shallow_node(self, fs: FakeFilesystem) -> None:
        """Ragged-floor KAT: a genuinely shallow PL=1 node renders unchanged at 1 level.

        A work-group with modal depth 2 and one PL=1 node (e.g. a Shape A overture among acts)
        must render the shallow node at 1 level — never padded up to 2.  The clamp is down-only;
        min(1, 2) = 1, so the shallow node is untouched.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # PL=1 track: 2-level hierarchy (root → leaf), no intermediate dirs.
        tags = self._make_tags_with_levels(1)

        result_no_clamp = build_dest_path(
            dest_root,
            self._make_rel(),
            self._make_trk(),
            tags,
            group_modal_depth=None,
        )
        result_clamped = build_dest_path(
            dest_root,
            self._make_rel(),
            self._make_trk(),
            tags,
            group_modal_depth=2,
        )

        # min(1, 2) = 1: clamped path must equal unclamped path (no padding).
        assert result_clamped == result_no_clamp, (
            "Shallow PL=1 node must render identically with or without a modal-depth-2 clamp"
        )

    # ------------------------------------------------------------------
    # (c) modal-tie → shallower: {2,2,3,3} resolves to 2
    # ------------------------------------------------------------------

    def test_modal_tie_resolves_to_shallower(self) -> None:
        """Modal-tie KAT: a group split evenly between depths 2 and 3 resolves to the shallower (2).

        Corner pin: on a tie, choose the shallower depth.  This is the conservative choice —
        it clamps more aggressively rather than leaving over-resolved branches standing.
        """
        result = work_group_modal_depth([2, 2, 3, 3])
        assert result == 2, f"Expected modal depth 2 (tie → shallower), got {result}"

    # ------------------------------------------------------------------
    # (d) PL=0 orphan excluded from modal computation
    # ------------------------------------------------------------------

    def test_pl0_orphan_excluded_from_modal(self) -> None:
        """PL=0 orphan KAT: Shape E orphan tracks are excluded from the modal computation.

        A group with one PL=0 orphan and three PL=2 tracks must compute the modal over the
        non-orphan tracks only, yielding 2 — not 0 (which would result if the orphan were included
        and happened to be the plurality).
        """
        result = work_group_modal_depth([0, 2, 2, 2])
        assert result == 2, f"Expected modal depth 2 (PL=0 excluded), got {result}"

    def test_pl0_orphan_excluded_mixed_depths(self) -> None:
        """PL=0 orphan exclusion with mixed non-orphan depths.

        A group with PL=0 orphans and non-orphan tracks at depths 1 and 2 (2 each) must resolve
        the tie among the non-orphans to the shallower depth (1), not be biased by the orphans.
        """
        result = work_group_modal_depth([0, 0, 1, 1, 2, 2])
        assert result == 1, f"Expected modal depth 1 (tie among non-orphans → shallower), got {result}"

    # ------------------------------------------------------------------
    # (e) no-group / parameter-absent: backward compatibility
    # ------------------------------------------------------------------

    def test_no_group_parameter_absent_renders_own_depth(self, fs: FakeFilesystem) -> None:
        """No-group KAT: build_dest_path with group_modal_depth=None renders the track's own depth.

        Backward-compatibility / no-regression proof: callers that do not supply group_modal_depth
        (e.g. single-file diagnostics, call sites that have not yet been wired to pass the modal
        depth) must get the same path as before — the track's own CWP_PART_LEVELS drives the depth
        unchanged.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # PL=2 track: 3-level hierarchy with one intermediate dir.
        tags = self._make_tags_with_levels(
            2,
            extra_parts={
                "cwp_part_0": "Aria",
                "cwp_part_1": "Act I",
                "cwp_ordering_key_1": "1",
            },
        )

        result_default = build_dest_path(
            dest_root,
            self._make_rel(),
            self._make_trk(),
            tags,
        )
        result_explicit_none = build_dest_path(
            dest_root,
            self._make_rel(),
            self._make_trk(),
            tags,
            group_modal_depth=None,
        )

        # Both calls must produce the same path (default=None is the no-group posture).
        assert result_default == result_explicit_none, (
            "group_modal_depth=None (default) must produce the same path as explicit None"
        )
        # The intermediate directory must be present (own depth PL=2 is used, not clamped).
        assert "Act I" in str(result_default), "Own-depth PL=2 path must contain the intermediate Act I directory"

    # ------------------------------------------------------------------
    # (f) all-orphan edge: work_group_modal_depth([0, 0]) returns 0
    # ------------------------------------------------------------------

    def test_all_orphan_edge_returns_zero(self) -> None:
        """All-orphan edge KAT: work_group_modal_depth([0, 0]) returns 0 (totality pin).

        When all tracks in a group are PL=0 orphans (Shape E), the non-orphan list is empty.
        The function must return 0 without raising — the totality pin guarantees a non-negative
        int in all cases.
        """
        assert work_group_modal_depth([0, 0]) == 0

    def test_empty_list_returns_zero(self) -> None:
        """Empty-list edge: work_group_modal_depth([]) returns 0 (totality pin).

        An empty part_levels_list (no tracks in the group) must return 0 without raising.
        """
        assert work_group_modal_depth([]) == 0


# ---------------------------------------------------------------------------
# Integrative parity KAT — uniform-ceiling / ragged-floor depth rule
# (STYLEGUIDE 4.5 / C-W3b)
#
# Belt-and-suspenders guard over the full work_group_modal_depth + build_dest_path
# stack.  Asserts that the clamp behaviour holds end-to-end for a representative
# Shape-C/D fixture: a work-group whose modal depth is 2 but whose one over-resolved
# movement carries PL=3 (e.g. Handel Water Music Suite 1 movt IIIa/IIIb) renders
# that movement's path at 2 levels — the over-resolution removed — not 3.
# ---------------------------------------------------------------------------


class TestDepthClampIntegrativeParity:
    """Integrative parity KAT for the uniform-ceiling / ragged-floor depth rule.

    Guards the full ``work_group_modal_depth`` + ``build_dest_path`` stack end-to-end
    against a representative Shape-C/D fixture (STYLEGUIDE 4.5 / C-W3b).  The rule:
    render each leaf at ``min(its own tree depth, the work-group's modal tree depth)``.
    Clamp over-resolution down; never pad shallow branches up.
    """

    @staticmethod
    def _make_rel() -> MBRelease:
        """Build a minimal release stub.

        :returns: An :class:`~music_annotator.models.MBRelease` instance.
        """
        return _rel({"id": "r1", "title": "Water Music", "artist-credit": [], "medium-list": []})

    @staticmethod
    def _make_trk() -> MBTrack:
        """Build a minimal track stub.

        :returns: An :class:`~music_annotator.models.MBTrack` instance.
        """
        return _trk({"id": "t1", "position": 1, "recording": {"id": "rec1", "title": "III. Allegro"}})

    @staticmethod
    def _make_tags_pl(part_levels: int, extra: dict[str, str] | None = None) -> TrackTags:
        """Build TrackTags with the given CWP_PART_LEVELS and optional extra fields.

        :param part_levels: The ``CWP_PART_LEVELS`` value to set.
        :param extra: Optional dict of additional model_extra fields.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            title="III. Allegro",
            movementnumber="3",
            movementtotal="17",
            cwp_work_top="Water Music, HWV 348-350",
            cwp_composer_lastnames="Handel",
            originaldate="1988",
            cwp_part_levels=str(part_levels),
            cwp_movt_num="3",
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        if extra:
            for key, val in extra.items():
                tags.model_extra[key] = val  # type: ignore[index]
        return tags

    def test_shape_cd_fixture_clamps_to_modal_depth(self, fs: FakeFilesystem) -> None:
        """Integrative parity KAT: Shape-C/D fixture clamps to the work-group modal depth.

        Simulates the Handel Water Music scenario: a work-group of 17 tracks at PL=2 and
        3 tracks at PL=3 (Suite 1 movt III sub-parts IIIa/IIIb).  The modal depth is 2.
        The PL=3 over-resolved movement must render at 2 levels (the over-resolution
        removed), not 3.  The PL=2 majority tracks must render unchanged.

        This test guards the full ``work_group_modal_depth`` + ``build_dest_path`` stack:
        - ``work_group_modal_depth([2]*17 + [3]*3)`` must return 2 (modal = 2).
        - ``build_dest_path(..., group_modal_depth=2)`` with a PL=3 track must produce a
          shallower path than the same call with ``group_modal_depth=None``.
        - ``build_dest_path(..., group_modal_depth=2)`` with a PL=2 track must produce the
          same path as ``group_modal_depth=None`` (no-op clamp: min(2,2)=2).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Compute the modal depth from the work-group's part_levels distribution.
        # 17 tracks at PL=2, 3 tracks at PL=3 → modal = 2.
        part_levels_list = [2] * 17 + [3] * 3
        modal = work_group_modal_depth(part_levels_list)
        assert modal == 2, f"Modal depth must be 2 for a {part_levels_list!r} group, got {modal}"

        # PL=3 over-resolved track (Suite 1 movt IIIa sub-part).
        tags_pl3 = self._make_tags_pl(
            3,
            extra={
                "cwp_part_0": "IIIa",
                "cwp_part_1": "Suite 1, movt III",
                "cwp_ordering_key_1": "3",
                "cwp_part_2": "Suite 1",
                "cwp_ordering_key_2": "1",
            },
        )

        # PL=2 majority track (a flat Suite 1 movement).
        tags_pl2 = self._make_tags_pl(
            2,
            extra={
                "cwp_part_0": "I. Allegro",
                "cwp_part_1": "Suite 1",
                "cwp_ordering_key_1": "1",
            },
        )

        rel = self._make_rel()
        trk = self._make_trk()

        # PL=3 track: clamped path must be shallower than unclamped path.
        path_pl3_clamped = build_dest_path(dest_root, rel, trk, tags_pl3, group_modal_depth=modal)
        path_pl3_unclamped = build_dest_path(dest_root, rel, trk, tags_pl3, group_modal_depth=None)
        assert len(path_pl3_clamped.parts) < len(path_pl3_unclamped.parts), (
            "Clamped PL=3 path must be shallower than unclamped PL=3 path "
            f"(clamped={path_pl3_clamped}, unclamped={path_pl3_unclamped})"
        )

        # PL=2 majority track: clamped path must equal unclamped path (no-op clamp).
        path_pl2_clamped = build_dest_path(dest_root, rel, trk, tags_pl2, group_modal_depth=modal)
        path_pl2_unclamped = build_dest_path(dest_root, rel, trk, tags_pl2, group_modal_depth=None)
        assert path_pl2_clamped == path_pl2_unclamped, (
            "Clamped PL=2 path must equal unclamped PL=2 path — min(2,2)=2 is a no-op "
            f"(clamped={path_pl2_clamped}, unclamped={path_pl2_unclamped})"
        )


# ---------------------------------------------------------------------------
# _old_bare_colon_split — private helper unit tests
# ---------------------------------------------------------------------------


class TestOldBareColonSplit:
    """Tests for _old_bare_colon_split — the retired bare-':' split reproducer.

    This helper exists solely to recognise labels the pre-'': "'' split corrupted; it is NOT
    the forward path.  These tests cover all three branches of the helper.
    """

    def test_bare_colon_present_returns_fragment(self) -> None:
        """When a bare colon is present, returns the stripped fragment after it."""
        # Haydn Hoboken: the recomputed label contains a catalogue colon.
        label = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        result = _old_bare_colon_split(label)
        assert result == "31"

    def test_no_colon_returns_label_unchanged(self) -> None:
        """When no colon is present, returns the label unchanged."""
        label = "Gigue"
        assert _old_bare_colon_split(label) == "Gigue"

    def test_colon_at_end_empty_after_returns_label(self) -> None:
        """When the colon is at the end and the fragment is empty, returns the label unchanged."""
        label = "Allegro:"
        assert _old_bare_colon_split(label) == "Allegro:"

    def test_colon_space_separator_also_splits(self) -> None:
        """A colon-space separator is also a bare colon — the helper splits on the first ':'.

        This confirms the helper models the *retired* bare-':' behaviour, not the current '': "'' rule.
        """
        label = "Symphony No. 1: I. Allegro"
        result = _old_bare_colon_split(label)
        assert result == "I. Allegro"


# ---------------------------------------------------------------------------
# rederive_part_label — KATs for the offline re-derivation helper
# ---------------------------------------------------------------------------


class TestRederivePartLabel:
    """Tests for rederive_part_label — offline re-derivation from the embedded CWP_WORK pair.

    Covers the cannot-recompute trigger (empty child_title), the root-level case (empty
    parent_title), and the normal re-derivation path.
    """

    def test_empty_child_returns_cannot_recompute(self) -> None:
        """Empty child_title → CANNOT_RECOMPUTE (the sole cannot-recompute trigger)."""
        result = rederive_part_label("", "String Quartets, Op. 20")
        assert result is CANNOT_RECOMPUTE

    def test_empty_parent_returns_child_unchanged(self) -> None:
        """Empty parent_title (root level) → child_title returned unchanged.

        An absent parent is not a failure — strip_common_prefix(child, '') returns child.
        """
        child = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        result = rederive_part_label(child, "")
        assert result == child

    def test_haydn_hoboken_recomputes_full_title(self) -> None:
        """Haydn Hoboken: recomputed label is the full child title (no split on catalogue colon).

        The shipped '': "'' rule does not split on a bare catalogue colon, so the recomputed label
        is the full child title when the parent prefix does not match and no '': "'' separator exists.
        """
        child = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        parent = "String Quartets, Op. 20"
        result = rederive_part_label(child, parent)
        assert result == child

    def test_colon_space_separator_strips_correctly(self) -> None:
        """A genuine '': "'' separator strips to the movement label."""
        result = rederive_part_label("Symphony No. 1: I. Allegro", "Symphony No. 1")
        assert result == "I. Allegro"

    def test_prefix_match_strips_correctly(self) -> None:
        """Parent prefix match strips the prefix and leading punctuation."""
        result = rederive_part_label("Fontane di Roma: I. Valle Giulia all'alba", "Fontane di Roma")
        assert result == "I. Valle Giulia all'alba"


# ---------------------------------------------------------------------------
# is_catalogue_colon_corrupt — KATs (a)–(e) + supporting cases
# ---------------------------------------------------------------------------


class TestIsCatalogueColonCorrupt:
    """KATs for is_catalogue_colon_corrupt — the catalogue-colon corruption detection predicate.

    Covers all three branches of the predicate (cannot-recompute, no-disagreement, signature)
    and the five behavioural witnesses required by the C-CAT-INT contract.
    """

    # ------------------------------------------------------------------
    # KAT (a): Haydn Hoboken bug fires
    # ------------------------------------------------------------------

    def test_haydn_hoboken_corrupt_label_fires(self) -> None:
        """KAT (a): Haydn Hoboken corrupt CWP_PART label is detected as corrupt.

        A file with CWP_WORK_1 = 'String Quartet in E major, Op. 20 No. 4, Hob. III:31',
        CWP_WORK_2 = 'String Quartets, Op. 20', and corrupt CWP_PART_1 = '31' must fire the
        predicate.  The recomputed label is the full child title (the shipped '': "'' rule does not
        split on the catalogue colon), and the old bare-':' split of that recomputed label yields
        '31' — matching the stored corrupt label.
        """
        child = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        parent = "String Quartets, Op. 20"
        stored = "31"
        assert is_catalogue_colon_corrupt(stored, child, parent) is True

    def test_haydn_hoboken_rederive_gives_full_title(self) -> None:
        """KAT (a): rederive_part_label returns the full corrected label, not '31'.

        The corrected label is the full child title — the shipped '': "'' rule does not split on
        the catalogue colon, so strip_common_prefix returns the child unchanged.
        """
        child = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        parent = "String Quartets, Op. 20"
        result = rederive_part_label(child, parent)
        assert result == child
        assert result != "31"

    # ------------------------------------------------------------------
    # KAT (b): legitimately-short label preserved (no false-positive)
    # ------------------------------------------------------------------

    def test_legitimately_short_label_not_corrupt(self) -> None:
        """KAT (b): a genuinely one-word correct label does not fire the predicate.

        A file with CWP_WORK_1 = 'Suite in G major: Gigue', CWP_WORK_2 = 'Suite in G major',
        and correct CWP_PART_1 = 'Gigue' must not fire.  The recomputed label is 'Gigue'
        (strip_common_prefix splits on '': "''), which equals the stored label — no disagreement.
        """
        child = "Suite in G major: Gigue"
        parent = "Suite in G major"
        stored = "Gigue"
        assert is_catalogue_colon_corrupt(stored, child, parent) is False

    def test_legitimately_short_label_rederives_to_itself(self) -> None:
        """KAT (b): rederive_part_label returns 'Gigue' (the correct label, not a longer form)."""
        child = "Suite in G major: Gigue"
        parent = "Suite in G major"
        result = rederive_part_label(child, parent)
        assert result == "Gigue"

    # ------------------------------------------------------------------
    # KAT (c): colon-space label preserved
    # ------------------------------------------------------------------

    def test_colon_space_label_not_corrupt(self) -> None:
        """KAT (c): a correct '': "'' label does not fire the predicate.

        CWP_WORK_1 = 'Symphony No. 1: I. Allegro', CWP_WORK_2 = 'Symphony No. 1',
        stored CWP_PART_1 = 'I. Allegro' → recomputed = 'I. Allegro' = stored → no fire.
        """
        child = "Symphony No. 1: I. Allegro"
        parent = "Symphony No. 1"
        stored = "I. Allegro"
        assert is_catalogue_colon_corrupt(stored, child, parent) is False

    def test_colon_space_label_rederives_to_itself(self) -> None:
        """KAT (c): rederive_part_label returns 'I. Allegro' for a correct colon-space label."""
        child = "Symphony No. 1: I. Allegro"
        parent = "Symphony No. 1"
        result = rederive_part_label(child, parent)
        assert result == "I. Allegro"

    # ------------------------------------------------------------------
    # KAT (d): CWP_WORK pair absent (child empty) → cannot-recompute, predicate False
    # ------------------------------------------------------------------

    def test_empty_child_title_cannot_recompute(self) -> None:
        """KAT (d): empty child_title → rederive_part_label returns CANNOT_RECOMPUTE."""
        result = rederive_part_label("", "String Quartets, Op. 20")
        assert result is CANNOT_RECOMPUTE

    def test_empty_child_title_predicate_false(self) -> None:
        """KAT (d): empty child_title → is_catalogue_colon_corrupt returns False (safe branch)."""
        assert is_catalogue_colon_corrupt("31", "", "String Quartets, Op. 20") is False

    def test_empty_child_title_any_parent_predicate_false(self) -> None:
        """KAT (d): empty child_title with any parent → predicate always False."""
        assert is_catalogue_colon_corrupt("anything", "", "") is False
        assert is_catalogue_colon_corrupt("anything", "", "Some Parent") is False

    # ------------------------------------------------------------------
    # KAT (e): CWP_GROUPHEADING segment re-derivation
    # ------------------------------------------------------------------

    def test_groupheading_segment_corrupt_fires(self) -> None:
        """KAT (e): the corrupt CWP_GROUPHEADING segment is detectable at the segment level.

        CWP_GROUPHEADING is ' :: '.join([work_top, part_1, part_0]).  When CWP_PART_1 = '31'
        is corrupt, the matching ' :: ' segment in CWP_GROUPHEADING is also '31'.  Applying
        is_catalogue_colon_corrupt at the segment level (same call as KAT (a)) fires.
        """
        # The corrupt segment is the same as the corrupt CWP_PART_1 value.
        child = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        parent = "String Quartets, Op. 20"
        corrupt_segment = "31"
        assert is_catalogue_colon_corrupt(corrupt_segment, child, parent) is True

    def test_groupheading_segment_rederives_to_full_title(self) -> None:
        """KAT (e): rederive_part_label gives the corrected segment label for the groupheading.

        The corrected segment is the full child title — the same value that would replace
        the corrupt '31' in the rebuilt CWP_GROUPHEADING.
        """
        child = "String Quartet in E major, Op. 20 No. 4, Hob. III:31"
        parent = "String Quartets, Op. 20"
        result = rederive_part_label(child, parent)
        assert result == child

    # ------------------------------------------------------------------
    # Additional branch coverage: disagreement without catalogue-colon signature
    # ------------------------------------------------------------------

    def test_disagreement_without_catalogue_colon_signature_not_corrupt(self) -> None:
        """A stored label that disagrees with the recomputed label but lacks the catalogue-colon
        signature does not fire the predicate.

        The predicate is bounded to the catalogue-colon signature: it fires only when the stored
        label is exactly what the old bare-':' split would have produced from the recomputed label.
        A label that disagrees for a different reason (e.g. manually edited) is not flagged.
        """
        # Recomputed = "I. Allegro" (from "Symphony No. 1: I. Allegro" with parent "Symphony No. 1").
        # Stored = "Allegro" — disagrees, but _old_bare_colon_split("I. Allegro") = "Allegro" only
        # if there is a colon in "I. Allegro", which there is not.  So the signature does not match.
        child = "Symphony No. 1: I. Allegro"
        parent = "Symphony No. 1"
        stored = "Allegro"  # disagrees with "I. Allegro" but no catalogue-colon signature
        # _old_bare_colon_split("I. Allegro") = "I. Allegro" (no colon) → stored != recomputed → False
        assert is_catalogue_colon_corrupt(stored, child, parent) is False
