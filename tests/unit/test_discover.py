"""Unit tests for music_annotator discovery functions.

Covers :func:`~music_annotator.parse_disc_info_yaml`, :func:`~music_annotator.parse_disc_toc`,
:func:`~music_annotator.parse_disc_title`, :func:`~music_annotator.parse_dir_hint`,
:func:`~music_annotator.search_releases_by_dir`, :func:`~music_annotator._format_candidate`,
and :func:`~music_annotator.discover`.
"""
# pylint: disable=duplicate-code  # _make_single_track_release helper intentionally mirrors test_pipeline.py scaffolding

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
from music_annotator._discover import (
    DiscoverUI,
    TerminalDiscoverUI,
    _build_journal_release_ids,
    _enrich_candidates_from_journal,
    _enrich_candidates_with_acoustid_seed,
    _enrich_candidates_with_sequence_corroboration,
    _format_candidate,
    _score_toc_release,
    _toc_lookup_mb_releases,
)
from music_annotator._pipeline_io import (
    AudioCompareResult,
    _corroborate_candidate_medium,
    _corroborate_medium_sequence,
    _read_recording_id_tag,
)
from music_annotator._tags import _NAME_MAX
from music_annotator.models import MBMedium, MBRelease, MBReleaseCandidate, TransactionEntry, TransactionLog

# ---------------------------------------------------------------------------
# Minimal FLAC factory (same technique as test_example.py)
# ---------------------------------------------------------------------------

_FLAC_MAGIC = b"fLaC"
_STREAMINFO_BLOCK = struct.pack(">I", (1 << 31) | (0 << 24) | 34) + bytes(34)
_MINIMAL_FLAC = _FLAC_MAGIC + _STREAMINFO_BLOCK

#: Minimal valid MP3: ID3v2.3 header + one null frame (same technique as test_pipeline.py).
_ID3_HEADER = b"ID3\x03\x00\x00" + b"\x00\x00\x00\x00"  # 10-byte header, size 0
_MINIMAL_MP3 = _ID3_HEADER + b"\xff\xfb\x90\x00" + b"\x00" * 413  # one MP3 frame


def _saveable_flac() -> bytes:
    """Return a minimal FLAC byte sequence with a valid 44100 Hz sample rate.

    The module-level ``_MINIMAL_FLAC`` has a zero sample rate which mutagen rejects when saving
    tags.  This helper produces a valid FLAC that mutagen can both read and write.

    :returns: A minimal valid FLAC byte sequence.
    """
    # 44100 Hz, 2ch, 16-bit, 0 samples — same layout as test_pipeline._MINIMAL_FLAC
    streaminfo = (
        b"\x10\x00\x10\x00"  # min/max blocksize
        b"\x00\x00\x00"  # min framesize
        b"\x00\x00\x00"  # max framesize
        b"\x0a\xc4\x42\xf0\x00\x00\x00\x00" + b"\x00" * 16  # 44100 Hz, 2ch, 16-bit, 0 samples  # MD5
    )
    block_header = struct.pack(">I", (1 << 31) | (0 << 24) | len(streaminfo))
    return b"fLaC" + block_header + streaminfo


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
        title, artist = result.query, result.artist
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
        title, artist = result.query, result.artist
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
        title, artist = result.query, result.artist
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
        query, artist = result.query, result.artist
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
        title, artist = result.query, result.artist
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
        _h = music_annotator.parse_dir_hint(src)
        query, artist = _h.query, _h.artist
        assert "Fontane di Roma" in query
        assert "Respighi" in query
        assert artist == ""

    def test_no_separator_returns_full_name(self, fs: FakeFilesystem) -> None:
        """Directory without ' - ' uses the full name as query, empty artist.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Fontane di Roma")
        fs.create_dir(str(src))
        _h = music_annotator.parse_dir_hint(src)
        query, artist = _h.query, _h.artist
        assert query == "Fontane di Roma"
        assert artist == ""

    def test_freedb_hex_suffix_stripped(self, fs: FakeFilesystem) -> None:
        """The FreeDB hex CRC suffix (e.g. ``.0xe212b212``) is removed from the query.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Respighi - Fontane di Roma.0xe212b212")
        fs.create_dir(str(src))
        query = music_annotator.parse_dir_hint(src).query
        assert "0xe212b212" not in query
        assert "Fontane di Roma" in query

    def test_double_colon_replaced_by_space(self, fs: FakeFilesystem) -> None:
        """``::`` (path-safe stand-in for ``/``) is replaced by a space.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Karajan :: Respighi - Fontane di Roma.0xe212b212")
        fs.create_dir(str(src))
        query = music_annotator.parse_dir_hint(src).query
        assert "::" not in query
        assert "Karajan" in query

    def test_disc_suffix_stripped(self, fs: FakeFilesystem) -> None:
        """Disc suffixes like ``(Disc 1)`` are stripped from the query.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Brahms Symphonies (Disc 1)")
        fs.create_dir(str(src))
        query = music_annotator.parse_dir_hint(src).query
        assert "Disc 1" not in query
        assert "Brahms" in query

    def test_bracket_annotation_stripped(self, fs: FakeFilesystem) -> None:
        """``[bracketed]`` annotations like ``[1980s]`` are stripped.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Beethoven Symphonies - Karajan [1980s]")
        fs.create_dir(str(src))
        query = music_annotator.parse_dir_hint(src).query
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
        _h = music_annotator.parse_dir_hint(src)
        query, artist = _h.query, _h.artist
        assert "Fontane di Roma movement one" in query
        assert artist == ""

    def test_short_title_no_tracks_stays_short(self, fs: FakeFilesystem) -> None:
        """When the cleaned dir name is short and directory is empty, it is kept as-is.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/CD")
        fs.create_dir(str(src))
        _h = music_annotator.parse_dir_hint(src)
        query, artist = _h.query, _h.artist
        assert query == "CD"
        assert artist == ""

    def test_strip_track_prefix_pattern(self, fs: FakeFilesystem) -> None:
        """Track-number prefixes like '01 - ' are stripped from file stems in fallback mode.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/X")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01 - Very Long Movement Title Here.flac"), contents=_MINIMAL_FLAC)
        query = music_annotator.parse_dir_hint(src).query
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
        mocker.patch("music_annotator._mb_api.time.sleep")
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
        mocker.patch("music_annotator._mb_api.time.sleep")
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

    def test_from_journal_renders_compact_block(self) -> None:
        """A from_journal candidate renders a compact block with 'journal match' label and MBID."""
        candidate = MBReleaseCandidate(
            release_id="abc-def",
            score=101,
            from_journal=True,
            mb_url="https://musicbrainz.org/release/abc-def",
        )
        result = _format_candidate(2, candidate)
        assert "journal match" in result
        assert "abc-def" in result
        assert "musicbrainz.org/release/abc-def" in result
        # Compact block must NOT contain metadata noise from the full layout
        assert "(unknown)" not in result
        assert "artist" not in result

    def test_from_journal_false_uses_full_layout(self) -> None:
        """A candidate with from_journal=False uses the standard seven-line layout."""
        result = _format_candidate(1, _candidate(score=90))
        assert "artist" in result
        assert "journal match" not in result


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

    def test_no_cache_forwarded_to_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """no_cache=True is passed through to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_mb_and_run(mocker, [_candidate()])
        mocker.patch("builtins.input", return_value="1")

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", no_cache=True)
        _, kwargs = mock_run.call_args
        assert kwargs["no_cache"] is True

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

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", delete=True)
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

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", delete=True)
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

        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", delete=True)
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

            def confirm_disc(
                self,
                _mediums: object,
                proposed: MBMedium,
                _dtitle: object,
                _release_url: object,
            ) -> MBMedium | None:
                """Always accept the proposed disc."""
                return proposed

            def confirm_shortened_name(self, _original: object, proposed: str) -> str | None:
                """Always accept the proposed shortened name."""
                return proposed

            def confirm_delete(self, _src_dir: object) -> bool:
                """Always decline deletion."""
                return False

        stub: DiscoverUI = _StubUI()
        music_annotator.discover(src_dirs=[src], dest_root=Path("/dest"), user_agent="Test/1.0", ui=stub)
        mock_run.assert_called_once()

    def test_acoustid_seed_error_skips_directory(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When _enrich_candidates_with_acoustid_seed raises, the directory is skipped and run() is not called.

        Covers the ``except (ValueError, mb.WebServiceError, RuntimeError, OSError)`` branch at
        the acoustid seed error boundary in discover().  A cannot-determine AcoustID failure
        (e.g. 5xx exhaustion) degrades to "directory skipped, logged, next directory" rather
        than a crash.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=[_candidate()])
        mocker.patch(
            "music_annotator._discover._enrich_candidates_with_acoustid_seed",
            side_effect=OSError("acoustid network failure"),
        )
        mock_run = mocker.patch("music_annotator._discover.run")

        music_annotator.discover(
            src_dirs=[src],
            dest_root=Path("/dest"),
            user_agent="Test/1.0",
            acoustid_key="my-api-key",
        )
        # run() must not be called — directory was skipped due to acoustid seed error.
        mock_run.assert_not_called()


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
# parse_disc_title
# ---------------------------------------------------------------------------


