"""Unit tests for music_annotator discovery functions.

Covers :func:`~music_annotator.parse_disc_info_yaml`, :func:`~music_annotator.parse_disc_toc`,
:func:`~music_annotator.parse_dir_hint`, :func:`~music_annotator.search_releases_by_dir`,
:func:`~music_annotator._format_candidate`, and :func:`~music_annotator.discover`.
"""

from __future__ import annotations

import struct
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import musicbrainzngs as mb
import pytest
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
import music_annotator._discover
from music_annotator._discover import DiscoverUI, _format_candidate, _score_toc_release, _toc_lookup_mb_releases
from music_annotator.models import MBReleaseCandidate

# ---------------------------------------------------------------------------
# Minimal FLAC factory (same technique as test_example.py)
# ---------------------------------------------------------------------------

_FLAC_MAGIC = b"fLaC"
_STREAMINFO_BLOCK = struct.pack(">I", (1 << 31) | (0 << 24) | 34) + bytes(34)
_MINIMAL_FLAC = _FLAC_MAGIC + _STREAMINFO_BLOCK


def _candidate(
    release_id: str = "rel-1",
    score: int = 90,
    title: str = "Fontane di Roma",
    artist: str = "Karajan",
    date: str = "1995",
    fmt: str = "CD",
    tracks: int = 4,
    label: str = "DG",
    catalog_number: str = "449 724-2",
    country: str = "DE",
    status: str = "Official",
) -> MBReleaseCandidate:
    """Build a minimal :class:`MBReleaseCandidate` for tests.

    :param release_id: MusicBrainz release MBID.
    :param score: MB relevance score (0–100).
    :param title: Release title.
    :param artist: Artist credit phrase.
    :param date: Release date string.
    :param fmt: Medium format (e.g. ``"CD"``).
    :param tracks: Total track count.
    :param label: Label name.
    :param catalog_number: Catalog number.
    :param country: Release country code.
    :param status: Release status string.
    :returns: A populated :class:`MBReleaseCandidate` instance.
    """
    return MBReleaseCandidate(
        release_id=release_id,
        score=score,
        title=title,
        artist=artist,
        date=date,
        format=fmt,
        tracks=tracks,
        label=label,
        catalog_number=catalog_number,
        country=country,
        status=status,
        mb_url=f"https://musicbrainz.org/release/{release_id}",
    )


# ---------------------------------------------------------------------------
# parse_disc_info_yaml
# ---------------------------------------------------------------------------

#: Minimal single-record yaml with a preferred flag and a DTITLE.
_SINGLE_RECORD_YAML = textwrap.dedent("""\
    disc_id: [1, 8, 150]
    record:
    - disc_info: {category: classical, disc_id: abc123, title: 'Karajan / Respighi'}
      preferred: true
      track_info:
        DTITLE: 'Karajan, Berliner Philharmoniker / Ottorino Respighi - Fontane di Roma'
        DYEAR: '1973'
    """)

#: Two-record yaml; the second is marked preferred.
_TWO_RECORD_YAML = textwrap.dedent("""\
    disc_id: [1, 8, 150]
    record:
    - disc_info: {category: misc, disc_id: abc123, title: 'Bad Title'}
      track_info:
        DTITLE: 'Wrong Artist / Wrong Title'
        DYEAR: '2000'
    - disc_info: {category: classical, disc_id: abc123, title: 'Correct'}
      preferred: true
      track_info:
        DTITLE: 'Furtwangler / Beethoven Symphonies'
        DYEAR: '1989'
    """)

#: Two-record yaml; neither is preferred — first should be used.
_TWO_RECORD_NO_PREFERRED_YAML = textwrap.dedent("""\
    disc_id: [1, 8, 150]
    record:
    - disc_info: {category: misc, disc_id: abc123, title: 'First'}
      track_info:
        DTITLE: 'First Artist / First Title'
        DYEAR: '1990'
    - disc_info: {category: classical, disc_id: abc123, title: 'Second'}
      track_info:
        DTITLE: 'Second Artist / Second Title'
        DYEAR: '1991'
    """)

#: DTITLE without a ' / ' separator.
_NO_SLASH_YAML = textwrap.dedent("""\
    disc_id: [1, 8, 150]
    record:
    - disc_info: {category: classical, disc_id: abc123, title: 'No Slash'}
      preferred: true
      track_info:
        DTITLE: 'Just A Title With No Slash'
        DYEAR: '2001'
    """)

#: Empty record list.
_EMPTY_RECORDS_YAML = textwrap.dedent("""\
    disc_id: [1, 8, 150]
    record: []
    """)