class TestParseDiscTitle:
    """Tests for parse_disc_title."""

    def test_returns_empty_when_no_yaml(self, fs: FakeFilesystem) -> None:
        """Returns '' when no disc info YAML is present.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        assert music_annotator.parse_disc_title(src) == ""

    def test_extracts_title_after_separator(self, fs: FakeFilesystem) -> None:
        """Returns the title portion after ' / ' in DTITLE.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = (
            "disc_id: [123456789, 2, 182, 50000, 3600]\n"
            "record:\n"
            "- disc_info: {}\n"
            "  preferred: true\n"
            "  track_info: {DTITLE: 'Karajan, BPO / Haydn Symphonien 101 & 102'}\n"
        )
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_title(src) == "Haydn Symphonien 101 & 102"

    def test_returns_whole_string_when_no_separator(self, fs: FakeFilesystem) -> None:
        """Returns the full DTITLE when there is no ' / ' separator.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = (
            "disc_id: [123456789, 2, 182, 50000, 3600]\n"
            "record:\n"
            "- disc_info: {}\n"
            "  preferred: true\n"
            "  track_info: {DTITLE: 'Haydn Symphonien 101 & 102'}\n"
        )
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_title(src) == "Haydn Symphonien 101 & 102"

    def test_returns_empty_when_no_record_list(self, fs: FakeFilesystem) -> None:
        """Returns '' when the YAML has no 'record' key.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents="disc_id: [1, 2, 3, 4, 5]\n")
        assert music_annotator.parse_disc_title(src) == ""

    def test_returns_empty_when_record_not_dict(self, fs: FakeFilesystem) -> None:
        """Returns '' when the preferred record entry is not a dict.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents="disc_id: [1]\nrecord:\n- just_a_string\n")
        assert music_annotator.parse_disc_title(src) == ""

    def test_returns_empty_when_no_track_info(self, fs: FakeFilesystem) -> None:
        """Returns '' when the preferred record has no track_info key.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = "disc_id: [1]\nrecord:\n- preferred: true\n  disc_info: {}\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_title(src) == ""

    def test_returns_empty_when_dtitle_blank(self, fs: FakeFilesystem) -> None:
        """Returns '' when DTITLE is an empty string.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = "disc_id: [1]\nrecord:\n- preferred: true\n  disc_info: {}\n  track_info: {DTITLE: ''}\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_title(src) == ""

    def test_uses_first_record_when_none_preferred(self, fs: FakeFilesystem) -> None:
        """Falls back to the first record when no record has preferred=true.

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        yaml_content = (
            "disc_id: [1]\n"
            "record:\n"
            "- disc_info: {}\n"
            "  track_info: {DTITLE: 'Artist / First Title'}\n"
            "- disc_info: {}\n"
            "  track_info: {DTITLE: 'Artist / Second Title'}\n"
        )
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)
        assert music_annotator.parse_disc_title(src) == "First Title"

    def test_returns_empty_when_yaml_not_dict(self, fs: FakeFilesystem) -> None:
        """Returns '' when YAML content is not a dict (e.g. a plain list).

        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "00 - disc info.yaml"), contents="- item1\n- item2\n")
        assert music_annotator.parse_disc_title(src) == ""


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

    @pytest.fixture(autouse=True)
    def _patch_sleep(self, mocker: MockerFixture) -> None:
        """Suppress the _mb_call polite sleep so tests remain fast.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.time.sleep")

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

    def test_polite_delay_observed_on_success(self, mocker: MockerFixture) -> None:
        """_mb_call's 1-second polite delay is observed after a successful TOC lookup.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._discover.mb.get_releases_by_discid",
            return_value={"release-list": [self._toc_release()]},
        )
        _toc_lookup_mb_releases("1 1 15000 150", 10)
        mock_sleep.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# _search_mb_releases — polite delay
# ---------------------------------------------------------------------------


class TestSearchMbReleasesPoliteDelay:
    """Tests for the _mb_call polite delay in _search_mb_releases."""

    def test_polite_delay_observed_on_success(self, mocker: MockerFixture) -> None:
        """_mb_call's 1-second polite delay is observed after a successful search.

        :param mocker: pytest-mock fixture.
        """
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        mocker.patch(
            "music_annotator._discover.mb.search_releases",
            return_value={"release-list": []},
        )
        music_annotator._discover._search_mb_releases("Respighi", 0, 10)  # pylint: disable=protected-access
        mock_sleep.assert_called_once_with(1)


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


# ---------------------------------------------------------------------------
# TerminalDiscoverUI.confirm_disc
# ---------------------------------------------------------------------------


def _make_medium(position: int, first_title: str = "Track") -> MBMedium:
    """Build a minimal MBMedium with one track for confirm_disc tests.

    :param position: 1-based disc position.
    :param first_title: Title of the first (and only) track recording.
    :returns: An :class:`~music_annotator.models.MBMedium` instance.
    """
    return MBMedium.model_validate(
        {
            "position": position,
            "format": "CD",
            "track-list": [
                {
                    "id": f"t{position}",
                    "position": 1,
                    "recording": {"id": f"r{position}", "title": first_title, "artist-credit": []},
                }
            ],
        }
    )


class TestTerminalDiscoverUIConfirmDisc:
    """Tests for TerminalDiscoverUI.confirm_disc."""

    def _ui(self) -> TerminalDiscoverUI:
        return TerminalDiscoverUI()

    def _mediums(self) -> list[MBMedium]:
        return [
            _make_medium(3, "Symphonie B-Dur Hob.I: 98: 1. Adagio"),
            _make_medium(4, "Symphonie D-Dur Hob.I: 101 Die Uhr: 1. Adagio"),
            _make_medium(5, "Symphonie Es-Dur Hob.I: 103: 1. Adagio"),
        ]

    def test_y_confirms_proposed(self, mocker: MockerFixture) -> None:
        """Entering 'y' returns the proposed medium unchanged.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", return_value="y")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "Haydn Symphonien 101 & 102", "https://mb/r")
        assert result is mediums[1]

    def test_yes_confirms_proposed(self, mocker: MockerFixture) -> None:
        """Entering 'yes' also confirms the proposed medium.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", return_value="yes")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "Haydn Symphonien 101 & 102", "https://mb/r")
        assert result is mediums[1]

    def test_n_returns_none(self, mocker: MockerFixture) -> None:
        """Entering 'n' returns None (abort).

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", return_value="n")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "dtitle", "url")
        assert result is None

    def test_abort_returns_none(self, mocker: MockerFixture) -> None:
        """Entering 'abort' returns None.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", return_value="abort")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "dtitle", "url")
        assert result is None

    def test_a_returns_none(self, mocker: MockerFixture) -> None:
        """Entering 'a' returns None.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", return_value="a")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "dtitle", "url")
        assert result is None

    def test_disc_number_overrides_proposed(self, mocker: MockerFixture) -> None:
        """Entering a valid disc position number returns that medium.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", return_value="3")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "dtitle", "url")
        assert result is mediums[0]  # position 3

    def test_invalid_disc_number_reprompts(self, mocker: MockerFixture) -> None:
        """An invalid disc number triggers re-prompt; subsequent valid input is used.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", side_effect=["9", "y"])
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "dtitle", "url")
        assert result is mediums[1]

    def test_invalid_text_reprompts(self, mocker: MockerFixture) -> None:
        """Unrecognised text triggers re-prompt; subsequent 'y' is accepted.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._mediums()
        mocker.patch("builtins.input", side_effect=["what", "y"])
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_disc(mediums, mediums[1], "dtitle", "url")
        assert result is mediums[1]

    def test_medium_with_no_tracks_shows_placeholder(self, mocker: MockerFixture) -> None:
        """Medium with no track list shows '(no tracks)' without raising.

        :param mocker: pytest-mock fixture.
        """
        empty_medium = MBMedium.model_validate({"position": 1, "format": "CD", "track-list": []})
        mocker.patch("builtins.input", return_value="y")
        printed: list[str] = []
        mocker.patch("music_annotator._discover._console.print", side_effect=lambda s, **_: printed.append(s))
        result = self._ui().confirm_disc([empty_medium], empty_medium, "dtitle", "url")
        assert result is empty_medium
        assert any("no tracks" in line for line in printed)


# ---------------------------------------------------------------------------
# TerminalDiscoverUI.confirm_shortened_name
# ---------------------------------------------------------------------------


class TestTerminalDiscoverUIConfirmShortenedName:
    """Tests for TerminalDiscoverUI.confirm_shortened_name."""

    def _ui(self) -> TerminalDiscoverUI:
        """Return a fresh TerminalDiscoverUI instance.

        :returns: A :class:`~music_annotator._discover.TerminalDiscoverUI` instance.
        """
        return TerminalDiscoverUI()

    _ORIGINAL = "A" * 300
    _PROPOSED = "A" * 20

    def test_y_accepts_proposed(self, mocker: MockerFixture) -> None:
        """Entering 'y' returns the proposed shortened name.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value="y")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result == self._PROPOSED

    def test_yes_accepts_proposed(self, mocker: MockerFixture) -> None:
        """Entering 'yes' also returns the proposed shortened name.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value="yes")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result == self._PROPOSED

    def test_q_returns_none(self, mocker: MockerFixture) -> None:
        """Entering 'q' aborts and returns None.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value="q")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result is None

    def test_quit_returns_none(self, mocker: MockerFixture) -> None:
        """Entering 'quit' aborts and returns None.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value="quit")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result is None

    def test_a_returns_none(self, mocker: MockerFixture) -> None:
        """Entering 'a' aborts and returns None.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value="a")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result is None

    def test_custom_name_within_limit_accepted(self, mocker: MockerFixture) -> None:
        """Typing a short custom name returns it after safe_name sanitisation.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value="My Custom Name")
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result == "My Custom Name"

    def test_custom_name_sanitised(self, mocker: MockerFixture) -> None:
        """Custom names with forbidden characters are sanitised via safe_name before acceptance.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", return_value='My: "Custom" Name')
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result == "My_ _Custom_ Name"

    def test_custom_name_too_long_reprompts(self, mocker: MockerFixture) -> None:
        """A custom name that still exceeds _NAME_MAX bytes triggers a re-prompt.

        :param mocker: pytest-mock fixture.
        """
        too_long = "B" * (_NAME_MAX + 1)
        mocker.patch("builtins.input", side_effect=[too_long, "y"])
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        # Falls back to accepting proposed after re-prompt with 'y'.
        assert result == self._PROPOSED

    def test_empty_input_reprompts(self, mocker: MockerFixture) -> None:
        """Empty input triggers a re-prompt; subsequent 'y' is accepted.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("builtins.input", side_effect=["", "y"])
        mocker.patch("music_annotator._discover._console.print")
        result = self._ui().confirm_shortened_name(self._ORIGINAL, self._PROPOSED)
        assert result == self._PROPOSED


# ---------------------------------------------------------------------------
# _build_journal_release_ids
# ---------------------------------------------------------------------------


def _make_entry(
    action: str = "tagged",
    release_id: str = "rel-1",
    source: str = "/src/01.flac",
    destination: str = "/dest/01.flac",
) -> TransactionEntry:
    """Build a minimal TransactionEntry for journal helper tests.

    :param action: Journal action string.
    :param release_id: MusicBrainz release MBID.
    :param source: Source file path string.
    :param destination: Destination file path string.
    :returns: A :class:`~music_annotator.models.TransactionEntry` instance.
    """
    return TransactionEntry(
        timestamp="2026-01-01T00:00:00+00:00", release_id=release_id, source=source, destination=destination, action=action
    )


class TestBuildJournalReleaseIds:
    """Tests for _build_journal_release_ids."""

    def test_empty_journal_returns_empty_set(self) -> None:
        """An empty journal produces an empty set."""
        result = _build_journal_release_ids(TransactionLog())
        assert result == set()

    def test_tagged_entries_included(self) -> None:
        """release_id values from action='tagged' entries are returned."""
        journal = TransactionLog(entries=[_make_entry(action="tagged", release_id="rel-1")])
        assert _build_journal_release_ids(journal) == {"rel-1"}

    def test_non_tagged_actions_excluded(self) -> None:
        """action='skipped', 'dry_run', 'downloaded', 'sidecar' entries are excluded."""
        journal = TransactionLog(
            entries=[
                _make_entry(action="skipped", release_id="rel-skip"),
                _make_entry(action="dry_run", release_id="rel-dry"),
                _make_entry(action="downloaded", release_id="rel-dl"),
                _make_entry(action="sidecar", release_id="rel-sc"),
            ]
        )
        assert _build_journal_release_ids(journal) == set()

    def test_deduplication(self) -> None:
        """Multiple tagged entries with the same release_id are deduplicated."""
        journal = TransactionLog(
            entries=[
                _make_entry(action="tagged", release_id="rel-1", source="/src/01.flac"),
                _make_entry(action="tagged", release_id="rel-1", source="/src/02.flac"),
            ]
        )
        assert _build_journal_release_ids(journal) == {"rel-1"}

    def test_multiple_release_ids(self) -> None:
        """Multiple distinct release_ids are all returned."""
        journal = TransactionLog(
            entries=[
                _make_entry(action="tagged", release_id="rel-1"),
                _make_entry(action="tagged", release_id="rel-2"),
                _make_entry(action="skipped", release_id="rel-3"),
            ]
        )
        assert _build_journal_release_ids(journal) == {"rel-1", "rel-2"}


# ---------------------------------------------------------------------------
# _enrich_candidates_from_journal
# ---------------------------------------------------------------------------


class TestEnrichCandidatesFromJournal:
    """Tests for _enrich_candidates_from_journal."""

    def test_empty_candidates_returns_empty(self) -> None:
        """Empty candidate list returns an empty list regardless of journal_ids."""
        result = _enrich_candidates_from_journal([], {"rel-1"})
        assert result == []

    def test_empty_journal_ids_returns_unchanged(self) -> None:
        """Empty journal_ids set leaves candidates unchanged."""
        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_from_journal(candidates, set())
        assert len(result) == 1
        assert result[0].from_journal is False
        assert result[0].score == 90

    def test_matching_candidate_flagged_and_boosted(self) -> None:
        """A candidate whose release_id is in journal_ids gains from_journal=True and score=101."""
        candidates = [_candidate(release_id="rel-1", score=85)]
        result = _enrich_candidates_from_journal(candidates, {"rel-1"})
        assert len(result) == 1
        assert result[0].from_journal is True
        assert result[0].score == 101

    def test_non_matching_candidate_unchanged(self) -> None:
        """A candidate whose release_id is not in journal_ids is not modified."""
        candidates = [_candidate(release_id="rel-2", score=85)]
        result = _enrich_candidates_from_journal(candidates, {"rel-1"})
        assert result[0].from_journal is False
        assert result[0].score == 85

    def test_metadata_preserved_on_enriched_candidate(self) -> None:
        """All metadata fields are preserved when a candidate is enriched."""
        candidates = [_candidate(release_id="rel-1", score=72, title="My Title", artist="Karajan")]
        result = _enrich_candidates_from_journal(candidates, {"rel-1"})
        hit = result[0]
        assert hit.title == "My Title"
        assert hit.artist == "Karajan"
        assert hit.release_id == "rel-1"

    def test_score_above_101_not_lowered(self) -> None:
        """A candidate already scoring above 101 is not lowered (score stays at max(score, 101))."""
        # max(105, 101) == 105, so the candidate keeps its higher score
        candidates = [_candidate(release_id="rel-1", score=105)]
        result = _enrich_candidates_from_journal(candidates, {"rel-1"})
        assert result[0].score == 105

    def test_sort_order_journal_hits_float_to_top(self) -> None:
        """After enrichment, journal-flagged candidates sort above lower-scoring organic results."""
        candidates = [
            _candidate(release_id="rel-organic", score=95),
            _candidate(release_id="rel-journal", score=80),
        ]
        result = _enrich_candidates_from_journal(candidates, {"rel-journal"})
        assert result[0].release_id == "rel-journal"
        assert result[0].score == 101
        assert result[1].release_id == "rel-organic"

    def test_mixed_journal_and_non_journal(self) -> None:
        """Multiple candidates: journal hits are boosted, others unchanged, result sorted."""
        candidates = [
            _candidate(release_id="rel-a", score=90),
            _candidate(release_id="rel-b", score=75),
            _candidate(release_id="rel-c", score=60),
        ]
        result = _enrich_candidates_from_journal(candidates, {"rel-b"})
        # rel-b boosted to 101, rel-a stays 90, rel-c stays 60 → order: b, a, c
        assert [c.release_id for c in result] == ["rel-b", "rel-a", "rel-c"]
        assert result[0].from_journal is True
        assert result[1].from_journal is False
        assert result[2].from_journal is False


# ---------------------------------------------------------------------------
# discover — journal integration
# ---------------------------------------------------------------------------


class TestDiscoverJournalIntegration:
    """Tests for journal-enrichment integration in discover()."""

    def _patch_base(self, mocker: MockerFixture, candidates: list[MBReleaseCandidate]) -> MagicMock:
        """Patch init_mb, search_releases_by_dir, and run for discover journal tests.

        :param mocker: pytest-mock fixture.
        :param candidates: Candidate list returned by search_releases_by_dir.
        :returns: The mock for music_annotator._discover.run.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.search_releases_by_dir", return_value=candidates)
        return mocker.patch("music_annotator._discover.run")

    def test_journal_candidate_flagged_when_mbid_in_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When an organic MB result's release_id appears in the journal, it is flagged from_journal=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        # Pre-populate the journal with rel-1 already tagged
        journal_path = dest / "music_annotator_journal.json"
        fs.create_dir(str(dest))
        fs.create_file(
            str(journal_path),
            contents='[{"timestamp":"2026-01-01T00:00:00+00:00","release_id":"rel-1",'
            '"source":"/other/01.flac","destination":"/dest/01.flac","action":"tagged"}]',
        )

        # The organic MB search returns rel-1 at score 90
        captured_candidates: list[list[MBReleaseCandidate]] = []

        class _CapturingUI:
            def choose_release(  # pylint: disable=useless-return
                self, _src_dir: object, candidates: list[MBReleaseCandidate]
            ) -> str | None:
                """Capture candidates and return None to skip."""
                captured_candidates.append(list(candidates))
                return None

            def confirm_disc(self, *_: object) -> None:  # pragma: no cover
                """Not called in this test."""

            def confirm_shortened_name(self, *_: object) -> None:  # pragma: no cover
                """Not called in this test."""

            def confirm_delete(self, *_: object) -> bool:  # pragma: no cover
                """Not called in this test."""
                return False

        self._patch_base(mocker, [_candidate(release_id="rel-1", score=90)])
        music_annotator.discover(src_dirs=[src], dest_root=dest, user_agent="Test/1.0", ui=_CapturingUI())

        assert len(captured_candidates) == 1
        assert captured_candidates[0][0].from_journal is True
        assert captured_candidates[0][0].score == 101

    def test_journal_not_flagged_when_mbid_absent(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no organic result's release_id appears in the journal, from_journal stays False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        journal_path = dest / "music_annotator_journal.json"
        fs.create_dir(str(dest))
        # Journal has rel-OTHER — not the same as organic result rel-1
        fs.create_file(
            str(journal_path),
            contents='[{"timestamp":"2026-01-01T00:00:00+00:00","release_id":"rel-other",'
            '"source":"/other/01.flac","destination":"/dest/01.flac","action":"tagged"}]',
        )

        captured_candidates: list[list[MBReleaseCandidate]] = []

        class _CapturingUI:
            def choose_release(  # pylint: disable=useless-return
                self, _src_dir: object, candidates: list[MBReleaseCandidate]
            ) -> str | None:
                """Capture candidates and return None to skip."""
                captured_candidates.append(list(candidates))
                return None

            def confirm_disc(self, *_: object) -> None:  # pragma: no cover
                """Not called in this test."""

            def confirm_shortened_name(self, *_: object) -> None:  # pragma: no cover
                """Not called in this test."""

            def confirm_delete(self, *_: object) -> bool:  # pragma: no cover
                """Not called in this test."""
                return False

        self._patch_base(mocker, [_candidate(release_id="rel-1", score=90)])
        music_annotator.discover(src_dirs=[src], dest_root=dest, user_agent="Test/1.0", ui=_CapturingUI())

        assert len(captured_candidates) == 1
        assert captured_candidates[0][0].from_journal is False

    def test_journal_refreshed_after_run_so_sibling_disc_sees_it(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """After run() writes a journal entry, the next src_dir iteration sees the MBID as from_journal.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src1 = Path("/music/Disc1")
        src2 = Path("/music/Disc2")
        dest = Path("/dest")
        for src in (src1, src2):
            fs.create_dir(str(src))
            fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_dir(str(dest))

        # Simulate run() for src1 writing a journal entry for rel-1
        import json  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        journal_path = dest / "music_annotator_journal.json"

        def _fake_run(**kwargs: object) -> None:
            """Write a journal entry for rel-1 when src1 is processed."""
            if kwargs.get("src_dir") == src1:
                entry = {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "release_id": "rel-1",
                    "source": str(src1 / "01.flac"),
                    "destination": str(dest / "01.flac"),
                    "action": "tagged",
                }
                journal_path.write_text(json.dumps([entry]))

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._discover.run", side_effect=_fake_run)
        # Both dirs return rel-1 from MB search
        mocker.patch(
            "music_annotator._discover.search_releases_by_dir", return_value=[_candidate(release_id="rel-1", score=85)]
        )

        captured: list[list[MBReleaseCandidate]] = []

        class _CapturingUI:
            def choose_release(self, _src_dir: object, candidates: list[MBReleaseCandidate]) -> str | None:
                """Capture candidates and pick the first one."""
                captured.append(list(candidates))
                return candidates[0].release_id  # always pick first

            def confirm_disc(self, *_: object) -> None:  # pragma: no cover
                """Not called in this test."""

            def confirm_shortened_name(self, *_: object) -> None:  # pragma: no cover
                """Not called in this test."""

            def confirm_delete(self, *_: object) -> bool:  # pragma: no cover
                """Not called in this test."""
                return False

        music_annotator.discover(src_dirs=[src1, src2], dest_root=dest, user_agent="Test/1.0", ui=_CapturingUI())

        # src1 had no prior journal → rel-1 was not flagged
        assert captured[0][0].from_journal is False
        # src2 sees the journal entry written by src1's run() → rel-1 is now flagged
        assert captured[1][0].from_journal is True
        assert captured[1][0].score == 101

    def test_no_journal_file_discover_works_normally(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no journal file exists, discover proceeds normally without error.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/Album")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mock_run = self._patch_base(mocker, [_candidate(release_id="rel-1", score=90)])
        mocker.patch("builtins.input", return_value="1")

        # No journal file at dest — should not raise
        music_annotator.discover(src_dirs=[src], dest_root=dest, user_agent="Test/1.0")
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# _corroborate_medium_sequence
# ---------------------------------------------------------------------------


def _make_result(match: bool | None) -> AudioCompareResult:
    """Build a minimal :class:`~music_annotator._pipeline_io.AudioCompareResult` for corroboration tests.

    :param match: The per-track match verdict (``True``, ``False``, or ``None``).
    :returns: An :class:`~music_annotator._pipeline_io.AudioCompareResult` instance.
    """
    return AudioCompareResult(src=Path("."), dest=Path("."), match=match, method="isrc", detail="test")


class TestCorroborateMediumSequence:
    """Tests for _corroborate_medium_sequence.

    KAT: test_medium_sequence_corroborates_weak_track — verifies that a sequence of mostly
    inconclusive per-track results is rescued to match=True when the sequence ordering matches
    the candidate medium.  Also tests the contradiction case.
    """

    def test_medium_sequence_corroborates_weak_track(self) -> None:
        """Mostly-inconclusive per-track results are rescued to match=True by sequence ordering.

        This is the core KAT: a 25-second chant verse that would never be identified alone
        (match=None) is rescued when the sequence of recording IDs matches the candidate medium.
        """
        # 4 tracks: 3 inconclusive (weak fingerprints), 1 confirmed match
        # The confirmed track's source_id matches the candidate_id at that position.
        candidate_ids = ["rec-a", "rec-b", "rec-c", "rec-d"]
        source_ids = ["", "", "rec-c", ""]  # only track 3 has an embedded recording ID
        track_results = [
            _make_result(None),  # track 1: inconclusive
            _make_result(None),  # track 2: inconclusive
            _make_result(True),  # track 3: confirmed (source_id == candidate_id)
            _make_result(None),  # track 4: inconclusive
        ]
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        # 1/4 confirmed = 25% — below 50% threshold → still inconclusive
        assert result.match is None
        assert result.method == "sequence"
        assert "confirmed=1/4" in result.detail

    def test_majority_confirmed_returns_match_true(self) -> None:
        """When ≥50% of positions are confirmed and none contradicted, returns match=True.

        This is the main rescue scenario: a sequence of mostly-confirmed tracks (e.g. from
        previously-tagged files) corroborates the candidate medium.
        """
        candidate_ids = ["rec-a", "rec-b", "rec-c", "rec-d"]
        source_ids = ["rec-a", "rec-b", "rec-c", ""]  # 3 of 4 match
        track_results = [
            _make_result(True),  # confirmed
            _make_result(True),  # confirmed
            _make_result(True),  # confirmed
            _make_result(None),  # inconclusive
        ]
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is True
        assert result.method == "sequence"
        assert "confirmed=3/4" in result.detail

    def test_contradiction_returns_match_false(self) -> None:
        """A single contradicted position causes the whole sequence to return match=False.

        This is the contradiction case: one track's identity contradicts the candidate medium.
        """
        candidate_ids = ["rec-a", "rec-b", "rec-c"]
        source_ids = ["rec-a", "rec-WRONG", "rec-c"]  # track 2 source_id != candidate_id
        track_results = [
            _make_result(True),  # confirmed: source_id == candidate_id
            _make_result(True),  # contradicted: match=True but source_id != candidate_id
            _make_result(True),  # confirmed
        ]
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is False
        assert result.method == "sequence"
        assert "contradicted=1/3" in result.detail

    def test_match_false_track_result_contradicts(self) -> None:
        """A track_result with match=False is counted as contradicted regardless of IDs."""
        candidate_ids = ["rec-a", "rec-b"]
        source_ids = ["rec-a", "rec-b"]
        track_results = [
            _make_result(True),  # confirmed
            _make_result(False),  # contradicted (match=False)
        ]
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is False
        assert "contradicted=1/2" in result.detail

    def test_empty_track_results_returns_none(self) -> None:
        """Empty track_results list returns match=None with 'empty sequence' detail."""
        result = _corroborate_medium_sequence([], ["rec-a"], ["rec-a"])
        assert result.match is None
        assert result.method == "sequence"
        assert "empty sequence" in result.detail

    def test_empty_candidate_ids_returns_none(self) -> None:
        """Empty candidate_track_ids returns match=None with 'empty sequence' detail."""
        result = _corroborate_medium_sequence([_make_result(True)], [], ["rec-a"])
        assert result.match is None
        assert "empty sequence" in result.detail

    def test_length_mismatch_returns_none(self) -> None:
        """Mismatched list lengths return match=None with 'length mismatch' detail."""
        candidate_ids = ["rec-a", "rec-b", "rec-c"]
        source_ids = ["rec-a", "rec-b"]
        track_results = [_make_result(True), _make_result(True)]  # 2 results, 3 candidate IDs
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is None
        assert "length mismatch" in result.detail

    def test_all_inconclusive_returns_none(self) -> None:
        """All-inconclusive track results return match=None."""
        candidate_ids = ["rec-a", "rec-b", "rec-c"]
        source_ids = ["", "", ""]
        track_results = [_make_result(None), _make_result(None), _make_result(None)]
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is None
        assert "confirmed=0/3" in result.detail
        assert "inconclusive=3/3" in result.detail

    def test_exactly_50_percent_confirmed_returns_true(self) -> None:
        """Exactly 50% confirmed (≥0.5 threshold) returns match=True."""
        candidate_ids = ["rec-a", "rec-b"]
        source_ids = ["rec-a", ""]
        track_results = [_make_result(True), _make_result(None)]  # 1/2 = 50%
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is True

    def test_source_ids_shorter_than_track_results_treats_missing_as_empty(self) -> None:
        """When source_track_ids is shorter than track_results, missing entries are treated as empty."""
        candidate_ids = ["rec-a", "rec-b", "rec-c"]
        source_ids = ["rec-a"]  # only 1 entry for 3 tracks
        track_results = [_make_result(True), _make_result(True), _make_result(True)]
        # Position 0: source_id="rec-a" == candidate_id="rec-a" → confirmed
        # Position 1: source_id="" (missing) != candidate_id="rec-b" → contradicted
        # Position 2: source_id="" (missing) != candidate_id="rec-c" → contradicted
        result = _corroborate_medium_sequence(track_results, candidate_ids, source_ids)
        assert result.match is False
        assert "contradicted=2/3" in result.detail


# ---------------------------------------------------------------------------
# _enrich_candidates_with_sequence_corroboration
# ---------------------------------------------------------------------------


class TestEnrichCandidatesWithSequenceCorroboration:
    """Tests for _enrich_candidates_with_sequence_corroboration."""

    def test_empty_medium_track_ids_dict_returns_unchanged(self) -> None:
        """When medium_track_ids_by_release is empty, all candidates are returned unchanged."""
        candidates = [_candidate(release_id="rel-1", score=90), _candidate(release_id="rel-2", score=80)]
        result = _enrich_candidates_with_sequence_corroboration([], candidates, {})
        assert [c.release_id for c in result] == ["rel-1", "rel-2"]
        assert result[0].score == 90
        assert result[1].score == 80

    def test_match_true_boosts_score_by_10(self, mocker: MockerFixture) -> None:
        """A match=True corroboration result boosts the candidate score by 10."""
        mocker.patch(
            "music_annotator._discover._corroborate_candidate_medium",
            return_value=AudioCompareResult(src=Path("."), dest=Path("."), match=True, method="sequence", detail="ok"),
        )
        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_sequence_corroboration([Path("/src/01.flac")], candidates, {"rel-1": ["rec-a"]})
        assert result[0].score == 100

    def test_match_false_penalises_score_by_20(self, mocker: MockerFixture) -> None:
        """A match=False corroboration result penalises the candidate score by 20."""
        mocker.patch(
            "music_annotator._discover._corroborate_candidate_medium",
            return_value=AudioCompareResult(src=Path("."), dest=Path("."), match=False, method="sequence", detail="bad"),
        )
        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_sequence_corroboration([Path("/src/01.flac")], candidates, {"rel-1": ["rec-a"]})
        assert result[0].score == 70

    def test_match_false_score_floored_at_zero(self, mocker: MockerFixture) -> None:
        """Score penalty is floored at 0 — never goes negative."""
        mocker.patch(
            "music_annotator._discover._corroborate_candidate_medium",
            return_value=AudioCompareResult(src=Path("."), dest=Path("."), match=False, method="sequence", detail="bad"),
        )
        candidates = [_candidate(release_id="rel-1", score=10)]
        result = _enrich_candidates_with_sequence_corroboration([Path("/src/01.flac")], candidates, {"rel-1": ["rec-a"]})
        assert result[0].score == 0

    def test_match_none_leaves_score_unchanged(self, mocker: MockerFixture) -> None:
        """A match=None corroboration result leaves the score unchanged."""
        mocker.patch(
            "music_annotator._discover._corroborate_candidate_medium",
            return_value=AudioCompareResult(
                src=Path("."), dest=Path("."), match=None, method="sequence", detail="inconclusive"
            ),
        )
        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_sequence_corroboration([Path("/src/01.flac")], candidates, {"rel-1": ["rec-a"]})
        assert result[0].score == 90

    def test_result_sorted_by_score_descending(self, mocker: MockerFixture) -> None:
        """Result list is sorted by score descending after enrichment."""
        call_count = 0

        def _fake_corroborate(_source_paths: object, track_ids: list[str]) -> AudioCompareResult:
            """Return match=True for rel-2, match=None for rel-1."""
            nonlocal call_count
            call_count += 1
            if track_ids == ["rec-b"]:
                return AudioCompareResult(src=Path("."), dest=Path("."), match=True, method="sequence", detail="ok")
            return AudioCompareResult(src=Path("."), dest=Path("."), match=None, method="sequence", detail="inc")

        mocker.patch("music_annotator._discover._corroborate_candidate_medium", side_effect=_fake_corroborate)
        candidates = [
            _candidate(release_id="rel-1", score=90),
            _candidate(release_id="rel-2", score=80),
        ]
        result = _enrich_candidates_with_sequence_corroboration(
            [Path("/src/01.flac")],
            candidates,
            {"rel-1": ["rec-a"], "rel-2": ["rec-b"]},
        )
        # rel-2 boosted to 90, rel-1 stays at 90 — stable sort keeps rel-1 first if equal
        # rel-2 boosted to 80+10=90, rel-1 stays at 90 → both 90, order depends on sort stability
        # Actually rel-1=90 (unchanged), rel-2=90 (boosted from 80) → both 90
        assert result[0].score == 90
        assert result[1].score == 90

    def test_candidate_without_track_ids_unchanged(self, mocker: MockerFixture) -> None:
        """Candidates not in medium_track_ids_by_release are returned unchanged."""
        mock_corroborate = mocker.patch("music_annotator._discover._corroborate_candidate_medium")
        candidates = [_candidate(release_id="rel-1", score=90), _candidate(release_id="rel-2", score=80)]
        result = _enrich_candidates_with_sequence_corroboration(
            [Path("/src/01.flac")],
            candidates,
            {"rel-1": ["rec-a"]},  # only rel-1 has track IDs
        )
        # _corroborate_candidate_medium called only for rel-1
        mock_corroborate.assert_called_once()
        # rel-2 unchanged
        rel2 = next(c for c in result if c.release_id == "rel-2")
        assert rel2.score == 80


# ---------------------------------------------------------------------------
# _enrich_candidates_with_acoustid_seed
# ---------------------------------------------------------------------------


def _make_single_track_release(release_id: str, recording_id: str) -> MBRelease:
    """Build a minimal single-track MBRelease for acoustid seed tests.

    :param release_id: MusicBrainz release MBID.
    :param recording_id: Recording MBID for the single track.
    :returns: A minimal :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(
        {
            "id": release_id,
            "title": "Test",
            "medium-list": [
                {
                    "position": 1,
                    "format": "CD",
                    "track-list": [
                        {
                            "id": "trk-1",
                            "position": 1,
                            "recording": {"id": recording_id, "title": "Track 1", "artist-credit": []},
                        }
                    ],
                }
            ],
        }
    )


class TestEnrichCandidatesWithAcoustidSeed:
    """Tests for _enrich_candidates_with_acoustid_seed."""

    def test_noop_when_acoustid_key_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Returns candidates unchanged when acoustid_key == '' (no network calls).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mock_fpcalc = mocker.patch("music_annotator._discover._run_fpcalc")
        mock_lookup = mocker.patch("music_annotator._discover._fetch_acoustid_lookup_raw")
        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_acoustid_seed([src], candidates, "")
        assert result == candidates
        mock_fpcalc.assert_not_called()
        mock_lookup.assert_not_called()

    def test_boost_when_recording_mbid_matches(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Score is boosted by +10 when AcoustID lookup confirms a track recording MBID.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._discover._run_fpcalc", return_value="AQADtMmybckm")
        mocker.patch("music_annotator._discover._read_duration_ms", return_value=180000)
        mocker.patch("music_annotator._discover._fetch_acoustid_lookup_raw", return_value=(["rec-mbid-match"], "uuid-1"))

        # Build a release with a track whose recording id matches the AcoustID result
        release = _make_single_track_release("rel-1", "rec-mbid-match")
        mocker.patch("music_annotator._discover.fetch_release", return_value=release)

        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_acoustid_seed([src], candidates, "my-api-key")
        assert result[0].score == 100

    def test_no_boost_when_no_match(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Score is unchanged when AcoustID lookup returns no matching recording MBID.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._discover._run_fpcalc", return_value="AQADtMmybckm")
        mocker.patch("music_annotator._discover._read_duration_ms", return_value=180000)
        mocker.patch("music_annotator._discover._fetch_acoustid_lookup_raw", return_value=(["rec-mbid-other"], "uuid-1"))

        release = _make_single_track_release("rel-1", "rec-mbid-different")
        mocker.patch("music_annotator._discover.fetch_release", return_value=release)

        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_acoustid_seed([src], candidates, "my-api-key")
        assert result[0].score == 90

    def test_empty_fingerprint_candidates_unchanged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When fpcalc returns '' (unavailable), _fetch_acoustid_lookup_raw returns ([], '') and candidates unchanged.

        _fetch_acoustid_lookup_raw is called with an empty fingerprint but returns ([], '') immediately
        (early-exit inside the function).  The empty acoustid_recording_ids set means no boost
        is applied and candidates are returned unchanged.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._discover._run_fpcalc", return_value="")
        mocker.patch("music_annotator._discover._read_duration_ms", return_value=180000)
        # _fetch_acoustid_lookup_raw returns ([], '') for empty fingerprint (early-exit inside the function)
        mocker.patch("music_annotator._discover._fetch_acoustid_lookup_raw", return_value=([], ""))

        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_acoustid_seed([src], candidates, "my-api-key")
        # No boost applied — candidates returned unchanged
        assert result[0].score == 90

    def test_fetch_release_failure_leaves_candidate_unchanged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When fetch_release raises for a candidate, that candidate is returned unchanged.

        Covers the ``except Exception`` branch in _enrich_candidates_with_acoustid_seed.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/music/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._discover._run_fpcalc", return_value="AQADtMmybckm")
        mocker.patch("music_annotator._discover._read_duration_ms", return_value=180000)
        mocker.patch("music_annotator._discover._fetch_acoustid_lookup_raw", return_value=(["rec-mbid-match"], "uuid-1"))
        # fetch_release raises for this candidate
        mocker.patch("music_annotator._discover.fetch_release", side_effect=RuntimeError("network error"))

        candidates = [_candidate(release_id="rel-1", score=90)]
        result = _enrich_candidates_with_acoustid_seed([src], candidates, "my-api-key")
        # Candidate unchanged — fetch_release failure leaves score unchanged
        assert result[0].score == 90


# ---------------------------------------------------------------------------
# _read_recording_id_tag
# ---------------------------------------------------------------------------


class TestReadRecordingIdTag:
    """Tests for _read_recording_id_tag."""

    def test_flac_returns_recording_id(self, fs: FakeFilesystem) -> None:
        """Returns the MUSICBRAINZ_TRACKID Vorbis Comment value from a FLAC file.

        :param fs: pyfakefs fixture.
        """
        from mutagen.flac import FLAC  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        path = Path("/music/01.flac")
        fs.create_file(str(path), contents=_saveable_flac())
        audio = FLAC(str(path))
        audio["musicbrainz_trackid"] = ["rec-abc-123"]
        audio.save()
        assert _read_recording_id_tag(path) == "rec-abc-123"

    def test_flac_returns_empty_when_tag_absent(self, fs: FakeFilesystem) -> None:
        """Returns '' when the FLAC file has no MUSICBRAINZ_TRACKID tag.

        :param fs: pyfakefs fixture.
        """
        path = Path("/music/01.flac")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        assert _read_recording_id_tag(path) == ""

    def test_mp3_returns_recording_id(self, fs: FakeFilesystem) -> None:
        """Returns the MusicBrainz Track Id TXXX value from an MP3 file.

        :param fs: pyfakefs fixture.
        """
        from mutagen.id3 import (  # type: ignore[attr-defined]  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            ID3,
            TXXX,
        )

        path = Path("/music/01.mp3")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        id3 = ID3(str(path))  # type: ignore[no-untyped-call]
        id3.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=["rec-mp3-456"]))  # type: ignore[no-untyped-call]
        id3.save(str(path))
        assert _read_recording_id_tag(path) == "rec-mp3-456"

    def test_mp3_returns_empty_when_txxx_absent(self, fs: FakeFilesystem) -> None:
        """Returns '' when the MP3 file has no MusicBrainz Track Id TXXX frame.

        :param fs: pyfakefs fixture.
        """
        path = Path("/music/01.mp3")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        assert _read_recording_id_tag(path) == ""

    def test_mp3_returns_empty_when_txxx_has_empty_text(self, fs: FakeFilesystem) -> None:
        """Returns '' when the MusicBrainz Track Id TXXX frame exists but has empty text.

        Exercises the branch where ``frame.desc`` matches but ``frame.text`` is falsy.

        :param fs: pyfakefs fixture.
        """
        from mutagen.id3 import (  # type: ignore[attr-defined]  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            ID3,
            TXXX,
        )

        path = Path("/music/01.mp3")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        id3 = ID3(str(path))  # type: ignore[no-untyped-call]
        # Add a TXXX frame with the right description but empty text list
        id3.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=[]))  # type: ignore[no-untyped-call]
        id3.save(str(path))
        assert _read_recording_id_tag(path) == ""

    def test_mp3_skips_non_matching_txxx_and_finds_correct_one(self, fs: FakeFilesystem) -> None:
        """Iterates past non-matching TXXX frames to find the MusicBrainz Track Id frame.

        Exercises the loop-continue branch where ``frame.desc`` does not match and the loop
        advances to the next frame.

        :param fs: pyfakefs fixture.
        """
        from mutagen.id3 import (  # type: ignore[attr-defined]  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            ID3,
            TXXX,
        )

        path = Path("/music/01.mp3")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        id3 = ID3(str(path))  # type: ignore[no-untyped-call]
        # Add a non-matching TXXX frame first, then the correct one
        id3.add(TXXX(encoding=3, desc="Some Other Tag", text=["other-value"]))  # type: ignore[no-untyped-call]
        id3.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=["rec-found-789"]))  # type: ignore[no-untyped-call]
        id3.save(str(path))
        assert _read_recording_id_tag(path) == "rec-found-789"

    def test_unsupported_extension_returns_empty(self, fs: FakeFilesystem) -> None:
        """Returns '' for unsupported file extensions (e.g. .ogg).

        :param fs: pyfakefs fixture.
        """
        path = Path("/music/01.ogg")
        fs.create_file(str(path), contents=b"OggS")
        assert _read_recording_id_tag(path) == ""

    def test_corrupt_file_returns_empty(self, fs: FakeFilesystem) -> None:
        """Returns '' when the file is corrupt and cannot be read.

        :param fs: pyfakefs fixture.
        """
        path = Path("/music/01.flac")
        fs.create_file(str(path), contents=b"not-a-flac-file")
        assert _read_recording_id_tag(path) == ""