class TestParseDiscInfoYaml:
    """Tests for parse_disc_info_yaml."""

    def test_returns_none_when_file_absent(self, fs: FakeFilesystem) -> None:
        """Returns None when no disc info yaml file exists in the directory.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        assert music_annotator.parse_disc_info_yaml(src) is None

    def test_preferred_record_dtitle_split(self, fs: FakeFilesystem) -> None:
        """DTITLE 'artist / title' is split and returned as (title, artist).

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_SINGLE_RECORD_YAML)
        result = music_annotator.parse_disc_info_yaml(src)
        assert result is not None
        title, artist = result
        assert title == "Ottorino Respighi - Fontane di Roma"
        assert artist == "Karajan, Berliner Philharmoniker"

    def test_preferred_record_chosen_over_first(self, fs: FakeFilesystem) -> None:
        """When multiple records exist, the preferred one is used.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_TWO_RECORD_YAML)
        result = music_annotator.parse_disc_info_yaml(src)
        assert result is not None
        title, artist = result
        assert title == "Beethoven Symphonies"
        assert artist == "Furtwangler"

    def test_first_record_used_when_no_preferred(self, fs: FakeFilesystem) -> None:
        """When no record is marked preferred, the first record is used.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_TWO_RECORD_NO_PREFERRED_YAML)
        result = music_annotator.parse_disc_info_yaml(src)
        assert result is not None
        title, artist = result
        assert title == "First Title"
        assert artist == "First Artist"

    def test_dtitle_without_slash_returned_as_query(self, fs: FakeFilesystem) -> None:
        """A DTITLE with no ' / ' is returned as the query with empty artist.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_NO_SLASH_YAML)
        result = music_annotator.parse_disc_info_yaml(src)
        assert result is not None
        query, artist = result
        assert query == "Just A Title With No Slash"
        assert artist == ""

    def test_empty_record_list_returns_none(self, fs: FakeFilesystem) -> None:
        """Returns None when the record list is empty.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_EMPTY_RECORDS_YAML)
        assert music_annotator.parse_disc_info_yaml(src) is None

    def test_returns_none_when_dtitle_missing(self, fs: FakeFilesystem) -> None:
        """Returns None when the preferred record has no DTITLE key.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = "disc_id: [1]\nrecord:\n- preferred: true\n  track_info: {DYEAR: '2000'}\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_info_yaml(src) is None

    def test_returns_none_when_dtitle_blank(self, fs: FakeFilesystem) -> None:
        """Returns None when DTITLE is present but empty/whitespace.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = "disc_id: [1]\nrecord:\n- preferred: true\n  track_info: {DTITLE: '   '}\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_info_yaml(src) is None

    def test_returns_none_when_track_info_missing(self, fs: FakeFilesystem) -> None:
        """Returns None when the preferred record has no track_info key.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = "disc_id: [1]\nrecord:\n- preferred: true\n  disc_info: {}\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_info_yaml(src) is None

    def test_returns_none_when_data_not_a_dict(self, fs: FakeFilesystem) -> None:
        """Returns None when the yaml file parses to a non-dict (e.g. a bare list).

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents="- item1\n- item2\n")
        assert music_annotator.parse_disc_info_yaml(src) is None

    def test_python_str_tag_decoded_correctly(self, fs: FakeFilesystem) -> None:
        """DTITLE values using the !!python/str tag are decoded to plain strings.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = (
            'disc_id: [1]\nrecord:\n- preferred: true\n  track_info:\n    DTITLE: !!python/str "K\\xFCnstler / Werk"\n'
        )
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        result = music_annotator.parse_disc_info_yaml(src)
        assert result is not None
        title, artist = result
        assert title == "Werk"
        assert artist == "Künstler"

    def test_returns_none_when_first_record_not_a_dict(self, fs: FakeFilesystem) -> None:
        """Returns None when the first record is not a dict and none is marked preferred.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        # record list contains a bare string, not a mapping
        fs.create_file(str(src / "00 - disc info.yaml"), contents="disc_id: [1]\nrecord:\n- not-a-dict\n")
        assert music_annotator.parse_disc_info_yaml(src) is None


# ---------------------------------------------------------------------------
# parse_dir_hint
# ---------------------------------------------------------------------------