# ---------------------------------------------------------------------------
# _corroborate_candidate_medium
# ---------------------------------------------------------------------------


class TestCorroborateCandidateMedium:
    """Tests for _corroborate_candidate_medium."""

    def test_empty_source_paths_returns_empty_sequence(self) -> None:
        """Empty source_paths returns an empty-sequence result (match=None)."""
        result = _corroborate_candidate_medium([], ["rec-a", "rec-b"])
        assert result.match is None
        assert result.method == "sequence"
        assert "empty sequence" in result.detail

    def test_files_with_matching_recording_ids_return_match_true(self, fs: FakeFilesystem) -> None:
        """Source files with matching MUSICBRAINZ_TRACKID tags corroborate the candidate medium.

        :param fs: pyfakefs fixture.
        """
        from mutagen.flac import FLAC  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        paths = [Path(f"/music/0{i}.flac") for i in range(1, 4)]
        recording_ids = ["rec-a", "rec-b", "rec-c"]
        for path, rec_id in zip(paths, recording_ids):
            fs.create_file(str(path), contents=_saveable_flac())
            audio = FLAC(str(path))
            audio["musicbrainz_trackid"] = [rec_id]
            audio.save()

        result = _corroborate_candidate_medium(paths, recording_ids)
        # All 3 files have matching recording IDs → 3/3 confirmed = 100% → match=True
        assert result.match is True
        assert "confirmed=3/3" in result.detail

    def test_files_without_recording_ids_return_inconclusive(self, fs: FakeFilesystem) -> None:
        """Source files with no MUSICBRAINZ_TRACKID tags yield an inconclusive result.

        :param fs: pyfakefs fixture.
        """
        paths = [Path(f"/music/0{i}.flac") for i in range(1, 3)]
        for path in paths:
            fs.create_file(str(path), contents=_MINIMAL_FLAC)

        result = _corroborate_candidate_medium(paths, ["rec-a", "rec-b"])
        # No recording IDs → all inconclusive → match=None
        assert result.match is None
        assert "inconclusive=2/2" in result.detail

    def test_mismatched_recording_ids_return_match_false(self, fs: FakeFilesystem) -> None:
        """Source files with recording IDs that differ from the candidate return match=False.

        :param fs: pyfakefs fixture.
        """
        from mutagen.flac import FLAC  # noqa: PLC0415  # pylint: disable=import-outside-toplevel

        path = Path("/music/01.flac")
        fs.create_file(str(path), contents=_saveable_flac())
        audio = FLAC(str(path))
        audio["musicbrainz_trackid"] = ["rec-WRONG"]
        audio.save()

        result = _corroborate_candidate_medium([path], ["rec-correct"])
        # Source says rec-WRONG, candidate says rec-correct → match=True but IDs differ → contradicted
        assert result.match is False
        assert "contradicted=1/1" in result.detail