class TestParseDirHint:
    """Tests for parse_dir_hint.

    ``parse_dir_hint`` never attempts an artist/title split.  FreeDB directory names have no consistent ordering (``"Artist -
    Work"`` and ``"Work - Artist"`` coexist), so the entire cleaned name is returned as the query and ``artist_hint`` is always
    ``""``.
    """

    def test_plain_name_used_as_query(self, fs: FakeFilesystem) -> None:
        """Whole directory name is returned as the query; artist_hint is always empty.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Respighi - Fontane di Roma")
        fs.create_dir(str(src))
        query, artist = music_annotator.parse_dir_hint(src)
        assert "Fontane di Roma" in query
        assert "Respighi" in query
        assert artist == ""

    def test_no_separator_returns_full_name(self, fs: FakeFilesystem) -> None:
        """Directory without ' - ' uses the full name as query, empty artist.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Fontane di Roma")
        fs.create_dir(str(src))
        query, artist = music_annotator.parse_dir_hint(src)
        assert query == "Fontane di Roma"
        assert artist == ""

    def test_freedb_hex_suffix_stripped(self, fs: FakeFilesystem) -> None:
        """The FreeDB hex CRC suffix (e.g. ``.0xe212b212``) is removed from the query.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Respighi - Fontane di Roma.0xe212b212")
        fs.create_dir(str(src))
        query, _ = music_annotator.parse_dir_hint(src)
        assert "0xe212b212" not in query
        assert "Fontane di Roma" in query

    def test_double_colon_replaced_by_space(self, fs: FakeFilesystem) -> None:
        """``::`` (path-safe stand-in for ``/``) is replaced by a space.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Karajan :: Respighi - Fontane di Roma.0xe212b212")
        fs.create_dir(str(src))
        query, _ = music_annotator.parse_dir_hint(src)
        assert "::" not in query
        assert "Karajan" in query

    def test_disc_suffix_stripped(self, fs: FakeFilesystem) -> None:
        """Disc suffixes like ``(Disc 1)`` are stripped from the query.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Brahms Symphonies (Disc 1)")
        fs.create_dir(str(src))
        query, _ = music_annotator.parse_dir_hint(src)
        assert "Disc 1" not in query
        assert "Brahms" in query

    def test_bracket_annotation_stripped(self, fs: FakeFilesystem) -> None:
        """``[bracketed]`` annotations like ``[1980s]`` are stripped.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Beethoven Symphonies - Karajan [1980s]")
        fs.create_dir(str(src))
        query, _ = music_annotator.parse_dir_hint(src)
        assert "[1980s]" not in query
        assert "Beethoven" in query

    def test_short_title_falls_back_to_track_stems(self, fs: FakeFilesystem) -> None:
        """When the cleaned dir name is very short, longest track stem is used as the query.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/CD1")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01 - Fontane di Roma movement one.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02 - Short.flac"), contents=_MINIMAL_FLAC)
        query, artist = music_annotator.parse_dir_hint(src)
        assert "Fontane di Roma movement one" in query
        assert artist == ""

    def test_short_title_no_tracks_stays_short(self, fs: FakeFilesystem) -> None:
        """When the cleaned dir name is short and directory is empty, it is kept as-is.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/CD")
        fs.create_dir(str(src))
        query, artist = music_annotator.parse_dir_hint(src)
        assert query == "CD"
        assert artist == ""

    def test_strip_track_prefix_pattern(self, fs: FakeFilesystem) -> None:
        """Track-number prefixes like '01 - ' are stripped from file stems in fallback mode.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/X")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01 - Very Long Movement Title Here.flac"), contents=_MINIMAL_FLAC)
        query, _ = music_annotator.parse_dir_hint(src)
        assert "Very Long Movement Title Here" in query


# ---------------------------------------------------------------------------
# search_releases_by_dir
# ---------------------------------------------------------------------------


class TestSearchReleasesByDir:
    """Tests for search_releases_by_dir."""

    def _raw_release(
        self,
        release_id: str = "rel-1",
        score: int = 95,
        title: str = "Fontane di Roma",
        artist_credit_phrase: str = "Karajan",
        date: str = "1995",
        status: str = "Official",
        country: str = "DE",
        tracks_per_medium: int = 4,
        fmt: str = "CD",
        label_name: str = "DG",
        cat_num: str = "449 724-2",
    ) -> dict[str, object]:
        """Build a raw MB API release dict as returned by musicbrainzngs.

        :param release_id: MBID string.
        :param score: ext:score value.
        :param title: Release title.
        :param artist_credit_phrase: Artist credit phrase.
        :param date: Release date.
        :param status: Release status.
        :param country: Country code.
        :param tracks_per_medium: Number of tracks in the single medium.
        :param fmt: Medium format.
        :param label_name: Label name.
        :param cat_num: Catalog number.
        :returns: A dict mirroring the musicbrainzngs parsed XML response.
        """
        return {
            "id": release_id,
            "ext:score": str(score),
            "title": title,
            "artist-credit-phrase": artist_credit_phrase,
            "date": date,
            "status": status,
            "country": country,
            "medium-list": [{"format": fmt, "track-list": [{}] * tracks_per_medium}],
            "label-info-list": [{"label": {"name": label_name}, "catalog-number": cat_num}],
        }

    def test_returns_candidates_sorted_by_score(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Results are sorted by score descending.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Respighi - Fontane di Roma")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        raw_results = [
            self._raw_release(release_id="r1", score=60),
            self._raw_release(release_id="r2", score=95),
            self._raw_release(release_id="r3", score=80),
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw_results})

        candidates = music_annotator.search_releases_by_dir(src)
        assert [c.release_id for c in candidates] == ["r2", "r3", "r1"]

    def test_raises_value_error_when_no_audio_files(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Raises ValueError when the source directory has no audio files.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/empty")
        fs.create_dir(str(src))
        mocker.patch("music_annotator._discover._search_mb_releases")

        with pytest.raises(ValueError, match="no audio files"):
            music_annotator.search_releases_by_dir(src)

    def test_empty_release_list_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Empty 'release-list' in MB response yields empty candidate list.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": []})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates == []

    def test_candidate_fields_populated(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Candidate fields are correctly mapped from the raw MB response.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Respighi - Fontane di Roma")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [self._raw_release(release_id="r1", score=90, title="Fontane di Roma", tracks_per_medium=4)]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.release_id == "r1"
        assert c.score == 90
        assert c.title == "Fontane di Roma"
        assert c.tracks == 4
        assert c.label == "DG"
        assert c.catalog_number == "449 724-2"
        assert c.mb_url == "https://musicbrainz.org/release/r1"

    def test_multi_medium_track_count_summed(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Track counts across multiple media are summed correctly.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "r1",
                "ext:score": "80",
                "title": "Double Album",
                "artist-credit-phrase": "Artist",
                "date": "2000",
                "status": "Official",
                "country": "US",
                "medium-list": [
                    {"format": "CD", "track-list": [{}] * 10},
                    {"format": "CD", "track-list": [{}] * 8},
                ],
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].tracks == 18

    def test_empty_label_info_list(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Empty label-info-list leaves label and catalog_number blank.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "r1",
                "ext:score": "70",
                "title": "Album",
                "artist-credit-phrase": "Artist",
                "date": "1990",
                "status": "Official",
                "country": "US",
                "medium-list": [{"format": "CD", "track-list": [{}]}],
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].label == ""
        assert candidates[0].catalog_number == ""

    def test_non_dict_item_in_release_list_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Non-dict items in the release-list are silently skipped.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch(
            "music_annotator._discover._search_mb_releases",
            return_value={"release-list": ["not-a-dict", self._raw_release(release_id="r1")]},
        )

        candidates = music_annotator.search_releases_by_dir(src)
        assert len(candidates) == 1
        assert candidates[0].release_id == "r1"

    def test_non_dict_medium_in_medium_list_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Non-dict items inside medium-list are silently skipped.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "r1",
                "ext:score": "80",
                "title": "Album",
                "artist-credit-phrase": "Artist",
                "date": "2000",
                "status": "Official",
                "country": "US",
                "medium-list": ["not-a-dict", {"format": "CD", "track-list": [{}] * 3}],
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].tracks == 3

    def test_format_taken_from_first_valid_medium(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Format string is taken from the first medium that has one.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "r1",
                "ext:score": "80",
                "title": "Album",
                "artist-credit-phrase": "Artist",
                "date": "2000",
                "status": "Official",
                "country": "US",
                "medium-list": [
                    {"format": "Vinyl", "track-list": [{}]},
                    {"format": "CD", "track-list": [{}]},
                ],
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].format == "Vinyl"

    def test_missing_release_list_key_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Missing 'release-list' key in MB response returns empty list.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._discover._search_mb_releases", return_value={})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates == []

    def test_release_list_not_a_list_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When 'release-list' value is not a list, returns empty candidate list.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": "invalid"})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates == []

    def test_medium_list_not_a_list_yields_zero_tracks(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Release with non-list medium-list yields tracks=0 and empty format.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "r1",
                "ext:score": "70",
                "title": "Album",
                "artist-credit-phrase": "Artist",
                "date": "1990",
                "status": "Official",
                "country": "US",
                "medium-list": "invalid",
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].tracks == 0
        assert candidates[0].format == ""

    def test_medium_without_track_list_or_track_count_yields_zero(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A medium with neither track-list nor track-count contributes 0 to the total.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "r1",
                "ext:score": "70",
                "title": "Album",
                "artist-credit-phrase": "Artist",
                "date": "1990",
                "status": "Official",
                "country": "US",
                "medium-list": [{"format": "CD"}],  # no track-list, no track-count
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].tracks == 0

    def test_limit_passed_to_mb_search(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """The limit parameter is forwarded to _search_mb_releases.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_search = mocker.patch(
            "music_annotator._discover._search_mb_releases",
            return_value={"release-list": []},
        )

        music_annotator.search_releases_by_dir(src, limit=5)
        mock_search.assert_called_once_with(mocker.ANY, mocker.ANY, 5)

    def test_empty_release_id_yields_empty_mb_url(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A release entry with empty 'id' produces an empty mb_url.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        raw = [
            {
                "id": "",
                "ext:score": "50",
                "title": "Unknown",
                "artist-credit-phrase": "",
                "date": "",
                "status": "",
                "country": "",
                "medium-list": [],
                "label-info-list": [],
            }
        ]
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": raw})

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].mb_url == ""

    def test_empty_query_falls_back_to_dir_name(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When parse_dir_hint returns an empty string, the raw directory name is used as the query.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        # A name that cleans to empty (pure hex suffix) triggers the fallback
        src = Path("/music/.0xe212b212")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_search = mocker.patch(
            "music_annotator._discover._search_mb_releases",
            return_value={"release-list": []},
        )

        music_annotator.search_releases_by_dir(src)
        # The query passed should be the raw directory name, not empty
        call_query = mock_search.call_args[0][0]
        assert call_query  # non-empty

    def test_search_mb_releases_without_tracks(self, mocker: MockerFixture) -> None:
        """_search_mb_releases calls mb.search_releases without 'tracks' when tracks=0.

        :param mocker: pytest-mock fixture.
        """
        mock_mb_search = mocker.patch(
            "music_annotator._discover.mb.search_releases",
            return_value={"release-list": []},
        )

        result = music_annotator._discover._search_mb_releases("Respighi", 0, 10)  # pylint: disable=protected-access
        assert result == {"release-list": []}
        mock_mb_search.assert_called_once_with("Respighi", limit=10)

    def test_search_mb_releases_with_tracks(self, mocker: MockerFixture) -> None:
        """_search_mb_releases calls mb.search_releases with 'tracks' when tracks>0.

        :param mocker: pytest-mock fixture.
        """
        mock_mb_search = mocker.patch(
            "music_annotator._discover.mb.search_releases",
            return_value={"release-list": []},
        )

        result = music_annotator._discover._search_mb_releases("Respighi", 12, 5)  # pylint: disable=protected-access
        assert result == {"release-list": []}
        mock_mb_search.assert_called_once_with("Respighi", limit=5, tracks=12)

    def test_uses_disc_info_yaml_query_when_present(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When 00 - disc info.yaml is present its DTITLE is used as the query.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album.0xdeadbeef")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_SINGLE_RECORD_YAML)

        mock_search = mocker.patch(
            "music_annotator._discover._search_mb_releases",
            return_value={"release-list": []},
        )

        music_annotator.search_releases_by_dir(src)
        # Query should come from DTITLE, not the directory name
        call_query = mock_search.call_args[0][0]
        assert "Ottorino Respighi" in call_query
        assert "0xdeadbeef" not in call_query

    def test_falls_back_to_dir_hint_when_no_yaml(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no disc info yaml exists, parse_dir_hint is used for the query.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Karajan Respighi Pini di Roma.0xe212b212")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_search = mocker.patch(
            "music_annotator._discover._search_mb_releases",
            return_value={"release-list": []},
        )

        music_annotator.search_releases_by_dir(src)
        call_query = mock_search.call_args[0][0]
        # Hex suffix should be stripped by parse_dir_hint
        assert "0xe212b212" not in call_query
        assert "Karajan" in call_query


# ---------------------------------------------------------------------------
# _format_candidate
# ---------------------------------------------------------------------------


class TestFormatCandidate:
    """Tests for _format_candidate."""

    def test_contains_index(self) -> None:
        """Formatted string contains the 1-based index."""
        result = _format_candidate(3, _candidate())
        assert "3" in result

    def test_contains_score(self) -> None:
        """Formatted string contains the score."""
        result = _format_candidate(1, _candidate(score=87))
        assert "score=87" in result

    def test_contains_title(self) -> None:
        """Formatted string contains the release title."""
        result = _format_candidate(1, _candidate(title="Pini di Roma"))
        assert "Pini di Roma" in result

    def test_contains_url(self) -> None:
        """Formatted string contains the MB URL."""
        result = _format_candidate(1, _candidate(release_id="abc-123"))
        assert "musicbrainz.org/release/abc-123" in result

    def test_unknown_fallback_for_empty_artist(self) -> None:
        """Empty artist field shows '(unknown)' in output."""
        result = _format_candidate(1, _candidate(artist=""))
        assert "(unknown)" in result


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


class TestDiscover:
    """Tests for discover."""

    def _patch_mb_and_run(self, mocker: MockerFixture, candidates: list[MBReleaseCandidate]) -> MagicMock:
        """Patch init_mb, search_releases_by_dir, and run for discover tests.

        :param mocker: pytest-mock fixture.
        :param candidates: Candidate list to return from search_releases_by_dir.
        :returns: The mock for music_annotator.run.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=candidates)
        return mocker.patch("music_annotator._discover.run")

    def test_numeric_choice_invokes_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Selecting a candidate by number causes run() to be called with its release_id.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        candidates = [_candidate(release_id="rel-1", score=95), _candidate(release_id="rel-2", score=80)]
        mock_run = self._patch_mb_and_run(mocker, candidates)
        mocker.patch("builtins.input", return_value="1")

        music_annotator.discover(
            src_dirs=[src],
            dest_root=Path("/dest"),
            user_agent="Test/1.0",
        )
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["release_id"] == "rel-1"

    def test_skip_choice_does_not_invoke_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Entering 's' skips the directory without calling run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        candidates = [_candidate()]
        mock_run = self._patch_mb_and_run(mocker, candidates)
        mocker.patch("builtins.input", return_value="s")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_run.assert_not_called()

    def test_empty_choice_skips(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Empty input skips the directory without calling run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_run.assert_not_called()

    def test_raw_mbid_choice_invokes_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Entering a raw MBID string causes run() to be called with that MBID.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="custom-mbid-string")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["release_id"] == "custom-mbid-string"

    def test_invalid_number_skips(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A number out of range prints an error message and skips without calling run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="99")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_run.assert_not_called()

    def test_no_candidates_skips_input(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When search returns no candidates, input() is never called.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [])
        mock_input = mocker.patch("builtins.input")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_input.assert_not_called()
        mock_run.assert_not_called()

    def test_search_value_error_skips_dir(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """ValueError from search_releases_by_dir (no audio files) is caught and logged; run() not called.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", side_effect=ValueError("no audio files"))
        mock_run = mocker.patch("music_annotator._discover.run")
        mock_input = mocker.patch("builtins.input")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_input.assert_not_called()
        mock_run.assert_not_called()

    def test_run_exception_logged_not_raised(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Exception from run() is caught and logged; discover() continues to completion.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[_candidate()])
        mocker.patch("music_annotator._discover.run", side_effect=RuntimeError("network failure"))
        mocker.patch("builtins.input", return_value="1")

        # Should not raise
        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")

    def test_multiple_dirs_processed_in_order(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Multiple source directories are all processed.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src1 = Path("/music/Album1")
        src2 = Path("/music/Album2")
        for src in (src1, src2):
            fs.create_dir(str(src))
            fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        search_mock = mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[_candidate()])
        mock_run = mocker.patch("music_annotator._discover.run")
        mocker.patch("builtins.input", return_value="1")

        music_annotator.discover(src_dirs=[src1, src2], dest_root=Path("/dest"), user_agent="Test/1.0")
        assert search_mock.call_count == 2
        assert mock_run.call_count == 2

    def test_dry_run_forwarded_to_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """dry_run=True is passed through to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="1")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", dry_run=True)
        _, kwargs = mock_run.call_args
        assert kwargs["dry_run"] is True

    def test_fetch_rels_false_forwarded_to_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """fetch_rels=False is passed through to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="1")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", fetch_rels=False)
        _, kwargs = mock_run.call_args
        assert kwargs["fetch_rels"] is False

    def test_skip_word_choice_does_not_invoke_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Entering 'skip' skips the directory without calling run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="skip")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        mock_run.assert_not_called()

    def test_delete_prompt_y_removes_src_dir(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Answering 'y' to the delete prompt removes the original source directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", side_effect=["1", "y"])

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        assert not src.exists()

    def test_delete_prompt_yes_removes_src_dir(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Answering 'yes' to the delete prompt removes the original source directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", side_effect=["1", "yes"])

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        assert not src.exists()

    def test_delete_prompt_n_keeps_src_dir(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Answering 'n' to the delete prompt leaves the source directory intact.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", side_effect=["1", "n"])

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        assert src.exists()

    def test_delete_prompt_suppressed_on_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When dry_run=True the delete prompt is never shown and the directory is untouched.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        self._patch_mb_and_run(mocker, [_candidate()])
        mock_input = mocker.patch("builtins.input", side_effect=["1"])

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", dry_run=True)
        assert mock_input.call_count == 1
        assert src.exists()

    def test_delete_prompt_suppressed_on_run_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When run() raises, the delete prompt is not shown.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[_candidate()])
        mocker.patch("music_annotator._discover.run", side_effect=RuntimeError("oops"))
        mock_input = mocker.patch("builtins.input", side_effect=["1"])

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0")
        assert mock_input.call_count == 1
        assert src.exists()

    def test_custom_ui_used_when_provided(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When a custom DiscoverUI is passed, it is used instead of creating a TerminalDiscoverUI.

        Verifies the ``ui is not None`` branch so ``TerminalDiscoverUI()`` is never instantiated.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[_candidate()])
        mock_run = mocker.patch("music_annotator._discover.run")

        class _StubUI:
            """Stub DiscoverUI that always selects the first candidate."""

            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Return first candidate MBID unconditionally."""
                return candidates[0].release_id if candidates else None

            def confirm_delete(self, _src_dir: object) -> bool:
                """Always decline deletion."""
                return False

        stub: DiscoverUI = _StubUI()
        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", ui=stub)
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# parse_disc_toc
# ---------------------------------------------------------------------------

#: A valid disc_id list with 4 tracks (num_tracks=4, leadout_seconds=200).
_VALID_TOC_YAML = textwrap.dedent("""\
    disc_id: [3792876050, 4, 150, 5000, 10000, 15000, 200]
    record:
    - preferred: true
      track_info:
        DTITLE: 'Artist / Title'
    """)

#: disc_id list where num_tracks does not match number of offsets.
_MISMATCHED_TOC_YAML = textwrap.dedent("""\
    disc_id: [123456789, 3, 150, 5000, 200]
    record: []
    """)

#: disc_id list that is too short (length < 4).
_SHORT_TOC_YAML = textwrap.dedent("""\
    disc_id: [1, 2, 150]
    record: []
    """)

#: disc_id list with a non-integer offset.
_BAD_OFFSET_TOC_YAML = textwrap.dedent("""\
    disc_id: [123, 2, 150, "bad", 200]
    record: []
    """)

#: disc_id list where num_tracks is zero.
_ZERO_TRACKS_TOC_YAML = textwrap.dedent("""\
    disc_id: [123, 0, 200]
    record: []
    """)

#: disc_id where total_seconds is zero.
_ZERO_SECONDS_TOC_YAML = textwrap.dedent("""\
    disc_id: [123, 1, 150, 0]
    record: []
    """)

#: No disc_id key at all.
_NO_DISC_ID_YAML = textwrap.dedent("""\
    record:
    - preferred: true
      track_info:
        DTITLE: 'Artist / Title'
    """)

#: disc_id is not a list.
_DISC_ID_NOT_LIST_YAML = textwrap.dedent("""\
    disc_id: "not-a-list"
    record: []
    """)


class TestParseDiscToc:
    """Tests for parse_disc_toc."""

    def test_returns_none_when_file_absent(self, fs: FakeFilesystem) -> None:
        """Returns None when no disc info yaml file exists.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        assert music_annotator.parse_disc_toc(src) is None

    def test_valid_toc_parsed_correctly(self, fs: FakeFilesystem) -> None:
        """Parses a well-formed disc_id list into (num_tracks, leadout_frame, track_frames).

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_VALID_TOC_YAML)
        result = music_annotator.parse_disc_toc(src)
        assert result is not None
        num_tracks, leadout_frame, track_frames = result
        assert num_tracks == 4
        assert leadout_frame == 200 * 75
        assert track_frames == [150, 5000, 10000, 15000]

    def test_returns_none_when_data_not_dict(self, fs: FakeFilesystem) -> None:
        """Returns None when yaml parses to a non-dict.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents="- item1\n- item2\n")
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_no_disc_id_key(self, fs: FakeFilesystem) -> None:
        """Returns None when the yaml has no disc_id key.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_NO_DISC_ID_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_disc_id_not_list(self, fs: FakeFilesystem) -> None:
        """Returns None when disc_id is not a list.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_DISC_ID_NOT_LIST_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_list_too_short(self, fs: FakeFilesystem) -> None:
        """Returns None when disc_id list has fewer than 4 elements.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_SHORT_TOC_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_num_tracks_zero(self, fs: FakeFilesystem) -> None:
        """Returns None when num_tracks is 0.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_ZERO_TRACKS_TOC_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_total_seconds_zero(self, fs: FakeFilesystem) -> None:
        """Returns None when total_seconds (last element) is 0.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_ZERO_SECONDS_TOC_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_offset_count_mismatches_num_tracks(self, fs: FakeFilesystem) -> None:
        """Returns None when the number of offsets does not equal num_tracks.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_MISMATCHED_TOC_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_offset_not_int(self, fs: FakeFilesystem) -> None:
        """Returns None when a track offset is not an integer.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=_BAD_OFFSET_TOC_YAML)
        assert music_annotator.parse_disc_toc(src) is None

    def test_leadout_frame_is_total_seconds_times_75(self, fs: FakeFilesystem) -> None:
        """Leadout frame equals total_seconds * 75.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        # total_seconds = 4788 → leadout = 4788*75 = 359100 (Respighi test disc)
        yaml_content = "disc_id: [3792876050, 2, 150, 19235, 4788]\nrecord: []\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        result = music_annotator.parse_disc_toc(src)
        assert result is not None
        _, leadout_frame, _ = result
        assert leadout_frame == 4788 * 75

    def test_returns_none_when_num_tracks_not_int(self, fs: FakeFilesystem) -> None:
        """Returns None when num_tracks element is not an integer.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = 'disc_id: [123, "two", 150, 200]\nrecord: []\n'
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_toc(src) is None

    def test_returns_none_when_total_seconds_not_int(self, fs: FakeFilesystem) -> None:
        """Returns None when total_seconds element is not an integer.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = 'disc_id: [123, 1, 150, "oops"]\nrecord: []\n'
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_toc(src) is None


# ---------------------------------------------------------------------------
# _score_toc_release
# ---------------------------------------------------------------------------


class TestScoreTocRelease:
    """Tests for _score_toc_release."""

    def _medium(self, track_count: int) -> dict[str, object]:
        """Build a minimal medium dict with a track-count.

        :param track_count: Number of tracks on this medium.
        :returns: A medium dict.
        """
        return {"format": "CD", "track-count": track_count}

    def test_single_disc_exact_match_scores_100(self) -> None:
        """A single-medium release with matching track-count scores 100."""
        item: dict[str, object] = {"medium-list": [self._medium(18)]}
        assert _score_toc_release(item, 18) == 100

    def test_no_matching_disc_scores_zero(self) -> None:
        """A release where no medium matches the expected count scores 0."""
        item: dict[str, object] = {"medium-list": [self._medium(10), self._medium(12)]}
        assert _score_toc_release(item, 18) == 0

    def test_one_of_many_media_matching_reduces_score(self) -> None:
        """One matching medium out of fifty scores much less than 100."""
        item: dict[str, object] = {"medium-list": [self._medium(18)] + [self._medium(10)] * 49}
        score = _score_toc_release(item, 18)
        assert score < 10

    def test_empty_medium_list_scores_zero(self) -> None:
        """An empty medium-list scores 0."""
        item: dict[str, object] = {"medium-list": []}
        assert _score_toc_release(item, 18) == 0

    def test_missing_medium_list_scores_zero(self) -> None:
        """Missing medium-list key scores 0."""
        item: dict[str, object] = {}
        assert _score_toc_release(item, 18) == 0

    def test_non_dict_medium_skipped(self) -> None:
        """Non-dict entries in medium-list are silently skipped."""
        item: dict[str, object] = {"medium-list": ["not-a-dict", self._medium(5)]}
        assert _score_toc_release(item, 5) == 50  # 1 match out of 2 total → 50

    def test_falls_back_to_track_list_length(self) -> None:
        """When track-count is absent, track-list length is used as fallback."""
        item: dict[str, object] = {"medium-list": [{"format": "CD", "track-list": [{}] * 8}]}
        assert _score_toc_release(item, 8) == 100

    def test_track_list_fallback_no_match_scores_zero(self) -> None:
        """track-list fallback with wrong count scores 0."""
        item: dict[str, object] = {"medium-list": [{"format": "CD", "track-list": [{}] * 8}]}
        assert _score_toc_release(item, 5) == 0

    def test_medium_list_not_list_scores_zero(self) -> None:
        """When medium-list is not a list, scores 0."""
        item: dict[str, object] = {"medium-list": "wrong"}
        assert _score_toc_release(item, 5) == 0

    def test_score_capped_at_100(self) -> None:
        """Score is never greater than 100."""
        item: dict[str, object] = {"medium-list": [self._medium(4), self._medium(4)]}
        score = _score_toc_release(item, 4)
        assert score <= 100


# ---------------------------------------------------------------------------
# _toc_lookup_mb_releases
# ---------------------------------------------------------------------------


class TestTocLookupMbReleases:
    """Tests for _toc_lookup_mb_releases."""

    def _toc_release(self, release_id: str = "rel-toc-1", track_count: int = 4) -> dict[str, object]:
        """Build a minimal raw release dict for TOC response tests.

        :param release_id: MBID string.
        :param track_count: track-count on the single medium.
        :returns: A dict mirroring a TOC lookup response.
        """
        return {
            "id": release_id,
            "title": "TOC Result",
            "artist-credit-phrase": "Composer",
            "date": "2000",
            "status": "Official",
            "country": "DE",
            "medium-list": [{"format": "CD", "track-count": track_count}],
            "label-info-list": [],
        }

    def test_fuzzy_path_returns_release_list(self, mocker: MockerFixture) -> None:
        """Fuzzy TOC response shape {'release-list': [...]} is handled correctly.

        :param mocker: pytest-mock fixture.
        """
        releases = [self._toc_release("r1"), self._toc_release("r2")]
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"release-list": releases, "release-count": 2},
        )
        result = _toc_lookup_mb_releases("1 4 15000 150 5000 10000 15000", 10)
        assert [r["id"] for r in result] == ["r1", "r2"]

    def test_exact_match_path_returns_release_list(self, mocker: MockerFixture) -> None:
        """Exact match response shape {'disc': {'release-list': [...]}} is handled correctly.

        :param mocker: pytest-mock fixture.
        """
        releases = [self._toc_release("r1")]
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"disc": {"release-list": releases}},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert [r["id"] for r in result] == ["r1"]

    def test_404_response_error_returns_empty_list(self, mocker: MockerFixture) -> None:
        """ResponseError with '404' returns empty list instead of raising.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            side_effect=mb.ResponseError(cause=Exception("404 Not Found")),
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert result == []

    def test_non_404_response_error_re_raised(self, mocker: MockerFixture) -> None:
        """Non-404 ResponseError is re-raised.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            side_effect=mb.ResponseError(cause=Exception("400 Bad Request")),
        )
        with pytest.raises(mb.ResponseError):
            _toc_lookup_mb_releases("1 1 15000 150", 10)

    def test_limit_applied_to_result(self, mocker: MockerFixture) -> None:
        """Result list is sliced to the limit.

        :param mocker: pytest-mock fixture.
        """
        releases = [self._toc_release(f"r{i}") for i in range(20)]
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"release-list": releases},
        )
        result = _toc_lookup_mb_releases("1 4 15000 150 5000 10000 15000", 5)
        assert len(result) == 5

    def test_non_dict_items_in_release_list_filtered(self, mocker: MockerFixture) -> None:
        """Non-dict items in release-list are filtered out.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"release-list": ["not-a-dict", self._toc_release("r1")]},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert len(result) == 1
        assert result[0]["id"] == "r1"

    def test_empty_release_list_returns_empty(self, mocker: MockerFixture) -> None:
        """Empty release-list returns empty list.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"release-list": []},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert result == []

    def test_disc_list_empty_returns_empty(self, mocker: MockerFixture) -> None:
        """Exact match path with empty disc release-list returns empty list.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"disc": {"release-list": []}},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert result == []

    def test_unexpected_response_shape_returns_empty(self, mocker: MockerFixture) -> None:
        """Response with neither 'disc' nor 'release-list' returns empty list.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"something-else": []},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert result == []

    def test_disc_release_list_not_list_falls_through_to_fuzzy(self, mocker: MockerFixture) -> None:
        """When disc.release-list is not a list, falls through to fuzzy release-list.

        :param mocker: pytest-mock fixture.
        """
        releases = [self._toc_release("r1")]
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"disc": {"release-list": None}, "release-list": releases},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert len(result) == 1
        assert result[0]["id"] == "r1"

    def test_fuzzy_list_not_list_returns_empty(self, mocker: MockerFixture) -> None:
        """When release-list is not a list, returns empty list.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"release-list": "invalid"},
        )
        result = _toc_lookup_mb_releases("1 1 15000 150", 10)
        assert result == []


# ---------------------------------------------------------------------------
# search_releases_by_dir — TOC path
# ---------------------------------------------------------------------------


class TestSearchReleasesByDirToc:
    """Tests for the TOC-lookup path in search_releases_by_dir."""

    def _toc_release(
        self,
        release_id: str = "toc-1",
        track_count: int = 4,
        label_name: str = "",
        cat_num: str = "",
    ) -> dict[str, object]:
        """Build a raw TOC response release dict.

        :param release_id: MBID string.
        :param track_count: Track count for the single medium (used in track-count key).
        :param label_name: Label name.
        :param cat_num: Catalog number.
        :returns: A dict mirroring a TOC lookup release entry.
        """
        label_info: list[object] = [{"label": {"name": label_name}, "catalog-number": cat_num}] if label_name else []
        return {
            "id": release_id,
            "title": "TOC Release",
            "artist-credit-phrase": "TOC Artist",
            "date": "2001",
            "status": "Official",
            "country": "DE",
            "medium-list": [{"format": "CD", "track-count": track_count}],
            "label-info-list": label_info,
        }

    def _make_src(self, fs: FakeFilesystem, n_tracks: int = 4) -> Path:
        """Create a fake source directory with n_tracks FLAC files and a valid TOC yaml.

        :param fs: pyfakefs fixture.
        :param n_tracks: Number of audio tracks.
        :returns: Path to the created directory.
        """
        src = Path("/music/TocAlbum")
        fs.create_dir(str(src))
        for i in range(1, n_tracks + 1):
            fs.create_file(str(src / f"{i:02d}.flac"), contents=_MINIMAL_FLAC)
        # Build a valid disc_id: [crc, n_tracks, 150, ..., total_seconds]
        offsets = [150 + i * 4000 for i in range(n_tracks)]
        total_seconds = 200
        disc_id_list = [3792876050, n_tracks, *offsets, total_seconds]
        yaml_lines = [f"disc_id: {disc_id_list!r}", "record: []"]
        fs.create_file(str(src / "00 - disc info.yaml"), contents="\n".join(yaml_lines))
        return src

    def test_toc_path_used_when_toc_present(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When a valid TOC is found, _toc_lookup_mb_releases is called.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=4)
        mock_toc = mocker.patch(
            "music_annotator._discover._toc_lookup_mb_releases",
            return_value=[self._toc_release("r1", track_count=4)],
        )
        mocker.patch("music_annotator._discover._search_mb_releases")

        candidates = music_annotator.search_releases_by_dir(src)
        mock_toc.assert_called_once()
        assert candidates[0].release_id == "r1"

    def test_toc_path_scores_synthesised(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """TOC candidates get synthesised scores from _score_toc_release, not ext:score.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=4)
        # Single-disc release matching 4 tracks → score should be 100.
        mocker.patch(
            "music_annotator._discover._toc_lookup_mb_releases",
            return_value=[self._toc_release("r1", track_count=4)],
        )

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].score == 100

    def test_toc_results_sorted_by_synthesised_score(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """TOC results are sorted descending by synthesised score.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=4)
        # r1: 1 matching disc out of 10 → low score; r2: exact single-disc → 100
        r1: dict[str, object] = {
            "id": "r1",
            "title": "Box Set",
            "artist-credit-phrase": "Various",
            "date": "2000",
            "status": "Official",
            "country": "US",
            "medium-list": [{"format": "CD", "track-count": 4}] + [{"format": "CD", "track-count": 8}] * 9,
            "label-info-list": [],
        }
        r2 = self._toc_release("r2", track_count=4)
        mocker.patch("music_annotator._discover._toc_lookup_mb_releases", return_value=[r1, r2])

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].release_id == "r2"
        assert candidates[1].release_id == "r1"

    def test_toc_fallback_to_text_search_when_toc_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When TOC lookup returns empty list, text search is used as fallback.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=4)
        # Also add a DTITLE so text search is used instead of dir hint
        src2_yaml = (
            "disc_id: [3792876050, 4, 150, 4150, 8150, 12150, 200]\n"
            "record:\n- preferred: true\n  track_info:\n    DTITLE: 'Composer / Work'\n"
        )
        (src / "00 - disc info.yaml").write_text(src2_yaml)
        mocker.patch("music_annotator._discover._toc_lookup_mb_releases", return_value=[])
        mock_text = mocker.patch(
            "music_annotator._discover._search_mb_releases",
            return_value={
                "release-list": [
                    {
                        "id": "text-r1",
                        "ext:score": "80",
                        "title": "Work",
                        "artist-credit-phrase": "Composer",
                        "date": "1990",
                        "status": "Official",
                        "country": "DE",
                        "medium-list": [{"format": "CD", "track-list": [{}] * 4}],
                        "label-info-list": [],
                    }
                ]
            },
        )

        candidates = music_annotator.search_releases_by_dir(src)
        mock_text.assert_called_once()
        assert candidates[0].release_id == "text-r1"

    def test_toc_string_format_correct(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """TOC string passed to _toc_lookup_mb_releases has the right format.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=2)
        mock_toc = mocker.patch("music_annotator._discover._toc_lookup_mb_releases", return_value=[])
        mocker.patch("music_annotator._discover._search_mb_releases", return_value={"release-list": []})

        music_annotator.search_releases_by_dir(src)
        toc_arg: str = mock_toc.call_args[0][0]
        # Format: "1 {num_tracks} {leadout_frame} {offset1} {offset2}"
        parts = toc_arg.split()
        assert parts[0] == "1"
        assert parts[1] == "2"  # num_tracks
        # leadout = total_seconds * 75; total_seconds=200 → 15000
        assert parts[2] == str(200 * 75)

    def test_track_count_from_track_count_field_in_toc_response(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """track-count field in TOC response media is used for total track count on candidate.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=4)
        mocker.patch(
            "music_annotator._discover._toc_lookup_mb_releases",
            return_value=[self._toc_release("r1", track_count=4)],
        )

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].tracks == 4

    def test_toc_label_info_populated(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Label and catalog_number are parsed from TOC response label-info-list.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = self._make_src(fs, n_tracks=4)
        mocker.patch(
            "music_annotator._discover._toc_lookup_mb_releases",
            return_value=[self._toc_release("r1", track_count=4, label_name="DG", cat_num="449 724-2")],
        )

        candidates = music_annotator.search_releases_by_dir(src)
        assert candidates[0].label == "DG"
        assert candidates[0].catalog_number == "449 724-2"
